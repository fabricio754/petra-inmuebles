"""Infraestructura de tool-calling para agentes Petra.

Patrón world-class para flujos conversacionales: en vez de switch/case
determinístico sobre texto, el LLM lee cada mensaje y decide:
  - Responder con texto natural (default)
  - Llamar una "tool" (función Python registrada) con parámetros validados

Las tools tienen schemas estrictos. Si el LLM intenta pasar valores
inválidos, se le rechaza y re-pregunta. Si intenta inventar algo que
no es una tool, no tiene efecto (solo texto conversacional).

Guardrails incluidos:
  - Validación de inputs por tool
  - Logging de cada llamada con metadata (quién, cuándo, args, resultado)
  - Fallback silencioso: si el LLM falla (timeout, API down), delega al
    flow determinístico tradicional.

Uso típico:
    from app.petra_agents import toolcalling as tc
    tools = [TOOL_X, TOOL_Y, ...]  # list of tool defs (anthropic shape)
    handlers = {"X": handle_x, "Y": handle_y}  # name → callable(args, ctx)
    tc.ejecutar_turno(telefono, texto, meta, tools, handlers, system_prompt)
"""
import json
import logging
import os
import time

log = logging.getLogger("petra")

MODEL = os.environ.get("PETRA_TOOLCALLING_MODEL", "claude-haiku-4-5")
MAX_TURNS_LLM = 3  # cantidad máxima de iteraciones LLM<->tools por mensaje
MAX_TOKENS = 1024
RETRY_INTENTOS = 2  # retries del LLM primario con backoff antes de encolar
RETRY_BACKOFF_BASE = 0.8  # segundos (0.8, 1.6)
MENSAJE_FALLO_HUMANO = (
    "💜 Perdón, se me enredó el sistema por un momento. "
    "Te escribo en 5 minutos apenas lo arregle. ¡No te muevas!"
)


def _log_tool_call(telefono, cid, fase, mensaje_user, tool_nombre,
                   tool_args, tool_resultado, ok, latencia_ms, modelo):
    """Inserta una fila en petra_tool_calls. Nunca lanza — si falla el
    log no debe tumbar la conversación. Fire-and-forget práctico."""
    try:
        from app import db as _db
        from psycopg.types.json import Jsonb
        # Resultado puede ser string, dict o cosa rara. Normalizamos a dict/
        # str para JSONB.
        if isinstance(tool_resultado, dict):
            res_json = tool_resultado
        elif isinstance(tool_resultado, str):
            res_json = {"_str": tool_resultado[:2000]}
        else:
            res_json = {"_repr": str(tool_resultado)[:2000]}
        with _db._conexion_directa(timeout=3) as conn:
            conn.execute(
                "INSERT INTO petra_tool_calls "
                "(telefono, cid, fase, mensaje_user, tool_nombre, "
                " tool_args, tool_resultado, ok, latencia_ms, modelo) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (telefono, cid, fase or "desconocida",
                 (mensaje_user or "")[:2000],
                 tool_nombre,
                 Jsonb(tool_args or {}),
                 Jsonb(res_json),
                 bool(ok), latencia_ms, modelo),
            )
    except Exception:
        log.exception("[toolcalling] _log_tool_call fallo tel=%s tool=%s",
                      telefono, tool_nombre)


def _llm_client():
    """Construye cliente Anthropic. Devuelve None si no hay API key.
    Si ANTHROPIC_WORKSPACE_ID está seteado, lo manda como header en cada
    request (requerido por workspace-scoped keys)."""
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return None
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    kwargs = {"api_key": api_key}
    workspace_id = (os.environ.get("ANTHROPIC_WORKSPACE_ID") or "").strip()
    if workspace_id:
        kwargs["default_headers"] = {"anthropic-workspace-id": workspace_id}
    return Anthropic(**kwargs)


