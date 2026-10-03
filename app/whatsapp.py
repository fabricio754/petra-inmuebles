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

# Formularios nativos de WhatsApp (Flows) del crédito. JSON en flows/.
# Si no hay IDs configurados, el bot pregunta por chat (bot.py).
FLOW_REQUISITOS_ID = os.environ.get("META_FLOW_REQUISITOS_ID", "")
FLOW_DATOS_ID = os.environ.get("META_FLOW_DATOS_ID", "")
# "draft" mientras los Flows estén en borrador en WhatsApp Manager;
# "published" cuando se publiquen.
FLOWS_MODE = os.environ.get("META_FLOWS_MODE", "draft")
USE_FLOWS = bool(FLOW_REQUISITOS_ID and FLOW_DATOS_ID)

# Plantilla de apertura aprobada por Meta (Hito 7). Variables del cuerpo:
# {{1}} tipo de inmueble, {{2}} portal, {{3}} monto "hasta" en millones.
PLANTILLA_APERTURA = os.environ.get("META_PLANTILLA_APERTURA", "")
PLANTILLA_IDIOMA = os.environ.get("META_PLANTILLA_IDIOMA", "es_CO")


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


def _flow(to, flow_id, screen, cuerpo, cta, resumen):
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "header": {"type": "text", "text": "Massi"},
            "body": {"text": cuerpo},
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_message_version": "3",
                    "flow_token": f"{screen}:{to}",
                    "flow_id": flow_id,
                    "flow_cta": cta,
                    "flow_action": "navigate",
                    "flow_action_payload": {"screen": screen},
                    "mode": FLOWS_MODE,
                },
            },
        },
    }
    return _dispatch(payload, f"{resumen} modo={FLOWS_MODE} flow_id={flow_id}")


# --- Flujo de crédito con Sureti (Hito 6) -----------------------------------

def send_form_requisitos(to, repetir=False):
    cuerpo = (
        "Toca el botón para abrir el formulario." if repetir else
        "Son 2 pasos cortos:\n"
        "1️⃣ Ubicación del inmueble y 4 preguntas de requisitos.\n"
        "2️⃣ Datos del propietario.\n\n"
        "Empecemos con tu inmueble."
    )
    return _flow(to, FLOW_REQUISITOS_ID, "REQUISITOS", cuerpo, "Paso 1: tu inmueble", "[FORM REQUISITOS]")


def send_form_datos(to, repetir=False):
    cuerpo = (
        "Toca el botón para abrir el formulario." if repetir else
        "¡Tu inmueble cumple los requisitos! 🎉\n\n"
        "Último paso: los datos del propietario (nombre, cédula y correo)."
    )
    return _flow(to, FLOW_DATOS_ID, "DATOS", cuerpo, "Paso 2: propietario", "[FORM DATOS]")


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


