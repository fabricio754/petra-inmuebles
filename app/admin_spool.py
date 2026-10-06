"""Blueprint para administrar el spool en disco de webhooks.

Cuando `webhook_worker._drenar_spool()` se atasca (p.ej. hay cientos de
archivos pendientes porque el pool DB estuvo caído un rato), se puede
limpiar el spool manualmente desde este endpoint. Protegido con el mismo
X-Admin-Token que usa el resto de endpoints `/admin/*`.
"""
import glob
import logging
import os

from flask import Blueprint, jsonify, request

from app import webhook_worker

log = logging.getLogger("petra")
admin_spool = Blueprint("admin_spool", __name__)
ADMIN_TOKEN = os.environ.get("ADMIN_RESET_TOKEN", "")


@admin_spool.post("/admin/purge-spool")
def purge_spool():
    """Borra TODOS los archivos del spool de webhooks en disco.

    Útil cuando se acumuló basura y el drenaje al arrancar se vuelve lento,
    o cuando el contenido del spool ya se procesó por otro canal y sobra.
    Los archivos borrados NO se pueden recuperar.
    """
    token = request.headers.get("X-Admin-Token", "")
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        return jsonify({"error": "no_autorizado"}), 401

    d = webhook_worker._spool_dir()
    if not d:
        return jsonify({"ok": True, "borrados": 0, "nota": "sin spool dir"}), 200

    borrados = 0
    errores = 0
    for ruta in glob.glob(os.path.join(d, "*.json")) + glob.glob(os.path.join(d, "*.json.tmp")):
        try:
            os.remove(ruta)
            borrados += 1
        except Exception:
            log.exception("[AdminSpool] No se pudo borrar %s", ruta)
            errores += 1

    log.warning("[AdminSpool] Spool purgado: %d archivo(s) borrados, %d error(es).",
                borrados, errores)
    return jsonify({"ok": True, "borrados": borrados, "errores": errores, "dir": d}), 200
