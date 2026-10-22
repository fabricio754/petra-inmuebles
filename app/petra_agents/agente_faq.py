"""Mini-agente FAQ aislado (M4.B).

Un único punto de entrada reusado en:
  1. Botón "Pedir ayuda" (Fase 7, ONBAG_PC_AYUDA).
  2. Escape hatch global: inscripcion_v3 / capacitacion_v3 /
     autorizacion_v3 lo invocan antes de contestar "no te entendí".

Contrato público:
    responder(telefono, texto_usuario, meta,
              ultimo_paso_descripcion=None, ultimo_boton=None,
              cid=None) -> dict

Devuelve::
    {"texto": str,         # texto a enviar
     "boton": dict|None,    # botón {id, title} para reofrecer el paso
     "no_era_pregunta": bool,  # si True → el caller sigue su flow normal
     "escalar": bool,       # si True → el caller marca requiere_humano
     "fuente": str}         # 'llm' | 'heuristica' | 'fallback' | 'passthrough'

Nunca envía mensajes por sí mismo — eso queda del lado del caller
(para que respete el estado_key del panel de botones).
"""
import logging
import os
import re
import time

log = logging.getLogger("petra")

# TODO: migrar a claude-haiku-5-5 cuando el resto del stack suba.
# Hoy mantenemos el mismo modelo que `_base.MODEL_PRIMARY` para no
# introducir una migración ahora.
_MODEL_DEFAULT = os.environ.get("PETRA_FAQ_MODEL",
                                os.environ.get("PETRA_ORCH_MODEL",
                                               "claude-haiku-4-5"))
_MAX_TOKENS = 512

# Patrones de "quiero hablar con un humano / que me llamen".
_PAL_LLAMADA = (
    "llamame", "llámame", "llamenme", "llámenme", "me llamen",
    "me llame", "quiero una llamada", "llamada",
    "hablar con alguien", "hablar con un humano",
    "hablar con asesor", "asesor",
)
# Insistencia tras la primera oferta de chat.
_PAL_INSISTE_LLAMADA = (
    "urgente", "no, necesito", "no necesito", "igual llámame",
    "igual llamame", "sí llámame", "si llamame", "por favor llamen",
    "mejor llamen",
)
# Palabras que cierran el modo FAQ (en Fase 7 "Pedir ayuda").
_PAL_FIN_FAQ = (
    "listo", "gracias", "ok", "dale", "entendido", "resolví",
    "resolvi", "ya está", "ya esta", "perfecto",
)
# Payloads de botón ONBAG_* → NUNCA son pregunta.
_RE_BOTON = re.compile(r"^(ONBAG_|CUSTOM_)", re.IGNORECASE)
# Preguntas o pedidos típicos → sí son FAQ.
_RE_INTERROGACION = re.compile(
    r"[¿?]|^(cu[aá]nto|c[oó]mo|cu[aá]ndo|d[oó]nde|qu[eé]|por qu[eé]|"
    r"para qu[eé]|cu[aá]l)\b|\b(ayuda|duda|pregunta|no entiendo|"
    r"explic|cont[aá]me|cu[eé]ntame)\b",
    re.IGNORECASE,
)


def _es_payload_boton(texto):
    if not texto:
        return False
    return bool(_RE_BOTON.match(texto.strip()))


def _pide_llamada(texto_lc):
    return any(p in texto_lc for p in _PAL_LLAMADA)


def _insiste_llamada(texto_lc):
    return any(p in texto_lc for p in _PAL_INSISTE_LLAMADA)


def _cerraste_faq(texto_lc):
    t = texto_lc.strip(" .!¡?¿,;:")
    return t in _PAL_FIN_FAQ


def _parece_pregunta(texto):
    """Heurística: ¿este input es una pregunta/duda, o un dato real?"""
    if not texto:
        return False
    t = texto.strip()
    if _es_payload_boton(t):
        return False
    # Muy corto y sin interrogación → probablemente un dato (nombre,
    # cédula, ciudad) o un ack.
    if len(t) <= 4 and "?" not in t and "¿" not in t:
        return False
    return bool(_RE_INTERROGACION.search(t))


