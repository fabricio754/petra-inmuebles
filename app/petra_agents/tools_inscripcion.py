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

CAMPOS = ["nombre", "cedula", "ciudad", "barrio", "conjunto"]
NOMBRE_CAMPO = {
    "nombre": "nombre completo",
    "cedula": "cédula",
    "ciudad": "ciudad",
    "barrio": "barrio",
    "conjunto": "conjunto / edificio",
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
    """Guarda los campos pasados en args. Rechaza los sospechosos."""
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
    return {
        "guardados": guardados,
        "rechazados": rechazados,
        "faltantes": faltantes,
        "ya_guardados": [c for c in CAMPOS if meta.get(c)],
        "proximo_a_pedir": NOMBRE_CAMPO[faltantes[0]] if faltantes else None,
        "todos_completos": not faltantes,
    }


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

La persona que te escribe quiere ser Agente Petra. Tu única tarea en esta fase es completar los 5 datos de registro:
nombre completo, cédula, ciudad, barrio, y nombre del conjunto/edificio donde vive.

📋 DATOS ACTUALES:
{datos_str}

ESTADO: {estado_str}

TU TRABAJO:
1. Si la agente te da uno o más datos → llamá tool `capturar_datos_personales` (podés guardar varios campos a la vez si vinieron juntos).
2. Si todos los campos ya están completos → llamá tool `mostrar_resumen_datos` para que vea el resumen con los botones.
3. Si pulsó el botón "Registrarme" (texto exacto "ONBAG_CONFIRM" o escribió "registrarme"/"confirmo") Y todos los datos están → llamá `confirmar_registro`.
4. Si pulsó "Corregir" o pide cambiar un dato → llamá `corregir_dato` con el campo.
5. Si pulsó "Cancelar" o explícitamente dice no querer → llamá `cancelar_inscripcion`.
6. Si tiene una duda sobre Petra (qué son las comisiones, cuánto tiempo lleva, etc.) → respondé con texto natural, SIN llamar tool.
7. Si no sabés qué hacer → llamá `escalar_a_humano`.

REGLAS DURAS (nunca romper):
- 🚫 NO INVENTES DATOS. Si te da algo que no parece válido (ej. "ya te lo dije", "¿cuánto se demora?", "si"), NO guardes nada: pedile aclaración por texto.
- 🚫 NO AFIRMÉS QUE ALGO DEL FLOW PASÓ hasta llamar la tool correspondiente. No digas "ya te registré" si no llamaste `confirmar_registro`. No digas "generé tu QR" (eso es otra fase, acá no toca).
- 🚫 NO MENCIONES autorización, QR, kit, dirección — nada de eso aplica en esta fase.
- 🚫 NO HABLÉS de otras agentes ni menciones nombres de otros conjuntos (ej. "Condominio Monaco") a menos que la agente misma te lo haya dicho AHORA.
- 🚫 NUNCA LISTES varios campos pendientes en el mismo mensaje. UNO solo por turno.
- 🚫 NUNCA RE-PIDAS un dato que ya tenés capturado (mirá "DATOS ACTUALES" arriba y el campo `ya_guardados` del resultado de la tool).
- ✅ Tono: cálido, breve, voseo rioplatense (vos, tenés, mandame). Emoji con mesura.
- ✅ Después de llamar `capturar_datos_personales`, mirá el campo `proximo_a_pedir` del resultado y pedí EXACTAMENTE ese UNO, en una frase corta y natural (no listes todos los pendientes, no inventes otros).
- ✅ Si `todos_completos: true`, llamá `mostrar_resumen_datos`.
- ✅ Si lo que te dio es sospechoso (pregunta, protesta, emoji suelto), pedí de nuevo con un ejemplo: "No me quedó claro — mandame tu nombre y apellido como ejemplo: Juana Pérez"."""


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
