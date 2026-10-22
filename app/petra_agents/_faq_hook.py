"""Helper compartido: escape-hatch de FAQ para los motores v3.

Lo usan `inscripcion_v3`, `capacitacion_v3`, `autorizacion_v3` cuando el
input no matcheó ninguna rama determinística. Si el agente FAQ puede
responder la duda (y re-ofrecer el botón del paso actual), lo hacemos
acá y devolvemos True. Si decide que el input es un dato real
(`no_era_pregunta=True`), devolvemos False y la fase sigue su flujo
normal (p. ej. pedir el dato de nuevo).
"""
import logging

log = logging.getLogger("petra")


def intentar_faq(telefono, texto, meta, conn, cid,
                 ultimo_paso_descripcion=None, ultimo_boton=None):
    """Devuelve True si el FAQ manejó el mensaje. False si debe seguir
    el flow determinístico."""
    if not texto or texto.startswith(("ONBAG_", "CUSTOM_")):
        return False
    try:
        from app.petra_agents import agente_faq
        res = agente_faq.responder(
            telefono, texto, meta,
            ultimo_paso_descripcion=ultimo_paso_descripcion,
            ultimo_boton=ultimo_boton, cid=cid,
        )
    except Exception:
        log.exception("[faq-hook] agente_faq fallo tel=%s", telefono)
        return False

    if res.get("no_era_pregunta"):
        return False

    # Entrega texto + botón (si hay).
    from app import petra_wa
    texto_r = res.get("texto") or ""
    boton = res.get("boton")
    if texto_r:
        if boton:
            try:
                petra_wa.send_buttons(telefono, texto_r, [boton])
            except Exception:
                log.exception("[faq-hook] send_buttons fallo")
                petra_wa.send_text(telefono, texto_r)
        else:
            petra_wa.send_text(telefono, texto_r)

    # Escalación: si pidió hablar con un humano, marcamos el lead.
    if res.get("escalar"):
        try:
            conn.execute(
                "INSERT INTO pilot_leads "
                "(telefono, requiere_humano, "
                " requiere_humano_motivo, requiere_humano_at) "
                "VALUES (%s, TRUE, %s, NOW()) "
                "ON CONFLICT (telefono) DO UPDATE SET "
                " requiere_humano=TRUE, "
                " requiere_humano_motivo=EXCLUDED.requiere_humano_motivo, "
                " requiere_humano_at=NOW()",
                (telefono, "faq_pedido_llamada"),
            )
        except Exception:
            log.exception("[faq-hook] marcar_requiere_humano fallo")

    # Flag: ya ofrecimos chat → en el próximo pedido de llamada escalamos.
    if res.get("fuente") == "heuristica" and not res.get("escalar"):
        try:
            import json as _json
            meta["faq_ofrecimos_chat"] = True
            conn.execute(
                "UPDATE petra_contacto_roles SET metadata=%s::jsonb, "
                "updated_at=NOW() WHERE contacto_id=%s AND rol='agente_petra'",
                (_json.dumps(meta), cid),
            )
        except Exception:
            log.exception("[faq-hook] guardar faq_ofrecimos_chat fallo")
    return True
