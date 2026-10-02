"""Capa delgada sobre la WhatsApp Cloud API (Meta), directa — sin WATI.

Motivo de esta decisión (ver mensaje de respuesta): para tener un
prototipo funcionando HOY no podemos depender de la verificación de
negocio de Meta ni de un plan pagado de WATI. El número de prueba
gratuito de Meta permite enviar mensajes reales a hasta 5 destinatarios
de prueba sin ninguno de esos trámites.

Si no hay credenciales configuradas (.env vacío), el módulo entra en
modo DRY-RUN: no llama a la API real, solo registra qué se habría
enviado. Así se puede probar toda la lógica de conversación sin
depender de que la cuenta de Meta ya exista.
"""
import logging
import os
import requests

log = logging.getLogger("petra")

GRAPH_API_VERSION = "v20.0"
ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("META_PHONE_NUMBER_ID", "")

DRY_RUN = not (ACCESS_TOKEN and PHONE_NUMBER_ID)

# Dirección pública del bot, para enlazar la política de datos (/privacidad).
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://petra-inmuebles.onrender.com").rstrip("/")


def _graph_url():
    return f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"


def _is_bsuid(to):
    """BSUID = ID de usuario por negocio ("CO.123..."): país + punto + id.
    Llega en lugar del teléfono cuando el usuario usa nombre de usuario."""
    return "." in to


def _dispatch(payload, human_summary):
    """Envía el payload a Meta, o lo simula en modo dry-run.
    Devuelve un resumen legible (usado por el simulador de consola)."""
    dest = payload["to"]
    if _is_bsuid(dest):
        # Meta no acepta un BSUID en "to": va en "recipient".
        payload = {k: v for k, v in payload.items() if k != "to"}
        payload["recipient"] = dest
    if DRY_RUN:
        log.info("[DRY-RUN → %s] %s", dest, human_summary)
        return {"status": "dry-run", "summary": human_summary}

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    resp = requests.post(_graph_url(), headers=headers, json=payload, timeout=15)
    if resp.status_code >= 300:
        log.error("[ERROR WhatsApp API] %s: %s", resp.status_code, resp.text)
    else:
        log.info("[OK WhatsApp API] %s → %s: %s", resp.status_code, dest, human_summary)
    return {"status": resp.status_code, "summary": human_summary, "body": resp.text}


def send_text(to, body):
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    return _dispatch(payload, body)


def _botones(to, cuerpo, botones, resumen):
    """Mensaje con hasta 3 botones de respuesta. Título de botón: máx. 20 caracteres."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": cuerpo},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": id_, "title": titulo}}
                for id_, titulo in botones
            ]},
        },
    }
    return _dispatch(payload, resumen)


# --- Flujo de crédito con Sureti (Hito 6) -----------------------------------

def send_autorizacion_datos(to, repetir=False):
    if repetir:
        cuerpo = (
            "Para continuar necesito que aceptes o no la autorización de datos. "
            f"Puedes leer la política aquí: {PUBLIC_BASE_URL}/privacidad"
        )
    else:
        cuerpo = (
            "Hola 👋 Soy Massi. Te ayudamos a conseguir liquidez usando tu inmueble "
            "como garantía, sin venderlo, con nuestro aliado financiero *Sureti*.\n\n"
            "Para revisar tu caso necesito tu autorización para tratar tus datos "
            "personales (Ley 1581 de 2012): los usaremos para evaluar tu solicitud "
            "de crédito y los *compartiremos con Sureti* para su estudio. Puedes "
            "consultarlos, corregirlos o pedir que los borremos cuando quieras.\n\n"
            f"Política de datos: {PUBLIC_BASE_URL}/privacidad\n\n"
            "Escribe *SALIR* en cualquier momento para no recibir más mensajes.\n\n"
            "¿Aceptas?"
        )
    return _botones(
        to, cuerpo, [("BOTON_SI", "Acepto"), ("BOTON_NO", "No acepto")],
        "[AUTORIZACIÓN DATOS]" + (" (repetida)" if repetir else ""),
    )


def send_pregunta_si_no(to, pregunta):
    return _botones(to, pregunta, [("BOTON_SI", "Sí"), ("BOTON_NO", "No")], f"[SÍ/NO] {pregunta[:60]}")


def send_no_contactar(to):
    return send_text(
        to,
        "Entendido. No te enviaremos más mensajes.\n\n"
        "Si en el futuro quieres retomar, solo escríbenos por aquí.",
    )


def send_no_califica(to, motivo):
    mensajes = {
        "HIPOTECA": (
            "Gracias por responder. Por ahora no podemos avanzar con un inmueble que "
            "tiene hipoteca o embargo vigente.\n\n"
            "Si la situación cambia, escríbenos y con gusto revisamos tu caso."
        ),
        "PATRIMONIO": (
            "Gracias por responder. Por ahora no podemos avanzar con un inmueble que "
            "tiene patrimonio de familia con hijos menores de edad.\n\n"
            "Si la situación cambia, escríbenos."
        ),
        "EDAD": (
            "Gracias por responder. Por ahora este crédito no aplica cuando el "
            "propietario tiene más de 75 años.\n\n"
            "Gracias por escribirnos."
        ),
    }
    return send_text(to, mensajes.get(motivo, "Gracias por escribirnos."))


def send_pausa_paz_salvo(to):
    return send_text(
        to,
        "Entendido. Para avanzar con el crédito el inmueble debe estar al día en "
        "predial, servicios y administración.\n\n"
        "Te escribiremos en unos 30 días por si quieres retomar. Si lo logras "
        "antes, escríbenos cuando quieras.",
    )


def send_confirmacion_pipeline(to, nombre_corto):
    sufijo = f", {nombre_corto}" if nombre_corto else ""
    return send_text(
        to,
        f"✅ ¡Listo{sufijo}!\n\n"
        "Ya tenemos tu información. Revisaremos tu caso y te escribiremos por este "
        "mismo WhatsApp en los próximos días hábiles.\n\n"
        "Gracias por confiar en Massi. 🙌",
    )
