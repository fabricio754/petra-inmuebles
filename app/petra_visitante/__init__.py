"""Flujo WhatsApp determinístico para VISITANTES (lectores de QR).

Zero LLM. Un visitante escanea un QR en un edificio y arranca este flow
en el mismo número piloto de WhatsApp. Convive — pero NO se mezcla —
con `petra_agents` (reclutamiento de agentes). El router web decide
cuál de los dos flujos atiende el mensaje antes de llegar al legacy.

Entrada única: `handle_message(telefono, texto, button_id, list_id,
contacto_nombre, wa_msg_id)`. Antes de llamar, `webhook_worker` usa
`es_visitante(telefono, texto)` para decidir si el mensaje le toca a
este flujo o al de agentes.

Estado persistido en `contactos.meta->>'visitante_estado'`.
"""
from app.petra_visitante.router import (
    es_visitante,
    handle_message,
)

__all__ = ["es_visitante", "handle_message"]
