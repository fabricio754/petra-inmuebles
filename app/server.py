"""Webhook de WhatsApp Cloud API. Este es el único servidor del
prototipo — reemplaza lo que en la arquitectura final haría WATI.
Se ejecuta local y se expone con un túnel (ver README) mientras no
haya hosting propio."""
import os
import json
import logging
from dotenv import load_dotenv

load_dotenv()  # debe cargar antes de importar app.bot -> app.whatsapp (lee env al importar)

from flask import Flask, request, jsonify, render_template

from app import bot

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("petra")

VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "petra-verify-token")

app = Flask(__name__)


@app.get("/")
def health():
    return {"status": "ok", "service": "petra-inmuebles-webhook"}


@app.get("/privacidad")
def privacidad():
    """Política de tratamiento de datos (Ley 1581), enlazada desde el bot."""
    return render_template(
        "privacidad.html",
        responsable=os.environ.get("POLITICA_RESPONSABLE", "Massi"),
        contacto=os.environ.get("POLITICA_CONTACTO", "WhatsApp +57 320 2813268"),
        fecha="2 de octubre de 2026",
    )


@app.get("/webhook")
def verify_webhook():
    """Meta llama esto una sola vez, al configurar la URL del webhook."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("Webhook verificado correctamente por Meta.")
        return challenge, 200
    log.warning("Verificación de webhook fallida (token no coincide).")
    return "Forbidden", 403


@app.post("/webhook")
def receive_webhook():
    payload = request.get_json(silent=True) or {}
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]["value"]
        messages = change.get("messages")
        if not messages:
            # Eventos de status (entregado/leído), los ignoramos en el MVP.
            return jsonify({"status": "ignored"}), 200

        message = messages[0]
        # Si el usuario oculta su número tras un nombre de usuario de WhatsApp,
        # Meta no manda "from" sino "from_user_id" (BSUID, ej. "CO.123...").
        phone = message.get("from") or message["from_user_id"]
        event = _to_event(message)
        log.info("Mensaje entrante de %s: %s", phone, event)
        bot.handle_incoming(phone, event)
    except Exception:
        log.exception("Error procesando webhook. Payload: %s", payload)
    return jsonify({"status": "received"}), 200


def _to_event(message):
    msg_type = message.get("type")
    if msg_type == "text":
        return {"type": "text", "text": message["text"]["body"]}
    if msg_type == "interactive":
        interactive = message["interactive"]
        if interactive["type"] == "list_reply":
            return {"type": "list_reply", "id": interactive["list_reply"]["id"]}
        if interactive["type"] == "button_reply":
            return {"type": "button_reply", "id": interactive["button_reply"]["id"]}
        if interactive["type"] == "nfm_reply":
            # WHATSAPP FLOW -- respuesta del formulario nativo
            # "Publicar mi inmueble". response_json llega como string.
            return {
                "type": "flow_reply",
                "response": json.loads(interactive["nfm_reply"]["response_json"]),
            }
    # Tipo no manejado (imagen, audio, ubicación...) -> se trata como texto vacío.
    return {"type": "text", "text": ""}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
