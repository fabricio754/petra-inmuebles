"""Tools de las fases KIT_PENDIENTE y KIT_ENVIADO del flow.

kit_pendiente:
  - Capturar dirección (torre, apto, barrio, ciudad)
  - Al completar: avanza auto a kit_enviado y manda el bloque completo
    (texto + video + link QR + botón "Ya instalé el QR")

kit_enviado:
  - Esperar que la agente diga que instaló el QR
  - Al confirmar: pide FOTO del QR (la foto se procesa en
    capturar_foto_qr() del flow, que transiciona a 'activa')
"""
import json
import logging
import re

log = logging.getLogger("petra")


CAMPOS_DIR = ["torre", "apto", "barrio", "ciudad"]
NOMBRE_DIR = {
    "torre": "torre o bloque",
    "apto": "número de apartamento",
    "barrio": "barrio",
    "ciudad": "ciudad",
}

PREGUNTA_DIR = {
    "torre": ("kit.pedir_torre",
              "Me faltaría la *torre o bloque*. Mandámela como *Torre 3* / "
              "*Bloque A*. Si vivís en casa individual, respondé *N/A*."),
    "apto": ("kit.pedir_apto",
             "Me faltaría el *número de apto o casa*. Mandámelo como "
             "*702*, *15B* o *Casa 49*."),
    "barrio": ("kit.pedir_barrio",
               "Me faltaría el *barrio*. Mandámelo como el nombre nomás "
               "(ej. *Chapinero*, *Portal del Norte*)."),
    "ciudad": ("kit.pedir_ciudad",
               "Me faltaría la *ciudad* (ej. *Bogotá*, *Zipaquirá*)."),
}

CONFIRMACION_DIR = {
    "torre": ("kit.ok_torre", "✅ Torre: *{valor}*."),
    "apto": ("kit.ok_apto", "✅ Apto/Casa: *{valor}*."),
    "barrio": ("kit.ok_barrio", "✅ Barrio: *{valor}*."),
    "ciudad": ("kit.ok_ciudad", "✅ Ciudad: *{valor}*."),
}

RECHAZO_DIR = {
    "torre": "La *torre* no me quedó clara. Mandámela como *Torre 3* / *Bloque A* / *N/A* 🙏",
    "apto": "El *apto o casa* no me quedó claro. Mandámelo como *702*, *15B* o *Casa 49* 🙏",
    "barrio": "El *barrio* no me quedó claro. Mandámelo como el nombre nomás (ej. *Chapinero*) 🙏",
    "ciudad": "La *ciudad* no me quedó clara. Mandámela con una palabra (ej. *Bogotá*) 🙏",
}

_VAL_SOSPECHOSOS = ("?", "ya te", "ya te la", "ya te lo", "ya les", "cuanto",
                    "cuándo", "cómo", "dónde", "donde", "disculp", "olvid",
                    "no sé", "no se", "mandame", "mandáme", "escribí",
                    "ya les dije", "ya les pase")


def _parece_sospechoso(valor):
    if not valor or not isinstance(valor, str):
        return True
    v = valor.strip().lower()
    if not v or v in ("si", "no", "ok", "listo"):
        return True
    if len(v) < 1 or len(v) > 120:
        return True
    for s in _VAL_SOSPECHOSOS:
        if s in v:
            return True
    return False


def _validar_dir(campo, valor):
    if not valor or not isinstance(valor, str):
        return False, None, "vacío"
    v = valor.strip()
    # "N/A" es válido para torre Y para apto (ej. casa individual sin apto).
    if campo in ("torre", "apto") and v.upper() in ("N/A", "NA", "NINGUNA", "NINGUNO", "-"):
        return True, "N/A", None
    if _parece_sospechoso(v):
        return False, None, f"no parece un {NOMBRE_DIR[campo]} válido"
    # Rechazar valores sin ninguna letra ni dígito útil (ej. "??", "mmm", ".").
    import re as _re
    utiles = _re.sub(r"[^a-záéíóúñA-ZÁÉÍÓÚÑ0-9]", "", v)
    if len(utiles) < 2:
        return False, None, f"'{v}' no parece un {NOMBRE_DIR[campo]} válido"
    return True, v[:80], None


# ===== Tools =====

