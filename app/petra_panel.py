"""Panel UI para operar la plataforma Petra (oportunidades, casos, visitas,
agentes, errores de integración). Reutiliza el mismo token del panel
productivo (?token=<PANEL_TOKEN>)."""
import os
from flask import Blueprint, render_template, request, redirect, url_for, abort

from app import db as _db

petra_panel_bp = Blueprint("petra_panel", __name__, url_prefix="/panel/petra")


def _check_token():
    expected = (os.environ.get("PANEL_TOKEN") or "").strip()
    if not expected:
        return True  # dev mode: sin token
    got = (request.args.get("token") or request.form.get("token") or "").strip()
    if got != expected:
        abort(403)
    return True


def _rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# =============================================================================
# Vista principal
# =============================================================================


def _metricas_piloto(conn):
    """KPIs del piloto Petra para el dashboard."""
    def scalar(sql, params=()):
        try:
            r = conn.execute(sql, params).fetchone()
            return (r[0] if r else 0) or 0
        except Exception:
            return 0

    kpis = {}
    # Edificios y agentes
    kpis["edificios_activos"] = scalar("SELECT COUNT(*) FROM petra_edificios WHERE activo=TRUE")
    kpis["agentes_total"] = scalar(
        "SELECT COUNT(*) FROM petra_contacto_roles WHERE rol='agente_petra'")
    kpis["agentes_operativos"] = scalar(
        "SELECT COUNT(*) FROM petra_contacto_roles "
        "WHERE rol='agente_petra' AND metadata->>'estado_agente'='operativo'")
    # Inventario
    kpis["inmuebles_activos"] = scalar(
        "SELECT COUNT(*) FROM petra_inmuebles WHERE estado='activo'")
    kpis["inmuebles_borrador"] = scalar(
        "SELECT COUNT(*) FROM petra_inmuebles WHERE estado='borrador'")
    kpis["inmuebles_cerrados_7d"] = scalar(
        "SELECT COUNT(*) FROM petra_inmuebles "
        "WHERE estado IN ('vendido','arrendado') "
        "AND updated_at > NOW() - interval '7 days'")
    # Oportunidades (pipeline)
    kpis["ops_abiertas"] = scalar(
        "SELECT COUNT(*) FROM petra_oportunidades "
        "WHERE estado NOT IN ('cerrado_ganado','cerrado_perdido')")
    kpis["ops_ganadas_30d"] = scalar(
        "SELECT COUNT(*) FROM petra_oportunidades "
        "WHERE estado='cerrado_ganado' AND cerrado_at > NOW() - interval '30 days'")
    kpis["ops_perdidas_30d"] = scalar(
        "SELECT COUNT(*) FROM petra_oportunidades "
        "WHERE estado='cerrado_perdido' AND cerrado_at > NOW() - interval '30 days'")
    cerradas = kpis["ops_ganadas_30d"] + kpis["ops_perdidas_30d"]
    kpis["conversion_30d_pct"] = int(
        round(100 * kpis["ops_ganadas_30d"] / cerradas)) if cerradas else 0
    # Casos de soporte
    kpis["casos_abiertos"] = scalar(
        "SELECT COUNT(*) FROM petra_casos_soporte "
        "WHERE estado IN ('abierto','en_gestion','pendiente_cliente')")
    kpis["casos_escalados"] = scalar(
        "SELECT COUNT(*) FROM petra_casos_soporte WHERE estado='escalado'")
    kpis["casos_sla_vencido"] = scalar(
        "SELECT COUNT(*) FROM petra_casos_soporte "
        "WHERE primera_respuesta_at IS NULL "
        "AND sla_primera_respuesta_at < NOW() "
        "AND estado IN ('abierto','en_gestion','escalado')")
    # Visitas
    kpis["visitas_proximas_7d"] = scalar(
        "SELECT COUNT(*) FROM petra_visitas "
        "WHERE estado IN ('programada','confirmada') "
        "AND programada_at BETWEEN NOW() AND NOW() + interval '7 days'")
    kpis["visitas_realizadas_30d"] = scalar(
        "SELECT COUNT(*) FROM petra_visitas "
        "WHERE estado='realizada' AND updated_at > NOW() - interval '30 days'")
    # Integraciones (salud)
    kpis["dead_letters"] = scalar(
        "SELECT COUNT(*) FROM petra_integracion_errores WHERE estado='dead_letter'")
    kpis["errores_pendientes"] = scalar(
        "SELECT COUNT(*) FROM petra_integracion_errores "
        "WHERE estado IN ('pendiente_retry','reintentando')")
    # LLM / observabilidad (últimas 24h)
    kpis["llm_requests_24h"] = scalar(
        "SELECT COUNT(*) FROM petra_llm_logs WHERE created_at > NOW() - interval '24 hours'")
    kpis["llm_latencia_promedio_ms"] = scalar(
        "SELECT COALESCE(AVG(latencia_ms)::int, 0) FROM petra_llm_logs "
        "WHERE created_at > NOW() - interval '24 hours' AND error IS NULL OR error = ''")
    kpis["llm_errores_24h"] = scalar(
        "SELECT COUNT(*) FROM petra_llm_logs "
        "WHERE created_at > NOW() - interval '24 hours' "
        "AND error IS NOT NULL AND error != ''")
    # Oportunidades por tipo (para pie chart conceptual, hoy solo números)
    try:
        rows = conn.execute(
            "SELECT tipo, COUNT(*) FROM petra_oportunidades "
            "WHERE estado NOT IN ('cerrado_ganado','cerrado_perdido') "
            "GROUP BY tipo ORDER BY COUNT(*) DESC"
        ).fetchall()
        kpis["ops_por_tipo"] = [{"tipo": r[0], "count": r[1]} for r in rows]
    except Exception:
        kpis["ops_por_tipo"] = []
    return kpis


