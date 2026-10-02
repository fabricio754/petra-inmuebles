"""Máquina de estados de Petra. Toda la lógica de negocio vive acá,
separada de cómo llega el mensaje (server.py/Meta) o de cómo se prueba
(scripts/simulate.py). Esto es lo único "programado" del prototipo:
todo lo demás (menú, catálogo, formularios) es contenido, no lógica.
"""
from app import state, whatsapp

# Conjuntos que Petra reconoce. Hoy solo Arrayanes (piloto). La palabra
# clave es la que va pre-cargada en el link/QR: wa.me/<numero>?text=ARRAYANES
CONJUNTOS_ACTIVOS = ["ARRAYANES"]

OPCIONES_EN_PREPARACION = {}

# === OPT-OUT UNIVERSAL =======================================================
# Palabras que en cualquier momento significan "no quiero más mensajes".
# "NO" solo en AUTORIZACION (primer paso del flujo) y como primer mensaje sin
# contexto activo (respuesta al outreach del scraper).
_OPT_OUT_KEYWORDS = {"STOP", "BAJA", "PARA", "SALIR"}


def _es_opt_out_global(texto, tiene_flow, tiene_conjunto):
    t = texto.strip().upper()
    if t in _OPT_OUT_KEYWORDS:
        return True
    # "NO" como respuesta a nuestro primer contacto (sin flujo activo)
    if t == "NO" and not tiene_flow and not tiene_conjunto:
        return True
    return False

def _detectar_conjunto(texto):
    texto_upper = texto.strip().upper()
    for conjunto in CONJUNTOS_ACTIVOS:
        if conjunto in texto_upper:
            return conjunto
    return None


def _buscar_apto(apto_id):
    for item in state.load_inventario():
        if item["id"] == apto_id:
            return item
    return None


def _listar_disponibles(conjunto, operacion):
    return [
        item for item in state.load_inventario()
        if item["conjunto"] == conjunto
        and item["operacion"] == operacion
        and item["disponibilidad"] == "DISPONIBLE"
    ]


# === WHATSAPP FLOW -- formulario nativo "Publicar mi inmueble" =============

def _iniciar_publicacion_flow(phone):
    return whatsapp.send_flow_publicar(phone)


def _procesar_flow_publicacion(phone, conjunto, response):
    """Recibe la respuesta ya parseada del webhook nfm_reply (ver
    server.py:_to_event, event type "flow_reply"): arma flow_data y pide
    la autorización del propietario (send_autorizacion_propietario). Los botones
    AUTORIZO_PUBLICAR / NO_AUTORIZO_PUBLICAR de más abajo no cambian."""
    def _numero(valor):
        # El Flow puede mandar número (68 / 68.5) o texto ("2.500.000").
        if isinstance(valor, (int, float)):
            return int(valor)
        limpio = str(valor).strip().replace(".", "").replace(",", "")
        return int(limpio) if limpio.isdigit() else 0

    apto_numero = str(response.get("apto", "")).strip()
    data = {
        "apto_numero": apto_numero,
        "apartamento": f"Apto {apto_numero}",
        "habitaciones": _numero(response.get("habitaciones")),
        "banos": _numero(response.get("banos")),
        "m2": _numero(response.get("area")),
        "precio": _numero(response.get("precio")),
        "operacion": str(response.get("operacion", "")).strip().upper(),
    }

    state.set_session(phone, flow="AUTORIZACION", flow_data=data)
    precio_fmt = f"${data['precio']:,.0f}".replace(",", ".")
    resumen = (
        f"{data['apartamento']} · {data['habitaciones']} hab · {data['banos']} baños · "
        f"{data['m2']} m² · {precio_fmt} · {data['operacion'].title()}"
    )
    return whatsapp.send_autorizacion_propietario(phone, resumen)
# === FIN WHATSAPP FLOW ======================================================


# === FLUJO SURETI — Bot de crédito con garantía hipotecaria (Hito 6) ========
# Reemplaza el flujo dummy CREDITO. El lead calificado queda en `pipeline`.

