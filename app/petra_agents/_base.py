"""Infraestructura compartida entre sub-agentes.

Reutiliza las piezas que ya funcionan del orchestrator monolítico:
contexto DB, dispatcher de tools, fallback Gemini, logging y keepalive.
Cada sub-agente aporta su propio prompt + lista de tools (subset).

Flujo típico de un sub-agente:
    1. cargar_contexto_contacto(telefono)
    2. construir system = [prompt_fijo, estado_contacto, intent_activo]
    3. run_claude_loop(telefono, texto, system, tools)
    4. petra_wa.send_text() con el resultado
"""
import logging
import os
import threading
import time

log = logging.getLogger("petra")

MODEL_PRIMARY = os.environ.get("PETRA_ORCH_MODEL", "claude-haiku-4-5")
MODEL_FALLBACK = os.environ.get("PETRA_ORCH_FALLBACK", "gemini-2.5-flash-lite")
MAX_TURNS = int(os.environ.get("PETRA_ORCH_MAX_TURNS", "6"))

# Reutilizamos las piezas del orchestrator monolítico via import tardío
# para no duplicar código. Si el monolito desaparece algún día, estas
# funciones se mueven acá.


def cargar_contexto_contacto(telefono):
    from app import petra_orchestrator
    return petra_orchestrator._cargar_contexto_contacto(telefono)


def dispatch_tool(name, args, telefono):
    from app import petra_orchestrator
    return petra_orchestrator._dispatch_tool(name, args, telefono)


def tool_schemas_todas():
    from app import petra_orchestrator
    return petra_orchestrator._tool_schemas()


def tool_schemas_subset(nombres):
    """Devuelve solo las tool schemas cuyo name esté en `nombres`."""
    todas = tool_schemas_todas()
    return [t for t in todas if t.get("name") in nombres]


def lanzar_keepalive(telefono, cancel_event):
    from app import petra_orchestrator
    petra_orchestrator._lanzar_keepalive(telefono, cancel_event)


def guardrail_pre_check(texto):
    from app import petra_orchestrator
    return petra_orchestrator._guardrail_pre_check(texto)


def fallback_gemini(texto_usuario, system_prompt=None):
    from app import petra_orchestrator
    return petra_orchestrator._fallback_gemini(texto_usuario)


def log_turno(telefono, texto_u, texto_a, latencia_ms, error, agente):
    """Loguea un turno en petra_llm_logs con rama = 'agent:<nombre>'."""
    from app import db as _db
    try:
        with _db._conexion_directa(timeout=5) as conn:
            conn.execute(
                "INSERT INTO petra_llm_logs "
                "(telefono, texto_usuario, texto_asistente, latencia_ms, "
                "error, rama) VALUES (%s, %s, %s, %s, %s, %s)",
                (telefono, texto_u, texto_a, latencia_ms,
                 (error or "")[:500], f"agent:{agente}"),
            )
    except Exception:
        log.exception("[agents] log fallo tel=%s agente=%s", telefono, agente)


def run_claude_loop(telefono, texto_usuario, system_bloques, tools,
                    max_turns=None):
    """Loop Claude tool_use estándar. Devuelve (texto_final, error).

    Cada sub-agente arma su propio system_bloques y tools, pero el loop
    (manejo de tool_use, dispatch, continuación) es idéntico — vive acá.

    Historial: cero (igual que el monolito actual). La memoria vive en DB.
    """
    from anthropic import Anthropic
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    workspace = (os.environ.get("ANTHROPIC_WORKSPACE_ID") or "").strip()
    if not api_key:
        return fallback_gemini(texto_usuario), "sin ANTHROPIC_API_KEY"
    kwargs = {}
    if workspace:
        kwargs["default_headers"] = {"anthropic-workspace-id": workspace}
    client = Anthropic(api_key=api_key, **kwargs)

    historial = [{"role": "user", "content": texto_usuario}]
    turns = max_turns or MAX_TURNS

    for turno in range(turns):
        try:
            resp = client.messages.create(
                model=MODEL_PRIMARY, max_tokens=2048,
                system=system_bloques, tools=tools, messages=historial,
            )
        except Exception as exc:
            log.exception("[agents] anthropic turno %d fallo", turno)
            return fallback_gemini(texto_usuario), str(exc)[:200]

        if resp.stop_reason == "end_turn":
            partes = [b.text for b in resp.content
                      if getattr(b, "type", "") == "text"]
            return " ".join(partes).strip(), None

        if resp.stop_reason == "tool_use":
            historial.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for b in resp.content:
                if getattr(b, "type", "") != "tool_use":
                    continue
                try:
                    out = dispatch_tool(b.name, b.input or {}, telefono)
                except Exception as exc:
                    log.exception("[agents] tool %s fallo", b.name)
                    out = {"error": str(exc)[:200]}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": str(out)[:4000],
                })
            historial.append({"role": "user", "content": tool_results})
            continue

        return "", f"stop_reason_inesperado:{resp.stop_reason}"

    return "", f"max_turns_sin_cerrar:{turns}"


def ejecutar(telefono, texto, agente_nombre, system_bloques, tools,
             prefijo_mensaje=None):
    """Entry-point común para un sub-agente: keepalive + loop + send + log.

    Un sub-agente llama esto desde su `handle(telefono, texto, ...)`
    pasando su system y tools. Si viene `prefijo_mensaje`, se antepone al
    output (ej: "👩‍🏫 Petra Capacitación: ..." para los handoffs).
    """
    from app import petra_wa
    t0 = time.time()
    cancel = threading.Event()
    lanzar_keepalive(telefono, cancel)
    try:
        texto_resp, err = run_claude_loop(telefono, texto, system_bloques, tools)
    except Exception as exc:
        log.exception("[agents:%s] loop fallo tel=%s", agente_nombre, telefono)
        texto_resp = ("Perdón, estoy teniendo un problema técnico momentáneo. "
                      "¿Me reescribes en un ratito? 🙏")
        err = str(exc)[:300]
    cancel.set()

    if prefijo_mensaje and texto_resp:
        texto_resp = f"{prefijo_mensaje}\n\n{texto_resp}"

    if texto_resp:
        try:
            petra_wa.send_text(telefono, texto_resp)
        except Exception:
            log.exception("[agents:%s] send fallo tel=%s",
                          agente_nombre, telefono)

    latencia = int((time.time() - t0) * 1000)
    log_turno(telefono, texto, texto_resp, latencia, err, agente_nombre)
    return {"ok": not err, "latencia_ms": latencia, "agente": agente_nombre}
