"""Webhook de WhatsApp Cloud API. Este es el único servidor del
prototipo — reemplaza lo que en la arquitectura final haría WATI.
Se ejecuta local y se expone con un túnel (ver README) mientras no
haya hosting propio."""
import os
import json
import logging
import socket
from dotenv import load_dotenv

# Python-level socket timeout so DB/network connections time out in worker threads.
# libpq's connect_timeout uses alarm(2) which only fires in the main thread;
# setdefaulttimeout() uses select(2) which works in all threads including gunicorn's.
socket.setdefaulttimeout(20)

load_dotenv()  # debe cargar antes de importar app.bot -> app.whatsapp (lee env al importar)

from flask import Flask, request, jsonify, render_template

from app import bot, envios, scraper, scheduler

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("petra")

VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "petra-verify-token")

# MIMEs soportados para documentos enviados por el vendedor
MIME_SOPORTADOS = {"image/jpeg", "image/png", "application/pdf"}

app = Flask(__name__)
envios.iniciar()
scraper.iniciar()
scheduler.iniciar()


@app.get("/")
def health():
    return {"status": "ok", "service": "petra-inmuebles-webhook"}


@app.post("/captura")
def captura():
    """Recibe un anuncio desde la extensión de Chrome \"Enviar a Massi\"."""
    token = os.environ.get("CAPTURA_TOKEN", "")
    if not token or request.headers.get("X-Massi-Token") != token:
        return jsonify({"resultado": "no_autorizado"}), 401
    from app import captacion  # requiere Postgres
    anuncio = request.get_json(silent=True) or {}
    resultado, detalle = captacion.procesar(anuncio)
    log.info("[Captura] %s: %s (%s)", resultado, detalle, anuncio.get("url"))
    return jsonify({"resultado": resultado, "detalle": detalle})


