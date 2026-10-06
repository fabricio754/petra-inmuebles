"""Cola persistente de webhooks de WhatsApp.

Antes el handler HTTP creaba un `Future` en un ThreadPoolExecutor en memoria.
Si el proceso moría (deploy, SIGTERM, OOM) o el thread quedaba colgado, los
mensajes encolados se perdían. Ahora el handler solo INSERTA el payload en
`webhook_queue` y responde 200. Este módulo levanta un hilo dispatcher que
lee las filas pendientes y las procesa en paralelo con un ThreadPoolExecutor
pequeño. Si el proceso muere a mitad de camino, los pendientes quedan en la
DB y se reprocesan al reiniciar — garantiza 0 pérdidas.
"""
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from psycopg.types.json import Jsonb

from app import db

log = logging.getLogger("petra")

# Reintentos antes de marcar la fila como 'error' definitivo.
MAX_INTENTOS = 3
# Cada cuántos segundos el dispatcher consulta la cola si no hay señal.
POLL_SEG = 1.0
# Cuántas filas se reclaman por consulta.
BATCH = 10

# Cuántos webhooks procesa en paralelo el worker. Configurable.
# Default bajado a 2: con 4 workers manteniendo conns a Meta en paralelo
# la sesión TLS a Postgres acumula tráfico y se corrompe más seguido
# (bad record mac → slot fantasma en el pool). Mantener configurable.
_WORKERS = int(os.environ.get("WEBHOOK_QUEUE_WORKERS", "2"))

_iniciado = False
_lock = threading.Lock()
_despertar = threading.Event()
_pool = None


# ----------------------------------------------------------------------------
# API pública
# ----------------------------------------------------------------------------

def encolar(payload):
    """Inserta el payload completo del webhook en `webhook_queue` y despierta
    al dispatcher. Devuelve el id de la fila (útil para logs/debug)."""
    with db._conexion() as conn:
        row = conn.execute(
            "INSERT INTO webhook_queue (payload) VALUES (%s) RETURNING id",
            (Jsonb(payload),),
        ).fetchone()
    _despertar.set()
    return row[0] if row else None


def iniciar():
    """Arranca el worker thread (idempotente)."""
    global _iniciado
    with _lock:
        if _iniciado:
            return
        if not db.DATABASE_URL:
            log.warning("[WebhookQueue] Sin DATABASE_URL — no se inicia.")
            return
        _iniciado = True

    # Al reiniciar, cualquier fila que haya quedado en 'procesando' pertenece
    # a un proceso anterior que murió a mitad de camino: la devolvemos a
    # 'pending' para que se reintente.
    try:
        with db._conexion() as conn:
            n = conn.execute(
                "UPDATE webhook_queue SET estado='pending' "
                "WHERE estado='procesando'"
            ).rowcount
        if n:
            log.info("[WebhookQueue] %d fila(s) reencoladas desde 'procesando'.", n)
    except Exception:
        log.exception("[WebhookQueue] Error reencolando filas pendientes.")

    threading.Thread(target=_bucle, name="webhook-queue", daemon=True).start()
    log.info("[WebhookQueue] Dispatcher iniciado (workers=%d).", _WORKERS)


# ----------------------------------------------------------------------------
# Dispatcher
# ----------------------------------------------------------------------------

def _bucle():
    global _pool
    _pool = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="whq")
    while True:
        try:
            filas = _reclamar_batch()
        except Exception:
            log.exception("[WebhookQueue] Error leyendo la cola.")
            filas = []

        if not filas:
            # Espera hasta POLL_SEG; si entra un webhook nuevo, el handler
            # llama a encolar() que hace _despertar.set() y salimos antes.
            _despertar.wait(timeout=POLL_SEG)
            _despertar.clear()
            continue

        for fila_id, payload in filas:
            _pool.submit(_procesar_fila, fila_id, payload)


def _reclamar_batch():
    """Toma hasta BATCH filas en estado pending, las marca 'procesando' y las
    devuelve. `FOR UPDATE SKIP LOCKED` evita que dos workers/instancias
    tomen la misma fila."""
    with db._conexion() as conn:
        with conn.transaction():
            filas = conn.execute(
                "SELECT id, payload FROM webhook_queue "
                "WHERE estado = 'pending' "
                "ORDER BY id "
                "LIMIT %s "
                "FOR UPDATE SKIP LOCKED",
                (BATCH,),
            ).fetchall()
            if not filas:
                return []
            ids = [f[0] for f in filas]
            conn.execute(
                "UPDATE webhook_queue SET estado='procesando' "
                "WHERE id = ANY(%s)",
                (ids,),
            )
    return filas


