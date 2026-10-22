"""Tools de la fase INSCRIPCIÓN del flow de agente Petra.

Expone:
  - TOOLS: lista de definiciones (shape Anthropic) para pasar al LLM
  - HANDLERS: dict nombre→callable(args, contexto) que ejecuta la acción
  - SYSTEM_PROMPT: función que arma el prompt con el estado actual
  - procesar(telefono, texto, cid, meta, conn): entry-point que llama al LLM

El `contexto` que reciben los handlers tiene:
  - telefono (str), cid (int), conn (DB), meta (dict mutable)
Los handlers mutan `meta` y guardan en DB vía _save_meta.
"""
import json
import logging
import re

log = logging.getLogger("petra")

CAMPOS = ["nombre", "cedula", "ciudad", "barrio", "conjunto", "torre_apto"]
NOMBRE_CAMPO = {
    "nombre": "nombre completo",
    "cedula": "cédula",
    "ciudad": "ciudad",
    "barrio": "barrio",
    "conjunto": "conjunto / edificio",
    "torre_apto": "torre y apto / casa",
}

# Pregunta templateada por campo (editable en petra_textos vía clave).
# Mantiene el bot predecible y evita que el LLM alucine qué pedir.
PREGUNTA_CAMPO = {
    "nombre": ("inscripcion.pedir_nombre",
               "¿Cuál es tu *nombre completo*? (nombre y apellido) 👤"),
    "cedula": ("inscripcion.pedir_cedula",
               "¿Cuál es tu *cédula*? (solo los números) 🪪"),
    "ciudad": ("inscripcion.pedir_ciudad",
               "¿En qué *ciudad* vivís? 🏙️"),
    "barrio": ("inscripcion.pedir_barrio",
               "¿En qué *barrio* vivís? 📍"),
    "conjunto": ("inscripcion.pedir_conjunto",
                 "¿Cómo se llama tu *conjunto o edificio*? 🏢"),
    "torre_apto": ("inscripcion.pedir_torre_apto",
                   "Último dato — ¿En qué *torre y apartamento* vivís? "
                   "(ej. _Torre 3 apto 702_, _Casa 49_, _Apto 15B_) 🏠"),
}

# Mensaje de confirmación cuando capturamos un dato. {valor} = lo guardado.
CONFIRMACION_CAMPO = {
    "nombre": ("inscripcion.ok_nombre", "✅ Perfecto, {valor}."),
    "cedula": ("inscripcion.ok_cedula", "✅ Perfecto, guardé tu cédula."),
    "ciudad": ("inscripcion.ok_ciudad", "✅ Perfecto, tu ciudad es {valor}."),
    "barrio": ("inscripcion.ok_barrio", "✅ Anotado el barrio {valor}."),
    "conjunto": ("inscripcion.ok_conjunto", "✅ Anotado {valor}."),
    "torre_apto": ("inscripcion.ok_torre_apto", "✅ Anotado: {valor}."),
}

# Mensaje de rechazo cuando el dato no es válido.
RECHAZO_CAMPO = {
    "nombre": "No me quedó claro tu nombre. Mandámelo como *Juana Pérez* (nombre + apellido) 🙏",
    "cedula": "La cédula tiene que ser solo números, entre 6 y 12 dígitos. Mandámela de nuevo 🙏",
    "ciudad": "No me quedó claro cuál es tu ciudad. Decímelo con una palabra (ej. *Bogotá*) 🙏",
    "barrio": "No me quedó claro cuál es tu barrio. Mandámelo como el nombre nomás (ej. *Chapinero*) 🙏",
    "conjunto": "No me quedó claro el nombre del conjunto. Mandámelo como aparece en la entrada (ej. *Torres del Parque*) 🙏",
    "torre_apto": "No me quedó claro. Mandámelo como *Torre 3 apto 702*, *Casa 49* o *Apto 15B* 🙏",
}


# ===== Definición de tools (schema Anthropic) =====

