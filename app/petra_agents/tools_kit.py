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
    # "N/A" es válido para torre
    if campo == "torre" and v.upper() in ("N/A", "NA", "NINGUNA", "NINGUNO"):
        return True, "N/A", None
    if _parece_sospechoso(v):
        return False, None, f"no parece un {NOMBRE_DIR[campo]} válido"
    return True, v[:80], None


# ===== Tools =====

TOOLS = [
    {
        "name": "capturar_direccion",
        "description": (
            "Guarda uno o más datos de la dirección física donde enviar "
            "el kit (torre, apto, barrio, ciudad). Podés pasar varios "
            "campos a la vez si vinieron juntos (ej. 'vivo en Torre 3 "
            "apto 702 barrio Chapinero Bogotá'). Para torre, aceptar "
            "'N/A' si no aplica. NO INVENTAR: si lo que te dio no parece "
            "un dato válido (ej. una pregunta, '?', 'ya te lo dije'), "
            "NO llamés esta tool: pedí aclaración por texto."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "torre": {"type": "string", "description": "Torre o bloque (ej. 'Torre 3', 'Bloque A', 'N/A')"},
                "apto": {"type": "string", "description": "Apartamento (ej. '702', 'Apto 15B', 'Casa 49')"},
                "barrio": {"type": "string", "description": "Barrio (ej. 'Chapinero', 'San Pablo')"},
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
    kit_enviado y manda el bloque de activación completo."""
    import datetime as _dt
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
                "transicion": "kit_pendiente→kit_enviado"}
    # Guardamos el próximo paso para que el prompt sepa qué pedir
    meta["direccion_paso"] = faltantes[0]
    _save_meta(ctx["conn"], ctx["cid"], meta)
    return {"ok": True, "guardados": guardados, "rechazados": rechazados,
            "faltantes": faltantes}


def handle_marcar_qr_instalado(args, ctx):
    """Pide la foto del QR instalado (NO avanza aún a activa; eso lo
    hace capturar_foto_qr cuando llega la imagen)."""
    from app.petra_agents.flow_onboarding_agente import _sm
    if ctx["meta"].get("estado_agente") != "kit_enviado":
        return {"ok": False, "error": f"estado invalido: {ctx['meta'].get('estado_agente')}"}
    _sm(ctx["telefono"],
        "activacion.msg_pedir_foto_qr",
        fallback=(
            "¡Buenísimo! 💜\n\nPara confirmar que el QR quedó bien "
            "instalado, mandame una *foto del QR pegado en el edificio* "
            "📸 (puede ser en la portería, el ascensor, el lobby — donde "
            "lo hayas puesto).\n\n"
            "Apenas me llegue la foto, te activo oficialmente 🚀"
        ),
    )
    return {"ok": True, "pide": "foto_qr"}


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
        proximo = faltantes[0] if faltantes else "nada"
        return base + f"""
Faltan estos datos de dirección: {", ".join(faltantes) or "ninguno"}
Próximo a pedir: *{proximo}*

TU TRABAJO:
1. Si te da uno o más datos de dirección (torre, apto, barrio, ciudad) → llamá `capturar_direccion` con los valores limpios (podés pasar varios juntos si vinieron juntos).
2. Si pregunta cuánto tarda el kit, qué incluye, etc. → respondé con texto natural: el kit llega en 2-3 días hábiles, incluye QR físico + regalo. NO llamés tool.
3. Si pide ayuda / no sabe qué poner / dice que no vive en el edificio → `escalar_a_humano`.

REGLAS DURAS:
- 🚫 NUNCA inventés datos. Si te dio "ya te lo dije", "?", "cuánto demora?" como respuesta → NO llamés capturar_direccion, pedí aclaración.
- 🚫 NUNCA MENCIONES el QR como ya instalado, ni digas "generé tu QR". El QR se activa SOLO cuando ella termine de darte la dirección Y pulse el botón 'Ya instalé el QR' después.
- 🚫 NUNCA menciones conjuntos ajenos (ej. "Condominio Monaco") a menos que ella lo diga ahora.
- ✅ Tono cálido, voseo rioplatense.
- ✅ Si pregunta por un dato que ya tenés, decile cuál tenés y confirmá si es correcto.
- ✅ Para torre, "N/A" es válido (si no aplica).
"""
    # kit_enviado
    return base + f"""
Dirección completa. La agente ya recibió el link del QR + instrucciones para instalarlo. Estamos esperando que ella confirme la instalación.

TU TRABAJO:
1. Si ella confirma que instaló el QR (texto "ya instalé", "listo lo pegué", pulsa botón '✅ Ya instalé el QR') → llamá `marcar_qr_instalado`. Esto hace que el bot le pida una FOTO del QR instalado para validar.
2. Si pregunta cómo instalarlo, dónde pegarlo, cuándo llega el kit físico → respondé con texto natural. El kit físico tarda 2-3 días hábiles. El QR digital ya lo recibió por el link. Puede imprimirlo y pegarlo mientras llega el físico.
3. Si tiene un problema (no le llegó, lo rompió, no sabe dónde pegar) → `escalar_a_humano`.

REGLAS DURAS:
- 🚫 NUNCA la actives sin que ella mande la FOTO del QR primero. `marcar_qr_instalado` SOLO pide la foto; la activación real pasa cuando llega la foto.
- 🚫 NUNCA inventés que ya la activaste.
- 🚫 NUNCA menciones direcciones específicas (ya está capturada, no la repitas inventando).
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
