"""Tools de la fase AUTORIZACIÓN del flow de agente Petra.

La agente terminó la capacitación. Ahora tiene que hablar con el
administrador de su edificio para que la autorice como representante
de Petra. En esta fase el bot:

- Puede enviar el material de apoyo (recordatorio video + demo + piezas)
- Responde dudas conversacionales
- Marca cuando ella confirma que tiene la autorización → pasa a kit_pendiente

Reglas duras (en el system prompt):
- NUNCA afirmar que el admin autorizó si ella no lo confirmó explícitamente
- NUNCA generar / mencionar QRs — eso es la fase siguiente
- NUNCA mencionar direcciones ni edificios específicos (puede alucinar)
"""
import json
import logging

log = logging.getLogger("petra")


# ===== Definición de tools (schema Anthropic) =====

TOOLS = [
    {
        "name": "enviar_material_autorizacion",
        "description": (
            "Envía a la agente el pack completo de material de apoyo para "
            "la reunión con el administrador: recordatorio del video de "
            "capacitación + demo del producto + piezas gráficas. Usar "
            "cuando ella pide material, lo pulsa, o pregunta cómo abordar "
            "al admin."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "marcar_tengo_autorizacion",
        "description": (
            "Marca que la agente YA obtuvo la autorización del administrador "
            "y la pasa a la siguiente fase (kit_pendiente, donde se le pide "
            "la dirección para enviarle el kit físico). Llamar cuando ella "
            "confirma — en lenguaje natural — que el admin le dijo que sí. "
            "Ejemplos reales que SÍ disparan: pulsar botón '✅ Ya tengo "
            "autorización'; 'ya me autorizó'; 'listo me dieron el ok'; "
            "'ya hablé y me dijo que sí'; 'ya hablé con el admin, dijo que sí'; "
            "'me autorizó'; 'me dio el ok'; 'ya puedo seguir'; 'dijo que sí'. "
            "También si en tu turno ANTERIOR preguntaste algo como '¿ya te "
            "autorizó?' o '¿el admin ya te dio el ok?' y ella responde "
            "afirmativamente ('sí', 'si', 'listo', 'ya', 'claro', 'obvio'), "
            "entonces SÍ llamá la tool. NO llamés si expresa duda real "
            "('creo que sí', 'me dijo que lo iba a pensar', 'a ver mañana', "
            "'todavía no'); en ese caso preguntá para desambiguar."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "escalar_a_humano",
        "description": (
            "Marca que la agente necesita atención humana. Usar si tiene "
            "un problema que no podés resolver (admin la rechazó, no sabe "
            "cómo abordarlo, pide hablar con alguien real)."
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


# ===== Handlers =====

def handle_enviar_material(args, ctx):
    """Envía el pack: recordatorio video + demo + piezas."""
    from app.petra_agents.flow_onboarding_agente import (
        _enviar_recordatorio_capacitacion, _enviar_material_autorizacion,
    )
    import time as _tm
    try:
        _enviar_recordatorio_capacitacion(ctx["conn"], ctx["telefono"])
        _tm.sleep(1)
        _enviar_material_autorizacion(ctx["conn"], ctx["telefono"])
        return {"ok": True, "enviado": "recordatorio + material", "_silenciar_llm": True}
    except Exception as e:
        log.exception("[tc-auth] enviar_material fallo")
        return {"ok": False, "error": str(e)[:200]}


def handle_marcar_tengo_autorizacion(args, ctx):
    """Transiciona a kit_pendiente + pide dirección (torre)."""
    import datetime as _dt
    import json as _json
    from app.petra_agents.flow_onboarding_agente import _set_estado, _sm
    meta = ctx["meta"]
    _set_estado(meta, "kit_pendiente")
    meta["direccion_paso"] = "torre"
    meta["autorizacion_confirmada_at"] = _dt.datetime.utcnow().isoformat()
    ctx["conn"].execute(
        "UPDATE petra_contacto_roles SET metadata=%s::jsonb, updated_at=NOW() "
        "WHERE contacto_id=%s AND rol='agente_petra'",
        (_json.dumps(meta), ctx["cid"]),
    )
    _sm(ctx["telefono"],
        "activacion.msg_pedir_direccion_inicio",
        fallback=(
            "🎉 *¡Buenísimo! Ya tenés la autorización del administrador.*\n\n"
            "📦 *Siguiente paso: te enviamos tu kit* (QR + un regalito 💜)\n\n"
            "Necesito tu dirección exacta para enviártelo. Empecemos por lo "
            "primero:\n\n"
            "*¿En qué torre vivís?* (ej. Torre 3, Bloque A, etc. Si no "
            "aplica, respondé *N/A*)"
        ),
    )
    return {"ok": True, "estado_nuevo": "kit_pendiente", "_silenciar_llm": True}


def handle_escalar(args, ctx):
    from app import db as _db
    motivo = (args.get("motivo") or "sin motivo")[:200]
    try:
        _db.marcar_requiere_humano(ctx["telefono"], f"[autorizacion] {motivo}")
    except Exception:
        pass
    return {"ok": True, "motivo": motivo}


HANDLERS = {
    "enviar_material_autorizacion": handle_enviar_material,
    "marcar_tengo_autorizacion": handle_marcar_tengo_autorizacion,
    "escalar_a_humano": handle_escalar,
}


# ===== System prompt =====

def build_system_prompt(meta):
    nombre = (meta.get("nombre") or "").split()[0] if meta.get("nombre") else ""
    conjunto = meta.get("conjunto") or "tu edificio"
    return f"""Sos Petra, el bot de WhatsApp que ayuda a {nombre or "la agente"} en su fase de AUTORIZACIÓN.

La agente ya terminó su capacitación (14 videos). Ahora tiene que hablar con el administrador de *{conjunto}* para que la autorice como Agente Petra representante del edificio.

TU TRABAJO:
1. Si pide material / "cómo abordo al admin" / pulsa botón "📎 Ver material" → llamá `enviar_material_autorizacion` (que le mandará recordatorio del video de capacitación + demo del producto + piezas gráficas para mostrar).
2. Si confirma que ya tiene la autorización — en lenguaje natural, no solo frases exactas — llamá `marcar_tengo_autorizacion`. Casos reales que disparan: pulsa botón "✅ Ya tengo autorización"; dice "ya me autorizó" / "listo me dieron el ok" / "ya hablé y me dijo que sí" / "ya hablé con el admin, dijo que sí" / "me autorizó" / "me dio el ok" / "ya puedo seguir". Y si en tu turno ANTERIOR vos preguntaste algo tipo "¿ya te autorizó?" / "¿el admin ya te dio el ok?" y ella responde afirmativa y corta ("sí", "si", "listo", "ya", "claro", "obvio") → también llamala. Si expresa duda real ("creo que sí", "me dijo que lo iba a pensar", "mañana veo") → preguntá para aclarar antes de llamar.
3. Si tiene una duda sobre Petra, comisiones, cómo hablar con el admin, etc. → respondé con texto natural usando lo que sepas de Petra. No llamés tool.
4. Si pide ayuda humana / dice que el admin la rechazó / no sabe qué hacer → llamá `escalar_a_humano`.

REGLAS DURAS (nunca romper):
- 🚫 NUNCA AFIRMÉS que el admin la autorizó hasta que ella lo diga EXPLÍCITAMENTE y vos llamés `marcar_tengo_autorizacion`.
- 🚫 NUNCA MENCIONES el QR, código QR, link QR — eso es la próxima fase, acá no toca aún.
- 🚫 NUNCA GENERES una dirección, barrio o datos específicos. Ella NO te pidió eso en esta fase.
- 🚫 NUNCA MENCIONES edificios/conjuntos que no sean *{conjunto}* (el suyo). No menciones "Condominio Monaco" ni ejemplos de otras agentes.
- ✅ Tono cálido, voseo rioplatense (vos, tenés, mandame), emoji con mesura.
- ✅ Si ella pregunta "¿qué le digo al admin?" → explicá de forma concreta y breve: Petra es una plataforma gratuita para el edificio, no cobra nada al admin, solo necesita permiso para activar el QR del lobby.
- ✅ Si pregunta sobre comisiones, cuánto puede ganar, etc. → respondé natural (1% ventas, X% arriendos — datos reales del deck).
- ✅ Si no estás 100% seguro de algo → preferí decir "déjame averiguar y te confirmo" en vez de inventar."""


# ===== Entry point =====

def procesar(telefono, texto, cid, meta, conn, historial_prev=None):
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
            log.exception("[tools_autorizacion] send fallo tel=%s", telefono)
    log.info("[tools_autorizacion] tel=%s ok=%s tools=%s",
             telefono, resultado.get("ok"),
             [t["nombre"] for t in resultado.get("tools_llamadas", [])])
    return resultado