TOOLS = [
    {
        "name": "capturar_datos_personales",
        "description": (
            "Guarda uno o más datos personales de la agente que te está "
            "escribiendo. Usar cuando la agente te da nombre, cédula, "
            "ciudad, barrio o nombre del conjunto/edificio. Podés pasar "
            "varios campos a la vez si vinieron juntos. NO INVENTES "
            "valores: solo guardá lo que ella explícitamente te dijo. "
            "Si lo que te dio no parece un valor válido (ej. una pregunta, "
            "'ya te lo dije', emoji, un '?'), NO llamés esta tool: pedí "
            "aclaración por texto."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre + apellido completo (ej. 'Juana Pérez García')"},
                "cedula": {"type": "string", "description": "Solo dígitos, 6-12 caracteres (ej. '1020800837')"},
                "ciudad": {"type": "string", "description": "Ciudad de Colombia (ej. 'Bogotá', 'Zipaquirá')"},
                "barrio": {"type": "string", "description": "Barrio dentro de la ciudad (ej. 'Chapinero', 'San Pablo')"},
                "conjunto": {"type": "string", "description": "Nombre del conjunto residencial o edificio donde vive (ej. 'Edificio Palermo', 'Condominio Monaco')"},
                "torre_apto": {"type": "string", "description": "Torre y apartamento o número de casa, en un solo string libre (ej. 'Torre 3 apto 702', 'Casa 49', 'Apto 15B'). NO desglosar, pasalo tal cual lo mandó."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "confirmar_registro",
        "description": (
            "Marca a la agente como oficialmente registrada en Petra. "
            "Solo llamar cuando ella CLARAMENTE confirma — pulsó el botón "
            "'Registrarme' O escribió explícitamente 'sí, confirmo' / "
            "'registrame' / 'está todo bien'. NUNCA llamés esta tool si "
            "faltan datos (verificá que estén los 5 campos antes)."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "mostrar_resumen_datos",
        "description": (
            "Envía a la agente un resumen con los 5 datos capturados + "
            "botones 'Registrarme / Corregir / Cancelar'. Llamá esto "
            "cuando ella pide ver sus datos antes de confirmar, O cuando "
            "capturaste todos los campos por primera vez."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "corregir_dato",
        "description": (
            "Borra UN dato específico para que la agente lo re-ingrese. "
            "Usar cuando ella pide corregir, cambiar o editar un campo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "campo": {
                    "type": "string",
                    "enum": CAMPOS,
                    "description": "Qué campo borrar",
                },
            },
            "required": ["campo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancelar_inscripcion",
        "description": (
            "Cancela la inscripción y borra el rol de agente. Usar SOLO "
            "cuando ella explícitamente dice 'cancelar', 'no quiero', "
            "'olvidalo'. No usar ante dudas o preguntas."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "escalar_a_humano",
        "description": (
            "Marca que la agente necesita atención humana. Usar si: la "
            "agente tiene un problema que no podés resolver, pide hablar "
            "con alguien real, o la conversación se traba. El motivo "
            "debe ser corto y específico."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {"type": "string", "description": "Qué pasó, en 1 línea"},
            },
            "required": ["motivo"],
            "additionalProperties": False,
        },
    },
]


# ===== Validadores por campo =====

_VAL_SOSPECHOSOS = ("?", "ya te", "ya te la", "ya te lo", "ya les", "cuanto",
                    "cuándo", "cómo", "dónde", "donde", "disculp", "olvid",
                    "no sé", "no se", "mandame", "mandáme", "escribí")


def _parece_sospechoso(valor):
    """True si el valor parece una pregunta o protesta, no un dato real."""
    if not valor or not isinstance(valor, str):
        return True
    v = valor.strip().lower()
    if not v or v in ("si", "no", "ok", "listo"):
        return True
    if len(v) < 2 or len(v) > 120:
        return True
    for s in _VAL_SOSPECHOSOS:
        if s in v:
            return True
    return False


def _validar_campo(campo, valor):
    """Devuelve (valido, valor_limpio, error_msg)."""
    if not valor or not isinstance(valor, str):
        return False, None, "vacío"
    v = valor.strip()
    if _parece_sospechoso(v):
        return False, None, f"no parece un {NOMBRE_CAMPO[campo]} válido"
    if campo == "cedula":
        soloDigitos = re.sub(r"\D", "", v)
        if not (6 <= len(soloDigitos) <= 12):
            return False, None, "la cédula debe tener entre 6 y 12 dígitos"
        return True, soloDigitos, None
    if campo == "nombre":
        if " " not in v:
            return False, None, "falta apellido"
        return True, v[:100], None
    return True, v[:100], None


# ===== Handlers =====

def _save_meta(conn, cid, meta):
    conn.execute(
        "UPDATE petra_contacto_roles SET metadata=%s::jsonb, updated_at=NOW() "
        "WHERE contacto_id=%s AND rol='agente_petra'",
        (json.dumps(meta), cid),
    )


def handle_capturar_datos(args, ctx):
    """Guarda los campos pasados en args, rechaza los sospechosos, y
    responde al user con un mensaje templateado (confirmación + próxima
    pregunta). El LLM NO escribe texto en este turno — así evitamos
    alucinaciones (ej. re-pedir cédula ya guardada, listar campos de más)."""
    from app import petra_wa
    from app.petra_agents.textos import t as _t
    meta = ctx["meta"]
    guardados = {}
    rechazados = {}
    for campo in CAMPOS:
        if campo not in args:
            continue
        ok, limpio, err = _validar_campo(campo, args[campo])
        if ok:
            meta[campo] = limpio
            guardados[campo] = limpio
        else:
            rechazados[campo] = err
    if guardados:
        _save_meta(ctx["conn"], ctx["cid"], meta)
    faltantes = [c for c in CAMPOS if not meta.get(c)]

    # Si no capturamos ni rechazamos nada (args vacío o campos desconocidos),
    # dejamos que el LLM responda — el user no mandó un dato real.
    if not guardados and not rechazados:
        return {"guardados": {}, "rechazados": {}, "faltantes": faltantes,
                "ya_guardados": [c for c in CAMPOS if meta.get(c)],
                "proximo_a_pedir": NOMBRE_CAMPO[faltantes[0]] if faltantes else None,
                "todos_completos": not faltantes,
                "nota": "no se guardo nada — explicá al user qué necesitás"}

    # Armamos el mensaje: 1 línea por guardado + 1 línea por rechazado +
    # pregunta por el próximo campo pendiente.
    partes = []
    for campo, valor in guardados.items():
        clave, fallback = CONFIRMACION_CAMPO[campo]
        partes.append(_t(clave, fallback=fallback, valor=valor))
    for campo, _err in rechazados.items():
        partes.append(RECHAZO_CAMPO[campo])

    if faltantes:
        prox = faltantes[0]
        clave, fallback = PREGUNTA_CAMPO[prox]
        # Si no hay guardados pero sí rechazados, no agregamos la pregunta
        # (ya está pedida en el mensaje de rechazo). Si hay guardados,
        # pedimos el próximo campo pendiente.
        if guardados:
            partes.append(_t(clave, fallback=fallback))
        mensaje = "\n\n".join(partes)
        try:
            petra_wa.send_text(ctx["telefono"], mensaje)
        except Exception:
            log.exception("[tools_inscripcion] send fallo tel=%s", ctx["telefono"])
        return {"ok": True, "guardados": guardados, "rechazados": rechazados,
                "faltantes": faltantes,
                "proximo_pedido": NOMBRE_CAMPO[prox],
                "_silenciar_llm": True}

    # Todos los campos capturados → disparamos el resumen con botones.
    try:
        petra_wa.send_text(ctx["telefono"], "\n\n".join(partes))
    except Exception:
        log.exception("[tools_inscripcion] send fallo tel=%s", ctx["telefono"])
    try:
        from app.petra_agents.flow_onboarding_agente import _enviar_resumen
        _enviar_resumen(ctx["telefono"], meta)
    except Exception:
        log.exception("[tools_inscripcion] resumen fallo tel=%s", ctx["telefono"])
    return {"ok": True, "guardados": guardados, "rechazados": rechazados,
            "todos_completos": True, "_silenciar_llm": True}


def handle_confirmar_registro(args, ctx):
    """Confirma registro: marca inscrito + envía felicitaciones."""
    import datetime as _dt
    meta = ctx["meta"]
    faltantes = [c for c in CAMPOS if not meta.get(c)]
    if faltantes:
        return {"ok": False, "error": "faltan datos", "faltantes": faltantes}
    from app.petra_agents.flow_onboarding_agente import _set_estado, _send_buttons
    from app.petra_agents.textos import t as _t, boton as _b
    _set_estado(meta, "inscrito")
    meta["cap_version"] = 2
    meta["inscrito_at"] = _dt.datetime.utcnow().isoformat()
    _save_meta(ctx["conn"], ctx["cid"], meta)
    ctx["conn"].execute(
        "UPDATE petra_contactos SET nombre=%s WHERE id=%s",
        (meta.get("nombre"), ctx["cid"]),
    )
    nombre_corto = (meta.get("nombre") or "").split()[0] if meta.get("nombre") else ""
    felicitaciones = _t(
        "inscripcion.msg_felicitaciones",
        fallback=(
            "🎉 *¡Felicitaciones {nombre_corto}, ya sos oficialmente Agente Petra!*\n\n"
            "Vas a representar a Petra en *{conjunto}*, {barrio}, {ciudad}."
        ),
        nombre_corto=nombre_corto,
        conjunto=meta.get("conjunto") or "",
        barrio=meta.get("barrio") or "",
        ciudad=meta.get("ciudad") or "",
    )
    _send_buttons(ctx["telefono"], felicitaciones,
        [{"id": "ONBAG_VER_JOURNEY",
          "title": _b("ONBAG_VER_JOURNEY", "Ver próximos pasos")}],
        estado_key="inscrito")
    return {"ok": True, "estado_nuevo": "inscrito"}


def handle_mostrar_resumen(args, ctx):
    """Envía el resumen con botones Registrarme/Corregir/Cancelar."""
    from app.petra_agents.flow_onboarding_agente import _enviar_resumen
    _enviar_resumen(ctx["telefono"], ctx["meta"])
    return {"ok": True}


def handle_corregir_dato(args, ctx):
    campo = args.get("campo")
    if campo not in CAMPOS:
        return {"ok": False, "error": "campo invalido"}
    ctx["meta"][campo] = None
    _save_meta(ctx["conn"], ctx["cid"], ctx["meta"])
    return {"ok": True, "campo_borrado": campo}


def handle_cancelar(args, ctx):
    ctx["conn"].execute(
        "DELETE FROM petra_contacto_roles "
        "WHERE contacto_id=%s AND rol='agente_petra'",
        (ctx["cid"],),
    )
    return {"ok": True, "rol_eliminado": True}


def handle_escalar(args, ctx):
    from app import db as _db
    motivo = (args.get("motivo") or "sin motivo")[:200]
    try:
        _db.marcar_requiere_humano(ctx["telefono"], f"[inscripcion] {motivo}")
    except Exception:
        pass
    return {"ok": True, "motivo": motivo}


HANDLERS = {
    "capturar_datos_personales": handle_capturar_datos,
    "confirmar_registro": handle_confirmar_registro,
    "mostrar_resumen_datos": handle_mostrar_resumen,
    "corregir_dato": handle_corregir_dato,
    "cancelar_inscripcion": handle_cancelar,
    "escalar_a_humano": handle_escalar,
}


# ===== System prompt =====

def _get_faqs_prompt():
    try:
        from app.petra_agents import faq_kb
        return faq_kb.seccion_para_prompt()
    except Exception:
        return ""


def build_system_prompt(meta):
    datos = []
    for c in CAMPOS:
        v = meta.get(c)
        datos.append(f"  - {NOMBRE_CAMPO[c]}: {'✅ ' + str(v) if v else '❌ FALTA'}")
    datos_str = "\n".join(datos)
    faltantes = [NOMBRE_CAMPO[c] for c in CAMPOS if not meta.get(c)]
    estado_str = (
        "Todos los datos capturados. Mostrale el resumen con botones."
        if not faltantes
        else f"Todavía faltan: {', '.join(faltantes)}."
    )
    return f"""Sos Petra, el bot de WhatsApp que ayuda a nuevas Agentes Petra a inscribirse en la plataforma inmobiliaria.

La persona que te escribe quiere ser Agente Petra. Tu única tarea en esta fase es completar los 6 datos de registro:
nombre completo, cédula, ciudad, barrio, nombre del conjunto/edificio, y torre + apartamento o número de casa.

📋 DATOS ACTUALES:
{datos_str}

ESTADO: {estado_str}

TU ÚNICO TRABAJO ES LLAMAR LA TOOL CORRECTA. El bot responde solo (templates), vos NO escribís texto cuando llamás una tool.

CUÁNDO LLAMAR CADA TOOL:
1. La agente te manda uno o más datos (nombre, cédula, ciudad, barrio, conjunto, torre_apto) → `capturar_datos_personales` con los campos que vinieron. Podés mandar varios si vinieron juntos. El bot ya se encarga de confirmar y pedir el próximo.
   • `torre_apto`: la agente puede decir "Torre 3 apto 702", "Casa 49", "Apto 15B", "Bloque A 302". Pasalo COMO ESTÁ en un único string — NO lo desgloses en 2 campos ni lo inventes.
   • Si la agente responde "Conjunto X torre Y" cuando le pediste el conjunto, extraé AMBOS: conjunto="X", torre_apto="Torre Y".
2. Pulsa botón "Registrarme" (texto "ONBAG_CONFIRM") o escribe "registrarme"/"confirmo" Y están los 5 campos → `confirmar_registro`.
3. Pulsa "Corregir" o pide cambiar un dato puntual → `corregir_dato` con el campo.
4. Pulsa "Cancelar" o dice explícitamente que no quiere → `cancelar_inscripcion`.
5. Pide hablar con alguien del equipo / tiene un problema que no podés resolver → `escalar_a_humano`.

CUÁNDO RESPONDER CON TEXTO LIBRE (sin tool):
Solo cuando la agente tiene una duda ABIERTA sobre Petra:
- "¿cuánto se demora todo esto?"
- "¿cuánto puedo ganar?"
- "¿cómo funcionan las comisiones?"
- "¿qué pasa si vendo?"
En esos casos, respondé breve y natural con lo que sepas de Petra, SIN llamar tool.

REGLAS DURAS:
- 🚫 NO INVENTES DATOS. Si te da algo sospechoso ("ya te lo dije", "?", emoji suelto, una pregunta), NO llamés `capturar_datos_personales` con eso — el bot va a rechazar sin tool. Mejor respondé breve pidiendo aclaración.
- 🚫 NO AFIRMÉS QUE ALGO PASÓ hasta llamar la tool. No digas "te registré" si no llamaste `confirmar_registro`.
- 🚫 NO MENCIONES autorización, QR, kit, dirección — esas fases vienen después.
- 🚫 NO HABLÉS de otras agentes ni menciones conjuntos (ej. "Condominio Monaco") que ella no te haya dicho AHORA.
- ✅ Tono: cálido, breve, voseo rioplatense.""" + _get_faqs_prompt()


# ===== Entry point =====

def procesar(telefono, texto, cid, meta, conn, historial_prev=None):
    """Procesa un mensaje de la agente en fase inscripción vía tool-calling.
    Devuelve el resultado de toolcalling.ejecutar_turno + envía el texto
    de respuesta por WhatsApp si lo hay."""
    from app.petra_agents import toolcalling as tc
    from app import petra_wa
    system_prompt = build_system_prompt(meta)
    ctx = {"telefono": telefono, "cid": cid, "meta": meta, "conn": conn}
    resultado = tc.ejecutar_turno(
        telefono=telefono, texto=texto, contexto=ctx,
        tools=TOOLS, handlers=HANDLERS,
        system_prompt=system_prompt, historial_prev=historial_prev,
        fase="inscripcion",
    )
    if resultado.get("texto_respuesta"):
        try:
            petra_wa.send_text(telefono, resultado["texto_respuesta"])
        except Exception:
            log.exception("[tools_inscripcion] send fallo tel=%s", telefono)
    log.info("[tools_inscripcion] tel=%s ok=%s tools=%s",
             telefono, resultado.get("ok"),
             [t["nombre"] for t in resultado.get("tools_llamadas", [])])
    return resultado
