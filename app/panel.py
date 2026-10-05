"""Panel de operación de Massi.

Vista web protegida por token (`PANEL_TOKEN`) que permite ver, por lead:
captación, sesión activa, pipeline, documentos recibidos y remarketing.

Monta dos rutas en el server principal:
  GET /panel           → lista de leads con estado actual
  GET /panel/<tel>     → línea de tiempo completa del lead
"""
import os

from flask import Blueprint, abort, render_template, request

from app import db

panel = Blueprint("panel", __name__)


def _check_token():
    token_cfg = os.environ.get("PANEL_TOKEN", "")
    token_req = request.args.get("token") or request.headers.get("X-Panel-Token", "")
    if not token_cfg or token_req != token_cfg:
        abort(401, description="PANEL_TOKEN no coincide")


def _rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    cols = [c.name for c in cur.description] if cur.description else []
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _estado_lead(c, s, p):
    """Deriva el estado actual del lead a partir de contacto, sesión y pipeline."""
    if p:
        if p.get("fecha_desembolso"):
            return "desembolsado"
        if p.get("sureti_lead_id"):
            return "en_sureti"
        if p.get("estado"):
            return str(p["estado"])
        return "en_pipeline"
    if c and c.get("no_contactar"):
        return "no_contactar"
    if s:
        est = s.get("estado") or ""
        if est.startswith("descartado"):
            return est
        if est == "pausado_paz_salvo":
            return "pausado_paz_salvo"
        if est:
            return f"en_flujo:{est}"
        return "en_flujo"
    if c and c.get("contactado"):
        return "contactado"
    if c:
        return "nuevo"
    return "sin_contacto"


@panel.get("/panel")
def panel_lista():
    _check_token()
    token = request.args.get("token", "")
    with db._conexion() as conn:
        contactos = _rows(conn, """
            SELECT telefono, nombre, ciudad, tipo_inmueble, portal,
                   monto_hasta_millones AS monto_hasta,
                   no_contactar, contactado, fecha_contacto, fecha_scraping
            FROM contactos
            ORDER BY COALESCE(fecha_contacto, fecha_scraping) DESC NULLS LAST
            LIMIT 500
        """)
        sesiones = {r["telefono"]: r for r in _rows(conn, """
            SELECT telefono, estado, ultima_actividad FROM sesiones
        """)}
        pipelines = {r["telefono"]: r for r in _rows(conn, """
            SELECT telefono, estado, sureti_lead_id, fecha_ingreso,
                   fecha_aprobacion, fecha_desembolso, monto_aprobado
            FROM pipeline
            ORDER BY fecha_ingreso DESC
        """)}

    filas = []
    for c in contactos:
        s = sesiones.get(c["telefono"])
        p = pipelines.get(c["telefono"])
        filas.append({
            **c,
            "estado": _estado_lead(c, s, p),
            "ultima_actividad": (s or {}).get("ultima_actividad")
                                or c.get("fecha_contacto")
                                or c.get("fecha_scraping"),
        })

    # Agregados simples arriba.
    por_estado = {}
    for f in filas:
        por_estado[f["estado"]] = por_estado.get(f["estado"], 0) + 1

    return render_template(
        "panel_lista.html",
        filas=filas, por_estado=sorted(por_estado.items()), token=token,
        total=len(filas),
    )


