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
# Envío de mensajes interactivos (CTA URL) via Kapso.
# ============================================================================

def send_cta_url(to, body, display_text, url, header=None, footer=None,
                 phone_number_id_override=None):
    """Envía un mensaje interactivo con UN botón Call-to-Action que abre una
    URL externa (ej. wa.me del bot principal). Kapso proxea el formato de
    Meta Cloud API tal cual.

    display_text ≤ 20 chars oficialmente. url ≤ 2000 chars.
    """
    base, api_key, phone_id, version = _config()
    if not (base and api_key):
        _LOG.error("[kapso] falta KAPSO_API_KEY o KAPSO_BASE_URL")
        return {"ok": False, "status": 0, "body": "config_faltante"}
    phone_id = phone_number_id_override or phone_id
    if not phone_id:
        return {"ok": False, "status": 0, "body": "phone_id_faltante"}

    to_clean = str(to).strip().lstrip("+")
    inter = {
        "type": "cta_url",
        "body": {"text": str(body)[:1024]},
        "action": {
            "name": "cta_url",
            "parameters": {
                "display_text": str(display_text)[:30],
                "url": str(url)[:2000],
            },
        },
    }
    if header:
        inter["header"] = {"type": "text", "text": str(header)[:60]}
    if footer:
        inter["footer"] = {"text": str(footer)[:60]}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_clean,
        "type": "interactive",
        "interactive": inter,
    }
    headers = {"Content-Type": "application/json", "X-API-Key": api_key}
    url_api = _url(phone_id, version)
    try:
        r = requests.post(url_api, headers=headers,
                          data=json.dumps(payload), timeout=(5, 15))
    except requests.RequestException as e:
        _LOG.exception("[kapso] send_cta_url POST fallo: %s", e)
        return {"ok": False, "status": 0, "body": f"request_error:{type(e).__name__}"}
    ok = 200 <= r.status_code < 300
    try:
        body_resp = r.json()
    except Exception:
        body_resp = r.text[:500]
    message_id = None
    if ok and isinstance(body_resp, dict):
        msgs = body_resp.get("messages") or []
        if msgs and isinstance(msgs[0], dict):
            message_id = msgs[0].get("id")
    if not ok:
        _LOG.error("[kapso] send_cta_url fallo to=%s status=%s body=%s",
                   to_clean, r.status_code, body_resp)
    return {"ok": ok, "status": r.status_code,
            "body": body_resp, "message_id": message_id}


def _marcar_estado_bienvenida(wamid, estado_meta, error_text=None):
    """Actualiza el estado de una bienvenida tracked en
    petra_bienvenidas_enviadas cuando llega un status event de Meta/Kapso.

    Mapeo: sent/delivered/read → ese mismo nombre. failed → fallido.
    Solo pisa estado si el nuevo es "más avanzado" (por prioridad), para
    que un 'delivered' tardío no sobreescriba un 'read' ya marcado.
    """
    if not wamid or not estado_meta:
        return
    prio = {"enviado": 0, "sent": 1, "delivered": 2, "read": 3,
            "clickeado": 4, "fallido": 5}
    mapping = {"sent": "sent", "delivered": "delivered",
               "read": "read", "failed": "fallido"}
    nuevo = mapping.get(estado_meta.lower())
    if not nuevo:
        return
    col_ts = {"sent": "sent_at", "delivered": "delivered_at",
              "read": "read_at", "fallido": "failed_at"}[nuevo]
    from app import db as _db
    with _db._conexion_directa(timeout=5) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT estado FROM petra_bienvenidas_enviadas WHERE wamid = %s",
            (wamid,),
        )
        row = cur.fetchone()
        if not row:
            return
        actual = row[0] or "enviado"
        # No pisar si el nuevo tiene menor prioridad.
        if prio.get(nuevo, -1) < prio.get(actual, -1):
            # Pero sí actualizar el timestamp del nuevo si estaba vacío.
            cur.execute(
                f"UPDATE petra_bienvenidas_enviadas "
                f"   SET {col_ts} = COALESCE({col_ts}, NOW()) "
                f" WHERE wamid = %s",
                (wamid,),
            )
        else:
            if nuevo == "fallido":
                cur.execute(
                    "UPDATE petra_bienvenidas_enviadas "
                    "   SET estado = %s, failed_at = COALESCE(failed_at, NOW()), "
                    "       error_detalle = %s "
                    " WHERE wamid = %s",
                    (nuevo, (error_text or "")[:500], wamid),
                )
            else:
                cur.execute(
                    f"UPDATE petra_bienvenidas_enviadas "
                    f"   SET estado = %s, {col_ts} = COALESCE({col_ts}, NOW()) "
                    f" WHERE wamid = %s",
                    (nuevo, wamid),
                )
        conn.commit()