@petra_panel_bp.get("")
def panel_home():
    _check_token()
    tab = request.args.get("tab", "oportunidades")
    token = request.args.get("token", "")
    with _db._conexion_directa(timeout=15) as conn:
        contexto = {
            "tab": tab,
            "token": token,
            "oportunidades": _rows(conn,
                """SELECT o.id, o.tipo, o.estado, o.valor_cop, o.origen,
                          o.updated_at, c.telefono, c.nombre,
                          o.inmueble_id
                   FROM petra_oportunidades o
                   JOIN petra_contactos c ON c.id = o.contacto_id
                   ORDER BY o.updated_at DESC LIMIT 100"""),
            "casos": _rows(conn,
                """SELECT k.id, k.categoria, k.prioridad, k.estado,
                          k.descripcion, k.responsable_humano,
                          k.sla_primera_respuesta_at, k.primera_respuesta_at,
                          k.created_at, c.telefono, c.nombre
                   FROM petra_casos_soporte k
                   JOIN petra_contactos c ON c.id = k.contacto_id
                   ORDER BY
                     CASE k.prioridad WHEN 'critica' THEN 0 WHEN 'alta' THEN 1
                                      WHEN 'media' THEN 2 ELSE 3 END,
                     k.created_at ASC LIMIT 100"""),
            "visitas": _rows(conn,
                """SELECT v.id, v.tipo, v.estado, v.programada_at,
                          v.inmueble_id, v.observaciones,
                          ca.nombre AS agente_nombre, ca.telefono AS agente_tel,
                          ci.nombre AS interesado_nombre, ci.telefono AS interesado_tel
                   FROM petra_visitas v
                   LEFT JOIN petra_contactos ca ON ca.id = v.agente_id
                   LEFT JOIN petra_contactos ci ON ci.id = v.interesado_id
                   ORDER BY v.programada_at DESC NULLS LAST LIMIT 100"""),
            "agentes": _rows(conn,
                """SELECT r.contacto_id, c.telefono, c.nombre,
                          r.metadata->>'estado_agente' AS estado_agente,
                          r.metadata->>'ciudad' AS ciudad,
                          (r.metadata->>'videos_vistos')::int AS videos_vistos,
                          r.metadata->>'origen' AS origen,
                          r.updated_at
                   FROM petra_contacto_roles r
                   JOIN petra_contactos c ON c.id = r.contacto_id
                   WHERE r.rol = 'agente_petra'
                   ORDER BY r.updated_at DESC LIMIT 100"""),
            "errores": _rows(conn,
                """SELECT id, proveedor, operacion, entidad_tipo, entidad_id,
                          error_mensaje, error_codigo, intentos, max_intentos,
                          estado, proximo_intento_at, created_at
                   FROM petra_integracion_errores
                   WHERE estado IN ('pendiente_retry','reintentando','dead_letter')
                   ORDER BY
                     CASE estado WHEN 'dead_letter' THEN 0 ELSE 1 END,
                     created_at DESC LIMIT 100"""),
            "contenidos": _rows(conn,
                """SELECT id, tipo, orden, titulo, url, texto, duracion_seg,
                          descripcion, activo, created_at, updated_at
                   FROM petra_contenidos
                   ORDER BY tipo ASC, orden ASC, created_at ASC"""),
            "autorizaciones": _rows(conn,
                """SELECT a.id, a.conjunto_nombre, a.estado, a.kit_enviado_at,
                          a.aprobada_at, a.nota, a.updated_at, a.edificio_id,
                          c.telefono AS agente_tel, c.nombre AS agente_nombre
                   FROM petra_autorizaciones_admin a
                   JOIN petra_contactos c ON c.id = a.agente_id
                   ORDER BY a.updated_at DESC LIMIT 100"""),
            "anuncios_pendientes": _rows(conn,
                """SELECT a.id, a.url_anuncio, a.foto_url, a.portal, a.estado,
                          a.nota, a.cargado_at, a.created_at,
                          c.telefono AS agente_tel, c.nombre AS agente_nombre,
                          e.nombre AS edificio_nombre
                   FROM petra_anuncios_detectados a
                   JOIN petra_contactos c ON c.id = a.agente_id
                   LEFT JOIN petra_edificios e ON e.id = a.edificio_id
                   ORDER BY a.created_at DESC LIMIT 100"""),
            "mandatos": _rows(conn,
                """SELECT m.id, m.tipo, m.estado, m.comision_pct,
                          m.canon_acordado, m.exclusivo, m.plazo_meses,
                          m.firmado_at, m.updated_at, m.inmueble_id,
                          c.telefono, c.nombre
                   FROM petra_mandatos m
                   JOIN petra_contactos c ON c.id = m.contacto_id
                   ORDER BY m.updated_at DESC LIMIT 100"""),
            "ofertas": _rows(conn,
                """SELECT o.id, o.oportunidad_id, o.monto, o.estado,
                          o.condiciones, o.respuesta_propietario,
                          o.respondida_at, o.created_at,
                          c.telefono, c.nombre
                   FROM petra_ofertas o
                   LEFT JOIN petra_contactos c ON c.id = o.interesado_id
                   ORDER BY o.created_at DESC LIMIT 100"""),
            "postulaciones": _rows(conn,
                """SELECT p.id, p.inmueble_id, p.oportunidad_id, p.estado,
                          p.codeudor_nombre, p.codeudor_tel, p.ingresos_decl,
                          p.nota, p.created_at,
                          c.telefono, c.nombre
                   FROM petra_postulaciones_arriendo p
                   LEFT JOIN petra_contactos c ON c.id = p.interesado_id
                   ORDER BY p.created_at DESC LIMIT 100"""),
            "pagos_arriendo": _rows(conn,
                """SELECT pa.id, pa.mes_anio, pa.monto_esperado,
                          pa.monto_pagado, pa.estado, pa.pagado_at,
                          pa.comprobante_url, pa.updated_at,
                          c.telefono, c.nombre, aa.canon
                   FROM petra_pagos_arriendo pa
                   JOIN petra_administraciones_arriendo aa ON aa.id = pa.administracion_id
                   JOIN petra_contactos c ON c.id = aa.propietario_id
                   ORDER BY pa.mes_anio DESC, pa.updated_at DESC LIMIT 100"""),
            "kpis": _metricas_piloto(conn),
        }
    return render_template("petra_panel.html", **contexto)


