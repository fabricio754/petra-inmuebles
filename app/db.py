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
            "(telefono, nombre, cedula, email, direccion_inmueble, ciudad, "
            "requiere_paz_salvo, autorizacion_datos_en, estado) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                data.get("telefono"),
                data.get("nombre"),
                data.get("cedula"),
                data.get("email"),
                data.get("direccion_inmueble"),
                data.get("ciudad"),
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


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline — funciones nuevas para el flujo de crédito completo
# ─────────────────────────────────────────────────────────────────────────────

def update_chip(telefono: str, chip: str):
    """Actualiza el CHIP catastral en la tabla pipeline."""
    with _conexion() as conn:
        conn.execute(
            "UPDATE pipeline SET chip = %s WHERE telefono = %s",
            (chip, telefono),
        )


def registrar_documento(doc: dict):
    """Inserta un documento en la tabla documentos."""
    with _conexion() as conn:
        conn.execute(
            """
            INSERT INTO documentos (telefono, tipo, media_id, url_storage, obtenido_automaticamente)
            VALUES (%(telefono)s, %(tipo)s, %(media_id)s, %(url_storage)s,
                    %(obtenido_automaticamente)s)
            ON CONFLICT DO NOTHING
            """,
            doc,
        )


def update_avaluo(telefono: str, avaluo: int):
    """Actualiza el avalúo catastral en pipeline."""
    with _conexion() as conn:
        conn.execute(
            "UPDATE pipeline SET avaluo_catastral = %s WHERE telefono = %s",
            (avaluo, telefono),
        )


def leads_nuevos() -> list[dict]:
    """Leads en estado NUEVO listos para enviar a Sureti (tienen docs mínimos)."""
    with _conexion() as conn:
        rows = conn.execute(
            "SELECT * FROM pipeline WHERE estado = 'nuevo' ORDER BY fecha_ingreso"
        ).fetchall()
        return [dict(r) for r in rows]


def leads_en_seguimiento() -> list[dict]:
    """Leads activos en Sureti: registrado, en_estudio o aprobado (esperando desembolso)."""
    with _conexion() as conn:
        rows = conn.execute(
            "SELECT * FROM pipeline WHERE estado IN ('registrado', 'en_estudio', 'aprobado')"
        ).fetchall()
        return [dict(r) for r in rows]


def marcar_registrado_sureti(telefono: str, sureti_lead_id: str):
    with _conexion() as conn:
        conn.execute(
            "UPDATE pipeline SET sureti_lead_id = %s, estado = 'registrado' WHERE telefono = %s",
            (sureti_lead_id, telefono),
        )


def actualizar_estado_lead(telefono: str, estado: str, resultado: dict):
    with _conexion() as conn:
        conn.execute(
            """UPDATE pipeline SET estado = %s,
               monto_aprobado = %(monto)s,
               razon_no_aprobado = %(razon)s,
               fecha_aprobacion = CASE WHEN %s = 'aprobado' THEN NOW() ELSE fecha_aprobacion END
               WHERE telefono = %s""",
            (estado, resultado.get("monto_aprobado"), resultado.get("razon"), estado, telefono),
        )


def documentos_de(telefono: str) -> list[dict]:
    """Retorna todos los documentos de un contacto."""
    with _conexion() as conn:
        rows = conn.execute(
            "SELECT * FROM documentos WHERE telefono = %s",
            (telefono,),
        ).fetchall()
        return [dict(r) for r in rows]


def contactos_sin_respuesta(dias: int, tipo_remarketing: str) -> list[dict]:
    """
    Contactos que no han respondido después de N días del primer contacto,
    y a los que aún no se les envió el tipo de remarketing indicado.
    """
    with _conexion() as conn:
        rows = conn.execute(
            """
            SELECT c.telefono, c.nombre, c.direccion, c.precio_publicado
            FROM contactos c
            WHERE c.contactado = TRUE
              AND c.resultado_contacto IN ('plantilla_enviada', 'no_responde')
              AND c.fecha_contacto <= NOW() - INTERVAL '%(dias)s days'
              AND c.no_contactar = FALSE
              AND NOT EXISTS (
                SELECT 1 FROM remarketing r
                WHERE r.telefono = c.telefono AND r.tipo = %(tipo)s
              )
            AT TIME ZONE 'America/Bogota'
            ORDER BY c.fecha_contacto
            LIMIT 100
            """,
            {"dias": dias, "tipo": tipo_remarketing},
        ).fetchall()
        return [dict(r) for r in rows]


def contactos_paz_salvo_pendiente(dias: int, tipo_remarketing: str) -> list[dict]:
    """
    Leads que respondieron 'Sí' a ponerse al día con paz y salvos,
    pero no han avanzado después de N días.
    """
    with _conexion() as conn:
        rows = conn.execute(
            """
            SELECT p.telefono, p.nombre, p.direccion_inmueble AS direccion,
                   c.precio_publicado
            FROM pipeline p
            LEFT JOIN contactos c ON c.telefono = p.telefono
            WHERE p.requiere_paz_salvo = TRUE
              AND p.estado NOT IN ('enviado_sureti', 'registrado', 'en_estudio',
                                   'aprobado', 'cerrado')
              AND p.fecha_ingreso <= NOW() - INTERVAL '%(dias)s days'
              AND NOT EXISTS (
                SELECT 1 FROM remarketing r
                WHERE r.telefono = p.telefono AND r.tipo = %(tipo)s
              )
            ORDER BY p.fecha_ingreso
            LIMIT 50
            """,
            {"dias": dias, "tipo": tipo_remarketing},
        ).fetchall()
        return [dict(r) for r in rows]


def registrar_remarketing_envio(telefono: str, tipo: str, mensaje: str):
    with _conexion() as conn:
        conn.execute(
            "INSERT INTO remarketing (telefono, tipo, mensaje_enviado, fecha_envio) "
            "VALUES (%s, %s, %s, NOW())",
            (telefono, tipo, mensaje),
        )


def calcular_comision(monto_aprobado: int) -> int:
    """
    Comisión de Massi sobre el monto desembolsado (en COP):
      3.5% para $15M–$99M
      3.0% para $100M–$399M
      2.5% para $400M+
    Devuelve la comisión en COP (entero).
    """
    if monto_aprobado <= 0:
        return 0
    m = monto_aprobado / 1_000_000
    if m < 100:
        tasa = 0.035
    elif m < 400:
        tasa = 0.030
    else:
        tasa = 0.025
    return round(monto_aprobado * tasa)


def registrar_desembolso(telefono: str, monto_aprobado: int, comision: int):
    """Marca el lead como desembolsado y registra comisión y fecha."""
    with _conexion() as conn:
        conn.execute(
            """UPDATE pipeline
               SET estado = 'desembolsado',
                   fecha_desembolso = NOW(),
                   monto_aprobado = COALESCE(%s, monto_aprobado),
                   comision = %s,
                   comision_cobrada = FALSE
               WHERE telefono = %s""",
            (monto_aprobado or None, comision, telefono),
        )


def marcar_comision_cobrada(telefono: str):
    with _conexion() as conn:
        conn.execute(
            "UPDATE pipeline SET comision_cobrada = TRUE WHERE telefono = %s",
            (telefono,),
        )


def cerrar_contacto(telefono: str, razon: str = "cerrado"):
    with _conexion() as conn:
        conn.execute(
            "UPDATE contactos SET resultado_contacto = %s WHERE telefono = %s",
            (razon, telefono),
        )
        conn.execute(
            "UPDATE pipeline SET estado = 'cerrado' WHERE telefono = %s",
            (telefono,),
        )