@panel.get("/panel/<telefono>")
def panel_detalle(telefono):
    _check_token()
    token = request.args.get("token", "")
    with db._conexion() as conn:
        contactos = _rows(conn,
            "SELECT * FROM contactos WHERE telefono = %s", (telefono,))
        sesiones = _rows(conn,
            "SELECT * FROM sesiones WHERE telefono = %s", (telefono,))
        pipelines = _rows(conn,
            "SELECT * FROM pipeline WHERE telefono = %s "
            "ORDER BY fecha_ingreso DESC", (telefono,))
        documentos = _rows(conn,
            "SELECT * FROM documentos WHERE telefono = %s "
            "ORDER BY fecha_recepcion DESC", (telefono,))
        remarketing = _rows(conn,
            "SELECT * FROM remarketing WHERE telefono = %s "
            "ORDER BY fecha_envio DESC", (telefono,))
        leads = _rows(conn,
            "SELECT fecha, operacion, datos FROM leads WHERE telefono = %s "
            "ORDER BY fecha DESC LIMIT 100", (telefono,))
        mensajes = _rows(conn,
            "SELECT fecha, direccion, tipo, resumen FROM mensajes "
            "WHERE telefono = %s ORDER BY fecha DESC LIMIT 500", (telefono,))

    contacto = contactos[0] if contactos else None
    sesion = sesiones[0] if sesiones else None
    pipeline = pipelines[0] if pipelines else None

    # Línea de tiempo: ordenamos todos los eventos conocidos.
    eventos = []
    if contacto:
        eventos.append({
            "cuando": contacto.get("fecha_scraping"),
            "tipo": "captacion",
            "detalle": f"Captado por {contacto.get('portal') or 'origen desconocido'}"
                       f"{' — ' + (contacto.get('url_listing') or '') if contacto.get('url_listing') else ''}",
        })
        if contacto.get("contactado"):
            eventos.append({
                "cuando": contacto.get("fecha_contacto"),
                "tipo": "mensaje_apertura",
                "detalle": f"Plantilla massi_apertura_a enviada "
                           f"(hasta ${contacto.get('monto_hasta_millones') or '?'}M)",
            })
        if contacto.get("no_contactar"):
            eventos.append({
                "cuando": contacto.get("fecha_contacto"),
                "tipo": "opt_out",
                "detalle": "Pidió no recibir más mensajes (STOP / botón Stop)",
            })
    for r in remarketing:
        eventos.append({
            "cuando": r.get("fecha_envio"),
            "tipo": f"remarketing:{r.get('tipo') or ''}",
            "detalle": (r.get("mensaje_enviado") or "")[:160]
                       + (" — RESPONDIÓ" if r.get("respondio") else ""),
        })
    for d in documentos:
        auto = " (automático)" if d.get("obtenido_automaticamente") else ""
        eventos.append({
            "cuando": d.get("fecha_recepcion"),
            "tipo": "documento",
            "detalle": f"{d.get('tipo') or 'archivo'}{auto}: {d.get('url_storage') or d.get('media_id')}",
        })
    if pipeline:
        eventos.append({
            "cuando": pipeline.get("fecha_ingreso"),
            "tipo": "pipeline",
            "detalle": f"Entró al pipeline: {pipeline.get('estado') or 'sin estado'}",
        })
        if pipeline.get("sureti_lead_id"):
            eventos.append({
                "cuando": pipeline.get("fecha_ingreso"),
                "tipo": "sureti",
                "detalle": f"Registrado en Sureti como {pipeline['sureti_lead_id']}",
            })
        if pipeline.get("fecha_aprobacion"):
            eventos.append({
                "cuando": pipeline.get("fecha_aprobacion"),
                "tipo": "aprobacion",
                "detalle": f"Aprobado por ${pipeline.get('monto_aprobado') or '?':,}",
            })
        if pipeline.get("fecha_desembolso"):
            eventos.append({
                "cuando": pipeline.get("fecha_desembolso"),
                "tipo": "desembolso",
                "detalle": f"Desembolsado — comisión ${pipeline.get('comision') or 0:,}"
                           + (" (cobrada)" if pipeline.get("comision_cobrada") else " (por cobrar)"),
            })
    for lead in leads:
        eventos.append({
            "cuando": lead.get("fecha"),
            "tipo": f"lead:{lead.get('operacion') or ''}",
            "detalle": str(lead.get("datos") or {})[:200],
        })
    eventos.sort(key=lambda e: e["cuando"] or 0, reverse=True)

    estado = _estado_lead(contacto, sesion, pipeline)

    return render_template(
        "panel_detalle.html",
        telefono=telefono, estado=estado, token=token,
        contacto=contacto, sesion=sesion, pipeline=pipeline,
        documentos=documentos, remarketing=remarketing,
        eventos=eventos, mensajes=mensajes,
    )