def _build_system(ultimo_paso_descripcion=None, ultimo_boton=None):
    """System prompt en 2 bloques: rol fijo + FAQ KB con cache."""
    rol = (
        "Sos el agente FAQ de Petra Inmuebles. Respondés dudas de "
        "agentes inmobiliarias en tono cálido, breve (máx 3-4 líneas), "
        "voseo rioplatense. NUNCA inventes datos (comisiones, tiempos, "
        "precios): si no sabés, decí 'No tengo esa info exacta, "
        "dejame averiguar' sin más. Si la agente pide hablar con un "
        "humano o que la llamen, ofrecé primero resolver por chat. "
        "Si insiste, decile que avisás al equipo."
    )
    paso_hint = ""
    if ultimo_paso_descripcion:
        paso_hint = (
            f"\n\nContexto del flow: la agente estaba en el paso "
            f"'{ultimo_paso_descripcion}'. Después de responder la duda, "
            "ofrecé continuar con ese paso usando la tool `volver_al_flow`."
        )
    # Bloque FAQ KB desde faq_kb (ya cacheado 5 min en memoria).
    try:
        from app.petra_agents import faq_kb
        bloque_faqs = faq_kb.obtener_bloque_faqs() or ""
    except Exception:
        log.exception("[faq] no pude cargar FAQ KB — sigo sin ella")
        bloque_faqs = ""
    # Opcional: docs/petra_agente_faq.md si existe.
    try:
        import pathlib
        md = pathlib.Path(__file__).resolve().parents[2] / "docs" / "petra_agente_faq.md"
        if md.exists():
            bloque_faqs = bloque_faqs + "\n\n" + md.read_text(encoding="utf-8")
    except Exception:
        pass

    bloques = [
        {"type": "text", "text": rol + paso_hint,
         "cache_control": {"type": "ephemeral"}},
    ]
    if bloque_faqs.strip():
        bloques.append({
            "type": "text",
            "text": (
                "═══ BASE DE CONOCIMIENTO (FAQ) ═══\n"
                + bloque_faqs
                + "\n═══ fin FAQ ═══"
            ),
            "cache_control": {"type": "ephemeral"},
        })
    return bloques


def _tools_schema():
    return [{
        "name": "volver_al_flow",
        "description": (
            "Llamalo UNA sola vez cuando ya respondiste la duda y querés "
            "reofrecer el próximo paso del onboarding. Sin argumentos."
        ),
        "input_schema": {"type": "object", "properties": {}},
    }]


def _llamar_llm(texto_usuario, system_bloques):
    """Un turno manual con tool-use. Devuelve (texto, pidio_volver, err)."""
    try:
        from anthropic import Anthropic
    except Exception as exc:
        return None, False, f"sin_anthropic:{exc}"
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return None, False, "sin_api_key"
    workspace = (os.environ.get("ANTHROPIC_WORKSPACE_ID") or "").strip()
    kwargs = {}
    if workspace:
        kwargs["default_headers"] = {"anthropic-workspace-id": workspace}
    client = Anthropic(api_key=api_key, **kwargs)
    try:
        resp = client.messages.create(
            model=_MODEL_DEFAULT,
            max_tokens=_MAX_TOKENS,
            system=system_bloques,
            tools=_tools_schema(),
            messages=[{"role": "user", "content": texto_usuario}],
        )
    except Exception as exc:
        log.exception("[faq] anthropic fallo")
        return None, False, f"api:{str(exc)[:120]}"
    partes_texto = []
    pidio_volver = False
    for b in resp.content:
        tipo = getattr(b, "type", "")
        if tipo == "text":
            partes_texto.append(b.text)
        elif tipo == "tool_use" and getattr(b, "name", "") == "volver_al_flow":
            pidio_volver = True
    return (" ".join(partes_texto).strip() or None), pidio_volver, None


def _respuesta_llamada_1(ultimo_boton):
    """Primera respuesta a 'llámame': ofrecemos chat primero."""
    texto = (
        "¡Dale! 💜 Antes de pedirle al equipo que te llame, "
        "contame por acá qué te preocupa — un montón de cosas las "
        "podemos ayudar a resolver *por chat* y es más rápido. "
        "Si no, le aviso a un asesor para que te contacte."
    )
    return {
        "texto": texto, "boton": ultimo_boton,
        "no_era_pregunta": False, "escalar": False,
        "fuente": "heuristica",
    }


