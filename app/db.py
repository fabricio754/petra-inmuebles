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
                url = DATABASE_URL
                if url and "connect_timeout" not in url:
                    sep = "&" if "?" in url else "?"
                    url = f"{url}{sep}connect_timeout=10"
                # sslmode=disable: Render's internal network is private; SSL is optional.
                # libpq's SSL handshake can hang indefinitely in non-main threads because
                # OpenSSL ignores connect_timeout. Disabling SSL avoids the hang entirely.
                # Always force sslmode=disable — replace any existing value in the URL.
                if url:
                    import re as _re
                    if "sslmode" in url:
                        url = _re.sub(r"sslmode=[^&\s]*", "sslmode=disable", url)
                    else:
                        sep = "&" if "?" in url else "?"
                        url = f"{url}{sep}sslmode=disable"
                # min_size=0: no pre-created connections (avoids blocking on init).
                # timeout=20: pool.connection() raises PoolTimeout if DB unreachable,
                #             so the gunicorn 120s limit is never hit silently.
                # open=False then pool.open(): socket.setdefaulttimeout(20) is already
                # active when open() runs, so the SSL handshake times out in threads.
                pool = ConnectionPool(url, min_size=0, max_size=2, open=False, timeout=20.0)
                pool.open()
                _preparar(pool)
                _pool = pool
    return _pool.connection(timeout=20.0)


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
