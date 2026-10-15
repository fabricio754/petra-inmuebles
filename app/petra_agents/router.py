"""Orquestador: clasifica intención y rutea a sub-agente especializado.

Lógica:
  1. Si hay intent_activo en DB → va directo al sub-agente del intent
     (ahorra latencia, mantiene continuidad).
  2. Si no → clasifica con Claude cuál sub-agente debe atender.
     - Si matchea un sub-agente implementado (hoy: agente_petra) →
       set_intent + ejecuta sub-agente con prefijo de handoff.
     - Si matchea algo NO implementado todavía (crédito, propietario,
       interesado, soporte, info) → cae al monolito legacy sin handoff
       visible (mientras migramos).
"""
import logging
import os

from app.petra_agents import _base, intent_store, agente_petra

log = logging.getLogger("petra")

AGENTES_IMPLEMENTADOS = {
    "agente_petra": agente_petra,
}

# Agentes conocidos pero aún no migrados: caen al monolito.
AGENTES_FUTUROS = {"interesado", "propietario", "credito", "soporte", "info"}


CLASIFICADOR_PROMPT = """\
Eres un router de Petra, la plataforma inmobiliaria en Colombia. Tu
única tarea es clasificar la intención del mensaje del usuario y
devolver UN label exacto de esta lista (nada más):

- agente_petra   → quiere ser agente Petra, o ya lo es y pregunta por
                   capacitación/autorización/QR/visitas/inventarios.
- interesado     → busca inmueble, pregunta por propiedad, escaneó QR,
                   quiere visitar, quiere arrendar o comprar.
- propietario    → quiere publicar, vender, arrendar o dar en
                   administración un inmueble suyo.
- credito        → pide crédito hipotecario, pregunta por tasas o Sureti.
- soporte        → tiene queja, problema, frustración, pide hablar con
                   persona, cancelación, reembolso.
- info           → saludo genérico, "qué hacen ustedes", "info general",
                   nada específico.

Si el mensaje es ambiguo o es un simple saludo, devolvé `info`.

Devolvé SOLO el label, en una palabra. Sin explicación, sin signos."""


def _clasificar(telefono, texto):
    """Un Haiku chiquito que devuelve el label. ~50 tokens de I/O."""
    import os as _os
    from anthropic import Anthropic
    api_key = (_os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return "info"
    workspace = (_os.environ.get("ANTHROPIC_WORKSPACE_ID") or "").strip()
    kwargs = {}
    if workspace:
        kwargs["default_headers"] = {"anthropic-workspace-id": workspace}
    client = Anthropic(api_key=api_key, **kwargs)
    contexto = _base.cargar_contexto_contacto(telefono)
    try:
        resp = client.messages.create(
            model=_base.MODEL_PRIMARY, max_tokens=20,
            system=[{"type": "text", "text": CLASIFICADOR_PROMPT}],
            messages=[{"role": "user",
                       "content": f"Estado del contacto en DB:\n{contexto}\n\n"
                                  f"Mensaje: {texto}"}],
        )
        partes = [b.text for b in resp.content
                  if getattr(b, "type", "") == "text"]
        label = (" ".join(partes).strip().lower()
                 .split()[0] if partes else "info")
        label = label.replace("`", "").replace(".", "").strip()
        if label not in AGENTES_IMPLEMENTADOS and label not in AGENTES_FUTUROS:
            return "info"
        return label
    except Exception:
        log.exception("[router] clasificar fallo tel=%s", telefono)
        return "info"


def _enviar_al_monolito(telefono, texto, button_id, list_id, contacto_nombre):
    """Fallback a petra_orchestrator mientras los otros sub-agentes migran."""
    from app import petra_orchestrator
    return petra_orchestrator.handle_message(
        telefono=telefono, texto=texto,
        button_id=button_id, list_id=list_id,
        contacto_nombre=contacto_nombre,
    )


def handle_message(telefono, texto, button_id=None, list_id=None,
                   contacto_nombre=None):
    texto_efectivo = button_id or list_id or texto or ""

    activo = intent_store.leer(telefono)
    if activo:
        agente = activo["agente"]
        intent_store.tocar(telefono)
        log.info("[router] intent activo tel=%s → %s", telefono, agente)
        if agente in AGENTES_IMPLEMENTADOS:
            return AGENTES_IMPLEMENTADOS[agente].handle(
                telefono, texto_efectivo, contacto_nombre=contacto_nombre,
                es_handoff=False,
            )
        return _enviar_al_monolito(telefono, texto, button_id, list_id,
                                   contacto_nombre)

    label = _clasificar(telefono, texto_efectivo)
    log.info("[router] clasificado tel=%s label=%s", telefono, label)

    if label in AGENTES_IMPLEMENTADOS:
        intent_store.set_intent(telefono, label,
                                razon=f"clasificado:{texto_efectivo[:60]}")
        return AGENTES_IMPLEMENTADOS[label].handle(
            telefono, texto_efectivo, contacto_nombre=contacto_nombre,
            es_handoff=True,
        )

    # Sub-agente aún no migrado: monolito.
    return _enviar_al_monolito(telefono, texto, button_id, list_id,
                               contacto_nombre)
