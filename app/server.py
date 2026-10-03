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

from app import bot, envios, scraper

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("petra")

VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "petra-verify-token")

app = Flask(__name__)
envios.iniciar()
scraper.iniciar()


@app.get("/")
def health():
    return {"status": "ok", "service": "petra-inmuebles-webhook"}


@app.post("/captura")
def captura():
    """Recibe un anuncio desde la extensión de Chrome "Enviar a Massi"."""
    token = os.environ.get("CAPTURA_TOKEN", "")
    if not token or request.headers.get("X-Massi-Token") != token:
        return jsonify({"resultado": "no_autorizado"}), 401
    from app import captacion  # requiere Postgres
    anuncio = request.get_json(silent=True) or {}
    resultado, detalle = captacion.procesar(anuncio)
    log.info("[Captura] %s: %s (%s)", resultado, detalle, anuncio.get("url"))
    return jsonify({"resultado": resultado, "detalle": detalle})


@app.get("/privacidad")
def privacidad():
    """Política de tratamiento de datos (Ley 1581), enlazada desde el bot."""
    return render_template(
        "privacidad.html",
        responsable=os.environ.get(
            "POLITICA_RESPONSABLE",
            "ALMOND CORP S.A.S. (marca Massi), NIT 901.931.289-4, domicilio principal en Bogotá D.C.",
        ),
        contacto=os.environ.get(
            "POLITICA_CONTACTO", "fabricio@petrasecondaries.com o WhatsApp +57 320 2813268"
        ),
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
    if msg_type == "button":
        # Botón de respuesta rápida de una plantilla (ej. "Quiero saber más").
        boton = message.get("button", {})
        return {"type": "template_button", "text": boton.get("text") or boton.get("payload") or ""}
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