TOOLS = [
    {
        "name": "capturar_direccion",
        "description": (
            "Guarda uno o más datos de la dirección física donde enviar "
            "el kit (torre, apto, barrio, ciudad). SIEMPRE que la agente "
            "mande datos de dirección, extraé TODOS los campos que puedas "
            "del mensaje en una sola llamada — no esperes a tener uno por "
            "turno.\n\n"
            "REGLAS DE INFERENCIA (importantes en Colombia):\n"
            "• Si menciona 'Casa X' (ej. 'Casa 49') → es CASA individual, "
            "no apartamento: poné torre='N/A' y apto='Casa 49'.\n"
            "• Si menciona 'Torre X' + 'apto Y' → torre='Torre X', apto='Y'.\n"
            "• Si menciona solo un número (ej. '702') → es apto, torre queda "
            "pendiente de preguntar.\n"
            "• Si menciona 'conjunto X' o 'edificio X' NO es ni torre ni "
            "barrio — es el conjunto que ya tenemos aparte, ignoralo.\n"
            "• Si NO está claro un campo, dejalo fuera (no lo inventes).\n\n"
            "NO llamés esta tool con '?', 'ya te lo dije', emoji suelto, "
            "preguntas: pedí aclaración por texto en su lugar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "torre": {"type": "string", "description": "Torre o bloque, o 'N/A' si vive en casa individual (ej. 'Torre 3', 'Bloque A', 'N/A')"},
                "apto": {"type": "string", "description": "Apartamento o casa (ej. '702', 'Apto 15B', 'Casa 49', 'N/A')"},
                "barrio": {"type": "string", "description": "Barrio o nombre del conjunto si el barrio no se menciona (ej. 'Chapinero', 'Portal del Norte')"},
                "ciudad": {"type": "string", "description": "Ciudad (ej. 'Bogotá', 'Zipaquirá')"},
            },
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
    """Guarda campos de dirección. Si quedan todos capturados, avanza a
    kit_enviado y manda el bloque de activación completo. Si falta algún
    campo, el handler mismo manda la confirmación + próxima pregunta
    (templateado) para evitar alucinaciones del LLM."""
    import datetime as _dt
    from app import petra_wa
    from app.petra_agents.textos import t as _t
    meta = ctx["meta"]
    direccion = dict(meta.get("direccion") or {})
    guardados = {}
    rechazados = {}
    for campo in CAMPOS_DIR:
        if campo not in args:
            continue
        ok, limpio, err = _validar_dir(campo, args[campo])
        if ok:
            direccion[campo] = limpio
            guardados[campo] = limpio
        else:
            rechazados[campo] = err
    if guardados:
        meta["direccion"] = direccion
        _save_meta(ctx["conn"], ctx["cid"], meta)
    faltantes = [c for c in CAMPOS_DIR if not direccion.get(c)]

    # Si no capturamos ni rechazamos nada (args vacío o campos desconocidos),
    # devolvemos sin silenciar — dejamos que el LLM responda (puede ser
    # pregunta abierta sobre el kit, ej. "¿cuánto tarda?").
    if not guardados and not rechazados:
        return {"guardados": {}, "rechazados": {}, "faltantes": faltantes,
                "nota": "no se guardó nada — explicá al user qué necesitás"}

    if not faltantes:
        # ¡Dirección completa! Avanzar a kit_enviado + bloque completo
        from app.petra_agents.flow_onboarding_agente import (
            _set_estado, _sm, _send_buttons,
        )
        from app.petra_agents.textos import t as _t
        meta.pop("direccion_paso", None)
        meta["direccion_capturada_at"] = _dt.datetime.utcnow().isoformat()
        _set_estado(meta, "kit_enviado")
        meta["kit_enviado_at"] = _dt.datetime.utcnow().isoformat()
        meta["kit_enviado_tras_direccion"] = True
        _save_meta(ctx["conn"], ctx["cid"], meta)
        _sm(ctx["telefono"],
            "activacion.msg_direccion_completa",
            fallback=(
                "🎉 *¡Dirección confirmada!* 📍\n\nYa pedimos tu kit de "
                "activación. En *2-3 días hábiles* lo recibís."
            ),
        )
        import time as _tm
        _tm.sleep(1)
        _send_buttons(ctx["telefono"], _t(
            "activacion.msg_qr_boton",
            fallback="Cuando lo tengas instalado, pulsá el botón 👇",
        ), [{"id": "ONBAG_QR_INSTALADO",
             "title": "✅ Ya instalé el QR"}])
        return {"ok": True, "guardados": guardados, "faltantes": [],
                "transicion": "kit_pendiente→kit_enviado", "_silenciar_llm": True}
    # Captura parcial. Armamos mensaje: confirmaciones + rechazos +
    # pregunta por el próximo campo. Lo envía el handler, el LLM queda
    # silenciado para no agregar texto encima.
    meta["direccion_paso"] = faltantes[0]
    _save_meta(ctx["conn"], ctx["cid"], meta)
    partes = []
    for campo, valor in guardados.items():
        clave, fallback = CONFIRMACION_DIR[campo]
        partes.append(_t(clave, fallback=fallback, valor=valor))
    for campo, _err in rechazados.items():
        partes.append(RECHAZO_DIR[campo])
    if guardados:
        prox = faltantes[0]
        clave, fallback = PREGUNTA_DIR[prox]
        partes.append(_t(clave, fallback=fallback))
    try:
        petra_wa.send_text(ctx["telefono"], "\n\n".join(partes))
    except Exception:
        log.exception("[tools_kit] send fallo tel=%s", ctx["telefono"])
    return {"ok": True, "guardados": guardados, "rechazados": rechazados,
            "faltantes": faltantes,
            "proximo_pedido": NOMBRE_DIR[faltantes[0]],
            "_silenciar_llm": True}


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

def build_system_prompt(meta):
    estado = meta.get("estado_agente", "kit_pendiente")
    nombre = (meta.get("nombre") or "").split()[0] if meta.get("nombre") else ""
    direccion = meta.get("direccion") or {}
    datos_dir = []
    for c in CAMPOS_DIR:
        v = direccion.get(c)
        datos_dir.append(f"  - {NOMBRE_DIR[c]}: {'✅ ' + str(v) if v else '❌ FALTA'}")
    datos_dir_str = "\n".join(datos_dir)
    faltantes = [NOMBRE_DIR[c] for c in CAMPOS_DIR if not direccion.get(c)]

    base = f"""Sos Petra, el bot de WhatsApp que acompaña a {nombre or "la agente"} en la fase de ACTIVACIÓN (kit + QR).

ESTADO INTERNO ACTUAL: {estado}

📋 DIRECCIÓN CAPTURADA:
{datos_dir_str}
"""

    if estado == "kit_pendiente":
        return base + """
TU ÚNICO TRABAJO ES LLAMAR LA TOOL CORRECTA. Cuando llamés una tool, el bot responde solo con un mensaje templateado — vos NO escribís texto en esos turnos.

CUÁNDO LLAMAR CADA TOOL:
1. Te da datos de dirección en UN MENSAJE LIBRE (ej. "Casa 49, Portal del Norte, Zipaquirá" o "Torre 3 apto 702, Chapinero, Bogotá") → `capturar_direccion` extrayendo TODOS los campos que puedas en una sola llamada. Reglas clave:
   • "Casa X" → torre="N/A", apto="Casa X" (es casa individual, muy común en Colombia).
   • "Torre X" + "apto Y" → torre="Torre X", apto="Y".
   • Solo un número suelto → apto, torre queda pendiente.
   • NUNCA inventes un campo que no está en el mensaje. Dejalo fuera y el bot lo pide después.
2. Pide ayuda / no sabe qué poner / problema real → `escalar_a_humano`.

CUÁNDO RESPONDER CON TEXTO LIBRE (sin tool):
Solo preguntas abiertas sobre el kit:
- "¿cuánto tarda?" → 2-3 días hábiles.
- "¿qué incluye?" → QR físico para pegar + un regalito.
- "¿puedo usar el QR mientras?" → sí, el digital ya lo tiene por el link.
Breve, natural, SIN llamar tool.

REGLAS DURAS:
- 🚫 NUNCA inventés datos. Si te dio "ya te lo dije", "?", "mmm", pregunta → NO llamés capturar_direccion.
- 🚫 NUNCA afirmés que el QR está instalado ni digas "generé tu QR".
- 🚫 NUNCA menciones otros conjuntos."""
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
- 🚫 NUNCA inventés que ya está activada."""
- ✅ Tono cálido, voseo.
"""


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
