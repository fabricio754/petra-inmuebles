"""Tools de la fase ACTIVA (día a día de la agente): citas + inventario.

Citas: el panel le manda a la agente propuestas de visita con botones
ONBAG_CITA_{PUEDO|NO_PUEDO|PROPONER}_<id>. Los botones siguen yendo por
el flow determinístico; acá el LLM maneja cuando ella responde con texto
("sí puedo", "no, mejor el jueves a las 3").

Inventario: captura de un inmueble (10 campos). El LLM extrae varios
campos de un mismo mensaje y los valida con `_validar_paso_inventario`
(misma validación que el flow). Al tener los 10 → `_finalizar_inventario`.

Los handlers re-entran a las funciones existentes del flow para no
duplicar lógica.
"""
import json
import logging

log = logging.getLogger("petra")

CAMPOS_INM = [
    "modalidad", "torre", "apto", "area_m2", "habitaciones", "banos",
    "parqueadero", "precio", "propietario_nombre", "propietario_tel",
]
NOMBRE_INM = {
    "modalidad": "modalidad (arriendo/venta)",
    "torre": "torre",
    "apto": "apartamento",
    "area_m2": "área en m²",
    "habitaciones": "habitaciones",
    "banos": "baños",
    "parqueadero": "parqueadero (sí/no)",
    "precio": "precio en millones",
    "propietario_nombre": "nombre del propietario",
    "propietario_tel": "teléfono del propietario",
}

PREGUNTA_INM = {
    "modalidad": ("activa.pedir_modalidad", "¿Es para *arriendo* o *venta*? 🏷️"),
    "torre": ("activa.pedir_torre_inm", "¿En qué *torre o bloque*? (ej. *Torre 3* o *N/A*) 🏢"),
    "apto": ("activa.pedir_apto_inm", "¿Cuál es el *número de apartamento*? 🚪"),
    "area_m2": ("activa.pedir_area", "¿Cuántos *metros cuadrados* tiene? 📐"),
    "habitaciones": ("activa.pedir_habitaciones", "¿Cuántas *habitaciones*? 🛏️"),
    "banos": ("activa.pedir_banos", "¿Cuántos *baños*? 🚿"),
    "parqueadero": ("activa.pedir_parqueadero", "¿Tiene *parqueadero*? (sí/no) 🚗"),
    "precio": ("activa.pedir_precio", "¿Cuál es el *precio*? (en millones de pesos) 💰"),
    "propietario_nombre": ("activa.pedir_prop_nombre", "¿Cómo se llama el *propietario*? 👤"),
    "propietario_tel": ("activa.pedir_prop_tel", "¿Cuál es el *teléfono del propietario*? (con indicativo, ej. 573001234567) 📞"),
}

CONFIRMACION_INM = {
    "modalidad": ("activa.ok_modalidad", "✅ Modalidad: *{valor}*."),
    "torre": ("activa.ok_torre_inm", "✅ Torre: *{valor}*."),
    "apto": ("activa.ok_apto_inm", "✅ Apto: *{valor}*."),
    "area_m2": ("activa.ok_area", "✅ Área: *{valor} m²*."),
    "habitaciones": ("activa.ok_habitaciones", "✅ Habitaciones: *{valor}*."),
    "banos": ("activa.ok_banos", "✅ Baños: *{valor}*."),
    "parqueadero": ("activa.ok_parqueadero", "✅ Parqueadero: *{valor}*."),
    "precio": ("activa.ok_precio", "✅ Precio: *{valor}* millones."),
    "propietario_nombre": ("activa.ok_prop_nombre", "✅ Propietario: *{valor}*."),
    "propietario_tel": ("activa.ok_prop_tel", "✅ Teléfono: *{valor}*."),
}

RECHAZO_INM_FALLBACK = "No me quedó claro ese dato ({campo}): {err}. Mandámelo de nuevo 🙏"


