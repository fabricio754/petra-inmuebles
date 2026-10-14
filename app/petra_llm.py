"""Endpoint OpenAI-compatible Chat Completions para ElevenLabs Custom LLM.

Arquitectura B'''' (un solo número Petra, chat + voz):
  ElevenLabs recibe WhatsApp/voz → llama a este endpoint como si fuera OpenAI
  → nosotros decidimos:
     1) Router determinista (petra_router) si hay estado activo o flujo portería.
        Respuesta instantánea (~50 ms), sin tocar Claude.
     2) Claude API (streaming) para conversación libre (dudas, chit-chat, ventas).

Expone:
  POST /petra/llm/v1/chat/completions   (OpenAI-compatible, soporta stream=true)
  GET  /petra/llm/v1/models             (lista mínima, para que ElevenLabs valide)
  GET  /petra/llm/health

Autenticación: Bearer token en Authorization. Validamos contra PETRA_LLM_TOKEN.
Si PETRA_LLM_TOKEN no está seteado, acepta cualquier token (modo dev).

Fuente de verdad del "teléfono" del interlocutor:
  - header x-elevenlabs-user-id   (preferido)
  - campo "user" del payload       (fallback)
  - metadata.phone / metadata.caller_number
"""
import json
import logging
import os
import re
import time
import uuid

from flask import Blueprint, Response, jsonify, request, stream_with_context

from app import petra_router


