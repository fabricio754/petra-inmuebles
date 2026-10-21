"""Tools de las fases KIT_PENDIENTE y KIT_ENVIADO del flow.

kit_pendiente:
  - Capturar dirección en UN SOLO mensaje libre (texto completo)
  - Al capturar: avanza auto a kit_enviado y manda el bloque completo
    (texto + botón "Ya instalé el QR")

kit_enviado:
  - Esperar que la agente diga que instaló el QR
  - Al confirmar: pide FOTO del QR (la foto se procesa en
    capturar_foto_qr() del flow, que transiciona a 'activa')
"""
import json
import logging
import re

log = logging.getLogger("petra")


_VAL_SOSPECHOSOS = ("?", "ya te", "ya te la", "ya te lo", "ya les",
                    "cuanto", "cuándo", "cómo", "dónde", "disculp",
                    "olvid", "no sé", "no se", "mandame", "mandáme",
                    "escribí", "ya les dije", "ya les pase")


def _validar_direccion_completa(valor):
    """Valida la dirección como UN solo texto libre.

    Reglas blandas:
    - No vacío, no sospechoso.
    - Mínimo 8 chars útiles (letras/dígitos) — una dirección real tiene
      al menos un número + un nombre.
    """
    if not valor or not isinstance(valor, str):
        return False, None, "vacío"
    v = valor.strip()
    if not v or len(v) < 6 or len(v) > 300:
        return False, None, "muy corto o muy largo"
    low = v.lower()
    for s in _VAL_SOSPECHOSOS:
        if s in low:
            return False, None, "parece pregunta o queja, no dirección"
    utiles = re.sub(r"[^a-záéíóúñA-ZÁÉÍÓÚÑ0-9]", "", v)
    if len(utiles) < 8:
        return False, None, "no tiene suficientes letras/dígitos"
    return True, v[:280], None


# ===== Tools =====

TOOLS = [
    {
        "name": "capturar_direccion",
        "description": (
            "Guarda la dirección física COMPLETA donde enviar el kit. "
            "La agente la manda en UN solo mensaje libre, con todo lo "
            "que tenga: torre/casa, apto, barrio, ciudad. Vos la pasás "
            "ENTERA como un único string, sin desglosar — el bot la "
            "guarda tal cual.\n\n"
            "Ejemplos de llamadas válidas:\n"
            "• 'Torre 3 apto 702, Chapinero, Bogotá'\n"
            "• 'Casa 49, Portal del Norte, Zipaquirá'\n"
            "• 'Apto 15B, Chicó, Bogotá'\n"
            "• 'Carrera 15 #93-47 apto 301, Chapinero, Bogotá'\n\n"
            "NO llamés esta tool con '?', 'ya te lo dije', emoji suelto, "
            "preguntas, o un dato demasiado corto (ej. solo '702'): pedí "
            "aclaración por texto en su lugar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "direccion": {
                    "type": "string",
                    "description": "Dirección completa tal como la mandó la agente, en un único string (ej. 'Casa 49, Portal del Norte, Zipaquirá').",
                },
            },
            "required": ["direccion"],
            "additionalProperties": False,
        },
    },
    {
        "name": "marcar_qr_instalado",
        "description": (
            "La agente confirma que YA INSTALÓ el QR físico en el "
            "edificio. SOLO llamar cuando ella lo dice clarito — pulsó "
            "el botón '✅ Ya instalé el QR' O escribió 'ya instalé', "
            "'listo lo pegué', 'ya está instalado'. El bot entonces le "
            "pedirá una FOTO del QR instalado para validar. SOLO llamar "
            "si el estado actual es 'kit_enviado'."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "escalar_a_humano",
        "description": (
            "Marca que la agente necesita atención humana (no llegó el "
            "kit, problema con el QR, cualquier bloqueador)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {"type": "string"},
            },
            "required": ["motivo"],
            "additionalProperties": False,
        },
    },
]


# ===== Handlers =====

def _save_meta(conn, cid, meta):
    conn.execute(
        "UPDATE petra_contacto_roles SET metadata=%s::jsonb, updated_at=NOW() "
        "WHERE contacto_id=%s AND rol='agente_petra'",
        (json.dumps(meta), cid),
    )