def ejecutar_turno(telefono, texto, contexto, tools, handlers,
                   system_prompt, historial_prev=None, modo_debug=False,
                   fase=None):
    """Procesa UN mensaje de la agente usando tool-calling.

    Args:
        telefono: str, para log
        texto: str, lo que la agente mandó
        contexto: dict con estado actual, datos ya capturados, cid, etc.
                  Se pasa a los handlers para que puedan actualizar DB.
        tools: list[dict], tool definitions en formato Anthropic
        handlers: dict[str, callable(args, contexto)] — se ejecuta cuando
                  el LLM llama una tool. Debe devolver str o dict (resultado).
        system_prompt: str, prompt completo con instrucciones + estado.
        historial_prev: list[dict], mensajes anteriores de ESTA conversación
                        (opcional, para contexto multi-turn).
        modo_debug: bool, si True no envía a WA, solo devuelve plan.

    Returns:
        dict: {
            "ok": bool,
            "texto_respuesta": str|None (lo que el bot le dijo),
            "tools_llamadas": [{"nombre": ..., "args": ..., "resultado": ...}],
            "fallo": str|None,
        }
    """
    client = _llm_client()
    if client is None:
        return {"ok": False, "fallo": "sin ANTHROPIC_API_KEY o SDK",
                "texto_respuesta": None, "tools_llamadas": []}
    mensajes = list(historial_prev or [])
    mensajes.append({"role": "user", "content": texto})
    tools_llamadas = []
    texto_respuesta = None
    for turn in range(MAX_TURNS_LLM):
        resp = None
        ultimo_error = None
        # Retry 2x con exponential backoff para hipos transitorios del LLM
        for intento in range(RETRY_INTENTOS + 1):
            try:
                resp = client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system_prompt,
                    tools=tools,
                    messages=mensajes,
                )
                ultimo_error = None
                break
            except Exception as e:
                ultimo_error = f"{type(e).__name__}: {str(e)[:200]}"
                log.warning("[toolcalling] LLM intento %d/%d fallo tel=%s err=%s",
                            intento + 1, RETRY_INTENTOS + 1, telefono, ultimo_error)
                if intento < RETRY_INTENTOS:
                    time.sleep(RETRY_BACKOFF_BASE * (2 ** intento))
        if ultimo_error:
            # Todos los retries fallaron → devolvemos fallo para que el
            # caller lo encole + mande el mensaje humano a la agente.
            log.error("[toolcalling] LLM AGOTÓ reintentos tel=%s", telefono)
            return {"ok": False, "fallo": ultimo_error,
                    "texto_respuesta": None, "tools_llamadas": tools_llamadas,
                    "retriable": True}
        # Separar texto + tool_use blocks del response
        bloques_texto = []
        bloques_tool = []
        for block in resp.content:
            if block.type == "text":
                bloques_texto.append(block.text)
            elif block.type == "tool_use":
                bloques_tool.append(block)
        if bloques_texto:
            texto_respuesta = "\n\n".join(bloques_texto).strip() or texto_respuesta
        # Si no hay tools pendientes y stop_reason != "tool_use", terminamos
        if not bloques_tool or resp.stop_reason != "tool_use":
            break
        # Procesar cada tool call
        mensajes.append({"role": "assistant", "content": resp.content})
        tool_results = []
        silenciar_llm = False
        for tb in bloques_tool:
            nombre = tb.name
            args = tb.input or {}
            handler = handlers.get(nombre)
            tc_t0 = time.monotonic()
            handler_ok = True
            if not handler:
                resultado = {"error": f"tool desconocida: {nombre}"}
                handler_ok = False
                log.warning("[toolcalling] tool desconocida=%s tel=%s", nombre, telefono)
            else:
                try:
                    resultado = handler(args, contexto)
                except Exception as e:
                    log.exception("[toolcalling] handler %s fallo tel=%s", nombre, telefono)
                    resultado = {"error": f"excepcion: {type(e).__name__}: {e}"}
                    handler_ok = False
            latencia_ms = int((time.monotonic() - tc_t0) * 1000)
            # ok real: si el handler devolvió {"ok": False, ...} también cuenta
            # como no-ok para fines de telemetría.
            ok_real = handler_ok and not (
                isinstance(resultado, dict)
                and resultado.get("ok") is False
            )
            _log_tool_call(
                telefono=telefono,
                cid=(contexto or {}).get("cid"),
                fase=fase,
                mensaje_user=texto,
                tool_nombre=nombre,
                tool_args=args,
                tool_resultado=resultado,
                ok=ok_real,
                latencia_ms=latencia_ms,
                modelo=MODEL,
            )
            # Si un handler ya mandó el mensaje al usuario, marca para que
            # no haya otro turno del LLM que agregue texto duplicado.
            if isinstance(resultado, dict) and resultado.get("_silenciar_llm"):
                silenciar_llm = True
            tools_llamadas.append({"nombre": nombre, "args": args, "resultado": resultado})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tb.id,
                "content": json.dumps(resultado, ensure_ascii=False, default=str),
            })
        mensajes.append({"role": "user", "content": tool_results})
        if silenciar_llm:
            # El handler ya envió su propio mensaje. No queremos otro turno
            # del LLM comentando encima. Cortamos y descartamos cualquier
            # texto_respuesta previo (no debería haber en este turno).
            texto_respuesta = None
            break
    return {
        "ok": True, "texto_respuesta": texto_respuesta,
        "tools_llamadas": tools_llamadas, "fallo": None,
    }
