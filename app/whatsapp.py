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

# --- WhatsApp Flow "Publicar mi inmueble" (formulario nativo) --------------
# FLOW_MODE controla si mandamos la versión draft (sin publicar, para probar
# contra el número de prueba) o la publicada. Por defecto "draft": NO publicar
# el Flow en WhatsApp Manager hasta aprobación explícita.
FLOW_ID = os.environ.get("META_FLOW_PUBLICAR_ID", "")
FLOW_MODE = os.environ.get("META_FLOW_PUBLICAR_MODE", "draft")


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


def send_menu(to, conjunto):
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": "Massi 👋"},
            "body": {"text": f"Hola 👋 Soy Massi.\nEstás en *{conjunto.title()}*.\n¿Qué necesitas?"},
            "footer": {"text": "Massi te conecta con lo que necesitas"},
            "action": {
                "button": "Ver opciones",
                "sections": [{
                    "title": "Menú principal",
                    "rows": [
                        {"id": "MENU_ARRIENDO", "title": "🏠 Tomar en arriendo", "description": "Ver apartamentos disponibles"},
                        {"id": "MENU_COMPRAR", "title": "🏡 Comprar", "description": "Ver apartamentos en venta"},
                        {"id": "MENU_PUBLICAR", "title": "📋 Publicar mi inmueble", "description": "Arrendar o vender el tuyo"},
                        {"id": "MENU_CREDITO", "title": "💳 Crédito hipotecario", "description": "Con garantía hipotecaria"},
                        {"id": "MENU_PAGOS", "title": "💰 Pago de servicios", "description": "Administración o servicios públicos"},
                    ],
                }],
            },
        },
    }
    summary = f"[MENÚ] {conjunto} → 5 opciones"
    return _dispatch(payload, summary)


def send_menu_pagos(to):
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": "💰 Pago de servicios"},
            "body": {"text": "¿Qué quieres pagar?"},
            "footer": {"text": "Massi"},
            "action": {
                "button": "Ver opciones",
                "sections": [{
                    "title": "Tipo de pago",
                    "rows": [
                        {"id": "PAGO_ADMINISTRACION", "title": "🏢 Cuota de administración", "description": "Del conjunto Arrayanes"},
                        {"id": "PAGO_SERVICIOS", "title": "🔌 Servicios públicos", "description": "Luz, agua, gas o internet"},
                    ],
                }],
            },
        },
    }
    return _dispatch(payload, "[MENÚ PAGOS] 2 opciones")


def send_catalogo(to, conjunto, operacion_label, listings):
    rows = []
    for item in listings:
        precio_fmt = f"${item['precio']:,.0f}".replace(",", ".")
        title = item["apartamento"]
        desc = f"{item['habitaciones']} hab · {item['banos']} baños · {item['m2']} m² · {precio_fmt}"
        rows.append({"id": f"APTO_{item['id']}", "title": title, "description": desc})

    if not rows:
        return send_text(to, f"En {conjunto} todavía no hay inmuebles publicados en esta operación.")

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": f"{conjunto} · {operacion_label}"},
            "body": {"text": "Inmuebles disponibles:"},
            "footer": {"text": "Toca un apartamento para ver más"},
            "action": {"button": "Ver inmuebles", "sections": [{"title": "Disponibles", "rows": rows}]},
        },
    }
    summary = f"[CATÁLOGO {operacion_label}] {conjunto} → {len(rows)} inmuebles: " + ", ".join(r["title"] for r in rows)
    return _dispatch(payload, summary)


def send_apto_detail(to, apto):
    precio_fmt = f"${apto['precio']:,.0f}".replace(",", ".")
    es_petra = apto["gestion"] == "PETRA"
    gestion_label = "Gestiona Petra" if es_petra else "Publicado por el propietario"
    boton_label = "Contactar Petra" if es_petra else "Contactar"
    body = (
        f"*{apto['apartamento']} · {apto['conjunto'].title()}*\n"
        f"{apto['habitaciones']} habitaciones · {apto['banos']} baños · {apto['m2']} m²\n"
        f"{precio_fmt} · {gestion_label}"
    )
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": [{"type": "reply", "reply": {"id": f"CONTACTAR_{apto['id']}", "title": boton_label}}]},
        },
    }
    summary = f"[FICHA] {apto['apartamento']} ({apto['conjunto']}) — botón {boton_label}"
    return _dispatch(payload, summary)


def send_confirmacion_contacto(to, apto):
    """Solo para inmuebles gestion=PETRA — sin cambios de comportamiento."""
    body = (
        f"✅ Recibimos tu interés en *{apto['apartamento']}* ({apto['conjunto'].title()}).\n"
        f"Un asesor de Petra te va a escribir por este mismo WhatsApp."
    )
    return send_text(to, body)


