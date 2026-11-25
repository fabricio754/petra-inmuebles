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
# URL base real del proxy de Kapso para la Graph API de Meta. El env var
# KAPSO_BASE_URL puede sobreescribirla, pero el default es el correcto.
_DEFAULT_BASE_URL = "https://api.kapso.ai/meta/whatsapp"


def _config():
    base = (os.environ.get("KAPSO_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")
    # Protección: si la env var tiene el valor viejo que apuntaba al host
    # equivocado (app.kapso.ai), lo reemplazamos por el correcto en runtime.
    if "app.kapso.ai" in base or "/api/meta" in base:
        base = _DEFAULT_BASE_URL
    api_key = (os.environ.get("KAPSO_API_KEY") or "").strip()
    # Prioridad: DB (petra_kapso_numeros activo) > env var KAPSO_PHONE_NUMBER_ID.
    phone_id = _phone_id_activo() or (os.environ.get("KAPSO_PHONE_NUMBER_ID") or "").strip()
    version = (os.environ.get("KAPSO_GRAPH_VERSION") or _DEFAULT_GRAPH_VERSION).strip()
    return base, api_key, phone_id, version


def _phone_id_activo():
    """Devuelve el phone_id marcado como activo en DB, o '' si no hay."""
    try:
        from app import db as _db
        with _db._conexion_directa() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT phone_id FROM petra_kapso_numeros WHERE activo LIMIT 1"
            )
            row = cur.fetchone()
            if row and row[0]:
                return str(row[0]).strip()
    except Exception as e:
        _LOG.debug("[kapso] no pude leer phone_id activo: %s", e)
    return ""


def _waba_id_activo():
    try:
        from app import db as _db
        with _db._conexion_directa() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT waba_id FROM petra_kapso_numeros WHERE activo LIMIT 1"
            )
            row = cur.fetchone()
            if row and row[0]:
                return str(row[0]).strip()
    except Exception:
        pass
    return ""


def _is_ready():
    base, api_key, phone_id, _ = _config()
    return bool(base and api_key and phone_id)


def _url(phone_id, version):
    base, _, _, _ = _config()
    # Kapso proxea la Graph API de Meta: {base}/{version}/{phone_id}/messages
    # Ejemplo: https://app.kapso.ai/api/meta/v24.0/1386371191233822/messages
    return f"{base}/{version}/{phone_id}/messages"


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
        # Fallback a Meta Cloud API directo si Kapso dice que no tiene
        # configurada la WABA (p.ej. provision incompleta en Kapso).
        if _es_kapso_no_configurado(r.status_code, body):
            fb = _send_template_via_meta(to_clean, template_name, language,
                                          components, phone_id)
            if fb.get("ok"):
                _LOG.info("[meta-fallback] send_template OK to=%s tpl=%s msg_id=%s",
                          to_clean, template_name, fb.get("message_id"))
            return fb

    return {
        "ok": ok,
        "status": r.status_code,
        "body": body,
        "message_id": message_id,
    }


def _es_kapso_no_configurado(status, body):
    if status != 404:
        return False
    try:
        if isinstance(body, dict):
            err = (body.get("error") or "")
            if isinstance(err, str) and "configuration not found" in err.lower():
                return True
        if isinstance(body, str) and "configuration not found" in body.lower():
            return True
    except Exception:
        pass
    return False


def _send_template_via_meta(to_clean, template_name, language, components,
                             phone_id):
    """Fallback directo a Meta Cloud API cuando Kapso no tiene configurada
    la WABA. Usa META_ACCESS_TOKEN."""
    access_token = (os.environ.get("META_ACCESS_TOKEN") or "").strip()
    if not access_token:
        _LOG.error("[meta-fallback] falta META_ACCESS_TOKEN")
        return {"ok": False, "status": 0, "body": "meta_falta_token"}
    graph_version = (os.environ.get("META_GRAPH_VERSION") or "v22.0").strip()
    url = f"https://graph.facebook.com/{graph_version}/{phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_clean,
        "type": "template",
        "template": {"name": template_name, "language": {"code": language}},
    }
    if components:
        payload["template"]["components"] = components
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    try:
        r = requests.post(url, headers=headers, data=json.dumps(payload),
                          timeout=(5, 15))
    except requests.RequestException as e:
        _LOG.exception("[meta-fallback] POST fallo: %s", e)
        return {"ok": False, "status": 0, "body": f"meta_request_error:{type(e).__name__}"}
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
    if not ok:
        _LOG.error("[meta-fallback] send_template fallo: %s", body)
    return {
        "ok": ok,
        "status": r.status_code,
        "body": body,
        "message_id": message_id,
    }


