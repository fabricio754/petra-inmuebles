"""Almacenamiento en PostgreSQL (se activa con la variable DATABASE_URL).

state.py llama a estas funciones; bot.py no sabe que existen. Al
arrancar se crean las tablas (schema.sql) y, la primera vez, se copian
los datos que había en la Google Sheet (o el inventario base si no hay
Sheet)."""
import json
import logging
import os
import threading

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

log = logging.getLogger("petra")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

_pool = None
_pool_lock = threading.Lock()


def _conexion():
    """Pool de conexiones, creado (y la base preparada) en el primer uso."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=4, open=True)
                _preparar(pool)
                _pool = pool
    return _pool.connection()


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
    """Copia lo que había en la Sheet (si está configurada); si no, el
    inventario base de data/inventario.seed.json."""
    from app import state  # import tardío: state importa este módulo

    inventario, sesiones, leads = [], {}, []
    if state.SHEET_ID:
        try:
            inventario, sesiones, leads = state.leer_todo_de_sheets()
            log.info(
                "[Postgres] Importando de la Sheet: %d inmuebles, %d sesiones, %d contactos.",
                len(inventario), len(sesiones), len(leads),
            )
        except Exception:
            log.exception("[Postgres] No se pudo leer la Sheet; se usa el inventario base.")
            inventario, sesiones, leads = [], {}, []
    if not inventario:
        inventario = state.inventario_seed()

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
            "requiere_paz_salvo, autorizacion_datos_en, estado) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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


# === Captación (Hito 7) ======================================================

def guardar_contacto(c):
    """Inserta un anuncio capturado. Devuelve True si es nuevo, False si el
    teléfono ya existía (no se toca: ni se recontacta ni se pisa no_contactar)."""
    with _conexion() as conn:
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
    """Leads ya registrados en Sureti con estado REGISTRADO o EN_ESTUDIO."""
    with _conexion() as conn:
        filas = conn.execute(
            "SELECT id, telefono, nombre, sureti_lead_id, estado "
            "FROM pipeline WHERE estado IN ('REGISTRADO', 'EN_ESTUDIO') "
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
