"""Tools de la fase CAPACITACIÓN (estados esperando_capacitacion_dia1 y
en_capacitacion).

Los 14 videos se mandan por cron a las 8am (2 por día). Acá el LLM solo
gestiona lo conversacional: confirmar que vio los del día, pedir el día
siguiente ahora, reenviar los de hoy, responder dudas.

Los handlers NO duplican lógica: re-entran a `_fase_capacitacion` con el
mismo texto/botón que usaría el flow determinístico, así una sola
implementación sostiene ambos caminos.
"""
import logging

log = logging.getLogger("petra")


TOOLS = [
    {
        "name": "marcar_videos_del_dia_vistos",
        "description": (
            "La agente confirma que YA VIO los 2 videos de capacitación de "
            "hoy. Equivale a pulsar '✅ Listo'. Avanza el contador de videos "
            "vistos; si completa los 14 pasa automáticamente a la fase de "
            "autorización. SOLO llamar cuando ella lo dice claro ('listo', "
            "'ya los vi', 'terminé los de hoy'). NO llamar si solo pregunta "
            "algo o dice que todavía no los vio."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "enviar_siguiente_dia",
        "description": (
            "Envía AHORA los 2 videos del día siguiente, sin esperar al cron "
            "de las 8am. Usar cuando ella pide seguir avanzando ya ('mandame "
            "los siguientes', 'quiero seguir ahora', 'ver día siguiente')."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "reenviar_videos_de_hoy",
        "description": (
            "Re-envía los 2 videos del día ACTUAL (los que ya le mandamos). "
            "Usar cuando dice que los perdió, no le llegaron, o pide que se "
            "los mande de nuevo."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "escalar_a_humano",
        "description": (
            "Marca que necesita atención humana (videos rotos, no puede "
            "verlos, problema que no podés resolver)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"motivo": {"type": "string"}},
            "required": ["motivo"],
            "additionalProperties": False,
        },
    },
]


def _reentrar(ctx, texto_sintetico):
    """Re-entra al handler determinístico con un texto/botón sintético."""
    from app.petra_agents.flow_onboarding_agente import _fase_capacitacion
    estado = ctx["meta"].get("estado_agente", "en_capacitacion")
    _fase_capacitacion(ctx["conn"], ctx["telefono"], ctx["cid"], ctx["meta"],
                       texto_sintetico, estado)


def handle_marcar_vistos(args, ctx):
    antes = int(ctx["meta"].get("videos_vistos", 0) or 0)
    _reentrar(ctx, "ONBAG_LISTO")
    despues = int(ctx["meta"].get("videos_vistos", 0) or 0)
    return {"ok": True, "videos_vistos_antes": antes, "videos_vistos_ahora": despues,
            "estado_ahora": ctx["meta"].get("estado_agente"),
            "_silenciar_llm": True}


def handle_siguiente_dia(args, ctx):
    _reentrar(ctx, "ONBAG_SIGUIENTE_DIA")
    return {"ok": True, "_silenciar_llm": True}


def handle_reenviar_hoy(args, ctx):
    _reentrar(ctx, "reenviar")
    return {"ok": True, "_silenciar_llm": True}


def handle_escalar(args, ctx):
    from app import db as _db
    motivo = (args.get("motivo") or "sin motivo")[:200]
    try:
        _db.marcar_requiere_humano(ctx["telefono"], f"[capacitacion] {motivo}")
    except Exception:
        pass
    return {"ok": True, "motivo": motivo}


HANDLERS = {
    "marcar_videos_del_dia_vistos": handle_marcar_vistos,
    "enviar_siguiente_dia": handle_siguiente_dia,
    "reenviar_videos_de_hoy": handle_reenviar_hoy,
    "escalar_a_humano": handle_escalar,
}


def build_system_prompt(meta):
    nombre = (meta.get("nombre") or "").split()[0] if meta.get("nombre") else ""
    vistos = int(meta.get("videos_vistos", 0) or 0)
    cap_v = int(meta.get("cap_version", 1) or 1)
    total = 14 if cap_v >= 2 else 12
    dia_enviado = int(meta.get("cap_dia_enviado", 0) or 0)
    return f"""Sos Petra, el bot de WhatsApp que acompaña a {nombre or "la agente"} durante su CAPACITACIÓN.

Son {total} videos cortos, 2 por día. Cada mañana a las 8am le llegan los 2 del día. Cuando los ve, pulsa "✅ Listo" o lo escribe.

📊 PROGRESO:
  - Videos vistos: {vistos}/{total}
  - Último día enviado: {dia_enviado}

TU TRABAJO:
1. Si confirma que vio los de hoy ("listo", "ya los vi", "terminé") → `marcar_videos_del_dia_vistos`.
2. Si quiere seguir YA con los del día siguiente → `enviar_siguiente_dia`.
3. Si dice que los perdió / no le llegaron / pide que se los mande de nuevo → `reenviar_videos_de_hoy`.
4. Si pregunta sobre el contenido de los videos, comisiones, cómo funciona Petra, cuánto falta → respondé con texto natural, breve. NO llamés tool.
5. Si tiene un problema técnico o pide hablar con alguien → `escalar_a_humano`.

REGLAS DURAS:
- 🚫 NO marqués videos como vistos si ella no lo confirmó explícitamente.
- 🚫 NO digas que terminó la capacitación ni hables de autorización/admin/QR/kit: esas fases vienen después y las maneja el flow.
- 🚫 NO inventés qué dicen los videos si no lo sabés; decí que puede verlos y preguntarte después.
- 🚫 NO menciones otros edificios ni otras agentes.
- ✅ Después de llamar una tool, NO repitas lo que el bot ya le mandó (el resultado te lo indica). Si querés agregar algo, que sea una línea corta.
- ✅ Tono cálido, voseo rioplatense, emoji con mesura."""


def procesar(telefono, texto, cid, meta, conn, historial_prev=None):
    from app.petra_agents import toolcalling as tc
    from app import petra_wa
    ctx = {"telefono": telefono, "cid": cid, "meta": meta, "conn": conn}
    resultado = tc.ejecutar_turno(
        telefono=telefono, texto=texto, contexto=ctx,
        tools=TOOLS, handlers=HANDLERS,
        system_prompt=build_system_prompt(meta), historial_prev=historial_prev,
    )
    if resultado.get("texto_respuesta"):
        try:
            petra_wa.send_text(telefono, resultado["texto_respuesta"])
        except Exception:
            log.exception("[tools_capacitacion] send fallo tel=%s", telefono)
    log.info("[tools_capacitacion] tel=%s ok=%s tools=%s",
             telefono, resultado.get("ok"),
             [t["nombre"] for t in resultado.get("tools_llamadas", [])])
    return resultado
