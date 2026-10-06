"""Máquina de estados de Massi: bot de crédito con garantía hipotecaria
(aliado financiero: Sureti). Toda la lógica de negocio vive acá, separada
de cómo llega el mensaje (server.py/Meta) o de cómo se prueba
(scripts/simulate.py).

Flujo (docs/PLAN.md, Hito 6):
  AUTORIZACION (Ley 1581) → 4 preguntas de descarte → 4 datos → pipeline.
Con los Flows configurados (META_FLOW_REQUISITOS_ID y META_FLOW_DATOS_ID) las
preguntas van en dos formularios de WhatsApp; si no, se hacen por chat.
"""
import logging
import os
import re
import unicodedata

from app import filtros, state, whatsapp

_log = logging.getLogger("petra")

# Alerta externa cuando se detecta un lead caliente (pide llamada). Si no
# está seteada, solo loguea — ver `_enviar_alerta_lead_caliente`.
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "").strip()

# Deprecated: usar `filtros.es_opt_out(texto)` (incluye estas palabras + patrones
# semanticos: "no me interesa", "ya no esta disponible", "numero equivocado", ...).
# Se mantiene aqui solo como referencia; nadie debe leerlo.
OPT_OUT_KEYWORDS = {"STOP", "BAJA", "SALIR", "PARA"}

# Palabras que indican solicitud de atención humana (se buscan sin tildes).
PALABRAS_HUMANO = {"asesor", "humano", "persona real", "hablar con alguien"}

# ─────────────────────────────────────────────────────────────────────────────
# Clasificador de respuestas en texto libre a la plantilla de apertura
# ─────────────────────────────────────────────────────────────────────────────
# Varios teléfonos del lado del propietario responden en texto libre (no tocan
# los botones). El clasificador detecta tres grupos antes de responder la
# autorización de datos, para no mandar autorización en loop a bots atendedores
# ni insistir con quien ya no vende.
#
# Todos los patrones se comparan en minúsculas y sin tildes (ver _sin_tildes).

BOT_PATTERNS = [
    "gracias por tu mensaje",
    "gracias por comunicar",
    "horario de atencion",
    "lo haremos lo antes posible",
    "soy sandra",
    "agencia de cambios",
    "inmobiliaria sala",
    "atendemos de lunes",
    "fuera de nuestro horario",
    "en este momento no podemos responder",
]

NO_VENDO_PATTERNS = [
    "ya no esta disponible",
    "ya vendi",
    "ya se vendio",
    "ya fue vendido",
    "no vendo",
    "no esta en venta",
    "ya no vendo",
]

NO_INTERESADO_PATTERNS = [
    "no me interesa",
    "no gracias",
    "no quiero",
]