_SURETI_STEPS = [
    "AUTORIZACION",    # Pedir consentimiento Ley 1581
    "DESC_HIPOTECA",   # ¿Hipoteca/embargo? SI = descarta
    "DESC_PATRIMONIO", # ¿Patrimonio de familia con menores? SI = descarta
    "DESC_EDAD",       # ¿Propietario > 75 años? SI = descarta
    "DESC_PAZSALVO",   # ¿Puede ponerse al día? NO = requiere_paz_salvo
    "DATOS_NOMBRE",
    "DATOS_CEDULA",
    "DATOS_CORREO",
    "DATOS_DIRECCION",
]

_DESCARTE_PREGUNTAS = {
    "DESC_HIPOTECA":   "¿Tu inmueble tiene hipoteca o embargo activo?",
    "DESC_PATRIMONIO": "¿El inmueble tiene patrimonio de familia con menores de edad?",
    "DESC_EDAD":       "¿El propietario del inmueble tiene más de 75 años?",
    "DESC_PAZSALVO":   "¿Puedes ponerte al día con predial, servicios y administración?",
}

_DATOS_PREGUNTAS = {
    "DATOS_NOMBRE":    "¿Cuál es tu nombre completo?",
    "DATOS_CEDULA":    "¿Cuál es tu número de cédula? (solo números)",
    "DATOS_CORREO":    "¿Cuál es tu correo electrónico?",
    "DATOS_DIRECCION": "Confirma la dirección del inmueble (calle, número, barrio, ciudad):",
}

_MOTIVO_DESCARTE = {
    "DESC_HIPOTECA":   "HIPOTECA",
    "DESC_PATRIMONIO": "PATRIMONIO",
    "DESC_EDAD":       "EDAD",
}


def _es_si(texto):
    return texto.strip().upper() in {"SI", "SÍ", "S", "YES"}


def _es_no(texto):
    return texto.strip().upper() in {"NO", "N"}


def _iniciar_sureti(phone):
    state.set_session(phone, flow="SURETI", flow_step="AUTORIZACION", flow_data={})
    return whatsapp.send_autorizacion_datos(phone)


