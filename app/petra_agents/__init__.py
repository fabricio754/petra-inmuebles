"""Multi-agent architecture for Petra WhatsApp assistant.

Un orquestador clasifica intención y hace handoff a sub-agentes
especializados por rol. Cada sub-agente tiene su propio system prompt y
subset de tools, pero comparten infraestructura (contexto DB, logging,
dispatcher de tools, fallback a Gemini).

Fase 1: router + agente_petra. Otros flujos (crédito, propietario,
interesado, soporte, info) caen al monolito legacy mientras se migran.

Activación: USE_MULTI_AGENT=true (feature flag).
"""
import os

from app.petra_agents.router import handle_message as _handle_router


def esta_activo():
    return (os.environ.get("USE_MULTI_AGENT") or "").lower() in ("1", "true", "yes")


def handle_message(telefono, texto, button_id=None, list_id=None,
                   contacto_nombre=None, wa_msg_id=None):
    return _handle_router(telefono=telefono, texto=texto,
                          button_id=button_id, list_id=list_id,
                          contacto_nombre=contacto_nombre,
                          wa_msg_id=wa_msg_id)
