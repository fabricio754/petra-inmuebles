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
    Devuelve la lista de respuestas enviadas (para logging/pruebas)."""
    session = state.get_session(phone)
    conjunto = session.get("conjunto")
    respuestas = []

    if event["type"] == "flow_reply":
        # --- WHATSAPP FLOW -----------------------------------------
        respuestas.append(_procesar_flow_publicacion(phone, conjunto, event["response"]))
        return respuestas

    if event["type"] == "text":
        if session.get("flow") == "CREDITO":
            respuestas.append(_procesar_paso_credito(phone, session, event["text"]))
            return respuestas

        if session.get("flow") == "PAGO":
            respuestas.append(_procesar_paso_pago(phone, session, event["text"]))
            return respuestas

        detectado = _detectar_conjunto(event["text"])
        if not conjunto and detectado:
            state.set_session(phone, conjunto=detectado)
            respuestas.append(whatsapp.send_menu(phone, detectado))
        elif conjunto:
            # Cualquier texto libre con conjunto ya identificado reabre el menú.
            respuestas.append(whatsapp.send_menu(phone, conjunto))
        else:
            respuestas.append(whatsapp.send_text(
                phone,
                "Hola 👋 Soy Massi. Escanea el código QR de tu conjunto para ver su inventario.",
            ))
        return respuestas

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

    # Cualquier otro evento no reconocido: fallback silencioso a texto libre.
    respuestas.append(whatsapp.send_text(phone, "No entendí eso. Escribe *menu* para ver las opciones."))
    return respuestas