def handle_capturar_direccion(args, ctx):
    """Guarda la dirección completa (un string libre) y avanza directo
    a kit_enviado. Envía el bloque de activación completo (confirmación
    + botón 'Ya instalé el QR'). Silencia al LLM."""
    import datetime as _dt
    from app import petra_wa
    from app.petra_agents.textos import t as _t
    from app.petra_agents.flow_onboarding_agente import (
        _set_estado, _sm, _send_buttons,
    )
    meta = ctx["meta"]
    direccion_raw = args.get("direccion") or ""
    ok, limpio, err = _validar_direccion_completa(direccion_raw)
    if not ok:
        # Reintentamos pedir con mensaje bonito, sin texto del LLM.
        try:
            petra_wa.send_text(
                ctx["telefono"],
                _t(
                    "activacion.msg_direccion_invalida",
                    fallback=(
                        "📍 Esa dirección no me quedó clara. Mandámela "
                        "*completa en un solo mensaje*, con todo lo que "
                        "tengas: torre o casa, apto, barrio y ciudad.\n\n"
                        "Ejemplos:\n"
                        "• _Torre 3 apto 702, Chapinero, Bogotá_\n"
                        "• _Casa 49, Portal del Norte, Zipaquirá_"
                    ),
                ),
            )
        except Exception:
            log.exception("[tools_kit] send fallo tel=%s", ctx["telefono"])
        return {"ok": False, "error": err, "_silenciar_llm": True}

    # Guardamos como string plano. Mantenemos compat: meta.direccion
    # pasa a ser {"completa": "<texto>"} para no romper lecturas que
    # esperaban un dict.
    meta["direccion"] = {"completa": limpio}
    meta["direccion_texto"] = limpio
    meta["direccion_capturada_at"] = _dt.datetime.utcnow().isoformat()
    meta.pop("direccion_paso", None)
    _set_estado(meta, "kit_enviado")
    meta["kit_enviado_at"] = _dt.datetime.utcnow().isoformat()
    meta["kit_enviado_tras_direccion"] = True
    _save_meta(ctx["conn"], ctx["cid"], meta)

    # Mensaje "Dirección confirmada" ya trae el botón ✨ Perfecto embebido.
    # Los adjuntos (videos del kit) se mandan en un segundo mensaje porque
    # WhatsApp no permite media + botones en un mismo interactive message.
    _send_buttons(
        ctx["telefono"],
        _t(
            "activacion.msg_direccion_completa",
            fallback=(
                "🎉 *¡Dirección confirmada!* 📍\n\n"
                "Ya pedimos tu kit de activación 📦\n"
                "En *2 a 3 días hábiles* te llega a esa dirección.\n\n"
                "💜 Mientras tanto, podés seguir con tu capacitación."
            ),
        ),
        [{"id": "ONBAG_KIT_PERFECTO", "title": "✨ Perfecto"}],
    )
    return {
        "ok": True,
        "direccion": limpio,
        "transicion": "kit_pendiente→kit_enviado",
        "_silenciar_llm": True,
    }


def handle_marcar_qr_instalado(args, ctx):
    """Pide la foto del QR instalado (NO avanza aún a activa; eso lo
    hace capturar_foto_qr cuando llega la imagen)."""
    from app.petra_agents.flow_onboarding_agente import _sm
    if ctx["meta"].get("estado_agente") != "kit_enviado":
        return {"ok": False, "error": f"estado invalido: {ctx['meta'].get('estado_agente')}"}
    _sm(ctx["telefono"],
        "activacion.msg_pedir_foto_qr",
        fallback=(
            "¡Casi listo! 💜\n\n"
            "Para activarte oficialmente necesito *una foto del QR instalado*. "
            "Seguí estos 3 pasos:\n\n"
            "1️⃣ Pegá el QR en un lugar *bien visible* (portería, lobby o ascensor).\n"
            "2️⃣ Ponete a *50 cm* y verificá que el código *completo* entre en la toma.\n"
            "3️⃣ Mandámela por este chat 📸\n\n"
            "💡 _Tip: con buena luz y sin reflejos se lee mejor._\n\n"
            "Apenas me llegue, te activo y empezás a recibir leads 🚀"
        ),
    )
    return {"ok": True, "pide": "foto_qr", "_silenciar_llm": True}


def handle_escalar(args, ctx):
    from app import db as _db
    motivo = (args.get("motivo") or "sin motivo")[:200]
    try:
        _db.marcar_requiere_humano(ctx["telefono"], f"[kit] {motivo}")
    except Exception:
        pass
    return {"ok": True, "motivo": motivo}


HANDLERS = {
    "capturar_direccion": handle_capturar_direccion,
    "marcar_qr_instalado": handle_marcar_qr_instalado,
    "escalar_a_humano": handle_escalar,
}


# ===== System prompt (varía por sub-estado) =====

def _get_faqs_prompt():
    try:
        from app.petra_agents import faq_kb
        return faq_kb.seccion_para_prompt()
    except Exception:
        return ""


