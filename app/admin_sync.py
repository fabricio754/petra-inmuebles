"""Blueprint para recuperar los 7 mensajes IN perdidos a las 13:01 UTC del
6-oct-2026 por saturación del pool DB durante el pilot.

Inserta en `mensajes` y marca opt-out en `contactos` para quienes deben
dejar de recibir mensajes. Idempotente: dedupa por telefono+fecha.
Auth: header X-Admin-Token con ADMIN_RESET_TOKEN.
"""
from flask import Blueprint, jsonify, request
import logging
import os

from app import db, state

log = logging.getLogger("petra")
admin_sync = Blueprint("admin_sync", __name__)
ADMIN_TOKEN = os.environ.get("ADMIN_RESET_TOKEN", "")


@admin_sync.post("/admin/recovery-sync")
def recovery_sync():
    token = request.headers.get("X-Admin-Token", "")
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        return jsonify({"error": "no_autorizado"}), 401

    mensajes_perdidos = [
        # (telefono, tipo, resumen, fecha_iso)
        ("573001994555", "text", "Gracias por comunicarte con Inmobiliaria Sala. ¿Cómo podemos ayudarte?", "2026-10-06 12:54:30.263153+00"),
        ("573001994555", "text", "Gracias por tu mensaje. En este momento no podemos responder, pero lo haremos lo antes posible.", "2026-10-06 12:54:30.788328+00"),
        ("573102067744", "text", "Bns días", "2026-10-06 12:57:42.441409+00"),
        ("573102067744", "text", "En q le puedo ayudar?", "2026-10-06 12:57:51.721591+00"),
        ("573132099381", "text", "Gracias por comunicarse con AGENCIA DE CAMBIOS COLOMBIA.", "2026-10-06 13:01:14.048533+00"),
        ("573114190747", "template_button", "No me interesa", "2026-10-06 13:01:34.571706+00"),
        ("573133071580", "template_button", "No me interesa", "2026-10-06 13:01:41.613836+00"),
    ]
    opt_outs = [
        "573001994555",   # Bot Inmobiliaria Sala
        "573132099381",   # Bot AGENCIA DE CAMBIOS COLOMBIA
        "573114190747",   # Dijo No me interesa
        "573133071580",   # Dijo No me interesa
    ]

    insertados = 0
    saltados = 0
    with db._conexion() as conn:
        for tel, tipo, resumen, fecha in mensajes_perdidos:
            dup = conn.execute(
                "SELECT 1 FROM mensajes WHERE telefono=%s AND fecha=%s::timestamptz",
                (tel, fecha),
            ).fetchone()
            if dup:
                saltados += 1
                continue
            conn.execute(
                "INSERT INTO mensajes (telefono, direccion, tipo, resumen, fecha) "
                "VALUES (%s, 'in', %s, %s, %s::timestamptz)",
                (tel, tipo, resumen[:4000], fecha),
            )
            insertados += 1

    for tel in opt_outs:
        state.set_no_contactar(tel, True)

    log.info("[Admin] recovery-sync: %d insertados, %d saltados, %d opt-outs",
             insertados, saltados, len(opt_outs))
    return jsonify({
        "insertados": insertados,
        "saltados_duplicado": saltados,
        "opt_outs": opt_outs,
    })