def _procesar_sureti(phone, session, event):
    """Procesa un evento (text o button_reply) dentro del flujo SURETI."""
    step = session.get("flow_step", "AUTORIZACION")
    data = dict(session.get("flow_data") or {})

    if event["type"] == "text":
        texto = event["text"].strip()
    elif event["type"] == "button_reply":
        texto = event["id"]  # "SURETI_SI" o "SURETI_NO"
    else:
        return whatsapp.send_text(phone, "Responde con las opciones que te mostramos.")

    # Opt-out universal dentro del flujo
    if event["type"] == "text" and texto.strip().upper() in _OPT_OUT_KEYWORDS:
        state.mark_no_contactar(phone)
        state.set_session(phone, flow=None, flow_step=None, flow_data=None)
        return whatsapp.send_no_contactar(phone)

    # --- AUTORIZACION -------------------------------------------------------
    if step == "AUTORIZACION":
        if _es_si(texto):
            data["autorizacion_en"] = state.now_iso()
            state.set_session(phone, flow_step="DESC_HIPOTECA", flow_data=data)
            return whatsapp.send_pregunta_si_no(phone, _DESCARTE_PREGUNTAS["DESC_HIPOTECA"])
        # NO o cualquier otra respuesta = opt-out
        state.mark_no_contactar(phone)
        state.set_session(phone, flow=None, flow_step=None, flow_data=None)
        return whatsapp.send_no_contactar(phone)

    # --- DESCARTE -----------------------------------------------------------
    if step in _DESCARTE_PREGUNTAS:
        if event["type"] == "button_reply":
            respondio_si = (texto == "SURETI_SI")
        elif _es_si(texto):
            respondio_si = True
        elif _es_no(texto):
            respondio_si = False
        else:
            return whatsapp.send_pregunta_si_no(phone, _DESCARTE_PREGUNTAS[step])

        if step == "DESC_PAZSALVO":
            # Puede ponerse al día → no requiere paz salvo; No puede → requiere
            data["requiere_paz_salvo"] = not respondio_si
            state.set_session(phone, flow_step="DATOS_NOMBRE", flow_data=data)
            return whatsapp.send_text(phone, _DATOS_PREGUNTAS["DATOS_NOMBRE"])

        if respondio_si:
            motivo = _MOTIVO_DESCARTE[step]
            state.set_session(phone, flow=None, flow_step=None, flow_data=None)
            return whatsapp.send_no_califica(phone, motivo)

        # "No" al problema → continuar al siguiente paso
        siguiente = _SURETI_STEPS[_SURETI_STEPS.index(step) + 1]
        state.set_session(phone, flow_step=siguiente, flow_data=data)
        return whatsapp.send_pregunta_si_no(phone, _DESCARTE_PREGUNTAS[siguiente])

    # --- DATOS --------------------------------------------------------------
    if step in _DATOS_PREGUNTAS:
        if event["type"] != "text":
            return whatsapp.send_text(phone, _DATOS_PREGUNTAS[step])

        valor = texto.strip()

        if step == "DATOS_CEDULA":
            limpio = valor.replace(".", "").replace("-", "").replace(" ", "")
            if not limpio.isdigit() or len(limpio) < 6:
                return whatsapp.send_text(
                    phone, "Ingresa solo los números de tu cédula. " + _DATOS_PREGUNTAS[step]
                )
            valor = limpio

        if step == "DATOS_CORREO":
            partes = valor.split("@")
            if len(partes) != 2 or "." not in partes[1]:
                return whatsapp.send_text(
                    phone, "Eso no parece un correo válido. " + _DATOS_PREGUNTAS[step]
                )

        _clave = {
            "DATOS_NOMBRE":    "nombre",
            "DATOS_CEDULA":    "cedula",
            "DATOS_CORREO":    "email",
            "DATOS_DIRECCION": "direccion_inmueble",
        }[step]
        data[_clave] = valor

        siguiente_idx = _SURETI_STEPS.index(step) + 1
        if siguiente_idx < len(_SURETI_STEPS):
            siguiente = _SURETI_STEPS[siguiente_idx]
            state.set_session(phone, flow_step=siguiente, flow_data=data)
            return whatsapp.send_text(phone, _DATOS_PREGUNTAS[siguiente])

        # Último dato recibido → guardar en pipeline y confirmar
        state.save_pipeline({
            "telefono": phone,
            "nombre": data.get("nombre"),
            "cedula": data.get("cedula"),
            "email": data.get("email"),
            "direccion_inmueble": data.get("direccion_inmueble"),
            "requiere_paz_salvo": data.get("requiere_paz_salvo", False),
            "autorizacion_datos_en": data.get("autorizacion_en"),
            "estado": "NUEVO",
        })
        state.set_session(phone, flow=None, flow_step=None, flow_data=None)
        nombre_corto = (data.get("nombre") or "").split()[0]
        return whatsapp.send_confirmacion_pipeline(phone, nombre_corto)

    # Paso no reconocido: reiniciar
    state.set_session(phone, flow=None, flow_step=None, flow_data=None)
    return whatsapp.send_text(phone, "Algo falló. Escribe *menu* para reintentar.")

# === FIN FLUJO SURETI ========================================================


# === PILOTO CRÉDITO (dummy) =================================================
# Sin proveedor real todavía: junta 3 datos por texto y guarda el lead.
# Punto de conexión a un proveedor real de crédito: justo donde termina
# _procesar_paso_credito, en vez de solo guardar el lead y confirmar.
CREDITO_STEPS = ["MONTO", "MOTIVO", "PROPIETARIO"]

CREDITO_PREGUNTAS = {
    "MONTO": "¿Cuánto crédito necesitas? (solo el número, ej: 20000000)",
    "MOTIVO": "¿Para qué necesitas el crédito?",
    "PROPIETARIO": "¿Eres propietario de un inmueble en Arrayanes que puedas dejar en garantía? Responde SI o NO.",
}