def build_system_prompt(meta):
    estado = meta.get("estado_agente", "kit_pendiente")
    nombre = (meta.get("nombre") or "").split()[0] if meta.get("nombre") else ""
    direccion_txt = meta.get("direccion_texto") or (meta.get("direccion") or {}).get("completa") or ""
    dir_linea = f"✅ {direccion_txt}" if direccion_txt else "❌ FALTA — pedila completa en un solo mensaje"

    base = f"""Sos Petra, el bot de WhatsApp que acompaña a {nombre or "la agente"} en la fase de ACTIVACIÓN (kit + QR).

ESTADO INTERNO ACTUAL: {estado}

📋 DIRECCIÓN: {dir_linea}
"""

    if estado == "kit_pendiente":
        return base + """
TU ÚNICO TRABAJO ES LLAMAR LA TOOL CORRECTA. Cuando llamés una tool, el bot responde solo con un mensaje templateado — vos NO escribís texto en esos turnos.

CUÁNDO LLAMAR CADA TOOL:
1. Te manda la dirección (en UN mensaje libre, con torre/casa + apto + barrio + ciudad, o lo que tenga) → `capturar_direccion` pasando el TEXTO ENTERO tal cual, en el campo `direccion`. NO desglosés, NO extraigás campos, NO inventés nada. Ejemplos de llamadas:
   • Ella dice "Torre 3 apto 702, Chapinero, Bogotá" → `capturar_direccion({"direccion": "Torre 3 apto 702, Chapinero, Bogotá"})`.
   • Ella dice "Casa 49, Portal del Norte, Zipaquirá" → `capturar_direccion({"direccion": "Casa 49, Portal del Norte, Zipaquirá"})`.
   • Ella dice "vivo en el apto 15B del Chicó" → `capturar_direccion({"direccion": "apto 15B del Chicó"})`.
2. Pide ayuda / problema real → `escalar_a_humano`.

CUÁNDO RESPONDER CON TEXTO LIBRE (sin tool):
Solo preguntas abiertas sobre el kit:
- "¿cuánto tarda?" → 2-3 días hábiles.
- "¿qué incluye?" → QR físico para pegar + un regalito 💜.
- "¿puedo usar el QR mientras?" → sí, el digital ya lo tiene por el link.
Si manda algo que no es dirección ni pregunta clara ("?", "mmm", emoji suelto) → pedile *amable y bonito* que te mande la dirección completa en un solo mensaje.

REGLAS DURAS:
- 🚫 NUNCA inventés datos. Si no entendés qué mandó, NO llamés capturar_direccion.
- 🚫 NUNCA afirmés que el QR está instalado ni digas "generé tu QR".
- 🚫 NUNCA menciones otros conjuntos.
- ✅ Tono cálido, voseo.""" + _get_faqs_prompt()
    # kit_enviado
    return base + """
TU ÚNICO TRABAJO ES LLAMAR LA TOOL CORRECTA.

CUÁNDO LLAMAR CADA TOOL:
1. Confirma que instaló el QR (texto "ya instalé", "listo lo pegué", pulsa botón "✅ Ya instalé el QR") → `marcar_qr_instalado`. El bot le pide la FOTO para validar.
2. Problema real (no llegó, lo rompió, no sabe dónde pegar) → `escalar_a_humano`.

CUÁNDO RESPONDER CON TEXTO LIBRE (sin tool):
Preguntas abiertas sobre el kit o la instalación:
- "¿cómo lo instalo?" / "¿dónde lo pego?" → en la entrada / portería / lobby, bien visible, idealmente a la altura de los ojos.
- "¿cuándo llega el físico?" → 2-3 días hábiles.
- "¿puedo usar el digital mientras?" → sí, imprimilo y pegalo.

REGLAS DURAS:
- 🚫 NUNCA la actives sin la FOTO del QR. `marcar_qr_instalado` solo pide la foto; la activación real pasa cuando llega la imagen.
- 🚫 NUNCA inventés que ya está activada.
- ✅ Tono cálido, voseo.""" + _get_faqs_prompt()


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
        fase="kit",
    )
    if resultado.get("texto_respuesta"):
        try:
            petra_wa.send_text(telefono, resultado["texto_respuesta"])
        except Exception:
            log.exception("[tools_kit] send fallo tel=%s", telefono)
    log.info("[tools_kit] tel=%s estado=%s ok=%s tools=%s",
             telefono, meta.get("estado_agente"), resultado.get("ok"),
             [t["nombre"] for t in resultado.get("tools_llamadas", [])])
    return resultado