@app.post("/pilot")
def pilot():
    """Piloto: scrapea una URL de Metrocuadrado e inyecta el contacto en la DB.

    Body JSON:
      url      — URL del anuncio en Metrocuadrado (requerido)
      telefono — teléfono del propietario (requerido; evita depender de 2captcha)
      nombre   — nombre opcional
      forzar   — si true, elimina el registro previo con ese teléfono y lo re-inserta

    Protegido con el mismo CAPTURA_TOKEN (header X-Massi-Token).
    Úsalo para verificar que el scraper captura correctamente tu publicación
    y que el bot envía la plantilla de apertura.
    """
    token = os.environ.get("CAPTURA_TOKEN", "")
    if not token or request.headers.get("X-Massi-Token") != token:
        return jsonify({"resultado": "no_autorizado"}), 401

    body = request.get_json(silent=True) or {}
    url = str(body.get("url") or "").strip()
    telefono_raw = str(body.get("telefono") or "").strip()
    nombre = body.get("nombre")
    forzar = bool(body.get("forzar", False))

    if not url or "metrocuadrado.com" not in url:
        return jsonify({"resultado": "error", "detalle": "Se requiere una URL de metrocuadrado.com"}), 400

    from app.captacion import normalizar_telefono, _precio, _buscar, CIUDADES, TIPOS, RESIDENCIAL, PORCENTAJE, MONTO_MAX_M, MONTO_MIN_M

    # Scraping de la URL específica con Playwright
    datos_scrapeados = {}
    error_scrape = None
    telefono_scrapeado = None
    try:
        from playwright.sync_api import sync_playwright
        chromium_path = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH", "")
        launch_kwargs = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if chromium_path:
            launch_kwargs["executable_path"] = chromium_path

        with sync_playwright() as pw:
            browser = pw.chromium.launch(**launch_kwargs)
            try:
                from app.scraper import _nueva_pagina, _mq_extraer_datos, _extraer_tel_comun
                page = _nueva_pagina(browser, url)
                try:
                    datos_scrapeados = _mq_extraer_datos(page, url) or {}
                    if not telefono_raw:
                        telefono_scrapeado = _extraer_tel_comun(page)
                        log.info("[Pilot] Teléfono scrapeado: %s", telefono_scrapeado)
                finally:
                    page.context.close()
            finally:
                browser.close()
    except Exception as exc:
        error_scrape = str(exc)
        log.warning("[Pilot] Error scrapeando %s: %s", url, exc)

    # Liberar memoria de Playwright antes de abrir conexión a DB (evita OOM en 512MB)
    import gc; gc.collect()

    # Usar teléfono provisto o el que se scrapeó
    if not telefono_raw and telefono_scrapeado:
        telefono_raw = telefono_scrapeado

    telefono = normalizar_telefono(telefono_raw)
    if not telefono:
        return jsonify({
            "resultado": "error",
            "detalle": "No se encontró teléfono en la publicación ni se proveyó uno.",
            "scrape_error": error_scrape,
        }), 400

    # Construir el contacto con los datos scrapeados + teléfono provisto
    from app import db as _db

    precio_raw = datos_scrapeados.get("precio_raw", "")
    precio = _precio(precio_raw)
    ciudad_raw = datos_scrapeados.get("ciudad_raw", "") or url
    tipo_raw = datos_scrapeados.get("tipo_raw", "") or url

    ciudad = _buscar(CIUDADES, ciudad_raw, url) or "Bogotá"
    tipo = _buscar(TIPOS, tipo_raw, url) or "apartamento"

    # Usar el precio scrapeado; si no se obtuvo, pedir al body
    if precio < 50_000_000:
        precio_body = int(str(body.get("precio", 0) or 0).replace(".", "").replace(",", "") or 0)
        if precio_body >= 50_000_000:
            precio = precio_body
        else:
            precio = 300_000_000  # fallback conservador para no bloquear el piloto

    categoria = "residencial" if tipo in RESIDENCIAL else "comercial"
    monto_hasta = min(int(precio * PORCENTAJE[categoria] / 1_000_000), MONTO_MAX_M)
    monto_hasta = max(monto_hasta, MONTO_MIN_M)

    contacto = {
        "telefono": telefono,
        "nombre": nombre or datos_scrapeados.get("nombre"),
        "direccion": datos_scrapeados.get("direccion"),
        "barrio": datos_scrapeados.get("barrio"),
        "ciudad": ciudad,
        "tipo": tipo,
        "precio": precio,
        "estrato": None,
        "requiere_ph": False,
        "url": url,
        "portal": "metrocuadrado",
        "foto": datos_scrapeados.get("foto"),
        "monto_hasta": monto_hasta,
    }

    log.info("[Pilot] Guardando contacto: tel=%s ciudad=%s tipo=%s precio=%d monto=%d", telefono, ciudad, tipo, precio, monto_hasta)
    try:
        log.info("[Pilot] Abriendo conexión DB…")
        if forzar:
            with _db._conexion() as conn:
                conn.execute("DELETE FROM contactos WHERE telefono = %s", (telefono,))
            log.info("[Pilot] DELETE completado.")

        guardado = _db.guardar_contacto(contacto)
        resultado = "nuevo" if guardado else "duplicado"
        log.info("[Pilot] INSERT completado: %s", resultado)
    except Exception as db_exc:
        log.exception("[Pilot] ERROR guardando contacto: %s", db_exc)
        return jsonify({"resultado": "error", "detalle": str(db_exc), "scrape_ok": not error_scrape}), 500

    log.info("[Pilot] %s — %s (%s, $%dM hasta $%dM)", resultado, telefono, ciudad, precio // 1_000_000, monto_hasta)
    return jsonify({
        "resultado": resultado,
        "contacto": {k: v for k, v in contacto.items() if v is not None},
        "scrape_ok": not error_scrape,
        "scrape_error": error_scrape,
        "datos_scrapeados": {k: v for k, v in datos_scrapeados.items() if v},
    })


INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")


ADMIN_RESET_TOKEN = os.environ.get("ADMIN_RESET_TOKEN", "")


@app.post("/admin/reset-sesion")
def admin_reset_sesion():
    """Elimina la sesión activa de un teléfono y reenvía la plantilla de apertura.

    Body JSON: { "telefono": "573..." }
    Auth: header X-Admin-Token.
    """
    token = request.headers.get("X-Admin-Token", "")
    if not ADMIN_RESET_TOKEN or token != ADMIN_RESET_TOKEN:
        return jsonify({"error": "no_autorizado"}), 401

    body = request.get_json(silent=True) or {}
    telefono = str(body.get("telefono") or "").strip()
    if not telefono:
        return jsonify({"error": "falta telefono"}), 400

    from app import db as _db, whatsapp as wa

    # Limpiar sesión activa
    with _db._conexion() as conn:
        deleted = conn.execute(
            "DELETE FROM sesiones WHERE telefono = %s", (telefono,)
        ).rowcount

    # Buscar datos del contacto para armar la plantilla
    contacto = _db.get_contacto(telefono)
    if contacto:
        tipo = contacto["tipo_inmueble"] or "inmueble"
        monto = contacto["monto_hasta"] or 100
        portal = "metrocuadrado"
    else:
        tipo, monto, portal = "inmueble", 100, "metrocuadrado"

    wa.send_plantilla_apertura(telefono, tipo, portal, monto)
    log.info("[Admin] Reset sesion %s — %d fila(s) eliminadas, template enviado", telefono, deleted)
    return jsonify({"sesion_eliminada": deleted, "template": "enviado", "tipo": tipo, "monto": monto})


@app.post("/scraper/ingest")
def scraper_ingest():
    """Recibe contactos crudos del actor Apify y los procesa con _guardar.

    Acepta un objeto JSON o array de objetos con campos:
      portal, telefono, precio_raw, tipo_raw, ciudad_raw, direccion,
      barrio, nombre, estrato_raw, foto, url, anunciante.
    Retorna { saved, total }.
    """
    token = request.headers.get("X-Ingest-Token", "")
    if not INGEST_TOKEN or token != INGEST_TOKEN:
        return jsonify({"error": "no_autorizado"}), 401

    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "payload_vacío"}), 400

    items = data if isinstance(data, list) else [data]
    saved = 0
    for item in items:
        try:
            portal = item.pop("portal", "unknown")
            telefono = item.pop("telefono", "")
            if scraper._guardar(portal, telefono, **item):
                saved += 1
        except Exception as exc:
            log.warning("[Ingest] Error procesando item: %s", exc)

    log.info("[Ingest] %d/%d contactos guardados.", saved, len(items))
    return jsonify({"saved": saved, "total": len(items)})


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

        if event["type"] == "media":
            from app import media as media_mod
            mime = event["mime_type"]
            if mime not in media_mod.MIME_SOPORTADOS:
                from app import whatsapp as wa
                wa.send_tipo_doc_invalido(phone)
                return jsonify({"status": "received"}), 200
            ruta = media_mod.download_and_save(phone, event["media_id"], mime)
            media_mod.registrar(phone, event["media_id"], mime, ruta)
            event["ruta_local"] = ruta

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
            return {
                "type": "flow_reply",
                "response": json.loads(interactive["nfm_reply"]["response_json"]),
            }
    if msg_type == "image":
        img = message.get("image", {})
        return {
            "type": "media",
            "media_id": img.get("id", ""),
            "mime_type": img.get("mime_type", "image/jpeg"),
            "filename": "",
        }
    if msg_type == "document":
        doc = message.get("document", {})
        return {
            "type": "media",
            "media_id": doc.get("id", ""),
            "mime_type": doc.get("mime_type", "application/pdf"),
            "filename": doc.get("filename", ""),
        }
    # Tipo no manejado (audio, ubicación...) → texto vacío.
    return {"type": "text", "text": ""}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
