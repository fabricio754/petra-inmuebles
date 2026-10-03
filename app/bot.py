"""Máquina de estados de Massi: bot de crédito con garantía hipotecaria
(aliado financiero: Sureti). Toda la lógica de negocio vive acá, separada
de cómo llega el mensaje (server.py/Meta) o de cómo se prueba
(scripts/simulate.py).

Flujo (docs/PLAN.md, Hito 6):
  AUTORIZACION (Ley 1581) → 4 preguntas de descarte → 4 datos → pipeline.
Con los Flows configurados (META_FLOW_REQUISITOS_ID y META_FLOW_DATOS_ID) las
preguntas van en dos formularios de WhatsApp; si no, se hacen por chat.
"""
from app import state, whatsapp

# Palabras que en cualquier momento significan "no quiero más mensajes".
OPT_OUT_KEYWORDS = {"STOP", "BAJA", "SALIR", "PARA"}

PASOS = [
    "AUTORIZACION",     # Ley 1581: tratamiento y envío a Sureti
    "DESC_HIPOTECA",    # Sí → descartado
    "DESC_PATRIMONIO",  # Sí → descartado
    "DESC_EDAD",        # Sí → descartado
    "DESC_PAZSALVO",    # No → pausado (remarketing a 30 días); Sí → sigue
    "DATOS_NOMBRE",
    "DATOS_CEDULA",
    "DATOS_CORREO",
    "DATOS_DIRECCION",
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
    "DATOS_NOMBRE": "Perfecto. ¿Cuál es el nombre completo del propietario?",
    "DATOS_CEDULA": "¿Cuál es el número de cédula del propietario? (solo números)",
    "DATOS_CORREO": "¿Cuál es tu correo electrónico?",
    "DATOS_DIRECCION": "¿Cuál es la dirección del inmueble? (calle, número, barrio y ciudad)",
}

CAMPO_DATO = {
    "DATOS_NOMBRE": "nombre",
    "DATOS_CEDULA": "cedula",
    "DATOS_CORREO": "email",
    "DATOS_DIRECCION": "direccion_inmueble",
}

# IDs de la lista de ciudades del formulario de datos (flows/credito_datos.json).
CIUDADES = {
    "BOGOTA": "Bogotá", "MEDELLIN": "Medellín", "BARRANQUILLA": "Barranquilla",
    "CARTAGENA": "Cartagena", "SANTA_MARTA": "Santa Marta", "CUCUTA": "Cúcuta", "OTRA": "Otra",
}

SI = {"SI", "SÍ", "S", "ACEPTO", "BOTON_SI"}
NO = {"NO", "N", "NO ACEPTO", "BOTON_NO"}


def _respuesta(event):
    """Texto normalizado de un mensaje o de un botón."""
    if event["type"] == "button_reply":
        return event["id"].strip().upper()
    if event["type"] == "text":
        return event["text"].strip().upper().rstrip(".!")
    return ""


def _terminar(phone, **extra):
    state.set_session(phone, flow=None, flow_step=None, flow_data=None, **extra)


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
    _guardar(phone, data, "nuevo")
    _terminar(phone)
    nombre = (data.get("nombre") or "").split()
    return whatsapp.send_confirmacion_pipeline(phone, nombre[0] if nombre else "")


def _procesar(phone, session, event):
    step = session.get("flow_step") or "AUTORIZACION"
    data = dict(session.get("flow_data") or {})
    resp = _respuesta(event)

    # --- Autorización de datos ------------------------------------------
    if step == "AUTORIZACION":
        if resp in SI:
            data["autorizacion_en"] = state.now_iso()
            state.set_no_contactar(phone, False)
            if whatsapp.USE_FLOWS:
                state.set_session(phone, flow_step="FORM_REQUISITOS", flow_data=data)
                return whatsapp.send_form_requisitos(phone)
            state.set_session(phone, flow_step="DESC_HIPOTECA", flow_data=data)
            return whatsapp.send_pregunta_si_no(phone, PREGUNTAS_DESCARTE["DESC_HIPOTECA"])
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
        for paso, campo in (("DESC_HIPOTECA", "hipoteca"), ("DESC_PATRIMONIO", "patrimonio"),
                            ("DESC_EDAD", "edad")):
            if str(r.get(campo)).upper() == "SI":
                return _descartar(phone, data, MOTIVO_DESCARTE[paso])
        if str(r.get("paz_salvo")).upper() != "SI":
            return _pausar(phone, data)
        data["requiere_paz_salvo"] = True
        state.set_session(phone, flow_step="FORM_DATOS", flow_data=data)
        return whatsapp.send_form_datos(phone)

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
            direccion_inmueble=", ".join(
                x for x in (str(r.get("direccion") or "").strip(), str(r.get("barrio") or "").strip()) if x
            ),
            ciudad=CIUDADES.get(str(r.get("ciudad") or "").upper(), str(r.get("ciudad") or "")),
        )
        return _completar(phone, data)

    # --- Preguntas de descarte (por chat, si no hay formularios) ---------
    if step in PREGUNTAS_DESCARTE:
        if resp not in SI and resp not in NO:
            return whatsapp.send_pregunta_si_no(phone, PREGUNTAS_DESCARTE[step])
        dijo_si = resp in SI

        if step == "DESC_PAZSALVO":
            if not dijo_si:
                return _pausar(phone, data)
            data["requiere_paz_salvo"] = True
            state.set_session(phone, flow_step="DATOS_NOMBRE", flow_data=data)
            return whatsapp.send_text(phone, PREGUNTAS_DATOS["DATOS_NOMBRE"])

        if dijo_si:
            return _descartar(phone, data, MOTIVO_DESCARTE[step])

        siguiente = PASOS[PASOS.index(step) + 1]
        state.set_session(phone, flow_step=siguiente, flow_data=data)
        return whatsapp.send_pregunta_si_no(phone, PREGUNTAS_DESCARTE[siguiente])

    # --- Datos básicos ---------------------------------------------------
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

        data[CAMPO_DATO[step]] = valor
        siguiente = PASOS[PASOS.index(step) + 1] if step != PASOS[-1] else None
        if siguiente:
            state.set_session(phone, flow_step=siguiente, flow_data=data)
            return whatsapp.send_text(phone, PREGUNTAS_DATOS[siguiente])

        return _completar(phone, data)

    # Paso desconocido (sesión vieja): empezar de nuevo.
    return _iniciar(phone)


def handle_incoming(phone, event):
    """event: {"type": "text", "text": str}
             | {"type": "button_reply", "id": str}
             | {"type": "list_reply", "id": str}
             | {"type": "flow_reply", "response": dict}
    Devuelve la lista de respuestas enviadas (para logging/pruebas)."""
    session = state.get_session(phone)
    en_flujo = session.get("flow") == "SURETI"
    resp = _respuesta(event)

    # Salir en cualquier momento (STOP, BAJA, SALIR, PARA).
    if event["type"] == "text" and resp in OPT_OUT_KEYWORDS:
        state.set_no_contactar(phone, True)
        _terminar(phone)
        return [whatsapp.send_no_contactar(phone)]

    if en_flujo:
        return [_procesar(phone, session, event)]

    # Sin conversación activa. "NO" como primera respuesta (p. ej. a nuestro
    # mensaje de apertura) también es salir.
    if event["type"] == "text" and resp in NO:
        state.set_no_contactar(phone, True)
        return [whatsapp.send_no_contactar(phone)]

    # Cualquier otro mensaje (también de alguien que antes dijo NO y vuelve
    # a escribir por su cuenta) empieza el flujo desde la autorización.
    return [_iniciar(phone)]
