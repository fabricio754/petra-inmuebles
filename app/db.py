"""Almacenamiento en PostgreSQL (se activa con la variable DATABASE_URL).

state.py llama a estas funciones; bot.py no sabe que existen. Al
arrancar se crean las tablas (schema.sql) y, la primera vez, se copian
los datos que había en la Google Sheet (o el inventario base si no hay
Sheet).

Spool a disco de OUTs
---------------------
Durante el piloto 6-oct se perdieron 6 filas de `mensajes` sentido='out'
porque `log_mensaje` fallo con `PoolTimeout` (pool DB saturado por
corrupcion TLS en la red interna de Render). El WA ya se habia enviado
exitosamente, pero perdimos la trazabilidad en DB.

Ahora `log_mensaje` cae a un spool en disco (`OUT_SPOOL_DIR`,
por default `/var/data/media/log_mensaje_spool`) cuando no logra escribir
en la DB. El scheduler drena el spool cada 10 min, y al arrancar el
servicio (ver `_drenar_spool_log_mensaje`). Patron equivalente al de
`app/webhook_worker.py` para los IN.
"""
import glob
import json
import logging
import os
import tempfile
import threading
import time
import uuid

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout

log = logging.getLogger("petra")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

_pool = None
_pool_lock = threading.Lock()

# === Spool a disco para `log_mensaje` (fallback cuando el pool esta saturado) ===
#
# En Render `/var/data/media` es un disco persistente (ver render.yaml); fuera
# de Render cae a tempdir. Se mantiene separado del spool de IN
# (`WEBHOOK_SPOOL`) para no mezclar formatos.
_DEFAULT_OUT_SPOOL = "/var/data/media/log_mensaje_spool"
OUT_SPOOL_DIR = os.environ.get("OUT_SPOOL_DIR", _DEFAULT_OUT_SPOOL)

# Cuantos archivos procesa `_drenar_spool_log_mensaje()` por tanda. Si hay
# cientos acumulados (ej. porque el pool DB estuvo caido), no bloqueamos el
# thread minutos. El scheduler re-llama cada 10 min hasta drenar todo.
OUT_SPOOL_DRAIN_BATCH = int(os.environ.get("OUT_SPOOL_DRAIN_BATCH", "20"))

# Kill-switch: si el spool esta roto o enorme, se puede saltar el drenaje
# inicial poniendo OUT_SPOOL_DRAIN_ON_START=false. Default true: el piloto
# 6-oct mostro payloads huerfanos tras reinicios.
OUT_SPOOL_DRAIN_ON_START = os.environ.get("OUT_SPOOL_DRAIN_ON_START", "true").lower() != "false"


def _pool_conexion(timeout=20.0):
    """Igual que `_conexion()` pero con timeout parametrizable. Útil en
    hot-paths (p. ej. webhook_worker.encolar) donde queremos fallar rápido
    si el pool está saturado en vez de bloquear el gthread 20s mientras
    Meta reintenta y cascadea más carga."""
    _asegurar_pool()
    return _pool.connection(timeout=timeout)


def _conexion():
    """Pool de conexiones, creado (y la base preparada) en el primer uso."""
    _asegurar_pool()
    return _pool.connection(timeout=20.0)


def _preparar_url(url):
    """Normaliza la DSN: agrega keepalives, connect_timeout y sslmode=prefer
    si no están ya presentes. Compartido entre el pool y las conns directas
    para asegurar que ambas usen los mismos parámetros TCP/SSL."""
    import re as _re

    # TCP keepalives: kernel-managed, work in all threads.
    # After 30s idle the kernel probes the connection; 3 failed probes
    # (15s total) close the socket with a real error instead of hanging.
    if "keepalives" not in url:
        sep = "&" if "?" in url else "?"
        url = (f"{url}{sep}keepalives=1&keepalives_idle=30"
               "&keepalives_interval=5&keepalives_count=3")
    if "connect_timeout" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}connect_timeout=10"
    # sslmode=prefer: try SSL first, fall back to plain — Render's internal
    # PostgreSQL may require SSL; sslmode=disable can cause the server to
    # drop the connection silently.
    if "sslmode" in url:
        url = _re.sub(r"sslmode=[^&\s]*", "sslmode=prefer", url)
    else:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=prefer"
    return url


def _conexion_directa(timeout=10):
    """Abre una conn directa a Postgres, bypassing el pool compartido.

    Usar SOLO en rutas de baja frecuencia (ej. panel) donde no vale la pena
    pelear por una conn del pool compartido. Paga 200-500ms extra de TCP
    + handshake TLS por request, pero es inmune a la corrupción del pool
    (errores SSL 'bad record mac' en la red interna de Render que dejan
    conns muertas ocupando slots)."""
    url = DATABASE_URL
    if not url:
        raise RuntimeError("[DB] DATABASE_URL no configurada")
    url = _preparar_url(url)
    # connect_timeout aquí sobre-escribe el de la DSN con el que pide el caller.
    return psycopg.connect(url, connect_timeout=timeout)