def _iniciar_credito(phone):
    state.set_session(phone, flow="CREDITO", flow_step="MONTO", flow_data={})
    return whatsapp.send_text(phone, CREDITO_PREGUNTAS["MONTO"])


def _procesar_paso_credito(phone, session, texto):
    step = session["flow_step"]
    data = session.get("flow_data", {})
    valor = texto.strip()

    if step == "MONTO":
        limpio = valor.replace(".", "").replace(",", "")
        if not limpio.isdigit():
            return whatsapp.send_text(phone, "Ese valor debe ser un número. " + CREDITO_PREGUNTAS[step])
        valor = int(limpio)

    if step == "PROPIETARIO":
        valor_upper = valor.upper()
        if valor_upper not in ("SI", "SÍ", "NO"):
            return whatsapp.send_text(phone, "Responde SI o NO.")
        valor = valor_upper in ("SI", "SÍ")

    data[step.lower()] = valor

    siguiente_index = CREDITO_STEPS.index(step) + 1
    if siguiente_index < len(CREDITO_STEPS):
        siguiente_step = CREDITO_STEPS[siguiente_index]
        state.set_session(phone, flow_step=siguiente_step, flow_data=data)
        return whatsapp.send_text(phone, CREDITO_PREGUNTAS[siguiente_step])

    # Completo -> (dummy) guardar lead y confirmar. Acá se conectaría un
    # proveedor real de crédito en vez de solo guardar y avisar.
    conjunto = session.get("conjunto")
    state.set_session(phone, flow=None, flow_step=None, flow_data=None)
    state.save_lead(
        phone,
        {"conjunto": conjunto, "id": None, "apartamento": None, "operacion": "CREDITO"},
        monto=data.get("monto"),
        motivo=data.get("motivo"),
        propietario=data.get("propietario"),
    )
    return whatsapp.send_confirmacion_credito(phone)
# === FIN PILOTO CRÉDITO ======================================================


# === PILOTO PAGO DE SERVICIOS (dummy) =======================================
# Sin pasarela de pagos real todavía: junta los datos por texto y guarda el
# lead. Punto de conexión a un proveedor real de pagos: justo donde termina
# _procesar_paso_pago, en vez de solo simular el pago.
PAGO_STEPS = {
    "ADMINISTRACION": ["APTO", "MONTO"],
    "SERVICIOS": ["SERVICIO", "REFERENCIA", "MONTO"],
}

PAGO_PREGUNTAS = {
    "APTO": "¿Cuál es el número de tu apartamento?",
    "MONTO": "¿Cuál es el monto a pagar? (solo el número, ej: 350000)",
    "SERVICIO": "¿Qué servicio quieres pagar? Responde luz, agua, gas o internet.",
    "REFERENCIA": "¿Cuál es el número de cuenta o referencia de pago?",
}


def _iniciar_pago(phone, tipo):
    primer_step = PAGO_STEPS[tipo][0]
    state.set_session(phone, flow="PAGO", flow_tipo=tipo, flow_step=primer_step, flow_data={})
    return whatsapp.send_text(phone, PAGO_PREGUNTAS[primer_step])


def _procesar_paso_pago(phone, session, texto):
    tipo = session["flow_tipo"]
    steps = PAGO_STEPS[tipo]
    step = session["flow_step"]
    data = session.get("flow_data", {})
    valor = texto.strip()

    if step == "MONTO":
        limpio = valor.replace(".", "").replace(",", "")
        if not limpio.isdigit():
            return whatsapp.send_text(phone, "Ese valor debe ser un número. " + PAGO_PREGUNTAS[step])
        valor = int(limpio)

    if step == "SERVICIO":
        valor_upper = valor.upper()
        if valor_upper not in ("LUZ", "AGUA", "GAS", "INTERNET"):
            return whatsapp.send_text(phone, "Responde luz, agua, gas o internet.")
        valor = valor_upper

    data[step.lower()] = valor

    siguiente_index = steps.index(step) + 1
    if siguiente_index < len(steps):
        siguiente_step = steps[siguiente_index]
        state.set_session(phone, flow_step=siguiente_step, flow_data=data)
        return whatsapp.send_text(phone, PAGO_PREGUNTAS[siguiente_step])

    # Completo -> (dummy) guardar lead y simular el pago. Acá se conectaría
    # una pasarela de pagos real en vez de solo simular.
    conjunto = session.get("conjunto")
    state.set_session(phone, flow=None, flow_tipo=None, flow_step=None, flow_data=None)
    state.save_lead(
        phone,
        {"conjunto": conjunto, "id": None, "apartamento": data.get("apto"), "operacion": f"PAGO_{tipo}"},
        # "apto" ya va en "apartamento"; pasarlo en **extra choca con el
        # parámetro apto de save_lead (TypeError).
        **{k: v for k, v in data.items() if k != "apto"},
    )
    return whatsapp.send_confirmacion_pago(phone, data["monto"])
