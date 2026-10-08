"""Acciones humanas disponibles desde el panel.

El asesor que habla con un lead por teléfono necesita cerrar la conversación
registrando qué pasó. Este módulo define el catálogo cerrado de acciones
posibles y las aplica atómicamente:

  1. Side-effect opcional (ej. enviar Flow 1 al cliente).
  2. UPDATE sobre `contactos` con los flags/campos que corresponden.
  3. INSERT en `panel_acciones` para audit (quién, qué, cuándo, nota).

Cada entrada en `ACCIONES` declara:
  - label       : texto para el humano (botón/radio).
  - descripcion : ayuda breve de qué hace.
  - updates     : dict de columnas → valor para `UPDATE contactos`. Si
                  incluye `resultado_contacto`, el helper agrega `_at`,
                  `_por` y `_nota` automáticamente.

Nota sobre backward-compat: el `resultado_contacto` guardado en DB para
"Enviar formulario" se mantiene como `enviar_formulario` (valor nuevo),
pero los rows legacy con `resultado_contacto='flow'` deben seguir
contándose como la misma clase a nivel de UI/bucket.
"""
import logging

log = logging.getLogger("petra")


ACCIONES = {
    "enviar_formulario": {
        "label": "Enviar formulario",
        "descripcion": "Dispara el Flow 1 (REQUISITOS) al cliente. "
                       "Marca que estamos esperando que lo complete.",
        "updates": {
            "no_contactar": False,
            "requiere_humano": False,
            "resultado_contacto": "enviar_formulario",
        },
    },
    "broker": {
        "label": "Es inmobiliaria / broker",
        "descripcion": "El contacto es broker. No volver a contactar.",
        "updates": {
            "no_contactar": True,
            "resultado_contacto": "broker",
        },
    },
    "no_interesa": {
        "label": "No le interesa",
        "descripcion": "No le interesa el producto. No volver a contactar.",
        "updates": {
            "no_contactar": True,
            "resultado_contacto": "no_interesa",
        },
    },
    "recontactar": {
        "label": "No le interesa ahora — recontactar después",
        "descripcion": "Dejarlo activo para una futura ronda "
                       "(sin reenganche automático por ahora).",
        "updates": {
            "no_contactar": False,
            "resultado_contacto": "recontactar",
        },
    },
    "sureti": {
        "label": "Entregado a Sureti",
        "descripcion": "Marcar como entregado al equipo humano externo.",
        "updates": {
            "resultado_contacto": "sureti",
        },
    },
    # Nueva acción: el asesor completó los Flows 1 y 2 con el cliente por
    # chat o llamada. El guardado real ocurre en panel.formulario_manual_guardar
    # (necesita escribir pipeline + mensaje sintético + audit, no sólo flags),
    # pero declaramos la entrada acá para que el label aparezca en el panel
    # (historial de acciones) y los tests tengan algo con qué chequear.
    "completar_formulario_manual": {
        "label": "Completar formulario manual",
        "descripcion": "Asesor completó los Flows 1+2 con el cliente por chat/llamada.",
        "updates": {
            "resultado_contacto": "enviar_a_sureti",
            "requiere_humano": False,
        },
    },
    "levantar_no_contactar": {
        "label": "Levantar no_contactar",
        "descripcion": "Deshace el no_contactar (ej. opt-out falso positivo).",
        "updates": {
            "no_contactar": False,
            # No cambia resultado_contacto para preservar historial.
        },
    },
    "marcar_lead_caliente": {
        "label": "Marcar lead caliente (requiere humano)",
        "descripcion": "Fuerza requiere_humano=true (ej. para re-disparar alerta).",
        "updates": {
            "requiere_humano": True,
            "resultado_contacto": "lead_caliente",
        },
    },
    "otro": {
        "label": "Otro",
        "descripcion": "Cerrar con nota libre, sin cambiar flags.",
        "updates": {
            "resultado_contacto": "otro",
        },
    },
}


def _enviar_flow_requisitos(telefono: str) -> None:
    """Dispara el Flow 1 (REQUISITOS) al cliente desde el panel.

    Reutiliza `whatsapp.send_form_requisitos`, la misma función con la que
    el bot antes autodisparaba el Flow tras BOTON_SI. Ahora el disparo lo
    hace el humano explícitamente. Si falla, levanta la excepción al caller
    para que `aplicar()` NO toque las flags ni inserte audit.
    """
    from app import whatsapp
    whatsapp.send_form_requisitos(telefono)


def aplicar(telefono: str, accion_key: str, nota: str = "", actor: str = "") -> dict:
    """Aplica la acción al contacto y audita.

    Retorna un dict con `ok` (bool). Si `ok=False`, incluye `error` con el
    motivo. Si `ok=True`, incluye `accion` y `label`.

    La acción `otro` exige nota — sin nota el cierre es inútil para el audit.

    La acción `enviar_formulario` tiene un side-effect: antes de tocar DB,
    dispara el Flow 1 (REQUISITOS) al cliente. Si el envío falla, no se
    aplica el UPDATE ni se inserta audit (volvemos con error).
    """
    from app import db

    if accion_key not in ACCIONES:
        return {"ok": False, "error": "accion_desconocida"}

    cfg = ACCIONES[accion_key]

    # Validación: "otro" sin nota no tiene sentido para audit.
    if accion_key == "otro" and not (nota or "").strip():
        return {"ok": False, "error": "nota_requerida"}

    # Side-effect previo: Enviar formulario dispara el Flow al cliente.
    # Si falla el envío, abortamos antes de tocar DB (nada de flags
    # cambiadas sin que el cliente haya recibido el Flow).
    if accion_key == "enviar_formulario":
        try:
            _enviar_flow_requisitos(telefono)
        except Exception as exc:  # noqa: BLE001
            log.exception("[Panel] Error enviando Flow 1 a %s desde panel", telefono)
            return {"ok": False, "error": "envio_flow_fallo"}

    updates = dict(cfg["updates"])

    # Si la acción setea resultado_contacto, agregamos los metadatos de cierre.
    # El helper de db reconoce "NOW()" como literal SQL (sin cast de parámetro).
    if "resultado_contacto" in updates:
        updates["resultado_contacto_at"] = "NOW()"
        updates["resultado_contacto_por"] = actor or ""
        updates["resultado_contacto_nota"] = nota or ""

    db.aplicar_accion_panel(telefono, updates, accion_key, nota, actor)
    log.info("[Panel] accion=%s telefono=%s actor=%s nota=%s",
             accion_key, telefono, actor or "-", (nota or "")[:80])
    return {"ok": True, "accion": accion_key, "label": cfg["label"]}