def _asegurar_pool():
    """Crea el pool (y prepara el schema) la primera vez. Hilo-seguro."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                import re as _re

                url = DATABASE_URL
                if not url:
                    raise Exception("[DB] DATABASE_URL no configurado")

                url = _preparar_url(url)

                safe_url = _re.sub(r":[^@]+@", ":***@", url)
                log.info("[DB] Conectando a: %s", safe_url)

                # connect_timeout via kwargs: psycopg3 handles this with select(),
                # which works in all threads — unlike connect_timeout in the DSN
                # which libpq resolves with alarm() (main thread only).
                # min_size=1: pool keeps one live connection so requests reuse it
                # without creating new TCP connections from worker threads.
                # check=ConnectionPool.check_connection: antes de servir una
                # conn al cliente la validamos con un SELECT 1 — si la conn
                # está muerta por error SSL (bad record mac) el pool la
                # descarta y abre una nueva, en vez de dejarla ocupando un
                # slot fantasma hasta reiniciar el proceso.
                # max_lifetime=600: recicla cada conn a los 10 min, antes de
                # que lleve suficiente tráfico TLS como para corromperse.
                # max_idle=180: cierra conns ociosas >3 min (menos presión
                # de slots inactivos).
                # reconnect_timeout=30: no se queda colgado reconectando.
                p = ConnectionPool(
                    url,
                    min_size=1,
                    max_size=int(os.environ.get("DB_POOL_MAX_SIZE", "20")),
                    open=True,
                    check=ConnectionPool.check_connection,
                    max_lifetime=600,
                    max_idle=180,
                    reconnect_timeout=30,
                    kwargs={"connect_timeout": 12},
                )
                _preparar(p)
                _pool = p


def _preparar(pool):
    with pool.connection() as conn:
        with open(SCHEMA_PATH, encoding="utf-8") as f:
            conn.execute(f.read())
        ya = conn.execute("SELECT 1 FROM meta WHERE clave = 'datos_iniciales'").fetchone()
        if ya:
            return
        _cargar_datos_iniciales(conn)
        conn.execute(
            "INSERT INTO meta (clave, valor) VALUES ('datos_iniciales', 'ok') "
            "ON CONFLICT (clave) DO NOTHING"
        )
    log.info("[Postgres] Base preparada.")


def _cargar_datos_iniciales(conn):
    """Carga el inventario base de data/inventario.seed.json.

    La importación desde Google Sheets se omite intencionalmente en el arranque
    para evitar bloqueos de red que cuelgan el worker de gunicorn."""
    from app import state  # import tardío: state importa este módulo

    inventario = state.inventario_seed()
    sesiones: dict = {}
    leads: list = []

    for apto in inventario:
        conn.execute(
            "INSERT INTO inventario (id, datos) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            (apto["id"], Jsonb(apto)),
        )
    for telefono, datos in sesiones.items():
        conn.execute(
            "INSERT INTO sesiones (telefono, estado, datos) VALUES (%s, %s, %s) "
            "ON CONFLICT (telefono) DO NOTHING",
            (telefono, datos.get("flow"), Jsonb(datos)),
        )
    for lead in leads:
        conn.execute(
            "INSERT INTO leads (telefono, operacion, datos) VALUES (%s, %s, %s)",
            (lead.get("telefono"), lead.get("operacion"), Jsonb(lead)),
        )


def load_inventario():
    with _conexion() as conn:
        filas = conn.execute("SELECT datos FROM inventario ORDER BY creado, id").fetchall()
    return [f[0] for f in filas]


def save_inmueble(apto):
    with _conexion() as conn:
        conn.execute(
            "INSERT INTO inventario (id, datos) VALUES (%s, %s) "
            "ON CONFLICT (id) DO UPDATE SET datos = EXCLUDED.datos",
            (apto["id"], Jsonb(apto)),
        )


def get_session(phone):
    with _conexion() as conn:
        fila = conn.execute("SELECT datos FROM sesiones WHERE telefono = %s", (phone,)).fetchone()
    return dict(fila[0]) if fila else {}


def save_session(phone, session):
    with _conexion() as conn:
        conn.execute(
            "INSERT INTO sesiones (telefono, estado, datos, ultima_actividad) "
            "VALUES (%s, %s, %s, NOW()) "
            "ON CONFLICT (telefono) DO UPDATE SET estado = EXCLUDED.estado, "
            "datos = EXCLUDED.datos, ultima_actividad = NOW()",
            (phone, session.get("flow"), Jsonb(session)),
        )


def append_lead(lead):
    # json.loads(json.dumps(...)) asegura que todo sea serializable.
    datos = json.loads(json.dumps(lead, ensure_ascii=False, default=str))
    with _conexion() as conn:
        conn.execute(
            "INSERT INTO leads (telefono, operacion, datos) VALUES (%s, %s, %s)",
            (lead.get("telefono"), lead.get("operacion"), Jsonb(datos)),
        )


def save_pipeline(data):
    with _conexion() as conn:
        conn.execute(
            "INSERT INTO pipeline "
            "(telefono, nombre, cedula, edad, email, direccion_inmueble, ciudad, "
            "tipo_inmueble, estrato, es_ph, objetivo_prestamo, valor_solicitado, "
            "avaluo_comercial, "
            "requiere_paz_salvo, autorizacion_datos_en, estado) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                data.get("telefono"),
                data.get("nombre"),
                data.get("cedula"),
                data.get("edad"),
                data.get("email"),
                data.get("direccion_inmueble"),
                data.get("ciudad"),
                data.get("tipo_inmueble"),
                data.get("estrato"),
                data.get("es_ph"),
                data.get("objetivo_prestamo"),
                data.get("valor_solicitado"),
                data.get("avaluo_comercial"),
                bool(data.get("requiere_paz_salvo", False)),
                data.get("autorizacion_datos_en"),
                data.get("estado", "NUEVO"),
            ),
        )


def set_no_contactar(phone, valor):
    # Quien escribe por su cuenta no está en contactos (eso lo llena el
    # raspador), así que se inserta si no existe.
    with _conexion() as conn:
        conn.execute(
            "INSERT INTO contactos (telefono, no_contactar) VALUES (%s, %s) "
            "ON CONFLICT (telefono) DO UPDATE SET no_contactar = EXCLUDED.no_contactar",
            (phone, valor),
        )


def esta_bloqueado(telefono):
    """True si el teléfono tiene no_contactar=TRUE en contactos (opt-out).

    Usado por el bot al inicio del handler de IN para quedarse mudo tras
    un opt-out, en vez de seguir contestando (loop de consent, etc.).
    No lanza: si la DB falla devuelve False y el bot responde normal.
    """
    try:
        with _conexion() as conn:
            fila = conn.execute(
                "SELECT no_contactar FROM contactos WHERE telefono = %s",
                (telefono,),
            ).fetchone()
        return bool(fila and fila[0])
    except Exception as exc:
        log.warning("[esta_bloqueado] no se pudo consultar (%s): %s", telefono, exc)
        return False


def log_mensaje(telefono, direccion, tipo, resumen, payload=None, wa_msg_id=None):
    """Inserta una fila en `mensajes`. Nunca lanza excepcion al caller.

    Fast path con 3 niveles para que los mensajes IN del webhook y OUT del
    bot aparezcan en el chat del panel al instante aun con el pool saturado:

      1. Pool con timeout corto (3s). Lo normal.
      2. `_conexion_directa` (bypass del pool). Si el pool esta saturado
         pero la DB responde, abrir una conn directa cuesta ~200-500ms
         extra pero no espera 20s. pgBouncer multiplexa, costo ok.
      3. Spool a disco. Ultimo recurso: si tambien falla la directa (DB
         caida, red rota, etc.), serializa args a JSON y los deja en
         `OUT_SPOOL_DIR` para que `_drenar_spool_log_mensaje()` los retire
         cuando la DB vuelva. El scheduler lo drena c/10 min.

    `wa_msg_id` es el id que devuelve Meta al aceptar un OUT. Se guarda en
    columna propia para que `actualizar_estado_entrega()` pueda matchear
    los eventos de status (sent/delivered/read/failed) contra la fila.

    El spool se escribe en el thread actual (sync), no asincrono: si el
    proceso muere aca no perdemos nada.
    """
    import json as _json

    sql = ("INSERT INTO mensajes "
           "(telefono, direccion, tipo, resumen, payload, wa_msg_id) "
           "VALUES (%s, %s, %s, %s, %s::jsonb, %s)")
    payload_json = _json.dumps(payload) if payload is not None else None
    values = (str(telefono)[:150], direccion[:4], (tipo or "")[:40],
              (resumen or "")[:4000], payload_json, wa_msg_id)

    # Nivel 1: pool con timeout corto (3s en vez de 20s).
    try:
        with _pool_conexion(timeout=3.0) as conn:
            conn.execute(sql, values)
        return
    except PoolTimeout:
        log.warning("[log_mensaje] pool_timeout (3s, %s) — intentando conexion directa.",
                    direccion)
    except Exception as exc:
        log.warning("[log_mensaje] fallo pool (%s, %s) — intentando conexion directa.",
                    direccion, exc)

    # Nivel 2: conn directa (bypass del pool). ~200-500ms extra pero no se
    # queda esperando una slot del pool.
    try:
        with _conexion_directa(timeout=5) as conn:
            conn.execute(sql, values)
        return
    except Exception as exc:
        log.warning("[log_mensaje] fallo conexion directa (%s, %s) — cayendo al spool.",
                    direccion, exc)

    # Nivel 3: spool a disco. El drain c/10 min reintenta.
    _spool_log_mensaje(telefono, direccion, tipo, resumen, payload,
                       motivo="pool_y_directa_fallaron",
                       wa_msg_id=wa_msg_id)


# Ranking monotonico para los estados de entrega. Se usa en el UPDATE para
# que un evento mas atrasado (ej. 'sent' que llega despues de 'delivered')
# no "baje" el estado. 'failed' es un caso aparte: siempre pisa.
_ESTADO_ENTREGA_ORDEN = {"sent": 1, "delivered": 2, "read": 3, "failed": 99}


def actualizar_estado_entrega(wa_msg_id, estado, timestamp_unix, error_text=None):
    """Actualiza el estado de entrega de un OUT identificado por `wa_msg_id`.

    Idempotente y monotonica:
      - sent -> delivered -> read: solo "sube".
      - cualquier estado -> failed: siempre pisa.
      - failed NO se puede sobreescribir con sent/delivered/read.

    Si el `wa_msg_id` no existe en `mensajes`, el UPDATE no afecta filas
    y no lanza (puede pasar si el OUT se envio desde otra instancia o si
    nunca se logueo el OUT).

    Usa `_conexion_directa` para no pelear por una slot del pool: los
    webhooks de status llegan en rafagas (sent + delivered + read por
    cada OUT) y a veces coinciden con un backlog del worker.
    """
    if not wa_msg_id or not estado:
        return

    nuevo_rank = _ESTADO_ENTREGA_ORDEN.get(estado, 0)
    try:
        with _conexion_directa(timeout=5) as conn:
            conn.execute(
                """
                UPDATE mensajes
                SET estado_entrega = %s,
                    estado_entrega_at = to_timestamp(%s),
                    estado_entrega_error = %s
                WHERE wa_msg_id = %s
                  AND (
                      estado_entrega IS NULL
                      OR %s = 'failed'
                      OR (
                          estado_entrega <> 'failed'
                          AND CASE estado_entrega
                              WHEN 'sent' THEN 1
                              WHEN 'delivered' THEN 2
                              WHEN 'read' THEN 3
                              WHEN 'failed' THEN 99
                              ELSE 0
                          END < %s
                      )
                  )
                """,
                (estado, int(timestamp_unix) if timestamp_unix else 0,
                 error_text, wa_msg_id, estado, nuevo_rank),
            )
    except Exception:
        log.exception("[actualizar_estado_entrega] fallo wa_msg_id=%s estado=%s",
                      wa_msg_id, estado)


# ---------------------------------------------------------------------------
# Spool a disco para `log_mensaje` (fallback cuando el pool esta saturado)
# ---------------------------------------------------------------------------

def _out_spool_dir():
    """Devuelve el directorio del spool, creandolo si hace falta. Cae a
    tempdir si el disco persistente no existe (desarrollo local)."""
    for d in (OUT_SPOOL_DIR, os.path.join(tempfile.gettempdir(), "petra_log_mensaje_spool")):
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except Exception:
            continue
    return None


def _spool_log_mensaje(telefono, direccion, tipo, resumen, payload,
                       motivo="unknown", wa_msg_id=None):
    """Serializa los args de `log_mensaje` a JSON en disco. Nunca lanza — el
    caller ya tuvo que decidir que no podia escalar el error."""
    try:
        d = _out_spool_dir()
        if not d:
            log.error("[LogMensajeSpool] Sin spool dir — LOG PERDIDO. motivo=%s", motivo)
            return
        row = {
            "telefono": telefono,
            "direccion": direccion,
            "tipo": tipo,
            "resumen": resumen,
            "payload": payload,
            "wa_msg_id": wa_msg_id,
            "spooled_at_ms": int(time.time() * 1000),
            "motivo": motivo,
        }
        nombre = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.json"
        tmp = os.path.join(d, nombre + ".tmp")
        final = os.path.join(d, nombre)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(row, f, ensure_ascii=False, default=str)
        # Rename atomico: el drenador nunca ve archivos a medio escribir.
        os.replace(tmp, final)
        log.warning("[LogMensajeSpool] fila spooled a %s (motivo=%s).", final, motivo)
    except Exception:
        log.exception("[LogMensajeSpool] No se pudo spoolear fila. motivo=%s", motivo)


def _drenar_spool_log_mensaje():
    """Lee hasta `OUT_SPOOL_DRAIN_BATCH` archivos del spool y reintenta
    `log_mensaje` por cada uno. Si funciona, borra el archivo. Si falla, lo
    deja para la proxima ronda (el scheduler lo re-llama c/10 min).

    Devuelve cuantos archivos se drenaron OK (para el log).
    """
    d = _out_spool_dir()
    if not d:
        return 0
    # Ordenamos por nombre (empieza con timestamp ms) → FIFO.
    archivos = sorted(glob.glob(os.path.join(d, "*.json")))[:OUT_SPOOL_DRAIN_BATCH]
    if not archivos:
        return 0
    ok = 0
    for ruta in archivos:
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                row = json.load(f)
        except Exception:
            log.exception("[LogMensajeSpool] Spool corrupto, removiendo: %s", ruta)
            try:
                os.remove(ruta)
            except Exception:
                pass
            continue
        try:
            payload_json = (json.dumps(row.get("payload"))
                            if row.get("payload") is not None else None)
            with _conexion() as conn:
                conn.execute(
                    "INSERT INTO mensajes "
                    "(telefono, direccion, tipo, resumen, payload, wa_msg_id) "
                    "VALUES (%s, %s, %s, %s, %s::jsonb, %s)",
                    (str(row.get("telefono"))[:150],
                     (row.get("direccion") or "")[:4],
                     (row.get("tipo") or "")[:40],
                     (row.get("resumen") or "")[:4000],
                     payload_json,
                     row.get("wa_msg_id")),
                )
            os.remove(ruta)
            ok += 1
        except PoolTimeout:
            # Sigue contendido: dejamos este y los que falten para la proxima
            # ronda. No vale la pena seguir probando con los demas si el pool
            # ya nos nego una conn.
            log.warning("[LogMensajeSpool] pool_timeout re-drenando %s — se deja.", ruta)
            break
        except Exception:
            log.exception("[LogMensajeSpool] Error re-insertando %s — se deja para reintento.",
                          ruta)
            # Un error de datos (ej. row corrupta a nivel esquema) va a quedar
            # trabando los que vienen detras. Rompemos el loop para no spamear
            # el log; proxima ronda sigue intentando y eventualmente la
            # removemos como "corrupta" si ya fallo con json decode.
            break
    if ok:
        log.info("[LogMensajeSpool] %d archivo(s) drenados.", ok)
    return ok


# === Captación (Hito 7) ======================================================

def guardar_contacto(c):
    """Inserta un anuncio capturado. Devuelve True si es nuevo, False si el
    teléfono ya existía (no se toca: ni se recontacta ni se pisa no_contactar).

    Usa `_conexion_directa` (bypass del pool) porque el endpoint que llama a
    esta función (`/scraper/ingest`) es de baja frecuencia (~1 req/20s desde
    Apps Script) y el pool compartido ha mostrado quedarse pegado tras
    errores iniciales (slots zombie, nunca escala a max_size). pgBouncer en
    6432 multiplexa las conns directas, así que el costo extra es mínimo."""
    with _conexion_directa(timeout=10) as conn:
        fila = conn.execute(
            "INSERT INTO contactos (telefono, nombre, direccion, barrio, ciudad, tipo_inmueble, "
            "precio_publicado, estrato, requiere_ph, url_listing, portal, foto_url, "
            "monto_hasta_millones) "
            "VALUES (%(telefono)s, %(nombre)s, %(direccion)s, %(barrio)s, %(ciudad)s, %(tipo)s, "
            "%(precio)s, %(estrato)s, %(requiere_ph)s, %(url)s, %(portal)s, %(foto)s, "
            "%(monto_hasta)s) "
            "ON CONFLICT (telefono) DO NOTHING RETURNING id",
            c,
        ).fetchone()
    return fila is not None


def contactos_por_enviar(limite, desde=None):
    """desde: solo contactos capturados a partir de esa fecha (deja fuera pruebas)."""
    with _conexion() as conn:
        filas = conn.execute(
            "SELECT telefono, tipo_inmueble, portal, monto_hasta_millones FROM contactos "
            "WHERE NOT contactado AND NOT COALESCE(no_contactar, FALSE) "
            "AND monto_hasta_millones IS NOT NULL "
            "AND (%s::timestamptz IS NULL OR fecha_scraping >= %s::timestamptz) "
            "ORDER BY fecha_scraping LIMIT %s",
            (desde, desde, limite),
        ).fetchall()
    return [dict(zip(("telefono", "tipo", "portal", "monto_hasta"), f)) for f in filas]


def get_contacto(telefono):
    """Devuelve tipo_inmueble y monto_hasta_millones del contacto (o None si no existe)."""
    with _conexion() as conn:
        fila = conn.execute(
            "SELECT tipo_inmueble, monto_hasta_millones FROM contactos WHERE telefono = %s",
            (telefono,),
        ).fetchone()
    return {"tipo_inmueble": fila[0], "monto_hasta": fila[1]} if fila else None


def enviados_desde(desde):
    with _conexion() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM contactos WHERE fecha_contacto >= %s", (desde,)
        ).fetchone()[0]


def marcar_contactado(telefono, resultado):
    with _conexion() as conn:
        conn.execute(
            "UPDATE contactos SET contactado = TRUE, fecha_contacto = NOW(), "
            "resultado_contacto = %s WHERE telefono = %s",
            (resultado, telefono),
        )


def marcar_respuesta(telefono, resultado):
    with _conexion() as conn:
        conn.execute(
            "UPDATE contactos SET resultado_contacto = %s WHERE telefono = %s AND contactado",
            (resultado, telefono),
        )


# === Pipeline / Sureti (Hito 8) =============================================

def leads_nuevos():
    """Leads en estado NUEVO sin sureti_lead_id (pendientes de registrar)."""
    with _conexion() as conn:
        filas = conn.execute(
            "SELECT id, telefono, nombre, cedula, edad, email, direccion_inmueble, ciudad, "
            "tipo_inmueble, estrato, es_ph, objetivo_prestamo, valor_solicitado, "
            "requiere_paz_salvo "
            "FROM pipeline WHERE (estado = 'NUEVO' OR estado IS NULL) "
            "AND sureti_lead_id IS NULL",
        ).fetchall()
    cols = ("id", "telefono", "nombre", "cedula", "edad", "email", "direccion_inmueble",
            "ciudad", "tipo_inmueble", "estrato", "es_ph", "objetivo_prestamo",
            "valor_solicitado", "requiere_paz_salvo")
    return [dict(zip(cols, f)) for f in filas]


def leads_en_seguimiento():
    """Leads registrados en Sureti con estado REGISTRADO, EN_ESTUDIO o APROBADO."""
    with _conexion() as conn:
        filas = conn.execute(
            "SELECT id, telefono, nombre, sureti_lead_id, estado "
            "FROM pipeline WHERE estado IN ('REGISTRADO', 'EN_ESTUDIO', 'APROBADO') "
            "AND sureti_lead_id IS NOT NULL",
        ).fetchall()
    return [dict(zip(("id", "telefono", "nombre", "sureti_lead_id", "estado"), f))
            for f in filas]


def marcar_registrado_sureti(pipeline_id, sureti_lead_id):
    with _conexion() as conn:
        conn.execute(
            "UPDATE pipeline SET sureti_lead_id = %s, estado = 'REGISTRADO' WHERE id = %s",
            (sureti_lead_id, pipeline_id),
        )


def actualizar_estado_lead(pipeline_id, estado, monto=None, razon=None):
    with _conexion() as conn:
        conn.execute(
            "UPDATE pipeline SET estado = %s, monto_aprobado = COALESCE(%s, monto_aprobado), "
            "razon_no_aprobado = COALESCE(%s, razon_no_aprobado), "
            "fecha_aprobacion = CASE WHEN %s = 'APROBADO' THEN NOW() ELSE fecha_aprobacion END "
            "WHERE id = %s",
            (estado, monto, razon, estado, pipeline_id),
        )


def documentos_de(telefono):
    """Devuelve los documentos disponibles para un teléfono."""
    with _conexion() as conn:
        filas = conn.execute(
            "SELECT tipo, media_id, url_storage FROM documentos WHERE telefono = %s",
            (telefono,),
        ).fetchall()
    return [dict(zip(("tipo", "media_id", "url_storage"), f)) for f in filas]


def update_chip(telefono, chip):
    """Guarda el CHIP catastral en el registro de pipeline más reciente."""
    with _conexion() as conn:
        conn.execute(
            "UPDATE pipeline SET chip = %s WHERE telefono = %s AND id = ("
            "  SELECT id FROM pipeline WHERE telefono = %s ORDER BY fecha_ingreso DESC LIMIT 1"
            ")",
            (chip, telefono, telefono),
        )


def registrar_documento(telefono, tipo, media_id, url_storage):
    with _conexion() as conn:
        conn.execute(
            "INSERT INTO documentos (telefono, tipo, media_id, url_storage) "
            "VALUES (%s, %s, %s, %s)",
            (telefono, tipo, media_id, url_storage),
        )


def update_avaluo(telefono, avaluo, direccion=None, matricula=None):
    """Actualiza el avalúo catastral y opcionalmente la dirección/matrícula en pipeline."""
    with _conexion() as conn:
        conn.execute(
            "UPDATE pipeline SET avaluo_catastral = %s, "
            "matricula_numero = COALESCE(%s, matricula_numero) "
            "WHERE telefono = %s AND id = ("
            "  SELECT id FROM pipeline WHERE telefono = %s ORDER BY fecha_ingreso DESC LIMIT 1"
            ")",
            (avaluo, matricula, telefono, telefono),
        )


# === Remarketing (Hito 8 — Paso 5) ==========================================

def contactos_sin_respuesta(dias: int, tipo_remarketing: str) -> list[dict]:
    """Contactos con resultado 'no_responde' cuya fecha_contacto fue hace N días
    y que aún no recibieron el tipo de remarketing indicado."""
    with _conexion() as conn:
        filas = conn.execute(
            "SELECT telefono, nombre, direccion, precio_publicado FROM contactos "
            "WHERE resultado_contacto = 'no_responde' "
            "AND NOT COALESCE(no_contactar, FALSE) "
            "AND fecha_contacto IS NOT NULL "
            "AND (fecha_contacto AT TIME ZONE 'America/Bogota')::date = "
            "    (CURRENT_TIMESTAMP AT TIME ZONE 'America/Bogota')::date - %s "
            "AND telefono NOT IN ("
            "    SELECT telefono FROM remarketing WHERE tipo = %s"
            ")",
            (dias, tipo_remarketing),
        ).fetchall()
    return [dict(zip(("telefono", "nombre", "direccion", "precio_publicado"), f))
            for f in filas]


def registrar_remarketing_envio(telefono: str, tipo: str, mensaje: str) -> None:
    with _conexion() as conn:
        conn.execute(
            "INSERT INTO remarketing (telefono, tipo, mensaje_enviado, fecha_envio) "
            "VALUES (%s, %s, %s, NOW())",
            (telefono, tipo, mensaje),
        )


def cerrar_contacto(telefono: str) -> None:
    with _conexion() as conn:
        conn.execute(
            "UPDATE contactos SET resultado_contacto = 'cerrado' WHERE telefono = %s",
            (telefono,),
        )


# === Comisión y desembolso (Hito 10) =========================================

def calcular_comision(monto_aprobado: int) -> int | None:
    """Escala: 3.5 % ($15M–$99M), 3 % ($100M–$399M), 2.5 % ($400M+)."""
    if not monto_aprobado or monto_aprobado <= 0:
        return None
    if monto_aprobado < 100_000_000:
        return round(monto_aprobado * 0.035)
    if monto_aprobado < 400_000_000:
        return round(monto_aprobado * 0.030)
    return round(monto_aprobado * 0.025)


def registrar_desembolso(pipeline_id: int, monto: int, fecha=None) -> int | None:
    """Marca el lead como DESEMBOLSADO y guarda la comisión calculada."""
    comision = calcular_comision(monto)
    with _conexion() as conn:
        conn.execute(
            "UPDATE pipeline SET estado = 'DESEMBOLSADO', "
            "fecha_desembolso = COALESCE(%s::timestamptz, NOW()), "
            "comision = %s "
            "WHERE id = %s",
            (fecha, comision, pipeline_id),
        )
    return comision


def marcar_comision_cobrada(pipeline_id: int) -> None:
    with _conexion() as conn:
        conn.execute(
            "UPDATE pipeline SET comision_cobrada = TRUE WHERE id = %s",
            (pipeline_id,),
        )


def marcar_requiere_humano(
    telefono: str, motivo: str, cooldown_minutos: int = 15
) -> dict:
    """Marca `contactos.requiere_humano = true` y decide si disparar alerta.

    Retorna dict con dos claves:
      - `nuevo`: True si `requiere_humano` pasó de FALSE → TRUE en esta llamada.
      - `debe_alertar`: True si es `nuevo` O si pasaron ≥ `cooldown_minutos`
        desde la última alerta (`requiere_humano_at`). Si `debe_alertar` es
        True, el UPDATE refresca `requiere_humano_at = NOW()`.

    Contexto: antes esta función retornaba un bool "es la primera vez". El
    caller (bot._enviar_alerta_lead_caliente) solo alertaba la 1ª vez; si el
    cliente volvía a pedir llamada horas después y nadie lo atendió, no se
    re-disparaba. Ahora re-dispara pasado el cooldown.

    Usa `_conexion_directa` (bypass del pool) por los mismos motivos que
    `guardar_contacto`: ruta de baja frecuencia y resistente al pool roto.
    """
    from datetime import datetime, timedelta, timezone

    with _conexion_directa(timeout=10) as conn:
        cur = conn.execute(
            "SELECT requiere_humano, requiere_humano_at FROM contactos "
            "WHERE telefono = %s FOR UPDATE",
            (telefono,),
        )
        row = cur.fetchone()
        if row is None:
            return {"nuevo": False, "debe_alertar": False}

        req_prev, at_prev = row[0], row[1]
        nuevo = not req_prev
        if nuevo or at_prev is None:
            debe_alertar = True
        else:
            # at_prev viene de Postgres TIMESTAMPTZ con tzinfo; comparamos
            # contra ahora en UTC. Si fuera naive (no debería), lo tratamos
            # como UTC para no romper.
            ahora = datetime.now(timezone.utc)
            if at_prev.tzinfo is None:
                at_prev = at_prev.replace(tzinfo=timezone.utc)
            debe_alertar = (ahora - at_prev) >= timedelta(minutes=cooldown_minutos)

        if debe_alertar:
            conn.execute(
                "UPDATE contactos SET requiere_humano = TRUE, "
                "requiere_humano_motivo = %s, requiere_humano_at = NOW() "
                "WHERE telefono = %s",
                (motivo, telefono),
            )
        else:
            # Mantener flag en TRUE y refrescar motivo, pero NO tocar
            # requiere_humano_at (ventana de cooldown corre desde la última
            # alerta efectiva).
            conn.execute(
                "UPDATE contactos SET requiere_humano = TRUE, "
                "requiere_humano_motivo = %s WHERE telefono = %s",
                (motivo, telefono),
            )

    return {"nuevo": nuevo, "debe_alertar": debe_alertar}


def esta_requiere_humano(telefono: str) -> bool:
    """True si el contacto ya está marcado como `requiere_humano`.

    No lanza: si la DB falla devuelve False (fail-open como `esta_bloqueado`).
    """
    try:
        with _conexion() as conn:
            fila = conn.execute(
                "SELECT requiere_humano FROM contactos WHERE telefono = %s",
                (telefono,),
            ).fetchone()
        return bool(fila and fila[0])
    except Exception as exc:
        log.warning("[esta_requiere_humano] no se pudo consultar (%s): %s", telefono, exc)
        return False


# === Remarketing paz y salvos (Hito 10) =====================================

def leads_paz_salvo_por_contactar(dias: int) -> list[dict]:
    """Leads en 'pausado_paz_salvo' cuya fecha_ingreso fue hace exactamente N días
    y que aún no recibieron el recordatorio correspondiente."""
    tipo = f"paz_salvo_dia_{dias}"
    with _conexion() as conn:
        filas = conn.execute(
            "SELECT id, telefono, nombre FROM pipeline "
            "WHERE estado = 'pausado_paz_salvo' "
            "AND NOT COALESCE((SELECT no_contactar FROM contactos "
            "                  WHERE contactos.telefono = pipeline.telefono LIMIT 1), FALSE) "
            "AND (fecha_ingreso AT TIME ZONE 'America/Bogota')::date = "
            "    (CURRENT_TIMESTAMP AT TIME ZONE 'America/Bogota')::date - %s "
            "AND telefono NOT IN (SELECT telefono FROM remarketing WHERE tipo = %s)",
            (dias, tipo),
        ).fetchall()
    return [dict(zip(("id", "telefono", "nombre"), f)) for f in filas]


# === Panel: acciones humanas + audit (PR acciones_panel) ===================

def aplicar_accion_panel(telefono, updates, accion_key, nota, actor):
    """Aplica una acción del panel: UPDATE en `contactos` + INSERT audit.

    Se corre bajo una sola conn directa a Postgres (bypass del pool, misma
    razón que el resto del panel). `psycopg` ejecuta ambas sentencias en la
    misma transacción implícita del context manager: si la segunda falla,
    la primera también se revierte.

    `updates` es un dict columna → valor; el literal "NOW()" se emite como
    función SQL en vez de parámetro, para no castear timestamps en Python.
    """
    with _conexion_directa(timeout=10) as conn:
        if updates:
            set_fragments = []
            values = []
            for k, v in updates.items():
                if v == "NOW()":
                    set_fragments.append(f"{k} = NOW()")
                else:
                    set_fragments.append(f"{k} = %s")
                    values.append(v)
            values.append(telefono)
            sql = (
                f"UPDATE contactos SET {', '.join(set_fragments)} "
                f"WHERE telefono = %s"
            )
            conn.execute(sql, values)
        conn.execute(
            "INSERT INTO panel_acciones (telefono, accion, nota, actor) "
            "VALUES (%s, %s, %s, %s)",
            (telefono, accion_key, nota or None, actor or None),
        )


def historial_acciones_panel(telefono, limite=20):
    """Últimas N acciones humanas sobre un teléfono (más recientes primero).

    Devuelve una lista de dicts con `id, telefono, accion, nota, actor, fecha`.
    Usa el pool normal: la vista de detalle del panel ya abre su propia conn
    directa — reutilizamos esa conn pasándola por `_rows` desde panel.py.
    Esta función existe para callers externos (tests, scripts); en caliente
    el panel la saltea.
    """
    with _conexion_directa(timeout=10) as conn:
        cur = conn.execute(
            "SELECT id, telefono, accion, nota, actor, fecha "
            "FROM panel_acciones WHERE telefono = %s "
            "ORDER BY fecha DESC LIMIT %s",
            (telefono, limite),
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# === Pausar bot per lead (take-over humano, iter 1 oct-2026) ================
#
# Sentinel para "pausa indefinida": fecha muy en el futuro. Permite usar
# siempre la misma columna TIMESTAMPTZ y el mismo guard (`> NOW()`) tanto
# para pausas por N horas como para pausas indefinidas, sin agregar una
# columna booleana extra.
_PAUSA_INDEFINIDA_SENTINEL = "2999-01-01 00:00:00+00"


def pausar_bot(telefono: str, horas: int | None = None,
               autor: str = "asesor") -> None:
    """Pausa el bot para `telefono` durante `horas` (o indefinido si None).

    Si el contacto no existe, lo inserta (misma lógica que `set_no_contactar`:
    algún día un asesor querrá pausar a alguien que no está en `contactos` —
    p. ej. porque escribió por su cuenta).

    Nota: `autor` se acepta y propaga a `eventos_contacto` cuando ese hook se
    conecta (feature timeline). Se registra sólo si contacto existe y la
    función `registrar_evento` está disponible — por ahora se deja como
    extensión futura (lazy import en el commit del timeline).
    """
    with _conexion_directa(timeout=10) as conn:
        if horas is None:
            conn.execute(
                "INSERT INTO contactos (telefono, bot_pausado_hasta) "
                "VALUES (%s, %s::timestamptz) "
                "ON CONFLICT (telefono) DO UPDATE SET "
                "  bot_pausado_hasta = EXCLUDED.bot_pausado_hasta",
                (telefono, _PAUSA_INDEFINIDA_SENTINEL),
            )
        else:
            conn.execute(
                "INSERT INTO contactos (telefono, bot_pausado_hasta) "
                "VALUES (%s, NOW() + make_interval(hours => %s)) "
                "ON CONFLICT (telefono) DO UPDATE SET "
                "  bot_pausado_hasta = NOW() + make_interval(hours => %s)",
                (telefono, int(horas), int(horas)),
            )
    # Hook de timeline: el commit `feat(panel): timeline unificado` conecta
    # este helper para que las pausas aparezcan en el historial del lead.
    try:
        detalle = {"horas": horas} if horas is not None else {"indefinido": True}
        registrar_evento_por_telefono(
            telefono, "bot_pausado", detalle, autor=autor)
    except NameError:
        pass


def reactivar_bot(telefono: str, autor: str = "asesor") -> None:
    """Reactiva el bot para `telefono`. Si no estaba pausado, es un no-op."""
    with _conexion_directa(timeout=10) as conn:
        conn.execute(
            "UPDATE contactos SET bot_pausado_hasta = NULL "
            "WHERE telefono = %s",
            (telefono,),
        )
    try:
        registrar_evento_por_telefono(telefono, "bot_reactivado", {}, autor=autor)
    except NameError:
        pass


def bot_esta_pausado(telefono: str) -> bool:
    """True si el bot está pausado AHORA para `telefono`.

    No lanza: si la DB falla devuelve False (fail-open, igual que
    `esta_bloqueado` — una pausa fallida no debe romper el flujo del bot).
    """
    try:
        with _conexion() as conn:
            fila = conn.execute(
                "SELECT bot_pausado_hasta > NOW() FROM contactos "
                "WHERE telefono = %s",
                (telefono,),
            ).fetchone()
        return bool(fila and fila[0])
    except Exception as exc:
        log.warning("[bot_esta_pausado] no se pudo consultar (%s): %s",
                    telefono, exc)
        return False


def bot_pausa_info(telefono: str) -> dict:
    """Devuelve info de la pausa de un lead: {pausado: bool, hasta:
    datetime|None, indefinida: bool}. Útil para la UI del detalle.
    """
    try:
        with _conexion_directa(timeout=5) as conn:
            fila = conn.execute(
                "SELECT bot_pausado_hasta, bot_pausado_hasta > NOW() "
                "FROM contactos WHERE telefono = %s",
                (telefono,),
            ).fetchone()
    except Exception as exc:
        log.warning("[bot_pausa_info] fallo (%s): %s", telefono, exc)
        return {"pausado": False, "hasta": None, "indefinida": False}
    if not fila:
        return {"pausado": False, "hasta": None, "indefinida": False}
    hasta, pausado = fila[0], bool(fila[1])
    indefinida = False
    if hasta is not None:
        try:
            # Si está después del año 2900 es nuestro sentinel.
            indefinida = hasta.year >= 2900
        except Exception:
            indefinida = False
    return {"pausado": pausado, "hasta": hasta, "indefinida": indefinida}


# === Notas internas sobre el lead (iter 1 oct-2026) =========================

def agregar_nota(contacto_id: int, texto: str,
                 autor: str = "asesor") -> int:
    """Inserta una nota interna sobre el contacto. Devuelve el id de la nota.

    `autor` se guarda tal cual venga (sin fallback vacío) y también en el
    hook de timeline, que se conecta en el commit de "timeline unificado".
    """
    autor_final = autor or "asesor"
    with _conexion_directa(timeout=10) as conn:
        fila = conn.execute(
            "INSERT INTO notas_contacto (contacto_id, texto, autor) "
            "VALUES (%s, %s, %s) RETURNING id",
            (contacto_id, texto, autor_final),
        ).fetchone()
        nota_id = fila[0] if fila else None
    # Hook de timeline (opcional; se activa cuando `registrar_evento`
    # existe en este módulo).
    try:
        detalle = {"texto_corto": (texto or "")[:80]}
        registrar_evento(contacto_id, "nota_agregada", detalle, autor=autor_final)
    except NameError:
        pass
    return nota_id


def listar_notas(contacto_id: int) -> list[dict]:
    """Lista las notas internas de un contacto, más recientes primero."""
    with _conexion_directa(timeout=10) as conn:
        cur = conn.execute(
            "SELECT id, contacto_id, texto, autor, created_at "
            "FROM notas_contacto WHERE contacto_id = %s "
            "ORDER BY created_at DESC",
            (contacto_id,),
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def borrar_nota(nota_id: int) -> None:
    """Borra una nota por id. No lanza si no existe."""
    with _conexion_directa(timeout=10) as conn:
        conn.execute("DELETE FROM notas_contacto WHERE id = %s", (nota_id,))


# === Marcar leído / no leído (iter 1 oct-2026) ==============================
#
# `contactos.visto_at` guarda la última vez que un asesor abrió el detalle
# del lead. Un lead está "no leído" cuando su visto_at es NULL (nunca lo
# miramos) o es anterior a la última actividad del lead. La "última
# actividad" la miramos contra el GREATEST de:
#   - sesiones.ultima_actividad (sesión del bot)
#   - MAX(fecha) sobre mensajes del teléfono con direccion='in'
# Esto evita que una ida del asesor "lea" un lead que luego recibió más
# mensajes del cliente — hay que volverlo a abrir para que vuelva a leído.

def marcar_visto(contacto_id: int) -> None:
    """Marca el contacto como visto AHORA (leído). Idempotente."""
    try:
        with _conexion_directa(timeout=5) as conn:
            conn.execute(
                "UPDATE contactos SET visto_at = NOW() WHERE id = %s",
                (contacto_id,),
            )
    except Exception as exc:
        log.warning("[marcar_visto] fallo (%s): %s", contacto_id, exc)


def marcar_no_visto(contacto_id: int) -> None:
    """Marca el contacto como no visto (visto_at = NULL)."""
    with _conexion_directa(timeout=5) as conn:
        conn.execute(
            "UPDATE contactos SET visto_at = NULL WHERE id = %s",
            (contacto_id,),
        )


def esta_no_leido(contacto_id: int) -> bool:
    """True si el contacto está "no leído": visto_at es NULL o es anterior
    a la última actividad (sesión o último IN).
    """
    try:
        with _conexion_directa(timeout=5) as conn:
            fila = conn.execute(
                """
                SELECT
                  c.visto_at,
                  GREATEST(
                    COALESCE(s.ultima_actividad, 'epoch'::timestamptz),
                    COALESCE((SELECT MAX(fecha) FROM mensajes
                              WHERE telefono = c.telefono AND direccion = 'in'),
                             'epoch'::timestamptz)
                  ) AS ultima
                FROM contactos c
                LEFT JOIN sesiones s ON s.telefono = c.telefono
                WHERE c.id = %s
                """,
                (contacto_id,),
            ).fetchone()
    except Exception as exc:
        log.warning("[esta_no_leido] fallo (%s): %s", contacto_id, exc)
        return False
    if not fila:
        return False
    visto, ultima = fila[0], fila[1]
    if not ultima:
        return False
    return (visto is None) or (visto < ultima)