def send_confirmar_contacto_propietario(to, apto):
    body = (
        f"🏠 *{apto['apartamento']} · {apto['conjunto'].title()}*\n\n"
        "Perfecto. Le vamos a compartir al propietario que estás interesado en este inmueble.\n\n"
        "¿Quieres que lo contactemos?"
    )
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": f"CONFIRMAR_CONTACTO_{apto['id']}", "title": "Sí, contactar"}},
                {"type": "reply", "reply": {"id": f"CANCELAR_CONTACTO_{apto['id']}", "title": "Cancelar"}},
            ]},
        },
    }
    summary = f"[CONFIRMAR CONTACTO] {apto['apartamento']} ({apto['conjunto']})"
    return _dispatch(payload, summary)


def send_conexion_confirmada(to, apto):
    body = (
        "✅ Listo.\n\n"
        f"Ya le avisamos al propietario de *{apto['apartamento']}* de tu interés en este inmueble "
        "y le compartimos tu contacto para que pueda comunicarse contigo.\n\n"
        "Puedes esperar su mensaje."
    )
    return send_text(to, body)


def send_cancelacion_contacto(to):
    return send_text(to, "Entendido, no compartimos tu contacto con el propietario.")


def send_autorizacion_propietario(to, resumen):
    body = (
        f"{resumen}\n\n"
        "Para publicar tu inmueble, necesitamos tu autorización:\n\n"
        "\"Autorizo a Petra Inmuebles a compartir mi información de contacto con las "
        "personas interesadas en este inmueble, exclusivamente para facilitar el contacto "
        "relacionado con su arriendo o venta.\""
    )
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": "AUTORIZO_PUBLICAR", "title": "Autorizo"}},
                {"type": "reply", "reply": {"id": "NO_AUTORIZO_PUBLICAR", "title": "No autorizo"}},
            ]},
        },
    }
    summary = "[AUTORIZACIÓN PUBLICAR] pidiendo consentimiento de contacto"
    return _dispatch(payload, summary)


def send_confirmacion_publicacion(to, apto):
    precio_fmt = f"${apto['precio']:,.0f}".replace(",", ".")
    body = (
        "✅ Tu inmueble quedó publicado:\n\n"
        f"*{apto['apartamento']} · {apto['conjunto'].title()}*\n"
        f"{apto['habitaciones']} hab · {apto['banos']} baños · {apto['m2']} m² · {precio_fmt}\n\n"
        "Ya está visible para quienes busquen en tu conjunto."
    )
    return send_text(to, body)


def send_publicacion_rechazada(to):
    return send_text(
        to,
        "Sin esa autorización no podemos publicar tu inmueble, porque no podríamos "
        "conectarte con las personas interesadas. Si cambias de opinión, escribe *menu* "
        "para intentarlo de nuevo.",
    )


def send_flow_publicar(to):
    """WHATSAPP FLOW — abre el formulario nativo "Publicar mi inmueble"
    dentro del chat (una pantalla, FORM; JSON en flows/publicar_inmueble.json).

    Requiere META_FLOW_PUBLICAR_ID configurado (el ID del Flow creado en
    WhatsApp Manager). FLOW_MODE="draft" permite probarlo sin publicar el Flow."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "header": {"type": "text", "text": "Petra Inmuebles"},
            "body": {"text": "Completa los datos de tu inmueble."},
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_message_version": "3",
                    "flow_token": to,
                    "flow_id": FLOW_ID,
                    "flow_cta": "Publicar mi inmueble",
                    "flow_action": "navigate",
                    "flow_action_payload": {"screen": "FORM"},
                    "mode": FLOW_MODE,
                },
            },
        },
    }
    summary = f"[FLOW PUBLICAR] modo={FLOW_MODE} flow_id={FLOW_ID or '(META_FLOW_PUBLICAR_ID sin configurar)'}"
    return _dispatch(payload, summary)


# --- Piloto Crédito (dummy) -------------------------------------------------
# Sin proveedor real todavía. Termina guardando el lead y avisando que un
# asesor contactará -- el punto donde se conectaría un proveedor real de
# crédito está marcado en bot.py.

def send_confirmacion_credito(to):
    body = (
        "✅ Recibimos tu solicitud de crédito con garantía hipotecaria.\n"
        "Un asesor te va a contactar por este mismo WhatsApp."
    )
    return send_text(to, body)


# --- Piloto Pago de servicios (dummy) ---------------------------------------
# Sin pasarela de pagos real todavía. Simula el pago y guarda el lead -- el
# punto donde se conectaría un proveedor real de pagos está marcado en bot.py.

def send_confirmacion_pago(to, monto):
    monto_fmt = f"${monto:,.0f}".replace(",", ".")
    body = (
        f"✅ Pago simulado por {monto_fmt} procesado correctamente.\n"
        "(Esto es una prueba piloto -- todavía no está conectado a una pasarela de pagos real.)"
    )
    return send_text(to, body)