def _marcar_bienvenida_clickeada(wamid):
    """Marca estado='clickeado' en petra_bienvenidas_enviadas para ese wamid."""
    if not wamid:
        return
    from app import db as _db
    try:
        with _db._conexion_directa(timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE petra_bienvenidas_enviadas "
                "   SET estado = 'clickeado', "
                "       clicked_at = COALESCE(clicked_at, NOW()) "
                " WHERE wamid = %s",
                (wamid,),
            )
            conn.commit()
    except Exception as e:
        _LOG.warning("[kapso] marcar_bienvenida_clickeada fallo: %s", e)


def _responder_ver_publicacion(telefono, context_wamid):
    """Handler del click en el botón 'Ver mi publicación' del template de
    bienvenida. Busca el conjunto/inmueble asociado al wamid del template
    original y responde con un CTA que lleva a wa.me del bot principal
    con mensaje precargado.
    """
    from app import db as _db
    import urllib.parse as _up

    PETRA_BOT_PHONE = (os.environ.get("PETRA_BOT_WA_PHONE") or "573001278631").strip()

    # 1) Lookup por wamid (si vino en el context)
    conjunto = None
    try:
        with _db._conexion_directa(timeout=5) as conn, conn.cursor() as cur:
            if context_wamid:
                cur.execute(
                    "SELECT c.codigo, c.nombre, c.ciudad "
                    "  FROM petra_bienvenidas_enviadas b "
                    "  JOIN conjuntos_visitante c ON c.id = b.conjunto_id "
                    " WHERE b.wamid = %s LIMIT 1",
                    (context_wamid,),
                )
                row = cur.fetchone()
                if row:
                    conjunto = {"codigo": row[0], "nombre": row[1], "ciudad": row[2]}
            # 2) Fallback: inmueble más reciente del propietario.
            # No filtramos por activo porque el propietario puede tener
            # inmuebles pausados pero igual querer ver su publicación.
            if not conjunto:
                cur.execute(
                    "SELECT c.codigo, c.nombre, c.ciudad "
                    "  FROM inmuebles_visitante i "
                    "  JOIN conjuntos_visitante c ON c.id = i.conjunto_id "
                    " WHERE regexp_replace(i.propietario_telefono,'[^0-9]','','g') = %s "
                    " ORDER BY i.creado_en DESC LIMIT 1",
                    (''.join(ch for ch in (telefono or '') if ch.isdigit()),),
                )
                row = cur.fetchone()
                if row:
                    conjunto = {"codigo": row[0], "nombre": row[1], "ciudad": row[2]}
    except Exception as e:
        _LOG.exception("[kapso] lookup bienvenida fallo: %s", e)
        return {"ok": False, "motivo": "lookup_fallo"}

    if not conjunto:
        _LOG.info("[kapso] no encontre inmueble para tel=%s wamid=%s",
                  telefono, context_wamid)
        return {"ok": False, "motivo": "sin_inmueble"}

    texto_precargado = (
        f"Estoy en el edificio {conjunto['nombre']} en "
        f"{conjunto.get('ciudad') or 'Colombia'}. Cod: {conjunto['codigo']}"
    )
    link = (f"https://wa.me/{PETRA_BOT_PHONE}?"
            f"text={_up.quote(texto_precargado)}")
    body = (
        f"📸 Tu publicación de {conjunto['nombre']}\n"
        "está lista para que la veas.\n\n"
        "Recuerda que en nuestro asistente podrás:\n\n"
        f"👀  Explorar arriendos y ventas de {conjunto['nombre']}\n"
        "📋  Publicar otro inmueble"
    )
    return send_cta_url(
        to=telefono,
        body=body,
        display_text="🏠 Ver mi publicación",
        url=link,
    )


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
                    # Sincronizar estado en petra_bienvenidas_enviadas (si es
                    # una bienvenida tracked, el UPDATE la actualiza; si no, no-op).
                    try:
                        _marcar_estado_bienvenida(target_id, estado, error_text)
                    except Exception as e:
                        log.warning("[kapso] marcar_estado_bienvenida fallo: %s", e)
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
            # log_mensaje async: no bloqueamos el webhook con el write a DB.
            import threading as _th
            def _log_in_bg(_tel, _tipo, _resumen, _payload, _wamid):
                try:
                    _db.log_mensaje(_tel, "IN", f"kapso_{_tipo}",
                                     _resumen[:4000],
                                     payload=_payload, wa_msg_id=_wamid)
                except Exception as e:
                    log.exception("[kapso] log_mensaje IN fallo: %s", e)
            _th.Thread(target=_log_in_bg,
                       args=(telefono, tipo, resumen, payload, wa_msg_id),
                       daemon=True).start()

            # Handler del click en el botón "Ver mi publicación" del template
            # de bienvenida. Kapso lo entrega de 3 formas según el tipo de
            # cliente:
            #   1. type=button con button.{text,payload}     (templates)
            #   2. type=interactive con button_reply         (interactivos)
            #   3. type=text con body="<label>" + context.id (algunos clientes
            #      rebajan el botón a texto). El context.id apuntando al
            #      wamid del template es la señal de que fue un click, no
            #      un mensaje libre.
            try:
                boton_label = ""
                context_wamid = None
                ctx = msg.get("context") or kapso_meta.get("context") or {}
                if isinstance(ctx, dict):
                    context_wamid = ctx.get("id") or ctx.get("message_id")
                # 1. type=button (template quick reply)
                btn = msg.get("button") or {}
                if btn:
                    boton_label = (btn.get("text") or btn.get("payload") or "").strip()
                # 2. type=interactive button_reply
                inter = msg.get("interactive") or {}
                if inter.get("type") == "button_reply":
                    br = inter.get("button_reply") or {}
                    boton_label = (br.get("title") or br.get("id") or "").strip()
                # 3. type=text con context: cliente que degradó el botón.
                if not boton_label and context_wamid:
                    txt = (msg.get("text") or {}).get("body") or ""
                    boton_label = txt.strip()

                if boton_label == "Ver mi publicación":
                    log.info("[kapso] click 'Ver mi publicación' tel=%s ctx=%s",
                             telefono, context_wamid)
                    # Marcar clickeado en la tabla de tracking (para que la
                    # columna de estado en el panel lo muestre).
                    if context_wamid:
                        try:
                            _marcar_bienvenida_clickeada(context_wamid)
                        except Exception:
                            log.exception("[kapso] marcar clickeada fallo")
                    # Background: libera el webhook (200 OK a Kapso al
                    # instante) y el POST del CTA corre en paralelo. Ahorra
                    # ~1s de latencia percibida.
                    _th.Thread(
                        target=_responder_ver_publicacion,
                        args=(telefono, context_wamid),
                        daemon=True,
                    ).start()
            except Exception as e:
                log.exception("[kapso] handler ver_publicacion fallo: %s", e)

            return {"ok": True, "accion": "insert_in", "wa_msg_id": wa_msg_id}

        return {"ok": True, "accion": "no_op", "motivo": "evento_sin_direction_ni_status"}

    except Exception as e:
        log.exception("[kapso] procesar_webhook fallo: %s", e)
        return {"ok": False, "accion": "excepcion", "error": str(e)}
