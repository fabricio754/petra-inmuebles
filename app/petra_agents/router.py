"""Orquestador: clasifica intención y rutea.

Flujo actualizado (world-class pattern):

  1. DEDUP: si el wa_msg_id de Meta ya se procesó, descartar silenciosamente.
  2. LOCK por teléfono (serializa mensajes del mismo contacto).
  3. SESIÓN: obtener/abrir sesión conversacional. Si es nueva, Claude
     arranca sin historial. Si está abierta, Claude ve mensajes de esa
     sesión.
  4. ESTADO: si rol=agente_petra con estado=recolectando → va al
     FLOW HANDLER determinístico (Python puro, sin LLM).
  5. Si hay intent_activo del sub-agente LLM → sub-agente responde con
     historial de la sesión.
  6. Si no → clasificador Haiku chico elige sub-agente → set_intent →
     sub-agente responde con historial.
  7. Si no matchea sub-agente implementado → cae al monolito legacy
     (los otros 5 flujos aún no migrados).
"""
import logging
import os
import threading

from app.petra_agents import (
    _base, intent_store, agente_petra, dedup, sesion,
    flow_onboarding_agente,
)

log = logging.getLogger("petra")

AGENTES_IMPLEMENTADOS = {
    "agente_petra": agente_petra,
}
AGENTES_FUTUROS = {"interesado", "propietario", "credito", "soporte", "info"}

# Lock in-memory por teléfono — serializa mensajes del mismo contacto.
# No sobrevive restart (eso no importa: el nuevo proceso arranca limpio).
_LOCKS = {}
_LOCKS_LOCK = threading.Lock()


def _lock_tel(telefono):
    with _LOCKS_LOCK:
        lk = _LOCKS.get(telefono)
        if lk is None:
            lk = threading.Lock()
            _LOCKS[telefono] = lk
    return lk


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
    from anthropic import Anthropic
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return "info"
    workspace = (os.environ.get("ANTHROPIC_WORKSPACE_ID") or "").strip()
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
    from app import petra_orchestrator
    return petra_orchestrator.handle_message(
        telefono=telefono, texto=texto,
        button_id=button_id, list_id=list_id,
        contacto_nombre=contacto_nombre,
    )


ESTADOS_FLOW_PYTHON = {
    "recolectando", "inscrito", "bienvenida_enviada",
    "esperando_capacitacion_dia1", "en_capacitacion",
    "capacitacion_completa", "certificado",
}


def _estado_flow_python(telefono):
    """Devuelve el estado si el contacto es agente_petra Y su estado
    pertenece al conjunto que maneja el flow handler determinístico.
    Si no, devuelve None (el router debe elegir otra vía)."""
    from app import db as _db
    try:
        with _db._conexion_directa(timeout=5) as conn:
            row = conn.execute(
                "SELECT r.metadata->>'estado_agente' "
                "FROM petra_contactos c "
                "JOIN petra_contacto_roles r "
                "  ON r.contacto_id=c.id AND r.rol='agente_petra' "
                "WHERE c.telefono=%s",
                (telefono,),
            ).fetchone()
            if row and row[0] in ESTADOS_FLOW_PYTHON:
                return row[0]
    except Exception:
        log.exception("[router] check estado fallo tel=%s", telefono)
    return None


def handle_message(telefono, texto, button_id=None, list_id=None,
                   contacto_nombre=None, wa_msg_id=None):
    # 1. Dedup — si Meta reenvía el mismo wamid, descartar silenciosamente.
    if dedup.ya_visto(wa_msg_id, telefono):
        log.info("[router] duplicado descartado wamid=%s tel=%s",
                 wa_msg_id, telefono)
        return {"ok": True, "duplicado": True}

    # 2. Lock por teléfono — serializa mensajes concurrentes del mismo tel.
    lock = _lock_tel(telefono)
    with lock:
        return _handle_locked(telefono, texto, button_id, list_id,
                              contacto_nombre)


def _handle_locked(telefono, texto, button_id, list_id, contacto_nombre):
    texto_efectivo = button_id or list_id or texto or ""

    # 3. Sesión — abrir si no hay, actualizar último_msg_at.
    sess = sesion.obtener_o_abrir(telefono)
    sesion_id = sess["id"] if sess else None

    # 4. Fast path: si está en un estado que el flow handler Python
    # determinístico maneja (recolectando / inscrito / bienvenida_enviada),
    # NO llamamos LLM. Flujo lineal y predecible.
    estado = _estado_flow_python(telefono)
    if estado:
        handled = flow_onboarding_agente.handle(
            telefono, texto_efectivo, es_handoff=False,
        )
        if handled:
            return {"ok": True, "flow": "onboarding_agente", "estado": estado}

    # 5. Intent activo → delegar al sub-agente del intent sin re-clasificar.
    activo = intent_store.leer(telefono)
    if activo:
        agente = activo["agente"]
        intent_store.tocar(telefono)
        log.info("[router] intent activo tel=%s → %s", telefono, agente)
        if agente in AGENTES_IMPLEMENTADOS:
            return AGENTES_IMPLEMENTADOS[agente].handle(
                telefono, texto_efectivo, contacto_nombre=contacto_nombre,
                es_handoff=False, sesion_id=sesion_id,
            )
        return _enviar_al_monolito(telefono, texto, button_id, list_id,
                                   contacto_nombre)

    # 6. Clasificar intención
    label = _clasificar(telefono, texto_efectivo)
    log.info("[router] clasificado tel=%s label=%s", telefono, label)

    if label in AGENTES_IMPLEMENTADOS:
        intent_store.set_intent(telefono, label,
                                razon=f"clasificado:{texto_efectivo[:60]}")
        # Si clasificó agente_petra, arrancamos directo al flow determinístico
        # (que creará el rol en estado=recolectando y hará la primera pregunta).
        if label == "agente_petra":
            flow_onboarding_agente.handle(
                telefono, texto_efectivo, es_handoff=True,
            )
            return {"ok": True, "flow": "onboarding_agente",
                    "handoff": True}
        # Otros sub-agentes: usan LLM con historial de sesión
        return AGENTES_IMPLEMENTADOS[label].handle(
            telefono, texto_efectivo, contacto_nombre=contacto_nombre,
            es_handoff=True, sesion_id=sesion_id,
        )

    # 7. Sub-agente aún no migrado → monolito legacy
    return _enviar_al_monolito(telefono, texto, button_id, list_id,
                               contacto_nombre)
