"""Acciones humanas disponibles desde el panel.

El asesor que habla con un lead por teléfono necesita cerrar la conversación
registrando qué pasó. Este módulo define el catálogo cerrado de acciones
posibles y las aplica atómicamente:

  1. UPDATE sobre `contactos` con los flags/campos que corresponden.
  2. INSERT en `panel_acciones` para audit (quién, qué, cuándo, nota).

Cada entrada en `ACCIONES` declara:
  - label       : texto para el humano (botón/radio).
  - descripcion : ayuda breve de qué hace.
  - updates     : dict de columnas → valor para `UPDATE contactos`. Si
                  incluye `resultado_contacto`, el helper agrega `_at`,
                  `_por` y `_nota` automáticamente.
"""
import logging

log = logging.getLogger("petra")


ACCIONES = {
    "mandar_al_flow": {
        "label": "Mandar al Flow",
        "descripcion": "Habilita al cliente para seguir el flujo normal "
                       "(bot le mandará FORM_REQUISITOS en próximo evento).",
        "updates": {
            "no_contactar": False,
            "requiere_humano": False,
            "resultado_contacto": "flow",
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
        "label": "Lead calificado — pasó a Sureti",
        "descripcion": "Marcar como entregado al equipo humano externo.",
        "updates": {
            "resultado_contacto": "sureti",
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


def aplicar(telefono: str, accion_key: str, nota: str = "", actor: str = "") -> dict:
    """Aplica la acción al contacto y audita.

    Retorna un dict con `ok` (bool). Si `ok=False`, incluye `error` con el
    motivo. Si `ok=True`, incluye `accion` y `label`.

    La acción `otro` exige nota — sin nota el cierre es inútil para el audit.
    """
    from app import db

    if accion_key not in ACCIONES:
        return {"ok": False, "error": "accion_desconocida"}

    cfg = ACCIONES[accion_key]

    # Validación: "otro" sin nota no tiene sentido para audit.
    if accion_key == "otro" and not (nota or "").strip():
        return {"ok": False, "error": "nota_requerida"}

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
