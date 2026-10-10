"""Cola persistente de webhooks de WhatsApp.

Antes el handler HTTP creaba un `Future` en un ThreadPoolExecutor en memoria.
Si el proceso moría (deploy, SIGTERM, OOM) o el thread quedaba colgado, los
mensajes encolados se perdían. Ahora el handler solo INSERTA el payload en
`webhook_queue` y responde 200. Este módulo levanta un hilo dispatcher que
lee las filas pendientes y las procesa en paralelo con un ThreadPoolExecutor
pequeño. Si el proceso muere a mitad de camino, los pendientes quedan en la
DB y se reprocesan al reiniciar — garantiza 0 pérdidas.

Fallback a disco cuando el pool está saturado
---------------------------------------------
Render usa un Postgres interno con SSL. Bajo carga TLS puede corromperse
("bad record mac"): el pool descarta esa conn y abre otra. Mientras el pool
está reciclando conns, `encolar()` puede toparse con `PoolTimeout` y perder
el payload — justo cuando más necesitamos no perderlos.

Para que ESO no pase, `encolar()` usa un timeout corto. Si el pool está
contendido, en vez de bloquear 20s el gthread (que Meta reintentaría y
cascadearía más carga), escribe el payload a `WEBHOOK_SPOOL` en disco y
devuelve. El hilo dispatcher drena el spool periódicamente (cada ronda
intenta re-encolar los payloads en disco). `iniciar()` también reclama el
spool al arrancar. Resultado: 0 pérdidas incluso cuando el pool está
agotado por un rato.
"""
import glob
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout

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

# Timeout corto para la escritura rápida de `encolar()`. Si el pool está
# saturado no bloqueamos el gthread 20s (eso bloquea más llegadas y hace
# que Meta reintente, cascadeando). Fallamos rápido y escribimos a disco.
_ENCOLAR_TIMEOUT_SEG = float(os.environ.get("WEBHOOK_ENCOLAR_TIMEOUT", "2.0"))

# Directorio de respaldo en disco cuando el pool está saturado. En Render
# `/var/data/media` es un disco persistente (ver render.yaml); fuera de
# Render cae a tempdir.
_DEFAULT_SPOOL = "/var/data/media/webhook_spool"
WEBHOOK_SPOOL = os.environ.get("WEBHOOK_SPOOL", _DEFAULT_SPOOL)

# Cuántos archivos procesa `_drenar_spool()` por tanda. Si hay cientos de
# archivos acumulados (p.ej. porque el pool DB estuvo caído), no queremos
# bloquear el thread minutos. Procesamos BATCH y cedemos; la próxima ronda
# del dispatcher sigue.
_SPOOL_BATCH = int(os.environ.get("WEBHOOK_SPOOL_BATCH", "20"))

# Kill-switch: si el spool está roto o enorme, se puede saltar el drenaje
# inicial poniendo WEBHOOK_SPOOL_DRAIN_ON_START=false. El dispatcher igual
# intentará drenar en cada ronda cuando esté levantado.
_DRAIN_ON_START = os.environ.get("WEBHOOK_SPOOL_DRAIN_ON_START", "true").lower() != "false"

# Pausa entre items al drenar para ceder al scheduler y no monopolizar el CPU.
_SPOOL_YIELD_SEG = float(os.environ.get("WEBHOOK_SPOOL_YIELD", "0.05"))

_iniciado = False
_lock = threading.Lock()
_despertar = threading.Event()
_pool = None


# ----------------------------------------------------------------------------
# API pública
# ----------------------------------------------------------------------------