def _log_request(telefono, rama, texto_usuario, texto_asistente, mensajes_in,
                 stream, latencia_ms, error=None):
    """Guarda el turno en petra_llm_logs. Silencioso si falla."""
    try:
        from app import db as _db
        with _db._conexion_directa(timeout=5) as conn:
            conn.execute(
                """INSERT INTO petra_llm_logs
                   (telefono, rama, texto_usuario, texto_asistente,
                    mensajes_in, stream, latencia_ms, error)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (telefono, rama, texto_usuario, texto_asistente,
                 json.dumps(mensajes_in, ensure_ascii=False) if mensajes_in else None,
                 stream, latencia_ms, error),
            )
    except Exception:
        log.exception("[petra_llm] fallo guardando log")

log = logging.getLogger("petra")

petra_llm_bp = Blueprint("petra_llm", __name__, url_prefix="/petra/llm")

MODEL_ID = "petra-massi-1"

SYSTEM_PROMPT = (
    "Eres Massi, asistente de PETRA Inmuebles en Colombia. Hablas en español, "
    "tono cercano, breve y profesional. Puedes ayudar con: publicar inmuebles "
    "en edificios Petra (venta o arriendo), ver inventario del edificio, "
    "información sobre crédito hipotecario y acompañamiento con nuestro "
    "aliado financiero. No inventas datos ni tasas; si no sabes algo, dilo. "
    "Nunca pides contraseñas ni números de tarjeta. "
    "Si el usuario quiere publicar, ver inventario o editar un anuncio, "
    "pídele que escanee el QR de su edificio para activar el flujo guiado."
)

# =============================================================================
# Auth
# =============================================================================


def _check_auth():
    required = (os.environ.get("PETRA_LLM_TOKEN") or "").strip()
    if not required:
        return True
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    return header[7:].strip() == required


# =============================================================================
# Extracción de identidad del usuario
# =============================================================================

_E164 = re.compile(r"\+?(\d{10,15})")


def _extract_phone(payload):
    """Busca el teléfono del interlocutor en varios lugares."""
    # Header de ElevenLabs
    for h in ("x-elevenlabs-user-id", "x-user-id", "x-caller-number"):
        v = request.headers.get(h)
        if v:
            m = _E164.search(v)
            if m:
                return m.group(1)
    # Campo "user" OpenAI estándar
    u = payload.get("user")
    if isinstance(u, str):
        m = _E164.search(u)
        if m:
            return m.group(1)
    # Metadata libre
    meta = payload.get("metadata") or {}
    for k in ("phone", "caller_number", "wa_from", "from"):
        v = meta.get(k)
        if v:
            m = _E164.search(str(v))
            if m:
                return m.group(1)
    return None


def _last_user_text(messages):
    for m in reversed(messages or []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                # OpenAI vision-style content array
                for part in c:
                    if isinstance(part, dict) and part.get("type") == "text":
                        return part.get("text", "")
    return ""


# =============================================================================
# Router determinista → texto plano
# =============================================================================


def _router_text(telefono, texto):
    """Llama al router de Petra; aplana la lista de mensajes a un solo texto
    (los botones/list se degradan a enumeración en voz)."""
    try:
        out = petra_router.handle_inbound(telefono=telefono, texto=texto)
    except Exception:
        log.exception("[petra_llm] router falló")
        return None
    msgs = out.get("messages") or []
    partes = []
    for m in msgs:
        t = m.get("type")
        if t == "text":
            partes.append(m.get("text", ""))
        elif t == "buttons":
            partes.append(m.get("text", ""))
            opciones = [b.get("title", "") for b in m.get("buttons", [])]
            if opciones:
                partes.append("Opciones: " + ", ".join(opciones) + ".")
        elif t == "list":
            partes.append(m.get("text", ""))
            for sec in m.get("sections", []):
                for row in sec.get("rows", []):
                    partes.append(f"- {row.get('title', '')}")
    return "\n".join(p for p in partes if p).strip() or None


def _tiene_estado_activo(telefono):
    """Detecta si el contacto está en medio de un flujo determinista."""
    from app import db as _db
    try:
        with _db._conexion_directa(timeout=5) as conn:
            row = conn.execute(
                """SELECT 1 FROM petra_contactos c
                   JOIN petra_conversacion_estado e ON e.contacto_id = c.id
                   WHERE c.telefono = %s AND e.cerrado_en IS NULL
                   LIMIT 1""",
                (telefono,),
            ).fetchone()
            return row is not None
    except Exception:
        return False


def _contexto_contacto(telefono):
    """Arma un resumen compacto del contacto para inyectar al system prompt.
    Trae nombre, roles activos, oportunidades abiertas y último lead crédito."""
    from app import db as _db
    if not telefono:
        return None
    try:
        with _db._conexion_directa(timeout=5) as conn:
            crow = conn.execute(
                "SELECT id, nombre FROM petra_contactos WHERE telefono = %s",
                (telefono,),
            ).fetchone()
            lead_nombre = None
            try:
                lrow = conn.execute(
                    """SELECT nombre, monto_solicitado, destino_credito,
                              enviado_sureti_at
                       FROM pilot_leads WHERE telefono = %s""",
                    (telefono,),
                ).fetchone()
            except Exception:
                lrow = None
            if not crow and not lrow:
                return None
            partes = []
            if crow:
                cid, nombre = crow
                if nombre:
                    partes.append(f"Nombre: {nombre}.")
                    lead_nombre = nombre
                try:
                    roles = conn.execute(
                        """SELECT rol FROM petra_contacto_roles
                           WHERE contacto_id = %s AND estado = 'activo'""",
                        (cid,),
                    ).fetchall()
                    if roles:
                        partes.append("Roles activos: " + ", ".join(r[0] for r in roles) + ".")
                except Exception:
                    pass
                try:
                    ops = conn.execute(
                        """SELECT tipo, estado, inmueble_id, valor_cop
                           FROM petra_oportunidades
                           WHERE contacto_id = %s
                             AND estado NOT IN ('cerrado_ganado','cerrado_perdido')
                           ORDER BY updated_at DESC LIMIT 3""",
                        (cid,),
                    ).fetchall()
                    for tipo, estado, inm_id, valor in ops:
                        extra = f" sobre inmueble #{inm_id}" if inm_id else ""
                        monto = f" (${(valor or 0)/1_000_000:.0f}M)" if valor else ""
                        partes.append(f"Oportunidad abierta: {tipo} en estado {estado}{extra}{monto}.")
                except Exception:
                    pass
            if lrow:
                nombre_l, monto, destino, enviado = lrow
                if nombre_l and not lead_nombre:
                    partes.append(f"Nombre: {nombre_l}.")
                if monto:
                    partes.append(f"Lead crédito registrado: ${monto}M {destino or ''}.".strip() + ".")
                if enviado:
                    partes.append("Solicitud ya fue enviada al broker Sureti.")
            if not partes:
                return None
            return "DATOS DEL CONTACTO:\n- " + "\n- ".join(partes)
    except Exception:
        log.exception("[petra_llm] contexto_contacto fallo tel=%s", telefono)
        return None


# =============================================================================
# Claude streaming
# =============================================================================


def _claude_stream(messages, contexto_contacto=None):
    """Yields chunks de texto desde Claude. Devuelve None si falla el import
    o faltan credenciales (el caller cae a texto genérico)."""
    try:
        from anthropic import Anthropic
    except Exception:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY") or ""
    if not api_key:
        return None
    client = Anthropic(api_key=api_key)
    model = os.environ.get("PETRA_LLM_CLAUDE_MODEL", "claude-haiku-5-5")
    # Convertir messages OpenAI → Claude (system aparte, resto como user/assistant)
    sys_msg = SYSTEM_PROMPT
    if contexto_contacto:
        sys_msg = SYSTEM_PROMPT + "\n\n" + contexto_contacto + (
            "\n\nUsa estos datos para personalizar la conversación, pero no "
            "los recites textualmente a menos que el usuario los pida. Si no "
            "corresponden con lo que el usuario dice, confía en lo que te dice."
        )
    convo = []
    for m in messages or []:
        role = m.get("role")
        if role == "system":
            sys_msg = m.get("content") or sys_msg
            continue
        content = m.get("content")
        if isinstance(content, list):
            # concatenar partes de texto
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if role in ("user", "assistant") and content:
            convo.append({"role": role, "content": content})
    if not convo:
        return None

    def gen():
        with client.messages.stream(
            model=model,
            max_tokens=512,
            system=sys_msg,
            messages=convo,
        ) as stream:
            for text in stream.text_stream:
                yield text

    return gen()


# =============================================================================
# Formato SSE OpenAI
# =============================================================================


def _sse_chunk(resp_id, created, delta_content=None, finish_reason=None, role=None):
    delta = {}
    if role:
        delta["role"] = role
    if delta_content is not None:
        delta["content"] = delta_content
    payload = {
        "id": resp_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": MODEL_ID,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _completion_object(resp_id, created, content, finish_reason="stop"):
    return {
        "id": resp_id,
        "object": "chat.completion",
        "created": created,
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


# =============================================================================
# Endpoints
# =============================================================================


@petra_llm_bp.get("/health")
def health():
    return jsonify({"ok": True, "model": MODEL_ID})


@petra_llm_bp.get("/v1/models")
def models():
    return jsonify({
        "object": "list",
        "data": [{"id": MODEL_ID, "object": "model", "owned_by": "petra"}],
    })


@petra_llm_bp.post("/v1/chat/completions")
def chat_completions():
    if not _check_auth():
        return jsonify({"error": {"message": "unauthorized", "type": "auth_error"}}), 401
    try:
        payload = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"error": {"message": "invalid json", "type": "bad_request"}}), 400

    messages = payload.get("messages") or []
    stream = bool(payload.get("stream"))
    telefono = _extract_phone(payload)
    texto = _last_user_text(messages)
    t0 = time.time()

    resp_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    # --- Rama 1: router determinista (si hay teléfono y estado activo) ---
    router_resp = None
    if telefono:
        if _tiene_estado_activo(telefono) or _looks_like_porteria(texto):
            router_resp = _router_text(telefono, texto)

    if router_resp:
        _log_request(telefono, "router", texto, router_resp, messages, stream,
                     int((time.time() - t0) * 1000))
        if not stream:
            return jsonify(_completion_object(resp_id, created, router_resp))
        def gen_router():
            yield _sse_chunk(resp_id, created, role="assistant", delta_content="")
            yield _sse_chunk(resp_id, created, delta_content=router_resp)
            yield _sse_chunk(resp_id, created, finish_reason="stop")
            yield "data: [DONE]\n\n"
        return Response(stream_with_context(gen_router()), mimetype="text/event-stream")

    # --- Rama 2: Claude streaming (con contexto enriquecido del contacto) ---
    ctx = _contexto_contacto(telefono)
    claude_gen = _claude_stream(messages, contexto_contacto=ctx)
    if claude_gen is None:
        fallback = (
            "Soy Massi de PETRA. Puedo ayudarte a publicar un inmueble, ver "
            "inventario de tu edificio o resolver dudas de crédito. ¿Qué "
            "necesitas?"
        )
        _log_request(telefono, "fallback", texto, fallback, messages, stream,
                     int((time.time() - t0) * 1000),
                     error="claude no disponible (sin ANTHROPIC_API_KEY)")
        if not stream:
            return jsonify(_completion_object(resp_id, created, fallback))
        def gen_fb():
            yield _sse_chunk(resp_id, created, role="assistant", delta_content="")
            yield _sse_chunk(resp_id, created, delta_content=fallback)
            yield _sse_chunk(resp_id, created, finish_reason="stop")
            yield "data: [DONE]\n\n"
        return Response(stream_with_context(gen_fb()), mimetype="text/event-stream")

    if not stream:
        # Agregamos todo y devolvemos como objeto completo
        full = "".join(list(claude_gen))
        _log_request(telefono, "claude", texto, full, messages, False,
                     int((time.time() - t0) * 1000))
        return jsonify(_completion_object(resp_id, created, full))

    def gen_claude():
        acumulado = []
        err = None
        yield _sse_chunk(resp_id, created, role="assistant", delta_content="")
        try:
            for chunk in claude_gen:
                if chunk:
                    acumulado.append(chunk)
                    yield _sse_chunk(resp_id, created, delta_content=chunk)
        except Exception as exc:
            log.exception("[petra_llm] claude stream falló")
            err = str(exc)[:500]
        yield _sse_chunk(resp_id, created, finish_reason="stop")
        yield "data: [DONE]\n\n"
        _log_request(telefono, "claude", texto, "".join(acumulado), messages,
                     True, int((time.time() - t0) * 1000), error=err)

    return Response(stream_with_context(gen_claude()), mimetype="text/event-stream")


# =============================================================================
# Observabilidad: ver últimos N requests
# =============================================================================


@petra_llm_bp.get("/logs")
def logs():
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401
    try:
        limit = min(int(request.args.get("limit", 50)), 500)
    except Exception:
        limit = 50
    telefono = request.args.get("telefono")
    from app import db as _db
    with _db._conexion_directa(timeout=5) as conn:
        if telefono:
            rows = conn.execute(
                """SELECT id, telefono, rama, texto_usuario, texto_asistente,
                          stream, latencia_ms, error, created_at
                   FROM petra_llm_logs
                   WHERE telefono = %s
                   ORDER BY created_at DESC LIMIT %s""",
                (telefono, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, telefono, rama, texto_usuario, texto_asistente,
                          stream, latencia_ms, error, created_at
                   FROM petra_llm_logs
                   ORDER BY created_at DESC LIMIT %s""",
                (limit,),
            ).fetchall()
    items = []
    for r in rows:
        items.append({
            "id": r[0], "telefono": r[1], "rama": r[2],
            "texto_usuario": r[3], "texto_asistente": r[4],
            "stream": r[5], "latencia_ms": r[6], "error": r[7],
            "created_at": r[8].isoformat() if r[8] else None,
        })
    return jsonify({"items": items, "count": len(items)})


# Palabras que indican intención clara de flujo portería
_PORTERIA_WORDS = (
    "publicar", "vender", "arrendar", "inventario", "ver inmuebles",
    "editar anuncio", "mis anuncios", "qr",
)


def _looks_like_porteria(texto):
    if not texto:
        return False
    t = texto.strip().lower()
    return any(w in t for w in _PORTERIA_WORDS)
