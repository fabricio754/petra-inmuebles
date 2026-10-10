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
    # 1) Forzar bucket "contactado" aunque el cliente ya haya respondido.
    #    Es una firma humana explícita que vence a la auto-derivación de
    #    `tiene_in → respondio`: si un humano dice "este lead sigue en
    #    'contactado'", el panel debe respetarlo.
    "regresar_a_contactado": {
        "label": "Regresar a contactado",
        "descripcion": "Fuerza el bucket 'contactado' aunque el cliente "
                       "ya haya respondido.",
        "updates": {
            "resultado_contacto": "contactado",
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
        `_nota` (nota). Esta firma humana vence a cualquier regla
        automática del bucket (ver `panel.bucket_de`).
      - Si `updates` incluye `resultado_contacto` == None, se limpia
        también la firma humana (ningún acción vigente hace esto; se
        mantiene por compat).
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

    # Snapshot del bucket actual ANTES del UPDATE, para registrar el cambio
    # en el timeline (eventos_contacto.bucket_cambio). Si el contacto no
    # existe en DB o la lectura falla, lo dejamos como 'desconocido'.
    bucket_from = _bucket_actual_snapshot(telefono)

    db.aplicar_accion_panel(telefono, updates, accion_key, nota, actor)

    # Hooks de timeline (iter 1 oct-2026): bucket_cambio para toda acción,
    # más eventos específicos para las acciones que terminan en opt-out.
    _emitir_eventos_timeline(telefono, accion_key, updates, bucket_from,
                             nota, actor)

    log.info("[Panel] accion=%s telefono=%s actor=%s nota=%s",
             accion_key, telefono, actor or "-", (nota or "")[:80])
    return {"ok": True, "accion": accion_key, "label": cfg["label"]}


def _bucket_actual_snapshot(telefono: str) -> str | None:
    """Devuelve el bucket actual del contacto (humano-first) o None si no
    se puede resolver. Replica la lógica de `panel.bucket_de` pero
    minimizada: sólo necesitamos saber qué bucket había ANTES del UPDATE,
    para rotularlo en el evento.
    """
    try:
        from app import db as _db
        with _db._conexion_directa(timeout=5) as conn:
            c = conn.execute(
                "SELECT contactado, no_contactar, resultado_contacto, "
                "       resultado_contacto_por "
                "FROM contactos WHERE telefono = %s",
                (telefono,),
            ).fetchone()
            tiene_in = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM mensajes WHERE telefono = %s "
                "AND direccion = 'in')",
                (telefono,),
            ).fetchone()[0]
    except Exception:
        return None
    if not c:
        return None
    contactado, no_contactar, rc, rc_por = c[0], c[1], c[2] or "", c[3]
    # Mismas reglas que panel.bucket_de.
    if rc_por and rc in ("broker", "no_interesa"):
        return "no_contactar"
    if rc_por and rc == "recontactar":
        return "recontactar"
    if rc_por and rc in ("sureti", "enviar_a_sureti", "entregado"):
        return "enviar_a_sureti"
    if rc_por and rc in ("enviar_formulario", "flow"):
        return "enviar_formulario"
    if rc_por and rc == "contactado":
        return "contactado"
    if tiene_in:
        return "respondio"
    if contactado:
        return "contactado"
    return "nuevo"


# Mapea resultado_contacto → bucket (destino). Mismo criterio que
# panel.bucket_de pero sin pasar por el objeto contacto entero.
_RC_A_BUCKET = {
    "broker": "no_contactar",
    "no_interesa": "no_contactar",
    "recontactar": "recontactar",
    "sureti": "enviar_a_sureti",
    "enviar_a_sureti": "enviar_a_sureti",
    "entregado": "enviar_a_sureti",
    "enviar_formulario": "enviar_formulario",
    "flow": "enviar_formulario",
    "contactado": "contactado",
}


def _emitir_eventos_timeline(telefono, accion_key, updates, bucket_from,
                              nota, actor):
    """Emite los eventos de `eventos_contacto` que corresponden a una
    acción humana ya aplicada. Nunca lanza — un evento perdido no debe
    tumbar la acción."""
    from app import db as _db
    rc = updates.get("resultado_contacto")
    bucket_to = _RC_A_BUCKET.get(rc or "")
    if bucket_to and bucket_from and bucket_to != bucket_from:
        _db.registrar_evento_por_telefono(
            telefono, "bucket_cambio",
            {"from": bucket_from, "to": bucket_to, "accion": accion_key,
             "nota": (nota or "")[:200]},
            autor=actor or "asesor",
        )
    # Marcas específicas que valen la pena por sí mismas en el timeline:
    if updates.get("no_contactar") is True:
        _db.registrar_evento_por_telefono(
            telefono, "no_contactar",
            {"motivo": rc or accion_key, "nota": (nota or "")[:200]},
            autor=actor or "asesor",
        )
    if accion_key == "sureti" or rc in ("sureti", "enviar_a_sureti", "entregado"):
        _db.registrar_evento_por_telefono(
            telefono, "sureti_enviado",
            {"accion": accion_key, "nota": (nota or "")[:200]},
            autor=actor or "asesor",
        )
    if accion_key == "enviar_formulario" or rc == "enviar_formulario":
        _db.registrar_evento_por_telefono(
            telefono, "formulario_enviado",
            {"accion": accion_key, "nota": (nota or "")[:200]},
            autor=actor or "asesor",
        )