def _sin_tildes(s):
    """Convierte a minúsculas y elimina diacríticos para comparar sin tildes."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def _enviar_alerta_lead_caliente(phone: str, mensaje_original: str) -> None:
    """Dispara una alerta externa (webhook configurable) cuando un contacto
    pide llamada por primera vez. Nunca lanza: fallos de red/HTTP se logean y
    el handler sigue como si nada (la marca en DB ya persistió).

    `ALERT_WEBHOOK_URL` lee del env en caliente para que, si lo rota en Render,
    el próximo lead ya use el nuevo endpoint (sin reiniciar workers). Si no
    está seteada, solo logea.
    """
    url = os.environ.get("ALERT_WEBHOOK_URL", ALERT_WEBHOOK_URL).strip()
    if not url:
        _log.info("[Alerta] ALERT_WEBHOOK_URL no seteado; lead caliente %s solo loguea", phone)
        return
    try:
        import requests  # import local: no quiero bajarlo al top-level
        requests.post(
            url,
            json={
                "phone": phone,
                "motivo": "pide_llamada",
                "mensaje_original": mensaje_original,
                "fuente": "massi",
            },
            timeout=5,
        )
        _log.info("[Alerta] webhook enviado para lead caliente %s", phone)
    except Exception:
        _log.exception("[Alerta] fallo al disparar webhook para %s (no rompe handler)", phone)

PASOS = [
    "AUTORIZACION",     # Ley 1581: tratamiento y envío a Sureti
    "DESC_HIPOTECA",    # Sí → descartado
    "DESC_PATRIMONIO",  # Sí → descartado
    "DESC_EDAD",        # Sí → descartado
    "DESC_PAZSALVO",    # No → pausado (remarketing a 30 días); Sí → sigue
    # Datos del inmueble:
    "DATOS_DIRECCION",
    "DATOS_ESTRATO",    # texto: número 1-6
    "DATOS_ES_PH",      # botón sí/no
    # Datos del propietario:
    "DATOS_NOMBRE",
    "DATOS_CEDULA",
    "DATOS_CORREO",
    "DATOS_OBJETIVO",   # lista interactiva
    "DATOS_EDAD_PROP",  # texto: edad del propietario
    # Nota: DATOS_TIPO y DATOS_VALOR se auto-rellenan desde contactos.
]

PREGUNTAS_DESCARTE = {
    "DESC_HIPOTECA": "¿El inmueble tiene hipoteca o embargo vigente?",
    "DESC_PATRIMONIO": "¿El inmueble tiene patrimonio de familia con hijos menores de edad?",
    "DESC_EDAD": "¿El propietario del inmueble tiene más de 75 años?",
    "DESC_PAZSALVO": (
        "¿Estás dispuesto a ponerte al día con predial, servicios y "
        "administración para avanzar con el crédito?"
    ),
}

# Pasos en los que responder "Sí" descarta el inmueble.
MOTIVO_DESCARTE = {
    "DESC_HIPOTECA": "HIPOTECA",
    "DESC_PATRIMONIO": "PATRIMONIO",
    "DESC_EDAD": "EDAD",
}

PREGUNTAS_DATOS = {
    "DATOS_NOMBRE": "¿Cuál es el nombre completo del propietario?",
    "DATOS_CEDULA": "¿Cuál es el número de cédula? (solo números)",
    "DATOS_CORREO": "¿Cuál es tu correo electrónico?",
    "DATOS_DIRECCION": "¿Cuál es la dirección del inmueble? (calle, número, barrio y ciudad)",
    "DATOS_ESTRATO": "¿Cuál es el estrato del inmueble? (1 al 6)",
    "DATOS_EDAD_PROP": "¿Cuántos años tiene el propietario?",
}

CAMPO_DATO = {
    "DATOS_NOMBRE": "nombre",
    "DATOS_CEDULA": "cedula",
    "DATOS_CORREO": "email",
    "DATOS_DIRECCION": "direccion_inmueble",
    "DATOS_ESTRATO": "estrato",
    "DATOS_EDAD_PROP": "edad",
}

OPCIONES_TIPO = {
    "TIPO_CASA": "casa",
    "TIPO_APTO": "apartamento",
    "TIPO_LOCAL": "local",
    "TIPO_OFICINA": "oficina",
    "TIPO_LOTE": "lote",
    "TIPO_BODEGA": "bodega",
}

OPCIONES_OBJETIVO = {
    "OBJ_CAPITAL": "capital de trabajo",
    "OBJ_DEUDAS": "pagar deudas",
    "OBJ_INVERSION": "inversión",
    "OBJ_GASTOS": "gastos personales",
    "OBJ_OTRO": "otro",
}

# IDs de la lista de ciudades del formulario de datos (flows/credito_datos.json).
CIUDADES = {
    "BOGOTA": "Bogotá", "MEDELLIN": "Medellín", "BARRANQUILLA": "Barranquilla",
    "CARTAGENA": "Cartagena", "SANTA_MARTA": "Santa Marta", "CUCUTA": "Cúcuta", "OTRA": "Otra",
}

SI = {"SI", "SÍ", "S", "ACEPTO", "BOTON_SI"}
NO = {"NO", "N", "NO ACEPTO", "BOTON_NO"}


def _respuesta(event):
    """Texto normalizado de un mensaje, botón o selección de lista."""
    if event["type"] in ("button_reply", "list_reply"):
        return event["id"].strip().upper()
    if event["type"] in ("text", "template_button"):
        return event["text"].strip().upper().rstrip(".!")
    return ""


def _terminar(phone, **extra):
    state.set_session(phone, flow=None, flow_step=None, flow_data=None, **extra)


# Residencial: hasta 40 % del avalúo. Comercial: hasta 30 %.
_TIPOS_RESIDENCIAL = {"apartamento", "casa", "parqueadero", "habitacion"}


def _calcular_monto_estimado(data, avaluo_raw):
    """El Flow pide el avalúo EN MILLONES (más fácil para el usuario en el
    teclado de un celular). Guardamos el avalúo en pesos y devolvemos el
    monto estimado también en millones (int) o None si no se pudo leer."""
    import re as _re
    digitos = _re.sub(r"\D", "", str(avaluo_raw or ""))
    if not digitos:
        return None
    try:
        avaluo_m = int(digitos)
    except ValueError:
        return None
    data["avaluo_comercial"] = avaluo_m * 1_000_000  # a pesos para la DB
    tipo = str(data.get("tipo_inmueble") or data.get("tipo") or "").lower()
    pct = 0.40 if tipo in _TIPOS_RESIDENCIAL else 0.30
    return int(avaluo_m * pct)


def _iniciar(phone):
    state.set_session(phone, flow="SURETI", flow_step="AUTORIZACION", flow_data={})
    return whatsapp.send_autorizacion_datos(phone)


def _guardar(phone, data, estado):
    state.save_pipeline({
        "telefono": phone,
        "nombre": data.get("nombre"),
        "cedula": data.get("cedula"),
        "email": data.get("email"),
        "direccion_inmueble": data.get("direccion_inmueble"),
        "ciudad": data.get("ciudad"),
        "tipo_inmueble": data.get("tipo_inmueble"),
        "estrato": data.get("estrato"),
        "es_ph": data.get("es_ph"),
        "objetivo_prestamo": data.get("objetivo_prestamo"),
        "valor_solicitado": data.get("valor_solicitado"),
        "edad": data.get("edad"),
        "requiere_paz_salvo": data.get("requiere_paz_salvo", False),
        "autorizacion_datos_en": data.get("autorizacion_en"),
        "estado": estado,
    })


def _limpiar_cedula(valor):
    valor = str(valor or "").strip()
    if valor.endswith(".0"):  # un campo numérico puede llegar como 1020304050.0
        valor = valor[:-2]
    valor = valor.replace(".", "").replace("-", "").replace(" ", "")
    return valor if valor.isdigit() and 5 <= len(valor) <= 12 else ""


def _correo_valido(valor):
    usuario, _, dominio = valor.partition("@")
    return bool(usuario) and "." in dominio and " " not in valor


def _ubicacion(r):
    """Dirección, apto y barrio en un solo texto + ciudad, desde un formulario."""
    partes = [str(r.get(c) or "").strip() for c in ("direccion", "apto", "barrio")]
    ciudad = str(r.get("ciudad") or "")
    return {
        "direccion_inmueble": ", ".join(p for p in partes if p),
        "ciudad": CIUDADES.get(ciudad.upper(), ciudad),
    }


def _descartar(phone, data, motivo):
    _guardar(phone, data, f"descartado_{motivo.lower()}")
    _terminar(phone)
    return whatsapp.send_no_califica(phone, motivo)


def _pausar(phone, data):
    # No está dispuesto a ponerse al día por ahora: se le vuelve a escribir
    # en 30 días (Hito 10).
    _guardar(phone, data, "pausado_paz_salvo")
    _terminar(phone)
    return whatsapp.send_pausa_paz_salvo(phone)


def _completar(phone, data):
    # Auto-rellenar tipo y monto desde la tabla contactos si no vienen en el form.
    try:
        from app import db as _db
        contacto = _db.get_contacto(phone)
        if contacto:
            if not data.get("tipo_inmueble") and contacto.get("tipo_inmueble"):
                data["tipo_inmueble"] = contacto["tipo_inmueble"]
            if not data.get("valor_solicitado") and contacto.get("monto_hasta"):
                data["valor_solicitado"] = int(contacto["monto_hasta"]) * 1_000_000
    except Exception:
        import logging
        logging.getLogger("petra").exception("[COMPLETAR] No se pudo leer contacto, se ignora.")

    _guardar(phone, data, "nuevo")

    # Consulta CHIP catastral (solo Bogotá; para el resto continúa sin él).
    try:
        from app import docs_auto, db as _db2
        direccion = data.get("direccion_inmueble") or ""
        ciudad    = data.get("ciudad") or ""
        chip = docs_auto.obtener_chip(direccion, ciudad)
        if chip:
            _db2.update_chip(phone, chip)
    except Exception:
        import logging
        logging.getLogger("petra").exception("[CHIP] Error en _completar, se ignora.")

    nombre = (data.get("nombre") or "").split()
    state.set_session(phone, flow="SURETI", flow_step="DOCS_EXTRACTOS", flow_data=data)
    whatsapp.send_confirmacion_pipeline(phone, nombre[0] if nombre else "")
    return whatsapp.send_pedir_extractos(phone)


def _procesar_extracto(phone, session, event):
    """Guarda un extracto bancario recibido y lleva la cuenta."""
    data = dict(session.get("flow_data") or {})
    media_id = event.get("media_id", "")

    if media_id:
        try:
            from app import db as _db
            _db.registrar_documento(phone, "extracto", media_id, None)
        except Exception:
            import logging
            logging.getLogger("petra").exception("[EXTRACTO] Error guardando documento.")

    # Contar extractos guardados
    count = data.get("extractos_count", 0) + (1 if media_id else 0)
    data["extractos_count"] = count
    state.set_session(phone, flow_step="DOCS_EXTRACTOS", flow_data=data)

    if count >= 3:
        whatsapp.send_extractos_completos(phone)
        state.set_session(phone, flow_step="DOCS_CTL", flow_data=data)
        return whatsapp.send_solicitud_ctl(phone)
    return whatsapp.send_extracto_recibido(phone, count)


def _procesar_ctl(phone, session, event):
    """Guarda el CTL recibido y confirma que la solicitud está completa para Sureti."""
    media_id = event.get("media_id", "")

    if media_id:
        try:
            from app import db as _db
            _db.registrar_documento(phone, "ctl", media_id, None)
        except Exception:
            import logging
            logging.getLogger("petra").exception("[CTL] Error guardando documento.")

    _terminar(phone)
    return whatsapp.send_solicitud_enviada(phone)


def _enviar_paso_inicial(phone, paso):
    """Envía el mensaje de apertura de un paso interactivo (lista o sí/no)."""
    if paso == "DATOS_ES_PH":
        return whatsapp.send_pregunta_si_no(phone, "¿El inmueble está en propiedad horizontal? (conjunto, edificio, etc.)")
    if paso == "DATOS_OBJETIVO":
        return whatsapp.send_objetivo_prestamo(phone)
    return None


def _procesar(phone, session, event):
    step = session.get("flow_step") or "AUTORIZACION"
    data = dict(session.get("flow_data") or {})
    resp = _respuesta(event)

    # --- Autorización de datos ------------------------------------------
    if step == "AUTORIZACION":
        _log.info("[Bot] AUTORIZACION phone=%s resp=%r", phone, resp)
        if resp in SI:
            try:
                data["autorizacion_en"] = state.now_iso()
                _log.info("[Bot] AUTORIZACION SI — guardando no_contactar=False")
                state.set_no_contactar(phone, False)
                if whatsapp.USE_FLOWS:
                    _log.info("[Bot] USE_FLOWS=True — enviando FORM_REQUISITOS")
                    state.set_session(phone, flow_step="FORM_REQUISITOS", flow_data=data)
                    return whatsapp.send_form_requisitos(phone)
                _log.info("[Bot] USE_FLOWS=False — enviando DESC_HIPOTECA")
                state.set_session(phone, flow_step="DESC_HIPOTECA", flow_data=data)
                return whatsapp.send_pregunta_si_no(phone, PREGUNTAS_DESCARTE["DESC_HIPOTECA"])
            except Exception:
                _log.exception("[Bot] ERROR en AUTORIZACION SI para %s", phone)
                raise
        if resp in NO:
            state.set_no_contactar(phone, True)
            _terminar(phone)
            return whatsapp.send_no_contactar(phone)
        # Cualquier otra cosa ("¿quién eres?"): volver a preguntar.
        return whatsapp.send_autorizacion_datos(phone, repetir=True)

    # --- Formularios de WhatsApp (Flows) --------------------------------
    if step == "FORM_REQUISITOS":
        r = event.get("response") if event["type"] == "flow_reply" else None
        if not r or "hipoteca" not in r:
            return whatsapp.send_form_requisitos(phone, repetir=True)
        data.update(_ubicacion(r))
        for paso, campo in (("DESC_HIPOTECA", "hipoteca"), ("DESC_PATRIMONIO", "patrimonio"),
                            ("DESC_EDAD", "edad")):
            if str(r.get(campo)).upper() == "SI":
                return _descartar(phone, data, MOTIVO_DESCARTE[paso])
        # "No sé" en patrimonio: no descarta, pero marcamos para verificar
        # con escrituras / CTL antes de enviar a Sureti.
        if str(r.get("patrimonio", "")).upper() == "NO_SE":
            data["patrimonio_verificar"] = True
        if str(r.get("paz_salvo")).upper() != "SI":
            return _pausar(phone, data)
        data["requiere_paz_salvo"] = True
        # Estrato y PH pueden venir en FORM_REQUISITOS (campo nuevo del form).
        if r.get("estrato"):
            try:
                data["estrato"] = int(str(r["estrato"]).strip())
            except (ValueError, TypeError):
                pass
        if "es_ph" in r:
            data["es_ph"] = str(r["es_ph"]).upper() == "SI"
        # Avalúo que declara el propietario → calculamos monto estimado.
        monto_estimado_m = _calcular_monto_estimado(data, r.get("avaluo"))
        state.set_session(phone, flow_step="FORM_DATOS", flow_data=data)
        return whatsapp.send_form_datos(phone, monto_estimado_m=monto_estimado_m)

    if step == "FORM_DATOS":
        r = event.get("response") if event["type"] == "flow_reply" else None
        if not r or "nombre" not in r:
            return whatsapp.send_form_datos(phone, repetir=True)
        cedula = _limpiar_cedula(r.get("cedula"))
        email = str(r.get("email") or "").strip().lower()
        if not cedula or not _correo_valido(email):
            whatsapp.send_text(phone, "Revisa la cédula (solo números) y el correo, por favor.")
            return whatsapp.send_form_datos(phone, repetir=True)
        data.update(
            nombre=str(r.get("nombre") or "").strip(),
            cedula=cedula,
            email=email,
            tipo_persona=str(r.get("tipo_persona") or "NATURAL").upper(),
        )
        if r.get("direccion"):  # versión anterior del formulario de datos
            data.update(_ubicacion(r))
        # Objetivo y edad pueden venir en FORM_DATOS (campos nuevos del form).
        if r.get("objetivo"):
            data["objetivo_prestamo"] = OPCIONES_OBJETIVO.get(str(r["objetivo"]).upper(), str(r["objetivo"]))
        if r.get("edad"):
            try:
                data["edad"] = int(str(r["edad"]).strip())
            except (ValueError, TypeError):
                pass
        # Si ya tenemos todos los campos del inmueble, completar directamente.
        if data.get("estrato") is not None and data.get("es_ph") is not None:
            return _completar(phone, data)
        # Sino, pedir estrato/PH por chat.
        state.set_session(phone, flow_step="DATOS_ESTRATO", flow_data=data)
        return whatsapp.send_text(phone, PREGUNTAS_DATOS["DATOS_ESTRATO"])

    # --- Preguntas de descarte (por chat, si no hay formularios) ---------
    if step in PREGUNTAS_DESCARTE:
        if resp not in SI and resp not in NO:
            return whatsapp.send_pregunta_si_no(phone, PREGUNTAS_DESCARTE[step])
        dijo_si = resp in SI

        if step == "DESC_PAZSALVO":
            if not dijo_si:
                return _pausar(phone, data)
            data["requiere_paz_salvo"] = True
            state.set_session(phone, flow_step="DATOS_DIRECCION", flow_data=data)
            return whatsapp.send_text(phone, PREGUNTAS_DATOS["DATOS_DIRECCION"])

        if dijo_si:
            return _descartar(phone, data, MOTIVO_DESCARTE[step])

        siguiente = PASOS[PASOS.index(step) + 1]
        state.set_session(phone, flow_step=siguiente, flow_data=data)
        return whatsapp.send_pregunta_si_no(phone, PREGUNTAS_DESCARTE[siguiente])

    # --- Datos básicos (texto libre) ------------------------------------
    if step in PREGUNTAS_DATOS:
        if event["type"] != "text" or not event["text"].strip():
            return whatsapp.send_text(phone, PREGUNTAS_DATOS[step])
        valor = event["text"].strip()

        if step == "DATOS_CEDULA":
            valor = _limpiar_cedula(valor)
            if not valor:
                return whatsapp.send_text(
                    phone, "Escribe solo los números de la cédula. " + PREGUNTAS_DATOS[step]
                )

        if step == "DATOS_CORREO":
            valor = valor.lower()
            if not _correo_valido(valor):
                return whatsapp.send_text(
                    phone, "Eso no parece un correo válido. " + PREGUNTAS_DATOS[step]
                )

        if step == "DATOS_ESTRATO":
            digitos = re.sub(r"\D", "", valor)
            try:
                estrato = int(digitos)
                assert 1 <= estrato <= 6
                valor = estrato
            except (ValueError, AssertionError):
                return whatsapp.send_text(
                    phone, "Escribe un número del 1 al 6. " + PREGUNTAS_DATOS[step]
                )

        if step == "DATOS_EDAD_PROP":
            digitos = re.sub(r"\D", "", valor)
            try:
                edad = int(digitos)
                assert 18 <= edad <= 99
                valor = edad
            except (ValueError, AssertionError):
                return whatsapp.send_text(
                    phone, "Escribe la edad en años (entre 18 y 99). " + PREGUNTAS_DATOS[step]
                )

        data[CAMPO_DATO[step]] = valor
        idx = PASOS.index(step)
        siguiente = PASOS[idx + 1] if idx + 1 < len(PASOS) else None

        if siguiente is None:
            return _completar(phone, data)

        state.set_session(phone, flow_step=siguiente, flow_data=data)
        if siguiente in PREGUNTAS_DATOS:
            return whatsapp.send_text(phone, PREGUNTAS_DATOS[siguiente])
        return _enviar_paso_inicial(phone, siguiente)

    # --- Pasos interactivos (Sureti: PH, objetivo) ----------------
    if step == "DATOS_ES_PH":
        if resp not in SI and resp not in NO:
            return whatsapp.send_pregunta_si_no(
                phone, "¿El inmueble está en propiedad horizontal? (conjunto, edificio, etc.)"
            )
        data["es_ph"] = resp in SI
        state.set_session(phone, flow_step="DATOS_NOMBRE", flow_data=data)
        return whatsapp.send_text(phone, PREGUNTAS_DATOS["DATOS_NOMBRE"])

    if step == "DATOS_OBJETIVO":
        objetivo = OPCIONES_OBJETIVO.get(resp) if event["type"] == "list_reply" else None
        if not objetivo:
            return whatsapp.send_objetivo_prestamo(phone)
        data["objetivo_prestamo"] = objetivo
        state.set_session(phone, flow_step="DATOS_EDAD_PROP", flow_data=data)
        return whatsapp.send_text(phone, PREGUNTAS_DATOS["DATOS_EDAD_PROP"])

    # --- Recolección de extractos bancarios ---------------------------------
    if step == "DOCS_EXTRACTOS":
        if resp in {"LISTO", "YA", "ENVIADOS", "LISTO.", "YA.", "YA LOS ENVIE", "YA LOS ENVIÉ"}:
            whatsapp.send_extractos_completos(phone)
            state.set_session(phone, flow_step="DOCS_CTL", flow_data=data)
            return whatsapp.send_solicitud_ctl(phone)
        return whatsapp.send_pedir_extractos(phone, recordar=True)

    # --- Espera del CTL del inmueble ----------------------------------------
    if step == "DOCS_CTL":
        # El usuario envió texto en lugar de un PDF: recordarle qué se espera.
        return whatsapp.send_solicitud_ctl(phone)

    # Paso desconocido (sesión vieja): empezar de nuevo.
    return _iniciar(phone)


def _procesar_media(phone, event):
    """Descarga el documento, lo parsea como predial y responde al cliente."""
    from app import parser as _parser, db as _db
    ruta = event.get("ruta_local")
    mime = event.get("mime_type", "")
    avaluo = direccion = matricula = None

    if ruta:
        try:
            datos = _parser.parsear_predial(ruta, mime)
            avaluo = datos.get("avaluo")
            direccion = datos.get("direccion", "")
            matricula = datos.get("matricula", "")
            if avaluo:
                _db.update_avaluo(phone, avaluo, direccion, matricula)
        except Exception:
            import logging
            logging.getLogger("petra").exception("[Media] Error parseando predial.")

    return whatsapp.send_doc_recibido(phone, avaluo=avaluo, direccion=direccion, matricula=matricula)


def handle_incoming(phone, event):
    """event: {"type": "text", "text": str}
             | {"type": "button_reply", "id": str}
             | {"type": "template_button", "text": str}
             | {"type": "list_reply", "id": str}
             | {"type": "flow_reply", "response": dict}
             | {"type": "media", "media_id": str, "mime_type": str}
             | {"type": "media_invalido"}
    Devuelve la lista de respuestas enviadas (para logging/pruebas)."""
    # B3: tras un opt-out (no_contactar=TRUE) el bot debe quedarse mudo.
    # Antes seguía respondiendo (loop de consent, "no entendí", etc). Esto
    # va lo más temprano posible para que ninguna rama de abajo mande OUTs.
    try:
        from app import db as _db
        if _db.esta_bloqueado(phone):
            _log.info("[Bot] IN de %s con no_contactar=true, ignorando", phone)
            return []
    except Exception:
        _log.exception("[Bot] Error consultando no_contactar, continuando normal")

    if event["type"] == "media_invalido":
        return [whatsapp.send_tipo_doc_invalido(phone)]

    session = state.get_session(phone)
    en_flujo = session.get("flow") == "SURETI"

    # Extractos bancarios esperados (paso DOCS_EXTRACTOS del flujo Sureti).
    if event["type"] == "media" and en_flujo and session.get("flow_step") == "DOCS_EXTRACTOS":
        return [_procesar_extracto(phone, session, event)]

    # CTL del inmueble (paso DOCS_CTL del flujo Sureti).
    if event["type"] == "media" and en_flujo and session.get("flow_step") == "DOCS_CTL":
        return [_procesar_ctl(phone, session, event)]

    # Documento genérico (predial) enviado por el vendedor.
    if event["type"] == "media":
        _procesar_media(phone, event)
        return []

    resp = _respuesta(event)

    # Botones de la plantilla de apertura (Hito 7).
    if event["type"] == "template_button":
        if "NO ME INTERESA" in event["text"].upper():
            state.marcar_respuesta(phone, "no_interesa")
            state.set_no_contactar(phone, True)
            _terminar(phone)
            return [whatsapp.send_no_contactar(phone)]
        state.marcar_respuesta(phone, "interesado")
        return [_iniciar(phone)]

    # Broker / inmobiliaria / auto-reply del otro lado: marcamos no_contactar
    # y dejamos la conversacion muerta (sin responder nada).
    if event["type"] == "text" and filtros.es_broker(event["text"]):
        _log.info("[Bot] broker detectado por patron, no respondemos: phone=%s", phone)
        state.set_no_contactar(phone, True)
        _terminar(phone)
        return []

    # Pide llamada / quiere hablar con un humano: detectar ANTES de opt-out y
    # broker. Es la señal más caliente que podemos recibir — el piloto 6-oct
    # (lead 573508463133) la mandó tres veces y el bot le siguió contestando
    # consent. Marcamos requiere_humano en `contactos` (idempotente), disparamos
    # alerta externa solo si es la primera vez, y respondemos UNA sola vez que
    # un asesor va a contactar. No seguimos con el flujo.
    if event["type"] == "text" and filtros.es_pedido_llamada(event["text"]):
        texto = event["text"]
        try:
            from app import db as _db
            marco_nuevo = _db.marcar_requiere_humano(
                phone, f"pidió llamada: {texto[:200]}"
            )
        except Exception:
            _log.exception("[Bot] Error marcando requiere_humano para %s", phone)
            marco_nuevo = False
        _log.info("[Bot] Pide llamada detectado: %s (nuevo=%s)", phone, marco_nuevo)
        if marco_nuevo:
            _enviar_alerta_lead_caliente(phone, texto)
        return [whatsapp.send_text(
            phone,
            "¡Entendido! Un asesor te contactará pronto por este mismo chat.",
        )]

    # Salir en cualquier momento: STOP/BAJA/SALIR/PARA + opt-outs semanticos
    # ("no me interesa", "ya no esta disponible", "numero equivocado", ...).
    if event["type"] == "text" and filtros.es_opt_out(event["text"]):
        state.set_no_contactar(phone, True)
        _terminar(phone)
        return [whatsapp.send_no_contactar(phone)]

    # Solicitud de asesor humano (en cualquier estado).
    if event["type"] == "text":
        texto_norm = _sin_tildes(event["text"].lower())
        if any(p in texto_norm for p in PALABRAS_HUMANO):
            try:
                from app import db as _db
                _db.marcar_requiere_humano(phone, f"palabra_humano: {event['text'][:200]}")
            except Exception:
                import logging
                logging.getLogger("petra").exception("[HUMANO] Error marcando requiere_humano.")
            return [whatsapp.send_human_handoff(phone)]

    _log.info("[Bot] handle_incoming phone=%s type=%s en_flujo=%s flow_step=%s", phone, event.get("type"), en_flujo, session.get("flow_step"))
    if en_flujo:
        return [_procesar(phone, session, event)]

    # B1: BOTON_SI (click en "Sí/Autorizo") con sesión expirada o inexistente.
    # Antes caía a `_iniciar()` o a "no entendí" tras varias horas. Ahora
    # reconstruimos el estado desde `contactos` y mandamos el consent /
    # FORM_REQUISITOS que correspondería después de autorizar.
    if event["type"] == "button_reply" and resp in SI:
        _log.info("[Bot] BOTON_SI sin flujo activo — reconstruyendo estado phone=%s", phone)
        return [_reconstruir_sesion_para_boton_si(phone)]

    # Sin conversación activa. "NO" como primera respuesta (p. ej. a nuestro
    # mensaje de apertura) también es salir.
    if event["type"] == "text" and resp in NO:
        state.set_no_contactar(phone, True)
        return [whatsapp.send_no_contactar(phone)]

    # BOTON_NO tardío sin sesión activa: si el usuario clickeó "No" después de
    # que la sesión expiró (o nunca existió), antes caía a `_iniciar()` y le
    # reenviaba la plantilla de apertura → loop. Ahora marcamos opt-out y
    # mandamos UNA sola confirmación corta.
    if event["type"] == "button_reply" and resp in NO:
        _log.info("[Bot] BOTON_NO sin sesión → opt-out para %s", phone)
        state.set_no_contactar(phone, True)
        _terminar(phone)
        return [whatsapp.send_text(
            phone,
            "Entendido, no te volveremos a contactar. Gracias por avisarnos.",
        )]

    # Clasificador de texto libre sin flujo activo (ver BOT_PATTERNS, etc).
    # Evita mandar autorización en loop a bots atendedores y despide con
    # elegancia a quien ya vendió o no quiere más mensajes.
    if event["type"] == "text":
        return _clasificar_texto_libre(phone, session, event["text"])

    # Cualquier otro mensaje (también de alguien que antes dijo NO y vuelve
    # a escribir por su cuenta) empieza el flujo desde la autorización.
    return [_iniciar(phone)]


def _reconstruir_sesion_para_boton_si(phone):
    """B1: llega BOTON_SI y no hay sesión activa (expiró o nunca existió).

    Reconstruimos un estado mínimo "ya autorizó" con los datos conocidos
    del contacto (ciudad/tipo/monto) y mandamos el FORM_REQUISITOS
    (o la primera pregunta por chat si USE_FLOWS=False), para no responder
    "no entendí" a un click explícito de interés.
    """
    data = {"autorizacion_en": state.now_iso()}
    try:
        from app import db as _db
        contacto = _db.get_contacto(phone)
        if contacto:
            if contacto.get("tipo_inmueble"):
                data["tipo_inmueble"] = contacto["tipo_inmueble"]
            if contacto.get("monto_hasta"):
                try:
                    data["valor_solicitado"] = int(contacto["monto_hasta"]) * 1_000_000
                except (TypeError, ValueError):
                    pass
    except Exception:
        _log.exception("[Bot] Error leyendo contacto para reconstruir BOTON_SI phone=%s", phone)

    # Si el contacto existe pero quedó con no_contactar=False (post-SI anterior),
    # no hace falta reafirmarlo acá. Si no existía, lo deja limpio.
    try:
        state.set_no_contactar(phone, False)
    except Exception:
        _log.exception("[Bot] Error reseteando no_contactar en reconstrucción phone=%s", phone)

    if whatsapp.USE_FLOWS:
        state.set_session(phone, flow="SURETI", flow_step="FORM_REQUISITOS", flow_data=data)
        return whatsapp.send_form_requisitos(phone)
    state.set_session(phone, flow="SURETI", flow_step="DESC_HIPOTECA", flow_data=data)
    return whatsapp.send_pregunta_si_no(phone, PREGUNTAS_DESCARTE["DESC_HIPOTECA"])


def _clasificar_texto_libre(phone, session, texto):
    """Clasifica texto libre recibido sin flujo activo.

    Devuelve siempre una lista de respuestas (posiblemente vacía) para
    homogeneizar con handle_incoming.
    """
    texto_norm = _sin_tildes((texto or "").lower())

    # 1) Bots atendedores: opt-out silencioso.
    if any(p in texto_norm for p in BOT_PATTERNS):
        _log.info("[Clasificador] BOT detectado phone=%s texto=%r", phone, texto)
        state.set_no_contactar(phone, True)
        _terminar(phone)
        return []

    # 2) "Ya vendí / no vendo": opt-out con despedida amable.
    if any(p in texto_norm for p in NO_VENDO_PATTERNS):
        _log.info("[Clasificador] NO VENDE detectado phone=%s texto=%r", phone, texto)
        state.set_no_contactar(phone, True)
        _terminar(phone)
        return [whatsapp.send_text(
            phone,
            "Entendido 🙌 Cuando lo pongas en venta y necesites crédito con "
            "tu inmueble como garantía, escríbenos aquí.",
        )]

    # 3) "No me interesa" en texto libre: mismo tratamiento que el botón.
    if any(p in texto_norm for p in NO_INTERESADO_PATTERNS):
        _log.info("[Clasificador] NO INTERESADO detectado phone=%s texto=%r", phone, texto)
        state.set_no_contactar(phone, True)
        _terminar(phone)
        return [whatsapp.send_no_contactar(phone)]

    # 4) Texto libre no clasificable: re-enviar plantilla la primera vez;
    #    a partir de la segunda, pasar a atención humana.
    veces = int(session.get("veces_mensaje_libre") or 0)
    veces += 1
    state.set_session(phone, veces_mensaje_libre=veces)
    _log.info("[Clasificador] texto libre sin match phone=%s veces=%d", phone, veces)

    if veces >= 2:
        try:
            from app import db as _db
            _db.marcar_requiere_humano(
                phone, f"texto_libre_sin_match x{veces}: {texto[:200]}"
            )
        except Exception:
            logging.getLogger("petra").exception(
                "[Clasificador] Error marcando requiere_humano."
            )
        return [whatsapp.send_text(
            phone, "Gracias por escribir, un asesor te contactará pronto 👋"
        )]

    # Primera vez: re-enviar la plantilla de apertura.
    try:
        return [whatsapp.send_plantilla_apertura(phone)]
    except Exception:
        logging.getLogger("petra").exception(
            "[Clasificador] Error reenviando plantilla de apertura; cae a autorización."
        )
        return [_iniciar(phone)]