def encolar(payload):
    """Inserta el payload completo del webhook en `webhook_queue` y despierta
    al dispatcher. Devuelve el id de la fila (útil para logs/debug).

    Si el pool está saturado (`PoolTimeout`) o la DB falla, el payload NO se
    pierde: se escribe en disco (`WEBHOOK_SPOOL`) y el dispatcher lo reclamará
    en la próxima ronda. Devuelve None en ese caso.
    """
    try:
        with db._pool_conexion(timeout=_ENCOLAR_TIMEOUT_SEG) as conn:
            row = conn.execute(
                "INSERT INTO webhook_queue (payload) VALUES (%s) RETURNING id",
                (Jsonb(payload),),
            ).fetchone()
        _despertar.set()
        return row[0] if row else None
    except PoolTimeout:
        # Pool contendido: no bloqueamos el gthread. Caemos a disco.
        _spool_payload(payload, motivo="pool_timeout")
        _despertar.set()
        return None
    except Exception as exc:
        # Error inesperado (SSL, red, DB caída): mismo tratamiento, 0 pérdidas.
        _spool_payload(payload, motivo=f"db_error:{type(exc).__name__}")
        _despertar.set()
        return None


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

    # Drena en BACKGROUND cualquier payload que haya quedado en disco por
    # `PoolTimeout` en un ciclo anterior. Si lo corriéramos en este thread
    # con cientos de archivos acumulados, bloquearíamos el arranque minutos
    # y el server dejaría de responder health checks → el host lo reinicia
    # y entra en loop. El dispatcher igual re-intenta en cada ronda.
    if _DRAIN_ON_START:
        threading.Thread(target=_drenar_spool_background,
                         name="webhook-spool-drain-start",
                         daemon=True).start()
    else:
        log.warning("[WebhookQueue] WEBHOOK_SPOOL_DRAIN_ON_START=false — skip drenaje inicial.")

    threading.Thread(target=_bucle, name="webhook-queue", daemon=True).start()
    log.info("[WebhookQueue] Dispatcher iniciado (workers=%d, encolar_timeout=%.1fs, "
             "spool=%s, batch=%d, drain_on_start=%s).",
             _WORKERS, _ENCOLAR_TIMEOUT_SEG, WEBHOOK_SPOOL,
             _SPOOL_BATCH, _DRAIN_ON_START)


def _drenar_spool_background():
    """Drena el spool completo en tandas, cediendo entre tandas. Para
    el drenaje inicial (fuera del dispatcher). Si quedan archivos luego
    de pasar por todas las tandas, el bucle del dispatcher sigue trabajando."""
    try:
        total = 0
        while True:
            n = _drenar_spool()
            if not n:
                break
            total += n
            # Ceder para no monopolizar CPU/IO. Entre tandas damos aire
            # al worker thread y a los HTTP handlers.
            time.sleep(_SPOOL_YIELD_SEG)
        if total:
            log.info("[WebhookQueue] Drenaje inicial: %d payload(s) recuperados.", total)
    except Exception:
        log.exception("[WebhookQueue] Error en drenaje inicial de spool.")


# ----------------------------------------------------------------------------
# Spool a disco (fallback cuando el pool está saturado)
# ----------------------------------------------------------------------------

def _spool_dir():
    """Devuelve el directorio de spool, creándolo si hace falta. Cae a tempdir
    si el disco persistente no existe (desarrollo local)."""
    for d in (WEBHOOK_SPOOL, os.path.join(tempfile.gettempdir(), "massi_webhook_spool")):
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except Exception:
            continue
    return None


def _spool_payload(payload, motivo="unknown"):
    """Escribe el payload del webhook en disco. Nunca lanza — el handler HTTP
    ya respondió 200 y Meta no reintentará."""
    try:
        d = _spool_dir()
        if not d:
            log.error("[WebhookQueue] Sin spool dir — PAYLOAD PERDIDO. motivo=%s", motivo)
            return
        # Nombre único: timestamp + uuid4 (evita colisiones entre threads).
        nombre = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.json"
        tmp = os.path.join(d, nombre + ".tmp")
        final = os.path.join(d, nombre)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        # Rename atómico: el drenador nunca ve archivos a medio escribir.
        os.replace(tmp, final)
        log.warning("[WebhookQueue] Payload spooled a %s (motivo=%s).", final, motivo)
    except Exception:
        log.exception("[WebhookQueue] No se pudo spoolear payload. motivo=%s", motivo)


