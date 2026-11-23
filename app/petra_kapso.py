"""Cliente para enviar mensajes de WhatsApp vía Kapso (BSP).

Kapso replica el formato de la Graph API de Meta, así que el payload
es exactamente igual a lo que ya hace petra_wa.send_template, solo
cambia la URL base y la autenticación (X-API-Key en vez de Bearer token).

Config por env vars:
  KAPSO_API_KEY           = PAT de Kapso (empieza con kps_... o similar)
  KAPSO_BASE_URL          = https://app.kapso.ai/api/meta/
  KAPSO_PHONE_NUMBER_ID   = ID del número conectado en Kapso
  KAPSO_WABA_ID           = WABA ID del número (opcional, referencia)
  KAPSO_GRAPH_VERSION     = v24.0 por default
"""
import json
import logging
import os

import requests

_LOG = logging.getLogger(__name__)

_DEFAULT_GRAPH_VERSION = "v24.0"


def _config():
    base = (os.environ.get("KAPSO_BASE_URL") or "").rstrip("/")
    api_key = (os.environ.get("KAPSO_API_KEY") or "").strip()
    phone_id = (os.environ.get("KAPSO_PHONE_NUMBER_ID") or "").strip()
    version = (os.environ.get("KAPSO_GRAPH_VERSION") or _DEFAULT_GRAPH_VERSION).strip()
    return base, api_key, phone_id, version


def _is_ready():
    base, api_key, phone_id, _ = _config()
    return bool(base and api_key and phone_id)


def _url(phone_id, version):
    base, _, _, _ = _config()
    return f"{base}/whatsapp/{version}/{phone_id}/messages"


def send_template(to, template_name, language="es_ES", components=None,
                  phone_number_id_override=None):
    """Envía un template aprobado vía Kapso.

    Args:
      to: teléfono E.164 destino, con o sin '+' (Kapso acepta ambos).
      template_name: nombre interno del template (ej. 'petra_test_hola').
      language: código de idioma del template (ej. 'es_ES', 'es_CO', 'en_US').
      components: lista opcional con header/body/buttons parameters.
      phone_number_id_override: por si querés usar otro número del mismo
        WABA; si no, usa KAPSO_PHONE_NUMBER_ID.

    Returns:
      dict con 'ok' (bool), 'status' (int HTTP), 'body' (dict o str),
      'message_id' (str o None si no aplicable).
    """
    base, api_key, phone_id, version = _config()
    if not (base and api_key):
        _LOG.error("[kapso] falta KAPSO_API_KEY o KAPSO_BASE_URL")
        return {"ok": False, "status": 0, "body": "config_faltante"}
    phone_id = phone_number_id_override or phone_id
    if not phone_id:
        _LOG.error("[kapso] falta KAPSO_PHONE_NUMBER_ID")
        return {"ok": False, "status": 0, "body": "phone_id_faltante"}

    to_clean = str(to).strip().lstrip("+")
    payload = {
        "messaging_product": "whatsapp",
        "to": to_clean,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
        },
    }
    if components:
        payload["template"]["components"] = components

    url = _url(phone_id, version)
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": api_key,
    }
    try:
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=(5, 15))
    except requests.RequestException as e:
        _LOG.exception("[kapso] POST fallo: %s", e)
        return {"ok": False, "status": 0, "body": f"request_error:{type(e).__name__}"}

    ok = 200 <= r.status_code < 300
    try:
        body = r.json()
    except Exception:
        body = r.text[:500]
    message_id = None
    if ok and isinstance(body, dict):
        msgs = body.get("messages") or []
        if msgs and isinstance(msgs[0], dict):
            message_id = msgs[0].get("id")

    _LOG.info("[kapso] send_template to=%s tpl=%s status=%s msg_id=%s",
              to_clean, template_name, r.status_code, message_id)
    if not ok:
        _LOG.error("[kapso] send_template fallo: %s", body)

    return {
        "ok": ok,
        "status": r.status_code,
        "body": body,
        "message_id": message_id,
    }