def _procesar_fila(fila_id, payload):
    try:
        _invocar_logica(payload)
    except Exception as exc:
        log.exception("[WebhookQueue] Fallo procesando fila %s", fila_id)
        _marcar_fallo(fila_id, exc)
        return
    _marcar_ok(fila_id)


def _marcar_ok(fila_id):
    try:
        with db._conexion() as conn:
            conn.execute(
                "UPDATE webhook_queue SET estado='ok', procesado_at=NOW() "
                "WHERE id = %s",
                (fila_id,),
            )
    except Exception:
        log.exception("[WebhookQueue] No se pudo marcar OK la fila %s", fila_id)


def _marcar_fallo(fila_id, exc):
    try:
        err = str(exc)[:4000]
        with db._conexion() as conn:
            conn.execute(
                "UPDATE webhook_queue "
                "SET intentos = intentos + 1, "
                "    ultimo_error = %s, "
                "    estado = CASE WHEN intentos + 1 >= %s "
                "                  THEN 'error' ELSE 'pending' END, "
                "    procesado_at = CASE WHEN intentos + 1 >= %s "
                "                        THEN NOW() ELSE procesado_at END "
                "WHERE id = %s",
                (err, MAX_INTENTOS, MAX_INTENTOS, fila_id),
            )
    except Exception:
        log.exception("[WebhookQueue] No se pudo marcar fallo la fila %s", fila_id)


# ----------------------------------------------------------------------------
# Lógica de procesamiento (equivalente al viejo _procesar_mensaje_async)
# ----------------------------------------------------------------------------

def _invocar_logica(payload):
    """Dado el payload crudo del webhook de Meta, reconstruye phone+event y
    corre la lógica equivalente al viejo `_procesar_mensaje_async` del
    handler HTTP."""
    # Import local para evitar ciclo con server.py en el import inicial.
    from app.server import _to_event

    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]["value"]
    except (KeyError, IndexError, TypeError):
        # Payload sin estructura esperada: nada que procesar.
        return

    # Opt-out de marketing (botón "Stop" en la tarjeta de la plantilla).
    for pref in change.get("user_preferences") or []:
        if (pref.get("category") == "marketing_messages"
                and str(pref.get("value", "")).lower() == "stop"):
            wa_id = pref.get("wa_id")
            if wa_id:
                from app import state
                state.set_no_contactar(wa_id, True)
                log.info("Opt-out de marketing recibido de %s (botón Stop).", wa_id)

    messages = change.get("messages")
    if not messages:
        # Eventos de status (entregado/leído) y preferencias puras: ya tratados.
        return

    message = messages[0]
    # Si el usuario oculta su número tras un nombre de usuario de WhatsApp,
    # Meta no manda "from" sino "from_user_id" (BSUID, ej. "CO.123...").
    phone = message.get("from") or message["from_user_id"]
    event = _to_event(message)
    log.info("Mensaje entrante de %s: %s", phone, event)

    _procesar_mensaje(phone, event, message)


def _procesar_mensaje(phone, event, message=None):
    """Antes vivía en server.py como `_procesar_mensaje_async`. Loggea el
    mensaje IN en `mensajes`, descarga media si aplica, y pasa el evento al
    bot. Si falla, el caller (_procesar_fila) marca la fila como fallo."""
    try:
        _resumen_in = (event.get("text")
                       or event.get("id")
                       or event.get("media_id")
                       or event.get("response")
                       or "")
        db.log_mensaje(phone, "in", event.get("type", "unknown"),
                       str(_resumen_in)[:4000], message)
    except Exception:
        log.exception("No se pudo registrar mensaje entrante.")

    if event["type"] == "media":
        from app import media as media_mod, whatsapp as wa
        mime = event["mime_type"]
        if mime not in media_mod.MIME_SOPORTADOS:
            wa.send_tipo_doc_invalido(phone)
            return
        ruta = media_mod.download_and_save(phone, event["media_id"], mime)
        media_mod.registrar(phone, event["media_id"], mime, ruta)
        event["ruta_local"] = ruta

    from app import bot
    bot.handle_incoming(phone, event)
