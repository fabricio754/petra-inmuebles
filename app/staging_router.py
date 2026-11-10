"""Router staging: cuando un remitente está en la whitelist de pruebas,
el webhook de prod se reenvía a staging y NO se procesa en prod.

Permite probar código nuevo con un número real (el tuyo, el de Sebas)
sin afectar al resto de propietarios reales que siguen hablando con
prod normal.

Config (env vars en PROD):
  PETRA_STAGING_URL         = https://petra-staging.onrender.com/webhook
  (si no está seteada, el router se apaga y todo va a prod normal)

La whitelist se guarda en la tabla petra_staging_whitelist y se edita
desde /panel/petra/staging-whitelist (bloque B). Cache en memoria 30s.

En staging (el que recibe el forward), el flag MODO_STAGING sigue
silenciando sends generales — pero los teléfonos en la whitelist
reciben respuesta de staging (ver bloque override en petra_wa.py).
"""
import logging
import os
import threading
import time
from functools import lru_cache

import requests

from app import db as _db

log = logging.getLogger(__name__)

_WHITELIST_CACHE = {"data": set(), "ts": 0.0, "ttl": 30.0}


def _cargar_whitelist():
    """Lee petra_staging_whitelist y devuelve set de teléfonos."""
    try:
        with _db._conexion_directa(timeout=2) as conn:
            rows = conn.execute("SELECT telefono FROM petra_staging_whitelist").fetchall()
        return {r[0] for r in rows if r[0]}
    except Exception:
        log.debug("[staging-router] fallo leyendo whitelist", exc_info=True)
        return set()


def whitelist_cache_bust():
    """Invalida el cache. Llamar desde el panel al editar."""
    _WHITELIST_CACHE["ts"] = 0.0


def esta_en_whitelist(telefono):
    """Devuelve True si el teléfono está en la whitelist (con cache 30s).
    Fire-and-forget: si la DB falla, devuelve False (no rompe el flujo)."""
    now = time.time()
    cache = _WHITELIST_CACHE
    if now - cache["ts"] > cache["ttl"]:
        cache["data"] = _cargar_whitelist()
        cache["ts"] = now
    return telefono in cache["data"]


def redirigir_a_staging(telefono, payload):
    """Si corresponde reenviar a staging, lo hace fire-and-forget y
    devuelve True. Si no, False (prod sigue procesando normal).

    Condiciones para reenviar:
      1. Env var PETRA_STAGING_URL está configurada
      2. El teléfono está en la whitelist (cacheada)

    Nunca lanza excepción: si el POST falla, el caller devuelve False
    y prod procesa normal (fallback seguro).
    """
    staging_url = (os.environ.get("PETRA_STAGING_URL") or "").strip()
    if not staging_url:
        return False
    if not esta_en_whitelist(telefono):
        return False
    # Fire-and-forget en thread separado para no bloquear la respuesta
    # 200 a Meta.
    def _enviar():
        try:
            requests.post(staging_url, json=payload, timeout=(2, 5))
        except Exception:
            log.exception("[staging-router] POST a staging fallo")
    threading.Thread(target=_enviar, daemon=True).start()
    return True