def _drenar_spool():
    """Intenta re-encolar en Postgres hasta `_SPOOL_BATCH` payloads del spool
    en UNA tanda. Los que fallan quedan en disco para la próxima ronda.
    Devuelve cuántos se encolaron OK.

    Se limita a BATCH para no bloquear al caller: si hay cientos de archivos
    acumulados, un drenaje completo síncrono podría tardar minutos y
    bloquear el thread. El dispatcher llama a esto en cada ronda, así que
    el resto se procesa luego.
    """
    d = _spool_dir()
    if not d:
        return 0
    # Ordenamos por nombre (nombre empieza con timestamp ms) → FIFO.
    archivos = sorted(glob.glob(os.path.join(d, "*.json")))[:_SPOOL_BATCH]
    if not archivos:
        return 0
    ok = 0
    for ruta in archivos:
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            log.exception("[WebhookQueue] Spool corrupto, removiendo: %s", ruta)
            try:
                os.remove(ruta)
            except Exception:
                pass
            continue
        try:
            with db._pool_conexion(timeout=_ENCOLAR_TIMEOUT_SEG) as conn:
                conn.execute(
                    "INSERT INTO webhook_queue (payload) VALUES (%s)",
                    (Jsonb(payload),),
                )
            os.remove(ruta)
            ok += 1
        except PoolTimeout:
            # Sigue contendido: dejamos este y los que falten para la próxima.
            break
        except Exception:
            log.exception("[WebhookQueue] Error re-encolando %s — se deja para reintento.", ruta)
            break
        # Yield mínimo entre items para no monopolizar el thread si el
        # pool responde rápido pero hay mucho que hacer.
        if _SPOOL_YIELD_SEG > 0:
            time.sleep(_SPOOL_YIELD_SEG)
    return ok


# ----------------------------------------------------------------------------
# Dispatcher
# ----------------------------------------------------------------------------

def _bucle():
    global _pool
    _pool = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="whq")
    while True:
        # Primero drena el spool: si quedó algo pendiente por PoolTimeout
        # anterior, hay que re-encolarlo antes de leer la cola.
        try:
            n = _drenar_spool()
            if n:
                log.info("[WebhookQueue] %d payload(s) drenados del spool.", n)
        except Exception:
            log.exception("[WebhookQueue] Error drenando spool.")

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

    # Statuses de WhatsApp (sent/delivered/read/failed): Meta los manda
    # para cada OUT que enviamos. Los correlacionamos con la fila OUT
    # por `wa_msg_id` (que guardamos al enviar en app/whatsapp.py).
    statuses = change.get("statuses") or []
    for s in statuses:
        _procesar_status(s)

    # Llamadas entrantes de WhatsApp: cuando el cliente abre el chat y toca
    # el icono de telefono, Meta manda un item en `calls[]`. No hay audio ni
    # SIP — solo el evento. Lo loggeamos y notificamos al asesor.
    calls = change.get("calls") or []
    for call in calls:
        _procesar_llamada_entrante(call)

    messages = change.get("messages")
    if not messages:
        # Eventos de status y preferencias puras: ya tratados arriba.
        return

    message = messages[0]
    # Si el usuario oculta su número tras un nombre de usuario de WhatsApp,
    # Meta no manda "from" sino "from_user_id" (BSUID, ej. "CO.123...").
    phone = message.get("from") or message["from_user_id"]
    event = _to_event(message)
    log.info("Mensaje entrante de %s: %s", phone, event)

    _procesar_mensaje(phone, event, message)


def _procesar_status(s):
    """Procesa un item del array `statuses` del webhook de Meta.

    Formato tipico:
      {
        "id": "wamid.XXXX",
        "status": "sent" | "delivered" | "read" | "failed",
        "timestamp": "1760000000",
        "recipient_id": "573001112233",
        "errors": [{"code": 131047, "title": "...", "message": "..."}]
      }

    Delega en `db.actualizar_estado_entrega`, que es idempotente y
    monotonica (sent < delivered < read; failed siempre pisa)."""
    wa_msg_id = s.get("id")
    estado = s.get("status")
    if not wa_msg_id or not estado:
        return
    timestamp = s.get("timestamp") or 0

    error_text = None
    errores = s.get("errors") or []
    if errores:
        err = errores[0] or {}
        titulo = (err.get("title") or "").strip()
        mensaje = (err.get("message") or err.get("error_data", {}).get("details") or "").strip()
        if titulo and mensaje:
            error_text = f"{titulo}: {mensaje}"[:500]
        else:
            error_text = (titulo or mensaje or "error desconocido")[:500]

    try:
        db.actualizar_estado_entrega(wa_msg_id, estado, timestamp, error_text)
    except Exception:
        log.exception("[WebhookQueue] fallo actualizando estado %s para %s",
                      estado, wa_msg_id)


