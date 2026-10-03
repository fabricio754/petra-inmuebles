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
