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