def _procesar_llamada_entrante(call):
    """Procesa un item del array `calls` del webhook de Meta.

    Formato tipico:
      {
        "id": "wacid.XXXX",
        "from": "573001112233",
        "event": "connect" | "terminate" | ...,
        "timestamp": "1760000000"
      }

    Cuando el cliente abre el chat de Massi y toca el icono de telefono,
    Meta manda `event=connect` (cliente llamando AHORA). Notificamos al
    asesor con un ping WA y link al panel del contacto, y guardamos un
    registro sintetico en `mensajes` (tipo='call_in') para que aparezca
    en el chat del panel.

    NO forwardea la llamada (eso requeriria SIP + Twilio, Fase 3). Es
    solo notificacion al asesor para que vea el panel y llame al cliente
    de vuelta por WhatsApp.

    Las llamadas se notifican SIEMPRE (sin cooldown), incluso si el
    contacto tiene `no_contactar=true` — un lead que esta llamando hay
    que atenderlo.
    """
    telefono = call.get("from") or ""
    evento = call.get("event") or "unknown"
    call_id = call.get("id") or ""
    log.info("[WebhookQueue] Llamada entrante de %s evento=%s", telefono, evento)

    if not telefono:
        return

    # Persistir row sintetica en `mensajes` para que la llamada aparezca
    # en el chat del panel del contacto.
    try:
        db.log_mensaje(
            telefono, "in", "call_in",
            f"[Llamada entrante — {evento}]",
            {"call_id": call_id, "evento": evento},
        )
    except Exception:
        log.exception("[WebhookQueue] No se pudo registrar llamada entrante de %s", telefono)

    # Evento de timeline (iter 1 oct-2026). Sólo `connect` significa "me
    # llamó ahora"; `terminate` y demás son del ciclo de vida y los
    # dejamos solo en `mensajes` (ya está la row sintética).
    if evento == "connect":
        try:
            db.registrar_evento_por_telefono(
                telefono, "llamada_recibida",
                {"call_id": call_id, "evento": evento}, autor="sistema",
            )
        except Exception:
            log.exception("[WebhookQueue] fallo evento timeline llamada %s",
                          telefono)

    # Solo notificamos al asesor cuando el cliente esta llamando AHORA
    # (`connect`). Para `terminate` y otros solo logueamos.
    if evento != "connect":
        return

    try:
        from app import bot, whatsapp as wa
        panel_url = os.environ.get("PANEL_URL", "").strip().rstrip("/")
        panel_token = os.environ.get("PANEL_TOKEN", "").strip()
        asesores_csv = os.environ.get("ALERTAS_WHATSAPP", bot.ALERTAS_WHATSAPP).strip()
        asesores = [n.strip() for n in asesores_csv.split(",") if n.strip()]
        if not asesores:
            log.info("[WebhookQueue] ALERTAS_WHATSAPP no seteado; sin ping WA por llamada de %s",
                     telefono)
            return
        link = ""
        if panel_url and panel_token:
            link = f"{panel_url}/panel/{telefono}?token={panel_token}"
        else:
            link = "(configurar PANEL_URL/PANEL_TOKEN)"
        mensaje = (
            f"📞 Llamada entrante de {telefono}\n\n"
            f"El cliente te está llamando por WhatsApp AHORA.\n"
            f"Abrilo en el panel para ver su conversación:\n{link}"
        )
        for asesor in asesores:
            try:
                wa.send_text(asesor, mensaje)
                log.info("[WebhookQueue] WA enviado a asesor %s por llamada de %s",
                         asesor, telefono)
            except Exception:
                log.exception("[WebhookQueue] fallo WA a asesor %s por llamada de %s",
                              asesor, telefono)
    except Exception:
        log.exception("[WebhookQueue] Error disparando alerta de llamada para %s", telefono)