# =============================================================================
# Contenidos (videos, PDFs, scripts, plantillas) — subida desde el panel
# =============================================================================
_TIPOS_CONTENIDO = {
    "video_capacitacion": "Video de capacitación",
    "documento_pdf": "Documento PDF",
    "kit_autorizacion": "Kit autorización administrador",
    "script_mensaje": "Script / plantilla de mensaje",
    "plantilla_whatsapp": "Plantilla WhatsApp aprobada",
    "otro": "Otro",
}


@petra_panel_bp.post("/contenido/crear")
def contenido_crear():
    _check_token()
    tipo = (request.form.get("tipo") or "").strip()
    titulo = (request.form.get("titulo") or "").strip()
    url = (request.form.get("url") or "").strip() or None
    texto = (request.form.get("texto") or "").strip() or None
    descripcion = (request.form.get("descripcion") or "").strip() or None
    try:
        orden = int(request.form.get("orden") or 0)
    except ValueError:
        orden = 0
    try:
        duracion = int(request.form.get("duracion_seg")) if request.form.get("duracion_seg") else None
    except ValueError:
        duracion = None
    if tipo not in _TIPOS_CONTENIDO or not titulo:
        return redirect(url_for("petra_panel.panel_home", tab="contenidos",
                                token=request.form.get("token", "")))
    if not url and not texto:
        return redirect(url_for("petra_panel.panel_home", tab="contenidos",
                                token=request.form.get("token", "")))
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute(
            """INSERT INTO petra_contenidos
               (tipo, orden, titulo, url, texto, duracion_seg, descripcion)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (tipo, orden, titulo, url, texto, duracion, descripcion),
        )
    return redirect(url_for("petra_panel.panel_home", tab="contenidos",
                            token=request.form.get("token", "")))


@petra_panel_bp.post("/contenido/<int:cid>/toggle")
def contenido_toggle(cid):
    _check_token()
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute(
            "UPDATE petra_contenidos SET activo = NOT activo, updated_at=NOW() WHERE id=%s",
            (cid,),
        )
    return redirect(url_for("petra_panel.panel_home", tab="contenidos",
                            token=request.form.get("token", "")))


@petra_panel_bp.post("/contenido/<int:cid>/eliminar")
def contenido_eliminar(cid):
    _check_token()
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute("DELETE FROM petra_contenidos WHERE id=%s", (cid,))
    return redirect(url_for("petra_panel.panel_home", tab="contenidos",
                            token=request.form.get("token", "")))


# =============================================================================
# Acciones (form POST)
# =============================================================================


@petra_panel_bp.post("/oportunidad/<int:oid>/estado")
def accion_estado_oportunidad(oid):
    _check_token()
    from app import petra_core
    nuevo = request.form.get("estado", "").strip()
    motivo = request.form.get("motivo") or None
    with _db._conexion_directa(timeout=10) as conn:
        petra_core.cambiar_estado_oportunidad(conn, oid, nuevo, motivo=motivo,
                                              actor="panel")
    return redirect(url_for("petra_panel.panel_home", tab="oportunidades",
                            token=request.form.get("token", "")))


@petra_panel_bp.post("/caso/<int:cid>/accion")
def accion_caso(cid):
    _check_token()
    from app import petra_core
    accion = request.form.get("accion", "").strip()
    with _db._conexion_directa(timeout=10) as conn:
        if accion == "primera_respuesta":
            conn.execute(
                """UPDATE petra_casos_soporte
                   SET primera_respuesta_at=NOW(), estado='en_gestion',
                       responsable_humano=COALESCE(responsable_humano, %s),
                       updated_at=NOW()
                   WHERE id=%s AND primera_respuesta_at IS NULL""",
                (request.form.get("responsable", "panel"), cid),
            )
        elif accion == "resolver":
            row = conn.execute("SELECT estado FROM petra_casos_soporte WHERE id=%s", (cid,)).fetchone()
            if row and petra_core.transicion_permitida(petra_core.CASO_TRANSICIONES, row[0], "resuelto"):
                conn.execute(
                    """UPDATE petra_casos_soporte
                       SET estado='resuelto', resuelto_at=NOW(),
                           resolucion_nota=%s, updated_at=NOW()
                       WHERE id=%s""",
                    (request.form.get("nota"), cid),
                )
        elif accion == "escalar":
            conn.execute(
                "UPDATE petra_casos_soporte SET estado='escalado', updated_at=NOW() WHERE id=%s", (cid,),
            )
    return redirect(url_for("petra_panel.panel_home", tab="casos",
                            token=request.form.get("token", "")))


@petra_panel_bp.post("/visita/<int:vid>/estado")
def accion_estado_visita(vid):
    _check_token()
    nuevo = request.form.get("estado", "").strip()
    valid = {"confirmada", "realizada", "cancelada", "no_show"}
    if nuevo in valid:
        with _db._conexion_directa(timeout=10) as conn:
            conn.execute(
                "UPDATE petra_visitas SET estado=%s, updated_at=NOW() WHERE id=%s",
                (nuevo, vid),
            )
    return redirect(url_for("petra_panel.panel_home", tab="visitas",
                            token=request.form.get("token", "")))


@petra_panel_bp.post("/agente/<int:cid>/avanzar")
def accion_agente_avanzar(cid):
    _check_token()
    from app import petra_core
    nuevo = request.form.get("estado", "").strip()
    with _db._conexion_directa(timeout=10) as conn:
        petra_core.avanzar_estado_agente(conn, cid, nuevo)
    return redirect(url_for("petra_panel.panel_home", tab="agentes",
                            token=request.form.get("token", "")))


@petra_panel_bp.post("/error/<int:eid>/resolver")
def accion_error_resolver(eid):
    _check_token()
    from app import petra_core
    with _db._conexion_directa(timeout=10) as conn:
        petra_core.resolver_error_integracion(conn, eid, nota=request.form.get("nota"))
    return redirect(url_for("petra_panel.panel_home", tab="errores",
                            token=request.form.get("token", "")))


# =============================================================================
# Acciones Fase A-D (POST desde el panel)
# =============================================================================

@petra_panel_bp.post("/autorizacion/<int:aid>/estado")
def accion_autorizacion_estado(aid):
    _check_token()
    from app import petra_core
    estado = (request.form.get("estado") or "").strip()
    nota = request.form.get("nota") or None
    with _db._conexion_directa(timeout=10) as conn:
        petra_core.actualizar_estado_autorizacion(conn, aid, estado, nota=nota)
    return redirect(url_for("petra_panel.panel_home", tab="autorizaciones",
                            token=request.form.get("token", "")))


@petra_panel_bp.post("/anuncio/<int:aid>/qr_cargado")
def accion_anuncio_qr_cargado(aid):
    _check_token()
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute(
            """UPDATE petra_anuncios_detectados
               SET estado='qr_cargado', cargado_at=NOW(), updated_at=NOW()
               WHERE id=%s""",
            (aid,),
        )
    return redirect(url_for("petra_panel.panel_home", tab="anuncios",
                            token=request.form.get("token", "")))


@petra_panel_bp.post("/anuncio/<int:aid>/descartar")
def accion_anuncio_descartar(aid):
    _check_token()
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute(
            "UPDATE petra_anuncios_detectados SET estado='descartado', updated_at=NOW() WHERE id=%s",
            (aid,),
        )
    return redirect(url_for("petra_panel.panel_home", tab="anuncios",
                            token=request.form.get("token", "")))


@petra_panel_bp.post("/mandato/<int:mid>/firmado")
def accion_mandato_firmado(mid):
    _check_token()
    from app import petra_core
    with _db._conexion_directa(timeout=10) as conn:
        petra_core.firmar_mandato(conn, mid, auco_doc_id=request.form.get("auco_doc_id"))
    return redirect(url_for("petra_panel.panel_home", tab="mandatos",
                            token=request.form.get("token", "")))


@petra_panel_bp.post("/oferta/<int:oid>/responder")
def accion_oferta_responder(oid):
    _check_token()
    from app import petra_core
    estado = (request.form.get("estado") or "").strip()
    respuesta = request.form.get("respuesta") or None
    with _db._conexion_directa(timeout=10) as conn:
        petra_core.responder_oferta(conn, oid, estado, respuesta=respuesta)
    return redirect(url_for("petra_panel.panel_home", tab="ofertas",
                            token=request.form.get("token", "")))


@petra_panel_bp.post("/pago/<int:pid>/reportar")
def accion_pago_reportar(pid):
    _check_token()
    from app import petra_core
    try:
        monto = int(request.form.get("monto_pagado") or 0)
    except ValueError:
        monto = 0
    with _db._conexion_directa(timeout=10) as conn:
        petra_core.reportar_pago(conn, pid, monto,
                                   comprobante_url=request.form.get("comprobante_url") or None,
                                   nota=request.form.get("nota") or None)
    return redirect(url_for("petra_panel.panel_home", tab="pagos",
                            token=request.form.get("token", "")))
