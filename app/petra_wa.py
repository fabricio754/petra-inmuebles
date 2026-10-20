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
import re
import threading
import requests

log = logging.getLogger("petra")

_GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v22.0")
_BSUID_RE = re.compile(r"^[A-Z]{2}\.\d+$")

# =============================================================================
# Simulación — modo dry-run para el "Simulador" del panel.
# =============================================================================
# Si el thread actual está "capturando", _post NO envía a Meta: encola el
# payload en SIM_STATE.messages y devuelve {ok, simulated}. El panel puede
# invocar el router entero (misma lógica que WhatsApp real) y recuperar los
# mensajes OUT para renderizarlos como chat.
_SIM_STATE = threading.local()


def _is_simulating():
    return bool(getattr(_SIM_STATE, "capturing", False))


def iniciar_captura():
    """Activa modo captura en este thread. Devuelve el buffer (lista)."""
    _SIM_STATE.capturing = True
    _SIM_STATE.messages = []
    return _SIM_STATE.messages


def detener_captura():
    """Desactiva modo captura y devuelve los mensajes capturados."""
    msgs = list(getattr(_SIM_STATE, "messages", []))
    _SIM_STATE.capturing = False
    _SIM_STATE.messages = []
    return msgs


def _is_bsuid(to):
    """BSUID = Business-Scoped User ID ("CO.123..."): dos letras país + punto + dígitos.
    Llega en lugar del teléfono cuando el usuario oculta el número con un
    WhatsApp username (rollout Meta mayo-julio 2026). Meta exige entregarlo
    en `recipient` (no `to`) junto con `recipient_type: "individual"`."""
    return bool(to) and bool(_BSUID_RE.match(to))


def _creds():
    phone_id = os.environ.get("WA_PILOT_PHONE_ID") or ""
    token = os.environ.get("WA_PILOT_TOKEN") or ""
    return phone_id, token


def _url(phone_id):
    return f"https://graph.facebook.com/{_GRAPH_VERSION}/{phone_id}/messages"


def _en_whitelist(dest):
    """Whitelist opcional de teléfonos / BSUIDs destino.

    Si env var PETRA_PILOTO_WHITELIST está seteada (CSV de números/BSUIDs),
    _post SOLO envía a los que están en la lista. El resto se descarta en
    silencio (dry-run). Útil para probar features sin spam a producción.
    Si la env var está ausente o vacía → modo normal, envía a todos."""
    wl = (os.environ.get("PETRA_PILOTO_WHITELIST") or "").strip()
    if not wl:
        return True
    permitidos = {s.strip() for s in wl.split(",") if s.strip()}
    return dest in permitidos


def _post(payload):
    # Modo simulación: capturar en vez de enviar. El panel reutiliza TODO
    # el pipeline (router + flow + LLM) para probar sin WhatsApp real.
    if _is_simulating():
        _SIM_STATE.messages.append({
            "to": payload.get("to") or payload.get("recipient"),
            "type": payload.get("type"),
            "resumen": _resumen_payload(payload),
            "payload": payload,
        })
        return {"ok": True, "simulated": True}
    phone_id, token = _creds()
    if not phone_id or not token:
        log.info("[petra_wa] STUB send to=%s type=%s (no credenciales)",
                 payload.get("to"), payload.get("type"))
        return {"ok": True, "stub": True, "nota": "credenciales no configuradas"}
    dest = payload.get("to") or payload.get("recipient") or ""
    if not _en_whitelist(dest):
        log.info("[petra_wa] WHITELIST: dest=%s NO está en PETRA_PILOTO_WHITELIST — skip (dry-run)",
                 dest)
        return {"ok": True, "whitelist_skip": True,
                "nota": f"dest {dest} fuera de whitelist"}
    if _is_bsuid(dest) and "to" in payload:
        # Meta valida "to" como E.164 → BSUID va en "recipient" + recipient_type.
        # Mandar ambos devuelve 131009 (to gana y es rechazado).
        payload = {k: v for k, v in payload.items() if k != "to"}
        payload["recipient_type"] = "individual"
        payload["recipient"] = dest
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
        if _is_bsuid(dest) and "131009" in (resp.text or ""):
            log.error("[petra_wa] BSUID rechazado por Meta (dest=%s). "
                      "Revisar que el WABA emisor esté en el mismo Business "
                      "Portfolio que generó el BSUID. body=%s",
                      dest, resp.text[:300])
        else:
            log.error("[petra_wa] ERROR %s: %s", resp.status_code, resp.text[:500])
        _log_mensaje_out(dest, payload, resp.status_code, None)
        return {"ok": False, "status": resp.status_code, "body": resp.text[:500]}
    try:
        wa_msg_id = resp.json().get("messages", [{}])[0].get("id")
    except Exception:
        wa_msg_id = None
    _log_mensaje_out(dest, payload, resp.status_code, wa_msg_id)
    return {"ok": True, "status": resp.status_code, "wa_msg_id": wa_msg_id}


