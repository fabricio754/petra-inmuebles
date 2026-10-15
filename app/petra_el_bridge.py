"""Puente para que ElevenLabs siga manejando voz cuando migremos texto fuera.

Dos piezas:
  1) /petra/el/init — endpoint que EL llama al empezar una call. Le devolvemos
     el contexto que necesita (dynamic_variables + first_message contextual).
  2) forward_call_to_el — función que reenvía payloads tipo 'calls' de Meta
     a ElevenLabs cuando nuestro webhook es el receptor principal.

Se activa junto con USE_OWN_ORCHESTRATOR=true: una vez nosotros somos el
webhook de Meta, EL ya no recibe las calls directo, se las pasamos nosotros.
"""
import json
import logging
import os

from flask import Blueprint, jsonify, request

log = logging.getLogger("petra")

petra_el_bp = Blueprint("petra_el_bridge", __name__, url_prefix="/petra/el")


def _check_auth():
    expected = (os.environ.get("PETRA_LLM_TOKEN") or "").strip()
    if not expected:
        return True
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer ") and header[7:].strip() == expected:
        return True
    q = (request.args.get("token") or "").strip()
    return q == expected


@petra_el_bp.post("/init")
def init_conversacion_voz():
    """ElevenLabs llama aquí antes de atender una call. Le devolvemos:
      - dynamic_variables: resumen del contacto + estado agente + mandatos
      - conversation_config_override.agent.first_message: saludo contextual

    Protegido por PETRA_LLM_TOKEN. EL debe estar configurado para pasar el
    header Authorization: Bearer <PETRA_LLM_TOKEN>.
    """
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}

    # EL manda caller_id en varios formatos — probamos los comunes.
    tel = (body.get("caller_id") or body.get("user_id")
           or body.get("phone_number") or "").strip()
    tel_norm = "".join(c for c in tel if c.isdigit())
    if not tel_norm:
        return jsonify({
            "type": "conversation_initiation_client_data",
            "dynamic_variables": {},
        }), 200

    dynamic_vars = {"telefono": tel_norm}
    resumen = []
    first_message = None

    try:
        from app import db as _db
        with _db._conexion_directa(timeout=5) as conn:
            # Contacto Petra y rol más reciente
            row = conn.execute(
                "SELECT c.id, c.nombre, r.rol, r.metadata "
                "FROM petra_contactos c "
                "LEFT JOIN petra_contacto_roles r ON r.contacto_id=c.id "
                "WHERE c.telefono=%s "
                "ORDER BY r.updated_at DESC NULLS LAST LIMIT 1",
                (tel_norm,),
            ).fetchone()
            if row:
                cid, nombre, rol, meta = row
                if nombre:
                    dynamic_vars["contacto_nombre"] = nombre
                if rol:
                    dynamic_vars["contacto_rol"] = rol
                if rol == "agente_petra" and meta:
                    dynamic_vars["estado_agente"] = meta.get("estado_agente", "")
                    resumen.append(
                        f"Agente Petra en estado {meta.get('estado_agente')}, "
                        f"{meta.get('videos_vistos', 0)} videos vistos.")
                # Últimos 3 mensajes del chat
                hist = conn.execute(
                    "SELECT texto_usuario, texto_asistente FROM petra_llm_logs "
                    "WHERE telefono=%s AND texto_usuario IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT 3",
                    (tel_norm,),
                ).fetchall()
                if hist:
                    turnos = []
                    for u, a in reversed(hist):
                        if u:
                            turnos.append(f"User: {u[:120]}")
                        if a:
                            turnos.append(f"Petra: {a[:120]}")
                    dynamic_vars["chat_reciente"] = " || ".join(turnos)[:1500]
                # Lead crédito si existe
                row_lead = conn.execute(
                    "SELECT nombre, ciudad, tipo_inmueble, avaluo "
                    "FROM pilot_leads WHERE telefono=%s", (tel_norm,),
                ).fetchone()
                if row_lead and any(row_lead):
                    dynamic_vars["lead_credito"] = ", ".join(
                        str(v) for v in row_lead if v)
                # Mandatos activos
                mandatos = conn.execute(
                    "SELECT tipo, estado FROM petra_mandatos "
                    "WHERE contacto_id=%s AND estado IN ('propuesto','firmado','activo') "
                    "ORDER BY updated_at DESC LIMIT 3",
                    (cid,),
                ).fetchall()
                if mandatos:
                    dynamic_vars["mandatos"] = "; ".join(
                        f"{m[0]}:{m[1]}" for m in mandatos)
                    resumen.append(f"Tiene {len(mandatos)} mandatos activos.")

                nombre_corto = (nombre or "").split()[0] if nombre else ""
                if nombre_corto:
                    first_message = (
                        f"Hola {nombre_corto}, soy Petra. "
                        f"Te retomo donde nos quedamos.")
    except Exception:
        log.exception("[el_bridge] init fallo tel=%s", tel_norm)

    out = {
        "type": "conversation_initiation_client_data",
        "dynamic_variables": dynamic_vars,
    }
    if first_message:
        out["conversation_config_override"] = {
            "agent": {"first_message": first_message},
        }
    log.info("[el_bridge] init tel=%s vars=%d resumen=%s",
             tel_norm, len(dynamic_vars), " | ".join(resumen))
    return jsonify(out), 200


def forward_call_to_el(payload):
    """Reenvía un webhook de Meta tipo 'calls' a ElevenLabs.

    Hoy EL recibe el webhook directo de Meta y nosotros somos pasivos.
    Cuando flipemos a USE_OWN_ORCHESTRATOR=true, nosotros somos el webhook y
    debemos reenviar las calls a EL.

    Fire-and-forget: no bloquea la respuesta a Meta.

    Nota técnica: cuando llegue el momento real, EL expone un webhook
    público de ingreso en platform_settings. Ese URL va en ELEVENLABS_WEBHOOK_INGRESS_URL.
    Si no está seteado, logeamos y seguimos — EL puede seguir recibiendo las
    calls directo de Meta por un tiempo (dual subscription) sin problema.
    """
    url = (os.environ.get("ELEVENLABS_WEBHOOK_INGRESS_URL") or "").strip()
    if not url:
        log.info("[el_bridge] forward_call: sin ELEVENLABS_WEBHOOK_INGRESS_URL, skip")
        return
    try:
        import requests
        import threading

        def _run():
            try:
                requests.post(url, json=payload, timeout=(3, 5))
            except Exception:
                log.exception("[el_bridge] forward_call fallo")
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        log.exception("[el_bridge] forward_call thread fallo")
