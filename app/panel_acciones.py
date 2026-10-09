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
                  incluye `resultado_contacto` (!= None), el helper agrega
                  `_at`, `_por` y `_nota` automáticamente.

Nota sobre buckets (sept-2026, refactor humano-first):
Los buckets del panel se derivan 100% de acciones humanas (firma
`resultado_contacto_por NOT NULL`). Las flags que el bot setea
(`no_contactar=true` por detección semántica, `requiere_humano=true` por
pide-llamada) ya NO afectan el bucket. El bot sigue seteándolas, pero
sólo con fines internos / alertas.

El orden en que se declaran las acciones acá es el que verá el asesor en
el modal del panel (Python 3.7+ preserva orden de inserción en dict).
"""
import logging

log = logging.getLogger("petra")


ACCIONES = {
    # 1) Resetear: vuelve a bucket auto-derivado (contactado/respondio).
    "regresar_a_contactado": {
        "label": "Regresar a contactado",
        "descripcion": "Resetea el cierre humano. El bucket vuelve a "
                       "derivarse automatico (contactado o respondio "
                       "segun si hay IN).",
        "updates": {
            "resultado_contacto": None,
            "resultado_contacto_por": None,
            "resultado_contacto_at": None,
            "resultado_contacto_nota": None,
        },
    },
    # 2) Lead listo para llenar el formulario con un asesor.
    #    IMPORTANTE: ya NO dispara el Flow (eso lo hacía antes). Hoy es
    #    sólo un marcador de bucket.
    "enviar_formulario": {
        "label": "Marcar \"enviar formulario\"",
        "descripcion": "Lead listo para llenar el formulario con un asesor. "
                       "No dispara nada automatico.",
        "updates": {
            "resultado_contacto": "enviar_formulario",
        },
    },
    # 3) Entregado al equipo humano externo.
    "sureti": {
        "label": "Entregado a Sureti",
        "descripcion": "Marcar como entregado al equipo humano externo.",
        "updates": {
            "resultado_contacto": "sureti",
        },
    },
    # 4) Dejarlo para una futura ronda.
    "recontactar": {
        "label": "No le interesa ahora — recontactar después",
        "descripcion": "Dejarlo activo para una futura ronda "
                       "(sin reenganche automático por ahora).",
        "updates": {
            "no_contactar": False,
            "resultado_contacto": "recontactar",
        },
    },
    # 5) No le interesa (cierre). no_contactar=True sólo como marker.
    "no_interesa": {
        "label": "No le interesa",
        "descripcion": "No le interesa el producto. No volver a contactar.",
        "updates": {
            "no_contactar": True,
            "resultado_contacto": "no_interesa",
        },
    },
    # 6) Es inmobiliaria / broker.
    "broker": {
        "label": "Es inmobiliaria / broker",
        "descripcion": "El contacto es broker. No volver a contactar.",
        "updates": {
            "no_contactar": True,
            "resultado_contacto": "broker",
        },
    },
    # Acción "completar formulario manual": sigue existiendo porque la
    # dispara el botón separado "📝 Completar formulario con el cliente"
    # (vista detalle, no modal). El guardado real ocurre en
    # panel.formulario_manual_guardar (necesita escribir pipeline +
    # mensaje sintético + audit, no sólo flags); acá se declara para que
    # el label aparezca en el historial de acciones.
    #
    # Esta entrada se filtra en el render del modal (ver panel.py:
    # acciones_modal) para que NO aparezca como opción manual: el modal
    # sólo muestra las 6 acciones de arriba, en ese orden.
    "completar_formulario_manual": {
        "label": "Completar formulario manual",
        "descripcion": "Asesor completó los Flows 1+2 con el cliente por "
                       "chat/llamada.",
        "updates": {
            "resultado_contacto": "enviar_a_sureti",
            "requiere_humano": False,
        },
    },
}


# Subconjunto que se muestra en el modal "Registrar acción humana".
# `completar_formulario_manual` se oculta porque se dispara desde el botón
# verde separado "📝 Completar formulario con el cliente".
_ACCIONES_MODAL_KEYS = (
    "regresar_a_contactado",
    "enviar_formulario",
    "sureti",
    "recontactar",
    "no_interesa",
    "broker",
)


def acciones_modal() -> dict:
    """Devuelve el dict de acciones que renderea el modal del panel, en el
    orden pre-establecido. Es lo que `panel_detalle.html` itera con
    ``{% for key, cfg in acciones.items() %}``."""
    return {k: ACCIONES[k] for k in _ACCIONES_MODAL_KEYS if k in ACCIONES}


def aplicar(telefono: str, accion_key: str, nota: str = "", actor: str = "") -> dict:
    """Aplica la acción al contacto y audita.

    Retorna un dict con `ok` (bool). Si `ok=False`, incluye `error` con el
    motivo. Si `ok=True`, incluye `accion` y `label`.

    Reglas:
      - La acción setea las columnas declaradas en `updates`.
      - Si `updates` incluye `resultado_contacto` != None, también se
        setean `resultado_contacto_at` ('NOW()'), `_por` (actor) y
        `_nota` (nota).
      - Si `updates` incluye `resultado_contacto` == None (ej.
        `regresar_a_contactado`), se limpia también la firma humana y
        los metadatos pasan a None: así el bucket vuelve a derivarse de
        forma automática.
      - No hay side-effects a WhatsApp: `enviar_formulario` ya NO dispara
        el Flow (eso quedó atrás con el refactor humano-first).
    """
    from app import db

    if accion_key not in ACCIONES:
        return {"ok": False, "error": "accion_desconocida"}

    cfg = ACCIONES[accion_key]
    updates = dict(cfg["updates"])

    # Si la acción setea resultado_contacto a un valor concreto, agregamos
    # los metadatos de cierre (firma humana).
    # Si lo setea a None (reset), limpiamos también los metadatos para
    # que el bucket vuelva a derivarse de forma automática.
    if "resultado_contacto" in updates:
        if updates["resultado_contacto"] is None:
            updates.setdefault("resultado_contacto_at", None)
            updates.setdefault("resultado_contacto_por", None)
            updates.setdefault("resultado_contacto_nota", None)
        else:
            updates["resultado_contacto_at"] = "NOW()"
            updates["resultado_contacto_por"] = actor or ""
            updates["resultado_contacto_nota"] = nota or ""

    db.aplicar_accion_panel(telefono, updates, accion_key, nota, actor)
    log.info("[Panel] accion=%s telefono=%s actor=%s nota=%s",
             accion_key, telefono, actor or "-", (nota or "")[:80])
    return {"ok": True, "accion": accion_key, "label": cfg["label"]}