def _lista(to, cuerpo, filas, boton, resumen):
    """Mensaje de lista interactiva (una sección, hasta 10 filas)."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": cuerpo},
            "action": {
                "sections": [{"title": "Opciones", "rows": filas}],
                "button": boton,
            },
        },
    }
    return _dispatch(payload, resumen)


def send_tipo_inmueble(to):
    return _lista(
        to,
        "¿Qué tipo de inmueble es la garantía?",
        [
            {"id": "TIPO_CASA", "title": "Casa"},
            {"id": "TIPO_APTO", "title": "Apartamento"},
            {"id": "TIPO_LOCAL", "title": "Local comercial"},
            {"id": "TIPO_OFICINA", "title": "Oficina"},
            {"id": "TIPO_LOTE", "title": "Lote"},
            {"id": "TIPO_BODEGA", "title": "Bodega"},
        ],
        "Ver tipos",
        "[TIPO INMUEBLE]",
    )


def send_objetivo_prestamo(to):
    return _lista(
        to,
        "¿Para qué necesitas el préstamo?",
        [
            {"id": "OBJ_CAPITAL", "title": "Capital de trabajo"},
            {"id": "OBJ_DEUDAS", "title": "Pagar deudas"},
            {"id": "OBJ_INVERSION", "title": "Inversión"},
            {"id": "OBJ_GASTOS", "title": "Gastos personales"},
            {"id": "OBJ_OTRO", "title": "Otro"},
        ],
        "Ver opciones",
        "[OBJETIVO PRÉSTAMO]",
    )


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


# --- Captación (Hito 7) ------------------------------------------------------

def send_doc_recibido(to, avaluo=None, direccion="", matricula=""):
    """Confirma recepción de un documento y muestra lo que se extrajo."""
    if avaluo:
        avaluo_fmt = f"${avaluo:,}".replace(",", ".")
        detalle = (
            f"✅ Recibí tu recibo de predial.\n\n"
            f"• Avalúo catastral: *{avaluo_fmt}*\n"
            + (f"• Dirección: {direccion}\n" if direccion else "")
            + (f"• Matrícula: {matricula}\n" if matricula else "")
            + "\nListo, ya tengo esta información para tu solicitud."
        )
    else:
        detalle = (
            "✅ Recibí el documento. No pude leer el avalúo catastral "
            "automáticamente — nuestro equipo lo revisará."
        )
    return send_text(to, detalle)


def send_tipo_doc_invalido(to):
    return send_text(
        to,
        "Solo acepto fotos (JPEG/PNG) o archivos PDF. "
        "Por favor envía el documento en uno de esos formatos.",
    )


def send_solicitud_ctl(to):
    """Pide al cliente el Certificado de Tradición y Libertad del inmueble."""
    return send_text(
        to,
        "📄 *Siguiente paso: Certificado de Tradición y Libertad*\n\n"
        "Para continuar con el estudio necesito que nos envíes el *Certificado "
        "de Tradición y Libertad* del inmueble.\n\n"
        "Puedes descargarlo en línea (sin costo) en:\n"
        "🔗 https://certificados.supernotariado.gov.co\n\n"
        "Búscalo con el número de matrícula inmobiliaria y envíalo aquí como PDF "
        "o foto. Si no lo tienes a la mano, puedes enviarlo después.",
    )


def send_credito_aprobado(to, nombre_corto, monto):
    sufijo = f", {nombre_corto}" if nombre_corto else ""
    monto_fmt = f"${monto:,}".replace(",", ".") if monto else ""
    cuerpo = (
        f"🎉 ¡Buenas noticias{sufijo}!\n\n"
        "Tu solicitud de crédito con *Sureti* fue *aprobada*."
        + (f" El monto aprobado es de *{monto_fmt} millones*." if monto_fmt else "")
        + "\n\nNuestro equipo te contactará pronto para coordinar los siguientes pasos. "
        "Gracias por confiar en Massi. 🙌"
    )
    return send_text(to, cuerpo)


def send_credito_rechazado(to, razon=None):
    detalle = f"\n\nMotivo: _{razon}_" if razon else ""
    return send_text(
        to,
        "Hola. Lamentamos informarte que tu solicitud de crédito con *Sureti* "
        f"no fue aprobada en esta ocasión.{detalle}\n\n"
        "Si tu situación cambia o tienes preguntas, escríbenos y con gusto te ayudamos.",
    )


def send_credito_desembolsado(to, nombre_corto, monto, comision):
    sufijo = f", {nombre_corto}" if nombre_corto else ""
    monto_fmt = f"${monto:,}".replace(",", ".") if monto else ""
    comision_fmt = f"${comision:,}".replace(",", ".") if comision else ""
    cuerpo = (
        f"🎊 ¡Excelente noticia{sufijo}!\n\n"
        "Tu crédito con *Sureti* fue *desembolsado*."
        + (f" Monto: *{monto_fmt}*." if monto_fmt else "")
        + (f"\n\nComisión Massi: *{comision_fmt}* (se cobra en 4 cuotas)." if comision_fmt else "")
        + "\n\nNuestro equipo te contactará pronto para coordinar los siguientes pasos. "
        "Muchas gracias por confiar en Massi. 🙌"
    )
    return send_text(to, cuerpo)


def send_paz_salvo_recordatorio(to, nombre_corto, dia):
    sufijo = f", {nombre_corto}" if nombre_corto else ""
    if dia == 15:
        cuerpo = (
            f"Hola{sufijo} 👋 Hace 15 días hablamos sobre tu crédito con garantía hipotecaria.\n\n"
            "¿Pudiste ponerte al día con predial, servicios y administración? "
            "Si es así, aquí estamos para ayudarte a avanzar con tu solicitud. "
            "Solo escríbenos."
        )
    else:
        cuerpo = (
            f"Hola{sufijo}. Te hacemos un último recordatorio: cuando puedas ponerte "
            "al día con el inmueble, escríbenos y retomamos tu solicitud de crédito. 🏡\n\n"
            "Escribe *SALIR* si prefieres no recibir más mensajes."
        )
    return send_text(to, cuerpo)


def send_plantilla_apertura(to, tipo, portal, monto_hasta):
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": PLANTILLA_APERTURA,
            "language": {"code": PLANTILLA_IDIOMA},
            "components": [{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": tipo},
                    {"type": "text", "text": portal or "internet"},
                    {"type": "text", "text": f"{monto_hasta:,}".replace(",", ".")},
                ],
            }],
        },
    }
    return _dispatch(payload, f"[PLANTILLA {PLANTILLA_APERTURA}] {tipo} en {portal}, hasta ${monto_hasta}M")