def _resumen_payload(payload):
    """Convierte un payload Meta en texto corto legible para el panel."""
    t = payload.get("type") or ""
    if t == "text":
        return (payload.get("text") or {}).get("body", "")[:500]
    if t == "interactive":
        inter = payload.get("interactive") or {}
        body_text = (inter.get("body") or {}).get("text", "")[:300]
        itype = inter.get("type")
        if itype == "button":
            btns = (inter.get("action") or {}).get("buttons") or []
            labels = [((b.get("reply") or {}).get("title") or "") for b in btns]
            return f"{body_text}\n[Botones: {', '.join(labels)}]"
        if itype == "list":
            label = (inter.get("action") or {}).get("button") or "Elegir"
            return f"{body_text}\n[Lista: {label}]"
        return body_text
    return f"[{t or 'unknown'}]"


def _log_mensaje_out(dest, payload, status_code, wa_msg_id):
    """Persiste el OUT en la tabla `mensajes` para que el panel pueda
    renderizar el chat con chulos de entrega. No propaga errores."""
    try:
        from app import db
        tipo = payload.get("type") or "text"
        resumen = _resumen_payload(payload)
        db.log_mensaje(
            dest, "out", tipo, resumen,
            {"status": status_code, "payload": payload},
            wa_msg_id=wa_msg_id,
        )
    except Exception:
        log.exception("[petra_wa] log_mensaje out fallo (no crítico)")


def send_text(to, body):
    return _post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body[:4096]},
    })


def _normalizar_url_publica(url):
    """Convierte rutas relativas ('/petra/media/xxx.png') a URL absoluta.
    Meta WhatsApp requiere URL completa accesible pública."""
    if not url:
        return url
    url = url.strip()
    if url.startswith(("http://", "https://")):
        return url
    base = (os.environ.get("PETRA_PUBLIC_BASE_URL") or
            "https://petra-inmuebles.onrender.com").rstrip("/")
    if not url.startswith("/"):
        url = "/" + url
    return base + url


_IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp")
_VID_EXT = (".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v")


def tipo_media_url(url):
    """Devuelve 'image', 'video' o None según la extensión del URL."""
    if not url:
        return None
    u = url.lower().split("?", 1)[0]
    if u.endswith(_IMG_EXT):
        return "image"
    if u.endswith(_VID_EXT):
        return "video"
    return None


def send_image(to, url, caption=None):
    """Envía una imagen por URL. Caption opcional (max 1024)."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": {"link": _normalizar_url_publica(url)},
    }
    if caption:
        payload["image"]["caption"] = caption[:1024]
    return _post(payload)


def send_video(to, url, caption=None):
    """Envía un video por URL. Caption opcional (max 1024)."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "video",
        "video": {"link": _normalizar_url_publica(url)},
    }
    if caption:
        payload["video"]["caption"] = caption[:1024]
    return _post(payload)


def send_media_o_texto(to, titulo, url):
    """Si la URL es imagen/video, envía como media nativa (con caption=titulo).
    Si no, cae a texto con el link embebido (preview nativo de WhatsApp)."""
    tipo = tipo_media_url(url)
    if tipo == "image":
        return send_image(to, url, caption=f"🖼 *{titulo}*" if titulo else None)
    if tipo == "video":
        return send_video(to, url, caption=f"🎬 *{titulo}*" if titulo else None)
    # Fallback: texto con URL
    cuerpo = f"🖼 *{titulo}*\n\n{url}" if titulo else url
    return send_text(to, cuerpo)


def send_typing_indicator(wa_msg_id):
    """Marca el mensaje como leído y muestra los 3 puntitos de typing.
    WhatsApp los muestra hasta que llegue el próximo mensaje nuestro, o
    máximo 25 segundos. API nueva de Meta (2024): status=read +
    typing_indicator.type=text."""
    if not wa_msg_id:
        return {"ok": True, "skip": "sin wa_msg_id"}
    return _post({
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": wa_msg_id,
        "typing_indicator": {"type": "text"},
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