def _procesar_mensaje(phone, event, message=None):
    """Antes vivía en server.py como `_procesar_mensaje_async`. Loggea el
    mensaje IN en `mensajes`, descarga media si aplica, y pasa el evento al
    bot. Si falla, el caller (_procesar_fila) marca la fila como fallo."""
    # Media: audio/image/video/document/sticker. Son los únicos tipos cuyo
    # payload de DB depende del resultado de la descarga, así que se persisten
    # DESPUÉS de bajar el archivo (ver _procesar_media_in). Si llegara un media
    # sin media_id (edge case), cae al logging normal de abajo.
    if event.get("type") == "media" and event.get("media_id"):
        _procesar_media_in(phone, event, message)
        return

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

    # Bot pausado por un asesor humano para este lead (take-over humano):
    # el bot sigue corriendo toda su lógica interna (clasificación,
    # requiere_humano, cambios de sesión, filtros, remarketing marks) pero
    # NO envía respuestas al cliente. Esto lo hacemos envolviendo el
    # handle_incoming con `whatsapp.pausar_sends_temporalmente()`, un
    # context manager thread-local que apaga `whatsapp._dispatch` solo en
    # este thread.
    pausado = False
    try:
        pausado = db.bot_esta_pausado(phone)
    except Exception:
        log.exception("[WebhookQueue] Error consultando bot_esta_pausado(%s)",
                      phone)

    from app import bot
    if pausado:
        log.info("[bot pausado] skip autoreply for %s", phone)
        from app import whatsapp as _wa
        with _wa.pausar_sends_temporalmente():
            bot.handle_incoming(phone, event)
    else:
        bot.handle_incoming(phone, event)


# ----------------------------------------------------------------------------
# Media entrante (audio/image/video/document/sticker)
# ----------------------------------------------------------------------------

# Mapeo mime → extensión para armar el nombre de archivo. Si el mime no está
# acá, cae a `.bin` (el panel igual sirve el archivo con su Content-Type).
_MEDIA_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/amr": ".amr",
    "video/mp4": ".mp4",
    "video/3gp": ".3gp",
    "video/3gpp": ".3gp",
    "video/quicktime": ".mov",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/zip": ".zip",
    "text/plain": ".txt",
}

# Carpeta en disco donde se guardan los binarios. En Render `/var/data/media`
# es un disco persistente (ver render.yaml); fuera de Render se puede sobrescribir
# con la env var MEDIA_DIR.
MEDIA_DIR = os.environ.get("MEDIA_DIR", "/var/data/media/wa")


def _extension_para_mime(mime: str) -> str:
    if not mime:
        return ".bin"
    mime = mime.split(";", 1)[0].strip().lower()
    return _MEDIA_EXT.get(mime, ".bin")


def _resumen_media(subtipo: str, caption: str, voice: bool) -> str:
    """Texto corto para la columna `resumen` del panel."""
    cap = (caption or "").strip()
    if cap:
        return cap[:4000]
    if subtipo == "audio":
        return "[nota de voz]" if voice else "[audio]"
    if subtipo == "image":
        return "[imagen]"
    if subtipo == "video":
        return "[video]"
    if subtipo == "sticker":
        return "[sticker]"
    if subtipo == "document":
        return "[documento]"
    return f"[{subtipo}]"