# === FIN PILOTO PAGO DE SERVICIOS ============================================


def handle_incoming(phone, event):
    """event: {"type": "text", "text": str}
             | {"type": "list_reply", "id": str}
             | {"type": "button_reply", "id": str}
             | {"type": "flow_reply", "response": dict}
    Devuelve la lista de respuestas enviadas (para logging/pruebas)."""
    session = state.get_session(phone)
    conjunto = session.get("conjunto")
    flow = session.get("flow")
    respuestas = []

    # --- WhatsApp Flow (formulario nativo) ----------------------------------
    if event["type"] == "flow_reply":
        respuestas.append(_procesar_flow_publicacion(phone, conjunto, event["response"]))
        return respuestas

    # --- Opt-out global (STOP/BAJA/etc. en cualquier momento) ---------------
    if event["type"] == "text":
        if _es_opt_out_global(event["text"], flow, conjunto):
            state.mark_no_contactar(phone)
            state.set_session(phone, flow=None, flow_step=None, flow_data=None)
            respuestas.append(whatsapp.send_no_contactar(phone))
            return respuestas

    # --- Flujo SURETI (texto o botones Sí/No) --------------------------------
    if flow == "SURETI":
        if event["type"] in ("text", "button_reply"):
            respuestas.append(_procesar_sureti(phone, session, event))
            return respuestas

    # --- Texto libre ---------------------------------------------------------
    if event["type"] == "text":
        if flow == "CREDITO":
            respuestas.append(_procesar_paso_credito(phone, session, event["text"]))
            return respuestas

        if flow == "PAGO":
            respuestas.append(_procesar_paso_pago(phone, session, event["text"]))
            return respuestas

        detectado = _detectar_conjunto(event["text"])
        if not conjunto and detectado:
            state.set_session(phone, conjunto=detectado)
            respuestas.append(whatsapp.send_menu(phone, detectado))
        elif conjunto:
            # Texto libre con conjunto activo → reabre el menú de Arrayanes.
            respuestas.append(whatsapp.send_menu(phone, conjunto))
        else:
            # Nuevo contacto sin contexto → inicia el flujo de crédito Sureti.
            respuestas.append(_iniciar_sureti(phone))
        return respuestas

    # --- Selección de lista (menú interactivo) --------------------------------
    if event["type"] == "list_reply":
        id_ = event["id"]

        if id_ == "MENU_ARRIENDO":
            if not conjunto:
                respuestas.append(whatsapp.send_text(phone, "Primero escanea el QR de tu conjunto."))
            else:
                listings = _listar_disponibles(conjunto, "ARRIENDO")
                respuestas.append(whatsapp.send_catalogo(phone, conjunto, "Arriendo", listings))
            return respuestas

        if id_ == "MENU_COMPRAR":
            if not conjunto:
                respuestas.append(whatsapp.send_text(phone, "Primero escanea el QR de tu conjunto."))
            else:
                listings = _listar_disponibles(conjunto, "VENTA")
                respuestas.append(whatsapp.send_catalogo(phone, conjunto, "Venta", listings))
            return respuestas

        if id_ == "MENU_PUBLICAR":
            if not conjunto:
                respuestas.append(whatsapp.send_text(phone, "Primero escanea el QR de tu conjunto."))
            else:
                respuestas.append(_iniciar_publicacion_flow(phone))
            return respuestas

        if id_ == "MENU_CREDITO":
            if not conjunto:
                respuestas.append(whatsapp.send_text(phone, "Primero escanea el QR de tu conjunto."))
            else:
                respuestas.append(_iniciar_credito(phone))
            return respuestas

        if id_ == "MENU_PAGOS":
            if not conjunto:
                respuestas.append(whatsapp.send_text(phone, "Primero escanea el QR de tu conjunto."))
            else:
                respuestas.append(whatsapp.send_menu_pagos(phone))
            return respuestas

        if id_ in ("PAGO_ADMINISTRACION", "PAGO_SERVICIOS"):
            tipo = "ADMINISTRACION" if id_ == "PAGO_ADMINISTRACION" else "SERVICIOS"
            respuestas.append(_iniciar_pago(phone, tipo))
            return respuestas

        if id_ in OPCIONES_EN_PREPARACION:
            respuestas.append(whatsapp.send_text(phone, "Estamos preparando esta opción."))
            return respuestas

        if id_.startswith("APTO_"):
            apto = _buscar_apto(id_[len("APTO_"):])
            if apto:
                respuestas.append(whatsapp.send_apto_detail(phone, apto))
            else:
                respuestas.append(whatsapp.send_text(phone, "Ese inmueble ya no está disponible."))
            return respuestas

    # --- Botones de respuesta rápida -----------------------------------------
    if event["type"] == "button_reply":
        id_ = event["id"]

        if id_ == "AUTORIZO_PUBLICAR":
            data = session.get("flow_data", {})
            nuevo_id = state.next_apto_id(conjunto, data["operacion"], data["apto_numero"])
            apto = {
                "id": nuevo_id,
                "conjunto": conjunto,
                "operacion": data["operacion"],
                "apartamento": data["apartamento"],
                "habitaciones": data["habitaciones"],
                "banos": data["banos"],
                "m2": data["m2"],
                "precio": data["precio"],
                "gestion": "PROPIETARIO",
                "disponibilidad": "DISPONIBLE",
                "propietario_telefono": phone,
                "owner_contact_authorized": True,
                "owner_contact_authorized_at": state.now_iso(),
            }
            state.save_inmueble(apto)
            state.set_session(phone, flow=None, flow_step=None, flow_data=None)
            respuestas.append(whatsapp.send_confirmacion_publicacion(phone, apto))
            return respuestas

        if id_ == "NO_AUTORIZO_PUBLICAR":
            state.set_session(phone, flow=None, flow_step=None, flow_data=None)
            respuestas.append(whatsapp.send_publicacion_rechazada(phone))
            return respuestas

        if id_.startswith("CONTACTAR_"):
            apto = _buscar_apto(id_[len("CONTACTAR_"):])
            if not apto:
                respuestas.append(whatsapp.send_text(phone, "Ese inmueble ya no está disponible."))
                return respuestas
            if apto["gestion"] == "PETRA":
                state.save_lead(phone, apto)
                respuestas.append(whatsapp.send_confirmacion_contacto(phone, apto))
            else:
                respuestas.append(whatsapp.send_confirmar_contacto_propietario(phone, apto))
            return respuestas

        if id_.startswith("CONFIRMAR_CONTACTO_"):
            apto = _buscar_apto(id_[len("CONFIRMAR_CONTACTO_"):])
            if apto:
                state.save_lead(
                    phone, apto,
                    gestion="PROPIETARIO",
                    autorizado_interesado=True,
                    autorizado_en=state.now_iso(),
                    estado="AUTORIZADO",
                )
                respuestas.append(whatsapp.send_conexion_confirmada(phone, apto))
            else:
                respuestas.append(whatsapp.send_text(phone, "Ese inmueble ya no está disponible."))
            return respuestas

        if id_.startswith("CANCELAR_CONTACTO_"):
            respuestas.append(whatsapp.send_cancelacion_contacto(phone))
            return respuestas

    respuestas.append(whatsapp.send_text(phone, "No entendí eso. Escribe *menu* para ver las opciones."))
    return respuestas