def _respuesta_llamada_insiste(ultimo_boton):
    """Insistencia tras oferta de chat: escalamos."""
    texto = (
        "Entendido 💜 Le paso aviso al equipo Petra para que te "
        "contacte lo antes posible. Si te acordás de algo que te "
        "podemos adelantar por acá, escribime igual."
    )
    return {
        "texto": texto, "boton": None,
        "no_era_pregunta": False, "escalar": True,
        "fuente": "heuristica",
    }


def _respuesta_fallback(ultimo_boton):
    return {
        "texto": (
            "Perdoná, se me cruzaron los cables 🤖 ¿Querés que un "
            "asesor Petra se comunique?"
        ),
        "boton": {"id": "ONBAG_HABLAR_HUMANO",
                  "title": "👤 Hablar con asesor"},
        "no_era_pregunta": False, "escalar": False,
        "fuente": "fallback",
    }


def responder(telefono, texto_usuario, meta,
              ultimo_paso_descripcion=None, ultimo_boton=None, cid=None):
    """Entry-point del mini-agente FAQ.

    No manda nada por WhatsApp — devuelve `texto` + `boton` opcional.
    El caller decide cómo entregarlos (send_text / send_buttons).
    """
    t_raw = (texto_usuario or "").strip()
    t_lc = t_raw.lower()

    # 0) Payload de botón → no es pregunta, dejamos pasar.
    if _es_payload_boton(t_raw):
        return {"texto": "", "boton": None,
                "no_era_pregunta": True, "escalar": False,
                "fuente": "passthrough"}

    # 0.5) Fin del modo FAQ (sólo relevante si el caller lo usa como ciclo).
    if _cerraste_faq(t_lc):
        return {
            "texto": "", "boton": ultimo_boton,
            "no_era_pregunta": False, "escalar": False,
            "fuente": "heuristica", "cerrar_faq": True,
        }

    # 1) Pide llamada: respuesta heurística (sin LLM) para que la
    #    detección sea confiable aun sin API key.
    #    Si ya ofrecimos chat (meta.faq_ofrecimos_chat) e insiste → escalamos.
    if _pide_llamada(t_lc) or _insiste_llamada(t_lc):
        ya_ofrecimos = bool((meta or {}).get("faq_ofrecimos_chat"))
        if ya_ofrecimos or _insiste_llamada(t_lc):
            # Marcamos la escalación en meta si viene cid; lo hace el caller.
            return _respuesta_llamada_insiste(ultimo_boton)
        return _respuesta_llamada_1(ultimo_boton)

    # 2) Si NO parece pregunta → dejamos pasar al flow determinístico.
    if not _parece_pregunta(t_raw):
        return {"texto": "", "boton": None,
                "no_era_pregunta": True, "escalar": False,
                "fuente": "passthrough"}

    # 3) LLM con prompt caching sobre la FAQ KB.
    t0 = time.time()
    system_bloques = _build_system(ultimo_paso_descripcion, ultimo_boton)
    texto_resp, pidio_volver, err = _llamar_llm(t_raw, system_bloques)
    latencia = int((time.time() - t0) * 1000)
    if err or not texto_resp:
        log.info("[faq] fallback (err=%s lat=%dms)", err, latencia)
        return _respuesta_fallback(ultimo_boton)
    # Si el LLM pidió volver_al_flow o venimos con botón, agregamos
    # "¿Seguimos con <paso>?" si corresponde.
    boton = None
    if ultimo_boton:
        boton = ultimo_boton
        if ultimo_paso_descripcion and "seguimos" not in texto_resp.lower():
            texto_resp = (
                texto_resp
                + f"\n\n¿Seguimos con {ultimo_paso_descripcion}? 👇"
            )
    elif pidio_volver and ultimo_paso_descripcion:
        texto_resp = (
            texto_resp
            + f"\n\n¿Seguimos con {ultimo_paso_descripcion}?"
        )
    log.info("[faq] ok lat=%dms boton=%s", latencia, bool(boton))
    return {
        "texto": texto_resp, "boton": boton,
        "no_era_pregunta": False, "escalar": False,
        "fuente": "llm",
    }