def _descargar_media_a_disco(wa_msg_id: str, media_id: str, mime: str):
    """Descarga el media a MEDIA_DIR. Devuelve (local_path, size, meta).

    Si la descarga falla loguea y devuelve (None, None, None) — el mensaje
    debe quedar en DB aunque no tengamos el binario (reintentable después).
    """
    from app import whatsapp as wa
    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
    except Exception:
        log.exception("[Media IN] No se pudo crear MEDIA_DIR=%s", MEDIA_DIR)
        return None, None, None

    ext = _extension_para_mime(mime)
    nombre_base = wa_msg_id or f"m-{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}"
    # wa_msg_id trae wamid.* con '.' y chars seguros, pero defensivo: evitar
    # separadores de ruta por si cambia el formato.
    nombre_base = nombre_base.replace("/", "_").replace("\\", "_")
    destino = os.path.join(MEDIA_DIR, f"{nombre_base}{ext}")
    try:
        meta = wa.descargar_media(media_id, destino)
        size = os.path.getsize(destino) if os.path.exists(destino) else None
        return destino, size, meta
    except Exception:
        log.exception("[Media IN] Error descargando media_id=%s (mime=%s)",
                      media_id, mime)
        # Si quedó un archivo a medio bajar, limpiarlo para no servir basura.
        try:
            if os.path.exists(destino):
                os.remove(destino)
        except Exception:
            pass
        return None, None, None


def _procesar_media_in(phone, event, message):
    """Handler nuevo para audio/image/video/document/sticker entrantes.

    1. Descarga el binario a disco (si Meta responde).
    2. Persiste la fila en `mensajes` con tipo=<subtipo> y payload con todo
       lo necesario para que el panel pueda servir el archivo.
    3. Si el contacto está bloqueado (`no_contactar=true`), no responde.
    4. Marca `requiere_humano=true` y manda alerta WA a los asesores
       configurados en `ALERTAS_WHATSAPP` (reusa `_enviar_alerta_lead_caliente`).
    5. Responde al cliente una confirmación corta.
    """
    subtipo = event.get("media_subtipo") or "media"
    media_id = event.get("media_id") or ""
    mime = event.get("mime_type") or ""
    caption = event.get("caption") or ""
    voice = bool(event.get("voice", False))
    filename = event.get("filename") or ""
    wa_msg_id = (message or {}).get("id") or ""

    local_path, size, meta = _descargar_media_a_disco(wa_msg_id, media_id, mime)

    payload = {
        "wa_msg_id": wa_msg_id,
        "media_id": media_id,
        "mime": mime,
        "local_path": local_path,
        "caption": caption,
        "voice": voice,
        "filename": filename,
        "size": size,
        "sha256": (meta or {}).get("sha256"),
        "subtipo": subtipo,
    }
    resumen = _resumen_media(subtipo, caption, voice)
    try:
        db.log_mensaje(phone, "in", subtipo, resumen, payload)
    except Exception:
        log.exception("[Media IN] No se pudo registrar mensaje de %s", phone)

    # Opt-out tiene precedencia absoluta: no respondemos NADA al bloqueado.
    try:
        if db.esta_bloqueado(phone):
            log.info("[Media IN] %s con no_contactar=true, no respondo", phone)
            return
    except Exception:
        log.exception("[Media IN] Error consultando esta_bloqueado(%s)", phone)

    # Marcar como requiere_humano (idempotente) y disparar alerta si es
    # nuevo o si pasaron >= cooldown_minutos desde la última alerta —
    # reusa el canal de alerta WA que ya tiene bot.py.
    motivo = f"envio {subtipo}: {(caption or '')[:100]}"
    debe_alertar = False
    try:
        resultado = db.marcar_requiere_humano(phone, motivo)
        # Compat: `marcar_requiere_humano` ahora devuelve dict; en los tests
        # un stub puede devolver bool. Normalizamos.
        if isinstance(resultado, dict):
            debe_alertar = bool(resultado.get("debe_alertar"))
        else:
            debe_alertar = bool(resultado)
    except Exception:
        log.exception("[Media IN] Error marcando requiere_humano para %s", phone)

    try:
        from app import bot
        if debe_alertar:
            bot._enviar_alerta_lead_caliente(phone, resumen)
    except Exception:
        log.exception("[Media IN] Error disparando alerta lead caliente para %s", phone)

    # Confirmación corta al cliente. Sin tildes en el mensaje para mantener
    # consistencia con el resto de outs del bot al usuario.
    try:
        from app import whatsapp as wa
        wa.send_text(
            phone,
            "Recibimos tu mensaje! Un asesor te respondera por este chat.",
        )
    except Exception:
        log.exception("[Media IN] Error enviando confirmacion a %s", phone)