# ============================================================================
# Webhook handler: mapea eventos de Kapso al schema interno de mensajes.
# ============================================================================

def procesar_webhook(payload):
    """Parsea un evento del webhook de Kapso y lo persiste en nuestras tablas.

    Soporta tres casos principales:
      1. Mensaje saliente nuevo (direction=outbound + sin statuses):
         INSERT en `mensajes` como OUT.
      2. Cambio de estado del saliente (statuses[] con sent/delivered/read/failed):
         UPDATE del estado_entrega del OUT via wa_msg_id.
      3. Mensaje entrante (direction=inbound):
         INSERT en `mensajes` como IN.

    Devuelve dict con 'ok' y descripcion de lo que se hizo (util para debug).
    No propaga excepciones — el webhook siempre debe responder 200.
    """
    from app import db as _db
    log = logging.getLogger("petra.webhook.kapso")

    try:
        msg = (payload or {}).get("message") or {}
        kapso_meta = msg.get("kapso") or {}
        direction = (kapso_meta.get("direction") or "").lower()
        wa_msg_id = msg.get("id")
        statuses = kapso_meta.get("statuses") or []

        # --- Caso 2: evento de estado de un OUT ya logueado ---
        if statuses:
            cambios = []
            for st in statuses:
                estado = (st.get("status") or "").lower()
                ts = st.get("timestamp") or 0
                try:
                    ts = int(ts)
                except (TypeError, ValueError):
                    ts = 0
                error_text = None
                errs = st.get("errors") or []
                if errs:
                    e0 = errs[0] or {}
                    error_text = (
                        f"{e0.get('code')}: {e0.get('title') or e0.get('message') or ''}"
                    )[:500]
                target_id = st.get("id") or wa_msg_id
                if target_id and estado:
                    try:
                        _db.actualizar_estado_entrega(target_id, estado, ts, error_text)
                        cambios.append(f"{estado}@{target_id[:16]}")
                    except Exception as e:
                        log.warning("[kapso] actualizar_estado_entrega fallo: %s", e)
            return {"ok": True, "accion": "status_update", "cambios": cambios}

        # --- Caso 1 o 3: mensaje nuevo (IN u OUT sin status previo) ---
        if direction == "outbound":
            telefono = msg.get("to") or ""
            tipo = msg.get("type") or "unknown"
            resumen = kapso_meta.get("content") or ""
            try:
                _db.log_mensaje(telefono, "OUT", f"kapso_{tipo}", resumen[:4000],
                                payload=payload, wa_msg_id=wa_msg_id)
                return {"ok": True, "accion": "insert_out", "wa_msg_id": wa_msg_id}
            except Exception as e:
                log.exception("[kapso] log_mensaje OUT fallo: %s", e)
                return {"ok": False, "accion": "insert_out_fallo", "error": str(e)}

        if direction == "inbound":
            telefono = msg.get("from") or kapso_meta.get("from") or ""
            tipo = msg.get("type") or "unknown"
            resumen = kapso_meta.get("content") or ""
            if not telefono:
                # Fallback: tomar del conversation
                conv = (payload or {}).get("conversation") or {}
                telefono = conv.get("phone_number") or conv.get("business_scoped_user_id") or ""
            try:
                _db.log_mensaje(telefono, "IN", f"kapso_{tipo}", resumen[:4000],
                                payload=payload, wa_msg_id=wa_msg_id)
                return {"ok": True, "accion": "insert_in", "wa_msg_id": wa_msg_id}
            except Exception as e:
                log.exception("[kapso] log_mensaje IN fallo: %s", e)
                return {"ok": False, "accion": "insert_in_fallo", "error": str(e)}

        return {"ok": True, "accion": "no_op", "motivo": "evento_sin_direction_ni_status"}

    except Exception as e:
        log.exception("[kapso] procesar_webhook fallo: %s", e)
        return {"ok": False, "accion": "excepcion", "error": str(e)}
