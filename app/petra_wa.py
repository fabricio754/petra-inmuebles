"""Helper de envío a WhatsApp Cloud API para el piloto Petra.

Credenciales independientes del bot productivo (app/whatsapp.py), para que
el piloto pueda coexistir con producción sin cross-contaminarse.

Env vars:
  WA_PILOT_PHONE_ID    - id del número piloto en Meta (si falta, dry-run)
  WA_PILOT_TOKEN       - access token del app ElevenLabs piloto
  META_GRAPH_VERSION   - opcional, default v20.0

API:
  send_text(to, body)
  send_buttons(to, body, buttons: [{id, title}])     max 3 botones
  send_list(to, body, button_label, sections)        max 10 items
  send_message(to, msg_dict)                         despacha según type

msg_dict usa el formato producido por petra_router (type: text|buttons|list).

Si env vars faltan → log + return {"ok": True, "stub": True}; permite probar
el pipeline completo sin número activo (dev/local/sandbox).
"""
import logging
import os
import requests

log = logging.getLogger("petra")

_GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v20.0")


def _creds():
    phone_id = os.environ.get("WA_PILOT_PHONE_ID") or ""
    token = os.environ.get("WA_PILOT_TOKEN") or ""
    return phone_id, token


def _url(phone_id):
    return f"https://graph.facebook.com/{_GRAPH_VERSION}/{phone_id}/messages"


def _post(payload):
    phone_id, token = _creds()
    if not phone_id or not token:
        log.info("[petra_wa] STUB send to=%s type=%s (no credenciales)",
                 payload.get("to"), payload.get("type"))
        return {"ok": True, "stub": True, "nota": "credenciales no configuradas"}
    try:
        resp = requests.post(
            _url(phone_id),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload, timeout=(5, 10),
        )
    except requests.RequestException as exc:
        log.exception("[petra_wa] send fallo transporte")
        return {"ok": False, "error": f"transport: {exc}"}
    if resp.status_code >= 300:
        log.error("[petra_wa] ERROR %s: %s", resp.status_code, resp.text[:500])
        return {"ok": False, "status": resp.status_code, "body": resp.text[:500]}
    try:
        wa_msg_id = resp.json().get("messages", [{}])[0].get("id")
    except Exception:
        wa_msg_id = None
    return {"ok": True, "status": resp.status_code, "wa_msg_id": wa_msg_id}


def send_text(to, body):
    return _post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body[:4096]},
    })


def send_buttons(to, body, buttons):
    """buttons: lista de dicts {id, title}. Max 3. title max 20 chars."""
    bts = []
    for b in buttons[:3]:
        bts.append({
            "type": "reply",
            "reply": {"id": str(b["id"])[:256], "title": str(b["title"])[:20]},
        })
    return _post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body[:1024]},
            "action": {"buttons": bts},
        },
    })


def send_list(to, body, button_label, sections):
    """sections: lista de dicts {title, rows:[{id, title, description?}]}."""
    secs = []
    for s in sections:
        rows = []
        for r in s.get("rows", []):
            row = {
                "id": str(r["id"])[:200],
                "title": str(r["title"])[:24],
            }
            if r.get("description"):
                row["description"] = str(r["description"])[:72]
            rows.append(row)
        if rows:
            secs.append({"title": str(s.get("title", "Opciones"))[:24], "rows": rows})
    return _post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body[:1024]},
            "action": {
                "button": str(button_label)[:20],
                "sections": secs,
            },
        },
    })


def send_message(to, msg):
    """Despacha un dict de mensaje producido por petra_router."""
    t = msg.get("type")
    if t == "text":
        return send_text(to, msg.get("text", ""))
    if t == "buttons":
        return send_buttons(to, msg.get("text", ""), msg.get("buttons", []))
    if t == "list":
        return send_list(
            to, msg.get("text", ""),
            msg.get("button_label", "Elegir"),
            msg.get("sections", []),
        )
    log.warning("[petra_wa] tipo de mensaje no soportado: %s", t)
    return {"ok": False, "error": f"tipo no soportado: {t}"}


def send_many(to, messages):
    """Envía una lista de mensajes en orden. Devuelve lista de resultados."""
    results = []
    for m in messages:
        results.append(send_message(to, m))
    return results


# =============================================================================
# Helpers de detección: ¿este webhook es del número piloto?
# =============================================================================

def pilot_phone_number_ids():
    """Devuelve el set de phone_number_id del WABA piloto, desde env
    WA_PILOT_PHONE_IDS (CSV, 1+). Fallback al único WA_PILOT_PHONE_ID."""
    csv = os.environ.get("WA_PILOT_PHONE_IDS", "") or ""
    ids = {s.strip() for s in csv.split(",") if s.strip()}
    single = (os.environ.get("WA_PILOT_PHONE_ID") or "").strip()
    if single:
        ids.add(single)
    return ids


def es_webhook_piloto(payload):
    """Decide si el payload del webhook de WhatsApp corresponde al número
    piloto. Petra se activa solo para esos mensajes; el resto va al bot
    productivo (bot.py) sin cambios."""
    try:
        change = payload["entry"][0]["changes"][0]["value"]
        meta = change.get("metadata") or {}
        pnid = meta.get("phone_number_id") or ""
    except Exception:
        return False
    return pnid in pilot_phone_number_ids()