TOOLS = [
    {
        "name": "responder_cita",
        "description": (
            "Registra la respuesta de la agente a una cita propuesta. "
            "accion='puedo' la confirma; 'no_puedo' la cancela; 'proponer' "
            "abre la contrapropuesta (usá `proponer_otra_hora` si ya te dio "
            "día/hora). SOLO llamar con un visita_id que esté en la lista de "
            "CITAS PENDIENTES del prompt. Si hay varias pendientes y no está "
            "claro a cuál se refiere, preguntá antes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "visita_id": {"type": "integer"},
                "accion": {"type": "string", "enum": ["puedo", "no_puedo", "proponer"]},
            },
            "required": ["visita_id", "accion"],
            "additionalProperties": False,
        },
    },
    {
        "name": "proponer_otra_hora",
        "description": (
            "La agente propone OTRO día/hora para una cita pendiente. Pasá "
            "la propuesta tal como ella la dijo (ej. 'jueves 3pm'). Usar "
            "cuando ya te dio la alternativa concreta."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "visita_id": {"type": "integer"},
                "propuesta": {"type": "string", "description": "Día/hora propuesta, texto libre"},
            },
            "required": ["visita_id", "propuesta"],
            "additionalProperties": False,
        },
    },
    {
        "name": "iniciar_inventario",
        "description": (
            "Arranca la carga de un inmueble nuevo (manda el video "
            "explicativo y la primera pregunta). Usar cuando pide registrar / "
            "cargar / subir un inmueble o apto y NO hay una carga en curso."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "capturar_datos_inmueble",
        "description": (
            "Guarda uno o más datos del inmueble en curso. Podés pasar varios "
            "campos si vinieron juntos ('apto 702 torre 3, 85 metros, 3 hab "
            "2 baños'). Pasá los valores como texto tal como los dijo; el "
            "sistema los normaliza y valida. Si lo que dijo no es un dato "
            "(pregunta, 'ya te dije', '?'), NO llamés la tool: pedí "
            "aclaración. Cuando los 10 campos están completos el inmueble se "
            "guarda solo y el bot manda el resumen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "modalidad": {"type": "string", "description": "'arriendo' o 'venta'"},
                "torre": {"type": "string", "description": "Torre/bloque o 'N/A'"},
                "apto": {"type": "string"},
                "area_m2": {"type": "string", "description": "Metros cuadrados, solo número"},
                "habitaciones": {"type": "string", "description": "Solo número"},
                "banos": {"type": "string", "description": "Solo número"},
                "parqueadero": {"type": "string", "description": "'sí' o 'no'"},
                "precio": {"type": "string", "description": "En millones de pesos (ej. '1200' venta, '2.5' arriendo)"},
                "propietario_nombre": {"type": "string"},
                "propietario_tel": {"type": "string", "description": "Con indicativo, ej. 573001234567"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "cancelar_inventario",
        "description": "Cancela la carga del inmueble en curso. Solo si ella lo pide explícitamente.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "escalar_a_humano",
        "description": "Marca que necesita atención humana (problema con una cita, un interesado, un inmueble, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {"motivo": {"type": "string"}},
            "required": ["motivo"],
            "additionalProperties": False,
        },
    },
]


def _save_meta(conn, cid, meta):
    conn.execute(
        "UPDATE petra_contacto_roles SET metadata=%s::jsonb, updated_at=NOW() "
        "WHERE contacto_id=%s AND rol='agente_petra'",
        (json.dumps(meta), cid),
    )


def _citas_pendientes(conn, cid):
    try:
        rows = conn.execute(
            "SELECT v.id, v.programada_at, v.estado, c.nombre "
            "FROM petra_visitas v "
            "LEFT JOIN petra_contactos c ON c.id = v.interesado_id "
            "WHERE v.agente_id=%s AND v.estado NOT IN ('realizada','cancelada') "
            "ORDER BY v.programada_at NULLS LAST, v.id DESC LIMIT 10",
            (cid,),
        ).fetchall()
    except Exception:
        log.exception("[tools_activa] citas query fallo cid=%s", cid)
        return []
    return [{"id": r[0], "cuando": r[1], "estado": r[2], "interesado": r[3]} for r in rows]


# ===== Handlers =====

def handle_responder_cita(args, ctx):
    from app.petra_agents.flow_onboarding_agente import _manejar_boton_cita
    vid = args.get("visita_id")
    accion = (args.get("accion") or "").lower()
    mapa = {"puedo": "PUEDO", "no_puedo": "NO_PUEDO", "proponer": "PROPONER"}
    if accion not in mapa or not isinstance(vid, int):
        return {"ok": False, "error": "args invalidos"}
    ids_validos = {c["id"] for c in _citas_pendientes(ctx["conn"], ctx["cid"])}
    if vid not in ids_validos:
        return {"ok": False, "error": f"visita {vid} no está pendiente para esta agente"}
    _manejar_boton_cita(ctx["conn"], ctx["telefono"], ctx["cid"], ctx["meta"],
                        f"ONBAG_CITA_{mapa[accion]}_{vid}")
    return {"ok": True, "visita_id": vid, "accion": accion,
            "_silenciar_llm": True}


def handle_proponer_otra_hora(args, ctx):
    from app.petra_agents.flow_onboarding_agente import _capturar_contrapropuesta_cita
    vid = args.get("visita_id")
    propuesta = (args.get("propuesta") or "").strip()
    if not isinstance(vid, int) or not propuesta:
        return {"ok": False, "error": "args invalidos"}
    ids_validos = {c["id"] for c in _citas_pendientes(ctx["conn"], ctx["cid"])}
    if vid not in ids_validos:
        return {"ok": False, "error": f"visita {vid} no está pendiente para esta agente"}
    _capturar_contrapropuesta_cita(ctx["conn"], ctx["telefono"], ctx["cid"],
                                   ctx["meta"], propuesta, vid)
    return {"ok": True, "visita_id": vid, "propuesta": propuesta,
            "_silenciar_llm": True}


def handle_iniciar_inventario(args, ctx):
    from app.petra_agents.flow_onboarding_agente import _iniciar_inventario
    if ctx["meta"].get("inventario_paso"):
        return {"ok": False, "error": "ya hay una carga en curso; usá capturar_datos_inmueble"}
    _iniciar_inventario(ctx["conn"], ctx["telefono"], ctx["cid"], ctx["meta"])
    return {"ok": True, "_silenciar_llm": True}


def handle_capturar_datos_inmueble(args, ctx):
    """Guarda campos del inmueble. Si completa los 10 → _finalizar_inventario
    (ya manda resumen). Si quedan faltantes → el handler mismo manda las
    confirmaciones + próxima pregunta (templateado) y silencia al LLM."""
    from app import petra_wa
    from app.petra_agents.textos import t as _t
    from app.petra_agents.flow_onboarding_agente import (
        _validar_paso_inventario, _finalizar_inventario,
    )
    meta = ctx["meta"]
    datos = dict(meta.get("inventario_datos") or {})
    guardados, rechazados = {}, {}
    for campo in CAMPOS_INM:
        if campo not in args or args[campo] is None:
            continue
        ok, valor, err = _validar_paso_inventario(campo, str(args[campo]))
        if ok:
            datos[campo] = valor
            guardados[campo] = valor
        else:
            rechazados[campo] = err
    faltantes = [c for c in CAMPOS_INM if c not in datos]

    # Nada capturado ni rechazado → dejamos al LLM responder (pregunta
    # abierta, confusión, etc).
    if not guardados and not rechazados:
        return {"guardados": {}, "rechazados": {}, "faltantes": faltantes,
                "nota": "no se guardó nada — explicá al user qué necesitás"}

    if not faltantes:
        _finalizar_inventario(ctx["conn"], ctx["telefono"], ctx["cid"], meta, datos)
        return {"ok": True, "guardados": guardados, "rechazados": rechazados,
                "faltantes": [], "completado": True,
                "_silenciar_llm": True}

    meta["inventario_datos"] = datos
    meta["inventario_paso"] = faltantes[0]
    _save_meta(ctx["conn"], ctx["cid"], meta)
    partes = []
    for campo, valor in guardados.items():
        clave, fallback = CONFIRMACION_INM[campo]
        partes.append(_t(clave, fallback=fallback, valor=valor))
    for campo, err in rechazados.items():
        partes.append(RECHAZO_INM_FALLBACK.format(campo=NOMBRE_INM[campo], err=err))
    if guardados:
        prox = faltantes[0]
        clave, fallback = PREGUNTA_INM[prox]
        partes.append(_t(clave, fallback=fallback))
    try:
        petra_wa.send_text(ctx["telefono"], "\n\n".join(partes))
    except Exception:
        log.exception("[tools_activa] send fallo tel=%s", ctx["telefono"])
    return {"ok": True, "guardados": guardados, "rechazados": rechazados,
            "faltantes": faltantes, "completado": False,
            "proximo_pedido": NOMBRE_INM[faltantes[0]],
            "_silenciar_llm": True}


def handle_cancelar_inventario(args, ctx):
    meta = ctx["meta"]
    meta.pop("inventario_paso", None)
    meta.pop("inventario_datos", None)
    _save_meta(ctx["conn"], ctx["cid"], meta)
    return {"ok": True}


def handle_escalar(args, ctx):
    from app import db as _db
    motivo = (args.get("motivo") or "sin motivo")[:200]
    try:
        _db.marcar_requiere_humano(ctx["telefono"], f"[activa] {motivo}")
    except Exception:
        pass
    return {"ok": True, "motivo": motivo}


HANDLERS = {
    "responder_cita": handle_responder_cita,
    "proponer_otra_hora": handle_proponer_otra_hora,
    "iniciar_inventario": handle_iniciar_inventario,
    "capturar_datos_inmueble": handle_capturar_datos_inmueble,
    "cancelar_inventario": handle_cancelar_inventario,
    "escalar_a_humano": handle_escalar,
}


def build_system_prompt(meta, conn, cid):
    from app.petra_agents.flow_onboarding_agente import _fmt_fecha
    nombre = (meta.get("nombre") or "").split()[0] if meta.get("nombre") else ""
    conjunto = meta.get("conjunto") or "su edificio"
    citas = _citas_pendientes(conn, cid)
    if citas:
        citas_str = "\n".join(
            f"  - visita_id={c['id']} · {c['interesado'] or 'interesado'} · "
            f"{_fmt_fecha(c['cuando'])} · estado={c['estado']}"
            for c in citas
        )
    else:
        citas_str = "  (ninguna)"
    esperando = meta.get("cita_esperando_propuesta_id")
    datos = meta.get("inventario_datos") or {}
    en_curso = bool(meta.get("inventario_paso"))
    if en_curso:
        inv_lines = "\n".join(
            f"  - {NOMBRE_INM[c]}: {'✅ ' + str(datos[c]) if c in datos else '❌ FALTA'}"
            for c in CAMPOS_INM
        )
        inv_str = f"CARGA DE INMUEBLE EN CURSO:\n{inv_lines}"
    else:
        inv_str = "No hay carga de inmueble en curso."
    return f"""Sos Petra, el bot de WhatsApp que acompaña a {nombre or "la agente"}, Agente Petra activa de *{conjunto}*, en su día a día.

Tus dos trabajos: (A) gestionar las CITAS que Petra le envía con interesados, y (B) ayudarla a CARGAR INMUEBLES de su edificio.

📅 CITAS PENDIENTES:
{citas_str}
{"⚠️ Está esperando que ella diga día/hora alternativo para visita_id=" + str(esperando) + "." if esperando else ""}

🏠 INVENTARIO:
{inv_str}

TU ÚNICO TRABAJO ES LLAMAR LA TOOL CORRECTA. Cuando llamés una tool, el bot responde solo con un mensaje templateado — vos NO escribís texto en esos turnos.

CUÁNDO LLAMAR CADA TOOL:
1. CITAS — responde con texto a una cita ("sí puedo", "no", "mejor el jueves 3pm") → `responder_cita` (accion=puedo/no_puedo) o `proponer_otra_hora` si da día/hora alternativo. SOLO con visita_id de la lista CITAS PENDIENTES. Si hay varias y no está claro cuál, preguntá antes.
2. INVENTARIO — pide cargar/registrar un inmueble SIN carga en curso → `iniciar_inventario`. Con carga en curso + te da datos → `capturar_datos_inmueble` (varios campos juntos si vinieron juntos). Pide cancelar → `cancelar_inventario`.
3. Pide hablar con alguien del equipo / problema real → `escalar_a_humano`.

CUÁNDO RESPONDER CON TEXTO LIBRE (sin tool):
Preguntas abiertas sobre Petra o el día a día:
- "¿cómo le muestro el inmueble al interesado?" → explicación breve.
- "¿qué comisión me queda?" → 1% ventas, X% arriendos.
- "¿qué le digo si pregunta por precio?" → consejo breve.
Breve, natural, SIN llamar tool.

REGLAS DURAS:
- 🚫 NUNCA confirmés/cancelés una cita sin que ella lo diga claro.
- 🚫 NUNCA inventés visita_id, interesados, fechas, precios ni datos de inmueble que no estén en este prompt.
- 🚫 NUNCA llamés capturar_datos_inmueble con preguntas o protestas ("ya te dije", "?").
- 🚫 NUNCA menciones otros edificios ni otras agentes."""


def procesar(telefono, texto, cid, meta, conn, historial_prev=None):
    from app.petra_agents import toolcalling as tc
    from app import petra_wa
    ctx = {"telefono": telefono, "cid": cid, "meta": meta, "conn": conn}
    resultado = tc.ejecutar_turno(
        telefono=telefono, texto=texto, contexto=ctx,
        tools=TOOLS, handlers=HANDLERS,
        system_prompt=build_system_prompt(meta, conn, cid),
        historial_prev=historial_prev,
    )
    if resultado.get("texto_respuesta"):
        try:
            petra_wa.send_text(telefono, resultado["texto_respuesta"])
        except Exception:
            log.exception("[tools_activa] send fallo tel=%s", telefono)
    log.info("[tools_activa] tel=%s ok=%s tools=%s",
             telefono, resultado.get("ok"),
             [t["nombre"] for t in resultado.get("tools_llamadas", [])])
    return resultado
