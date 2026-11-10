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


def _detect_role():
    """Devuelve 'fabricio' | 'sebastian' | None según el token enviado.
    - PANEL_TOKEN           → fabricio (admin, ve todo)
    - PANEL_TOKEN_HERMANO   → sebastian (solo /panel/petra/hermano)
    Si ninguno está configurado (dev local), devuelve 'fabricio'.
    """
    token = (request.args.get("token") or request.form.get("token") or "").strip()
    fab = (os.environ.get("PANEL_TOKEN") or "").strip()
    seb = (os.environ.get("PANEL_TOKEN_HERMANO") or "").strip()
    if not fab and not seb:
        return 'fabricio'  # dev
    if fab and token == fab:
        return 'fabricio'
    if seb and token == seb:
        return 'sebastian'
    return None


def _require_hermano():
    """Solo acepta PANEL_TOKEN_HERMANO."""
    expected = (os.environ.get("PANEL_TOKEN_HERMANO") or "").strip()
    if not expected:
        abort(403)
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
    # ---- Red Petra (tablas reales en uso: conjuntos_visitante,
    #     inmuebles_visitante, petra_contacto_roles) --------------------
    kpis["conjuntos_total"] = scalar("SELECT COUNT(*) FROM conjuntos_visitante")
    kpis["conjuntos_7d"] = scalar(
        "SELECT COUNT(*) FROM conjuntos_visitante "
        "WHERE creado_en > NOW() - interval '7 days'")
    kpis["inmuebles_vis_activos"] = scalar(
        "SELECT COUNT(*) FROM inmuebles_visitante WHERE activo=TRUE")
    kpis["inmuebles_vis_arr"] = scalar(
        "SELECT COUNT(*) FROM inmuebles_visitante "
        "WHERE activo=TRUE AND operacion='arriendo'")
    kpis["inmuebles_vis_ven"] = scalar(
        "SELECT COUNT(*) FROM inmuebles_visitante "
        "WHERE activo=TRUE AND operacion='venta'")
    kpis["inmuebles_vis_7d"] = scalar(
        "SELECT COUNT(*) FROM inmuebles_visitante "
        "WHERE creado_en > NOW() - interval '7 days'")
    kpis["inmuebles_vis_petra"] = scalar(
        "SELECT COUNT(*) FROM inmuebles_visitante "
        "WHERE activo=TRUE AND gestionado_por_petra=TRUE")

    # ---- Agentes Petra ---------------------------------------------------
    kpis["agentes_total"] = scalar(
        "SELECT COUNT(*) FROM petra_contacto_roles WHERE rol='agente_petra'")
    kpis["agentes_activas"] = scalar(
        "SELECT COUNT(*) FROM petra_contacto_roles "
        "WHERE rol='agente_petra' AND metadata->>'estado_agente'='activa'")
    kpis["agentes_recolectando"] = scalar(
        "SELECT COUNT(*) FROM petra_contacto_roles "
        "WHERE rol='agente_petra' AND metadata->>'estado_agente'='recolectando'")
    kpis["agentes_capacitacion"] = scalar(
        "SELECT COUNT(*) FROM petra_contacto_roles "
        "WHERE rol='agente_petra' "
        "AND metadata->>'estado_agente' IN "
        "   ('inscrito','bienvenida_enviada','esperando_capacitacion_dia1',"
        "    'en_capacitacion')")
    kpis["agentes_autorizando"] = scalar(
        "SELECT COUNT(*) FROM petra_contacto_roles "
        "WHERE rol='agente_petra' "
        "AND metadata->>'estado_agente' IN "
        "   ('capacitacion_completa','autorizacion_pendiente','kit_pendiente',"
        "    'kit_enviado')")
    kpis["agentes_pausadas"] = scalar(
        "SELECT COUNT(*) FROM petra_contacto_roles "
        "WHERE rol='agente_petra' AND metadata->>'estado_agente' IN "
        "('pausada','autorizacion_rechazada')")
    kpis["agentes_7d"] = scalar(
        "SELECT COUNT(*) FROM petra_contacto_roles "
        "WHERE rol='agente_petra' AND created_at > NOW() - interval '7 days'")
    # Tasa de inscripción = agentes con estado >= 'inscrito' / total
    completadas = (kpis["agentes_total"] - kpis["agentes_recolectando"])
    kpis["tasa_inscripcion_pct"] = int(
        round(100 * completadas / kpis["agentes_total"])
    ) if kpis["agentes_total"] else 0

    # ---- Visitantes QR / Interesados -------------------------------------
    kpis["interesados_qr_total"] = scalar(
        "SELECT COUNT(*) FROM petra_interesados_qr")
    kpis["interesados_qr_7d"] = scalar(
        "SELECT COUNT(*) FROM petra_interesados_qr "
        "WHERE creado_en > NOW() - interval '7 days'")
    kpis["interesados_qr_24h"] = scalar(
        "SELECT COUNT(*) FROM petra_interesados_qr "
        "WHERE creado_en > NOW() - interval '24 hours'")
    # Promedio de inmuebles por solicitud (señal multi-select).
    try:
        r = conn.execute(
            "SELECT ROUND(AVG(COALESCE(ARRAY_LENGTH(inmueble_ids, 1), 0))::numeric, 1) "
            "FROM petra_interesados_qr "
            "WHERE creado_en > NOW() - interval '30 days'"
        ).fetchone()
        kpis["interesados_avg_items"] = float(r[0]) if r and r[0] is not None else 0.0
    except Exception:
        kpis["interesados_avg_items"] = 0.0
    kpis["visitante_contactos_7d"] = scalar(
        "SELECT COUNT(DISTINCT telefono) FROM visitante_contactos "
        "WHERE created_at > NOW() - interval '7 days'")

    # ---- Conversaciones (salud WhatsApp) ---------------------------------
    kpis["msgs_in_24h"] = scalar(
        "SELECT COUNT(*) FROM mensajes "
        "WHERE direccion='in' AND fecha > NOW() - interval '24 hours'")
    kpis["msgs_out_24h"] = scalar(
        "SELECT COUNT(*) FROM mensajes "
        "WHERE direccion='out' AND fecha > NOW() - interval '24 hours'")
    kpis["msgs_failed_24h"] = scalar(
        "SELECT COUNT(*) FROM mensajes "
        "WHERE direccion='out' AND estado_entrega='failed' "
        "AND fecha > NOW() - interval '24 hours'")
    out24 = kpis["msgs_out_24h"] or 0
    kpis["msgs_failed_24h_pct"] = int(
        round(100 * kpis["msgs_failed_24h"] / out24)) if out24 else 0

    # ---- Legacy "pipeline comercial" (tablas que casi no se usan todavía)
    # Conservado por si en un futuro arrancan las oportunidades formales.
    kpis["edificios_activos"] = scalar("SELECT COUNT(*) FROM petra_edificios WHERE activo=TRUE")
    kpis["agentes_operativos"] = kpis["agentes_activas"]  # alias por compat con template
    kpis["inmuebles_activos"] = kpis["inmuebles_vis_activos"]  # alias
    kpis["inmuebles_borrador"] = 0  # tabla legacy
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


_ESTADOS_AGENTE_ORDEN = [
    "recolectando", "inscrito", "bienvenida_enviada",
    "esperando_capacitacion_dia1", "en_capacitacion",
    "capacitacion_completa", "autorizacion_pendiente",
    "autorizacion_rechazada", "kit_pendiente", "kit_enviado",
    "activa", "pausada", "certificado",
]

_ESTADOS_LABEL = {
    "recolectando": "Recolectando datos",
    "inscrito": "Inscrito",
    "bienvenida_enviada": "Bienvenida enviada",
    "esperando_capacitacion_dia1": "Esperando día 1",
    "en_capacitacion": "En capacitación",
    "capacitacion_completa": "Capacitación OK",
    "autorizacion_pendiente": "Autorización pendiente",
    "autorizacion_rechazada": "Autorización rechazada",
    "kit_pendiente": "Kit pendiente",
    "kit_enviado": "Kit enviado",
    "activa": "Activa",
    "pausada": "Pausada",
    "certificado": "Certificada",
}


def _cargar_agentes(conn, estado=None, q=None):
    """Lista enriquecida de agentes con filtros opcionales:
      - estado: filtra por metadata.estado_agente exacto
      - q: substring (case-insensitive) de nombre o teléfono
    Trae conteos de edificios/inmuebles y última actividad IN.
    """
    where = ["r.rol = 'agente_petra'"]
    params = []
    if estado:
        where.append("r.metadata->>'estado_agente' = %s")
        params.append(estado)
    if q:
        where.append("(LOWER(c.nombre) LIKE %s OR c.telefono LIKE %s)")
        qlike = f"%{q.lower()}%"
        params.extend([qlike, f"%{q}%"])
    where_sql = " AND ".join(where)
    sql = f"""
        SELECT r.contacto_id, c.telefono, c.nombre, c.username,
               r.metadata->>'estado_agente' AS estado_agente,
               r.metadata->>'ciudad'       AS ciudad,
               COALESCE((r.metadata->>'videos_vistos')::int, 0) AS videos_vistos,
               r.metadata->>'origen'       AS origen,
               r.metadata->>'bot_pausado'  AS bot_pausado,
               r.metadata->>'inscrito_at'  AS inscrito_at,
               r.updated_at,
               (SELECT MAX(fecha) FROM mensajes
                 WHERE telefono = c.telefono AND direccion = 'in') AS ultimo_in,
               (SELECT COUNT(*) FROM conjuntos_visitante
                 WHERE creado_por_cid = c.id)                      AS n_edificios,
               (SELECT COUNT(*) FROM inmuebles_visitante iv
                  JOIN conjuntos_visitante cv ON cv.id = iv.conjunto_id
                 WHERE cv.creado_por_cid = c.id
                   AND iv.activo = TRUE)                            AS n_inmuebles
          FROM petra_contacto_roles r
          JOIN petra_contactos c ON c.id = r.contacto_id
         WHERE {where_sql}
         ORDER BY r.updated_at DESC
         LIMIT 200
    """
    return _rows(conn, sql, tuple(params))


def _resumen_estados_agentes(conn):
    """Devuelve {estado: count} para pills tipo kanban arriba del listado."""
    try:
        rows = conn.execute(
            "SELECT COALESCE(r.metadata->>'estado_agente', '—') AS estado, "
            "       COUNT(*) "
            "FROM petra_contacto_roles r "
            "WHERE r.rol = 'agente_petra' "
            "GROUP BY COALESCE(r.metadata->>'estado_agente', '—')"
        ).fetchall()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return []
    conteo = {r[0] or "—": r[1] for r in rows}
    # Orden consistente: estados conocidos en el orden del flow, resto al final.
    conocidos = [(e, conteo.pop(e, 0)) for e in _ESTADOS_AGENTE_ORDEN]
    extras = sorted(conteo.items(), key=lambda x: -x[1])
    out = []
    for e, n in conocidos + extras:
        if n == 0:
            continue
        out.append({
            "estado": e,
            "label": _ESTADOS_LABEL.get(e, e),
            "count": n,
        })
    return out


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
            "agentes": _cargar_agentes(
                conn,
                estado=request.args.get("estado_f", "").strip(),
                q=request.args.get("q", "").strip(),
            ),
            "agentes_resumen": _resumen_estados_agentes(conn),
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
    # Textos editables (FASE A). Se cargan por categoría para el panel.
    from app.petra_agents import textos as _textos_mod
    textos_lista = _textos_mod.listar_textos()
    contexto["textos"] = textos_lista
    contexto["textos_por_categoria"] = _agrupar_textos(textos_lista)
    # Simulador — presets + textos relacionados de cada estación
    from app.petra_agents import simulador as _sim_mod
    from app.petra_agents import textos as _textos_mod
    todos_textos = _textos_mod.listar_textos()
    textos_por_clave = {t["clave"]: t for t in todos_textos}
    # Carga toda la media_estacion de una vez. Las agrupa por estado (fallback
    # cuando clave_texto es NULL) y por clave de mensaje puntual.
    media_por_estado = {}
    media_por_clave = {}
    try:
        with _db._conexion_directa(timeout=10) as conn:
            rows = conn.execute(
                "SELECT id, estado_key, clave_texto, titulo, url, activo, created_at "
                "FROM petra_media_estacion ORDER BY estado_key, orden, id"
            ).fetchall()
        for r in rows:
            item = {
                "id": r[0], "titulo": r[3], "url": r[4],
                "activo": r[5], "created_at": r[6],
                "clave_texto": r[2],
            }
            if r[2]:
                media_por_clave.setdefault(r[2], []).append(item)
            else:
                media_por_estado.setdefault(r[1], []).append(item)
    except Exception:
        pass
    # Mensajes extra agrupados por parent_clave
    extras_por_clave = {}
    try:
        with _db._conexion_directa(timeout=10) as conn:
            rows = conn.execute(
                "SELECT id, parent_clave, orden, contenido, activo "
                "FROM petra_mensajes_extra "
                "ORDER BY parent_clave, orden, id"
            ).fetchall()
        for r in rows:
            extras_por_clave.setdefault(r[1], []).append({
                "id": r[0], "orden": r[2], "contenido": r[3], "activo": r[4],
            })
    except Exception:
        pass
    # Botones editables por estación. Default = los que el flow envía hoy
    # hardcodeados; override permite ocultarlos. Custom = agregados aquí.
    DEFAULTS_BOTONES_POR_ESTADO = {
        "registro_pendiente_confirmacion": [
            {"id": "ONBAG_CONFIRM", "titulo": "✅ Registrarme"},
            {"id": "ONBAG_CORREGIR", "titulo": "✏️ Corregir"},
            {"id": "ONBAG_CANCELAR", "titulo": "❌ Cancelar"},
        ],
        "inscrito": [
            {"id": "ONBAG_VER_JOURNEY", "titulo": "Ver próximos pasos"},
        ],
        "bienvenida_enviada": [
            {"id": "ONBAG_EMPEZAR_AHORA", "titulo": "Empezar ahora"},
        ],
        "en_capacitacion": [
            {"id": "ONBAG_LISTO", "titulo": "✅ Listo"},
        ],
        "autorizacion_pendiente": [
            {"id": "ONBAG_INICIAR_AUTORIZACION", "titulo": "🚀 Iniciar"},
            {"id": "ONBAG_VER_MATERIAL", "titulo": "📎 Ver material"},
            {"id": "ONBAG_TENGO_DUDA", "titulo": "❓ Tengo una duda"},
            {"id": "ONBAG_YA_TENGO_AUTORIZACION", "titulo": "✅ Ya tengo autorización"},
        ],
    }
    botones_por_estado = {}
    botones_por_clave = {}
    try:
        with _db._conexion_directa(timeout=10) as conn:
            rows = conn.execute(
                "SELECT id, estado_key, tipo, boton_id, titulo, "
                "texto_respuesta, orden, activo, clave_texto "
                "FROM petra_botones_estacion ORDER BY estado_key, tipo, orden, id"
            ).fetchall()
        for r in rows:
            item = {
                "id": r[0], "tipo": r[2], "boton_id": r[3],
                "titulo": r[4], "texto_respuesta": r[5],
                "orden": r[6], "activo": r[7],
                "clave_texto": r[8],
            }
            if r[8]:
                botones_por_clave.setdefault(r[8], []).append(item)
            else:
                botones_por_estado.setdefault(r[1], []).append(item)
    except Exception:
        pass
    presets_extended = []
    for k, v in _sim_mod.PRESETS.items():
        textos_resueltos = []
        for clave in v.get("textos_claves", []):
            t = textos_por_clave.get(clave)
            textos_resueltos.append({
                "clave": clave,
                "contenido": (t["contenido"] if t else None),
                "descripcion": (t["descripcion"] if t else None),
                "cargado": bool(t),
                "activo": (t.get("activo", True) if t else True),
                "media": media_por_clave.get(clave, []),
                "botones": botones_por_clave.get(clave, []),
                "extras": extras_por_clave.get(clave, []),
            })
        estado_real = _sim_mod.estado_real_del_preset(k)
        defaults_est = DEFAULTS_BOTONES_POR_ESTADO.get(estado_real, []) if estado_real else []
        reglas_est = botones_por_estado.get(estado_real, []) if estado_real else []
        ocultos_set = {
            r["boton_id"] for r in reglas_est
            if r["tipo"] == "override" and not r["activo"]
        }
        defaults_decorados = [
            {**b, "oculto": b["id"] in ocultos_set} for b in defaults_est
        ]
        customs = [r for r in reglas_est if r["tipo"] == "custom"]
        presets_extended.append({
            "key": k, "label": v["label"], "contexto": v.get("contexto", ""),
            "grupo": v.get("grupo", "otros"),
            "categoria": v.get("categoria", "otros"),
            "textos": textos_resueltos,
            "transiciones": v.get("transiciones", []),
            "estado_real": estado_real,
            "media": media_por_estado.get(estado_real, []) if estado_real else [],
            "botones_default": defaults_decorados,
            "botones_custom": customs,
        })
    contexto["sim_presets"] = presets_extended
    contexto["sim_preset_actual"] = request.args.get("preset") or "nuevo"
    contexto["sim_estado"] = _sim_mod.estado_actual()
    # Nudges — reglas de remarketing (FASE F)
    from app.petra_agents import nudges as _nudges_mod
    contexto["nudges"] = _nudges_mod.listar_reglas(solo_activas=False)
    # Inmuebles capturados por agentes (FASE E)
    with _db._conexion_directa(timeout=10) as conn:
        contexto["inmuebles"] = _rows(conn,
            """SELECT i.id, i.modalidad, i.tipo, i.torre, i.piso,
                      i.area_m2, i.habitaciones, i.banos, i.parqueadero,
                      i.precio_millones, i.precio_arriendo_mensual_millones,
                      i.nombre_anunciante, i.telefono_contacto,
                      i.estado, i.fotos, i.notas, i.created_at,
                      e.nombre AS edificio_nombre, e.ciudad,
                      c.nombre AS agente_nombre, c.telefono AS agente_tel
               FROM petra_inmuebles i
               LEFT JOIN petra_edificios e ON e.id = i.edificio_id
               LEFT JOIN petra_contactos c ON c.id = i.capturado_por_agente_id
               ORDER BY i.created_at DESC LIMIT 100""")
    # Agentes activas (FASE D) — para el dropdown de alta de citas
    with _db._conexion_directa(timeout=10) as conn:
        contexto["agentes_activas"] = _rows(conn,
            """SELECT c.id AS cid, c.telefono, c.nombre,
                      r.metadata->>'conjunto' AS conjunto
               FROM petra_contacto_roles r
               JOIN petra_contactos c ON c.id = r.contacto_id
               WHERE r.rol = 'agente_petra'
                 AND r.metadata->>'estado_agente' = 'activa'
               ORDER BY c.nombre ASC NULLS LAST""")
    # Kits a enviar (FASE C) — agentes con estado kit_pendiente que ya
    # capturaron dirección completa y esperan despacho humano.
    with _db._conexion_directa(timeout=10) as conn:
        contexto["kits_a_enviar"] = _rows(conn,
            """SELECT c.id AS cid, c.telefono, c.nombre,
                      r.metadata->>'conjunto' AS conjunto,
                      r.metadata->'direccion' AS direccion,
                      r.metadata->>'direccion_capturada_at' AS capturada_at,
                      r.updated_at
               FROM petra_contacto_roles r
               JOIN petra_contactos c ON c.id = r.contacto_id
               WHERE r.rol = 'agente_petra'
                 AND r.metadata->>'estado_agente' = 'kit_pendiente'
                 AND (r.metadata->'direccion'->>'ciudad') IS NOT NULL
               ORDER BY r.updated_at ASC""")
        contexto["kits_enviados"] = _rows(conn,
            """SELECT c.id AS cid, c.telefono, c.nombre,
                      r.metadata->>'conjunto' AS conjunto,
                      r.metadata->>'kit_enviado_at' AS enviado_at,
                      r.updated_at
               FROM petra_contacto_roles r
               JOIN petra_contactos c ON c.id = r.contacto_id
               WHERE r.rol = 'agente_petra'
                 AND r.metadata->>'estado_agente' = 'kit_enviado'
               ORDER BY r.updated_at DESC LIMIT 30""")
    # FAQ (knowledge base del LLM) — tipo 'faq' o 'otro' legacy
    with _db._conexion_directa(timeout=10) as conn:
        contexto["faqs"] = _rows(conn,
            """SELECT id, tipo, titulo, texto, descripcion, activo,
                      created_at, updated_at
               FROM petra_contenidos
               WHERE tipo IN ('faq', 'otro')
                 AND texto IS NOT NULL
                 AND LENGTH(TRIM(texto)) > 0
               ORDER BY tipo DESC, created_at DESC""")
    contexto["modo_staging"] = (os.environ.get("MODO_STAGING") or "").strip() == "1"
    return render_template("petra_panel.html", **contexto)


def _agrupar_textos(textos):
    """Agrupa la lista plana de textos por categoría (dict ordenado)."""
    orden_categorias = [
        "inscripcion", "capacitacion", "autorizacion", "activacion",
        "citas", "inventario", "nudges", "fallback", "otro",
    ]
    agrupado = {cat: [] for cat in orden_categorias}
    for t in textos:
        cat = t.get("categoria") or "otro"
        agrupado.setdefault(cat, []).append(t)
    # Quita categorías vacías para no mostrar secciones huecas
    return {k: v for k, v in agrupado.items() if v}


# =============================================================================
# Contenidos (videos, PDFs, scripts, plantillas) — subida desde el panel
# =============================================================================
_TIPOS_CONTENIDO = {
    "bienvenida": "🎬 Video de bienvenida (día 1 cap v2 — al pulsar 'Empezar ahora')",
    "proximos_pasos": "🗺 Video de próximos pasos (día 1 cap v2 — va con el de bienvenida)",
    "video_capacitacion": "📚 Video de capacitación (días 2-7 cap v2, 2 por día)",
    "video_autorizacion": "🎬 Video 'Cómo pedir autorización al administrador' (al terminar capacitación)",
    "video_recordatorio_autorizacion": "🎓 Video a recordar antes de la autorización (ej. el día de capacitación que explica cómo abordar al admin)",
    "video_como_usar_qr": "📲 Video 'Cómo usar el QR' (al activar — después de recibir el kit)",
    "video_citas": "📅 Video 'Cómo manejar las citas' (al pasar a estado=activa, una vez)",
    "video_inventario": "🏠 Video 'Cómo registrar un inmueble' (se envía al arrancar la captura)",
    "demo_autorizacion": "📱 Demo del producto (al terminar capacitación)",
    "pieza_grafica_autorizacion": "🖼 Pieza gráfica para mostrarle al admin (hasta 2, en orden)",
    "documento_pdf": "📄 Documento PDF",
    "kit_autorizacion": "📋 Kit autorización administrador",
    "script_mensaje": "✍️ Script / plantilla de mensaje",
    "plantilla_whatsapp": "📱 Plantilla WhatsApp aprobada",
    "faq": "❓ FAQ (knowledge base del LLM — responde dudas de agentes)",
    "otro": "📦 Otro",
}


_MEDIA_DIR = "/var/data/media"
_MEDIA_MAX_BYTES = 16 * 1024 * 1024  # 16MB (límite WhatsApp)
_MEDIA_ALLOWED_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp",  # imágenes
    ".mp4", ".mov", ".avi", ".webm", ".mkv",   # videos
}


def _guardar_archivo_media(file_storage, public_base_url=""):
    """Guarda un archivo subido en /var/data/media con nombre uuid+ext.
    Devuelve URL pública relativa (/petra/media/<name>) o None si no es
    válido. No propaga excepciones."""
    import os as _os
    import uuid as _uuid
    try:
        _os.makedirs(_MEDIA_DIR, exist_ok=True)
    except Exception:
        return None
    nombre = (file_storage.filename or "").strip()
    if not nombre:
        return None
    _, ext = _os.path.splitext(nombre.lower())
    if ext not in _MEDIA_ALLOWED_EXT:
        return None
    # Chequear tamaño leyendo el stream (werkzeug ya tiene content_length)
    try:
        file_storage.stream.seek(0, 2)  # SEEK_END
        size = file_storage.stream.tell()
        file_storage.stream.seek(0)
    except Exception:
        size = 0
    if size > _MEDIA_MAX_BYTES:
        return None
    nuevo = f"{_uuid.uuid4().hex}{ext}"
    destino = _os.path.join(_MEDIA_DIR, nuevo)
    try:
        file_storage.save(destino)
    except Exception:
        return None
    # URL pública relativa (el blueprint tiene url_prefix=/panel/petra, y la
    # ruta `servir_media` responde en /media/<name> → ruta real /panel/petra/media/<name>).
    base = (public_base_url or os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
    return f"{base}/panel/petra/media/{nuevo}" if base else f"/panel/petra/media/{nuevo}"


@petra_panel_bp.get("/media/<name>")
def servir_media(name):
    """Sirve un archivo subido desde /var/data/media. Público sin token para
    que Meta/WhatsApp pueda hacer fetch de la URL al enviar como preview."""
    import os as _os
    from flask import send_from_directory
    # Sanitize: solo nombre simple sin paths
    if "/" in name or ".." in name:
        abort(404)
    full = _os.path.join(_MEDIA_DIR, name)
    if not _os.path.exists(full):
        abort(404)
    return send_from_directory(_MEDIA_DIR, name)


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
    # Si subieron archivo, lo guardamos y usamos su URL como url del contenido
    archivo = request.files.get("archivo")
    if archivo and archivo.filename:
        url_archivo = _guardar_archivo_media(archivo)
        if url_archivo:
            url = url_archivo
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


@petra_panel_bp.post("/agente/<int:cid>/eliminar")
def accion_agente_eliminar(cid):
    """Elimina el rol agente_petra de un contacto y su progreso de
    capacitación. El contacto en sí NO se borra — solo deja de ser agente.

    Útil cuando una persona ya no quiere ser agente, o para re-arrancar el
    flujo de inscripción desde cero durante testing. Idempotente."""
    _check_token()
    with _db._conexion_directa(timeout=10) as conn:
        tel_row = conn.execute(
            "SELECT telefono FROM petra_contactos WHERE id=%s", (cid,)
        ).fetchone()
        tel = tel_row[0] if tel_row else None
        conn.execute(
            "DELETE FROM petra_contacto_roles "
            "WHERE contacto_id=%s AND rol='agente_petra'",
            (cid,),
        )
        try:
            conn.execute(
                "DELETE FROM petra_capacitacion_progreso WHERE contacto_id=%s",
                (cid,),
            )
        except Exception:
            pass
        # Cerrar el intent activo en el router multi-agent: si el
        # contacto estaba siendo atendido por agente_petra, el próximo
        # mensaje debe volver al router para re-clasificar.
        if tel:
            try:
                conn.execute(
                    "DELETE FROM petra_intent_activo WHERE telefono=%s", (tel,)
                )
            except Exception:
                pass
        # Nota deliberada: NO borramos petra_llm_logs. El historial del
        # chat se preserva — es contexto conversacional válido. El rol del
        # contacto es responsabilidad del campo petra_contacto_roles y el
        # orchestrator lo inyecta en el system prompt al iniciar cada
        # conversación. La fuente de verdad es la DB, no el chat.
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


# =============================================================================
# Ficha de agente Petra: vista detallada con chat, datos, progreso, timeline.
# Reconstruida desde cero (NO reusa panel_detalle.html legacy, pero sí la
# tabla `mensajes` para el chat con chulos ✓✓).
# =============================================================================

import datetime as _dt


def _cargar_ficha_agente(conn, cid):
    """Trae todo lo que la ficha necesita en un dict. Ordenado para render."""
    cur = conn.execute(
        "SELECT c.id, c.telefono, c.nombre, c.email, c.opt_out, "
        "       c.username, c.created_at AS contacto_desde, "
        "       r.rol, r.estado, r.metadata, r.estado_desde, "
        "       r.proximo_contacto_at, r.updated_at AS rol_updated_at "
        "FROM petra_contactos c "
        "LEFT JOIN petra_contacto_roles r "
        "  ON r.contacto_id = c.id AND r.rol = 'agente_petra' "
        "WHERE c.id = %s", (cid,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    agente = dict(zip(cols, row))
    tel = agente["telefono"]

    mensajes = _rows(conn,
        "SELECT id, direccion, tipo, resumen, fecha, wa_msg_id, "
        "       estado_entrega, estado_entrega_at, estado_entrega_error "
        "FROM mensajes WHERE telefono = %s "
        "ORDER BY fecha ASC LIMIT 500", (tel,))

    # Agrupa por día para el separador en el chat
    ultimo_dia = None
    for m in mensajes:
        f = m.get("fecha")
        dia = f.date() if f else None
        m["nuevo_dia"] = dia != ultimo_dia
        if m["nuevo_dia"] and dia:
            m["etiqueta_dia"] = dia.strftime("%d/%m/%Y")
        m["hora_hm"] = f.strftime("%H:%M") if f else "—"
        ultimo_dia = dia

    ultimo_in = conn.execute(
        "SELECT MAX(fecha) FROM mensajes WHERE telefono = %s AND direccion = 'in'",
        (tel,)).fetchone()
    ultimo_in_at = ultimo_in[0] if ultimo_in else None
    puede_responder = False
    if ultimo_in_at:
        delta = _dt.datetime.now(tz=ultimo_in_at.tzinfo or _dt.timezone.utc) - ultimo_in_at
        puede_responder = delta.total_seconds() < 24 * 3600

    progreso = _rows(conn,
        "SELECT p.contenido_id, p.visto_at, ct.titulo, ct.tipo AS tipo_contenido "
        "FROM petra_capacitacion_progreso p "
        "LEFT JOIN petra_contenidos ct ON ct.id = p.contenido_id "
        "WHERE p.contacto_id = %s ORDER BY p.visto_at ASC", (cid,))
    videos_vistos = len([p for p in progreso if (p.get("tipo_contenido") or "").startswith("capacitacion")])

    timeline = _rows(conn,
        "SELECT accion, actor_tipo, actor_label, metadata, created_at "
        "FROM petra_auditoria_eventos "
        "WHERE entidad_tipo = 'contacto' AND entidad_id = %s "
        "ORDER BY created_at DESC LIMIT 50", (cid,))

    meta = agente.get("metadata") or {}
    bot_pausado = str(meta.get("bot_pausado", "")).lower() == "true"

    # Edificios + inmuebles del agente (vive en conjuntos_visitante.creado_por_cid).
    edificios = _rows(conn,
        """SELECT cv.id, cv.codigo, cv.nombre, cv.ciudad, cv.creado_en,
                  COALESCE(SUM(CASE WHEN iv.activo AND iv.operacion='arriendo'
                                    THEN 1 ELSE 0 END), 0) AS arr_activos,
                  COALESCE(SUM(CASE WHEN iv.activo AND iv.operacion='venta'
                                    THEN 1 ELSE 0 END), 0) AS ven_activos,
                  COALESCE(SUM(CASE WHEN iv.activo AND iv.gestionado_por_petra
                                    THEN 1 ELSE 0 END), 0) AS petra_gestiona
             FROM conjuntos_visitante cv
             LEFT JOIN inmuebles_visitante iv ON iv.conjunto_id = cv.id
            WHERE cv.creado_por_cid = %s
            GROUP BY cv.id
            ORDER BY cv.creado_en DESC""",
        (cid,))

    # "¿Qué sigue?" — call-to-action humano del paso actual del flow.
    estado_actual = (meta.get("estado_agente") or "").strip()
    sig = _SIGUIENTE_PASO.get(estado_actual) or {
        "titulo": "— sin estado —",
        "texto": "La agente todavía no está en el flow determinístico.",
        "accion": None,
    }

    return {
        "agente": agente,
        "meta": meta,
        "mensajes": mensajes,
        "ultimo_in_at": ultimo_in_at,
        "puede_responder": puede_responder,
        "progreso": progreso,
        "videos_vistos": videos_vistos,
        "timeline": timeline,
        "bot_pausado": bot_pausado,
        "edificios": edificios,
        "estado_actual": estado_actual,
        "siguiente_paso": sig,
    }


# Mapa estado→descripción humana + call-to-action para el panel admin.
# Usado por la ficha /agente/<cid> para mostrar "¿Qué le falta?" sin que
# el humano tenga que recordar en qué fase cae cada estado.
_SIGUIENTE_PASO = {
    "recolectando": {
        "titulo": "Recolectando datos de inscripción",
        "texto": "Le estamos pidiendo nombre/cédula/ciudad/barrio/conjunto. Si lleva mucho parada acá, revisá si abandonó.",
        "accion": "Reenviar último paso",
    },
    "inscrito": {
        "titulo": "Inscrita — esperando empezar capacitación",
        "texto": "Ya terminó inscripción. Le mostramos los próximos pasos; falta que pulse 'Empezar ahora'.",
        "accion": "Reenviar último paso",
    },
    "bienvenida_enviada": {
        "titulo": "Bienvenida enviada — esperando 'Empezar ahora'",
        "texto": "Vio el journey. Tiene que pulsar 'Empezar ahora' para arrancar los videos del día 1.",
        "accion": "Reenviar último paso",
    },
    "esperando_capacitacion_dia1": {
        "titulo": "Esperando día 1 de capacitación",
        "texto": "El cron de las 11am Bogotá le va a mandar el día 1. Si no quiere esperar, forzá con 'Reenviar último paso'.",
        "accion": "Reenviar último paso",
    },
    "en_capacitacion": {
        "titulo": "En capacitación",
        "texto": "Está viendo los videos. El cron sigue avanzando los días. 'Reenviar' manda otra vez el día actual si quedó parada.",
        "accion": "Reenviar último paso",
    },
    "capacitacion_completa": {
        "titulo": "Capacitación completa — falta activar edificio",
        "texto": "Terminó los 14 videos. Ahora tiene que agregar su edificio para que arranque la autorización.",
        "accion": "Reenviar último paso",
    },
    "autorizacion_pendiente": {
        "titulo": "Autorización pendiente del administrador",
        "texto": "Está hablando con el admin del edificio para pegar el QR. Esperamos foto del QR instalado.",
        "accion": "Reenviar último paso",
    },
    "autorizacion_rechazada": {
        "titulo": "Autorización rechazada por admin",
        "texto": "El admin dijo que no. Decidimos si re-intentamos en otro momento o pausamos la agente.",
        "accion": None,
    },
    "kit_pendiente": {
        "titulo": "Esperando dirección para enviar kit",
        "texto": "Tiene que mandar su dirección completa (torre, apto, barrio, ciudad).",
        "accion": "Reenviar último paso",
    },
    "kit_enviado": {
        "titulo": "Kit en camino — esperando foto del QR instalado",
        "texto": "El kit salió. Cuando reciba, tiene que pegar el QR y mandar foto.",
        "accion": "Reenviar último paso",
    },
    "activa": {
        "titulo": "Activa — operativa 🎉",
        "texto": "Ya está recibiendo citas y gestionando inventario. Panel operativo disponible desde WA.",
        "accion": None,
    },
    "pausada": {
        "titulo": "Pausada",
        "texto": "Pausada manualmente desde panel. Reactivá cuando corresponda.",
        "accion": None,
    },
    "certificado": {
        "titulo": "Certificada",
        "texto": "Capacitación cerrada con certificación formal.",
        "accion": None,
    },
}


@petra_panel_bp.get("/agente/<int:cid>")
def ficha_agente(cid):
    _check_token()
    token = (request.args.get("token") or "").strip()
    flash = request.args.get("flash", "")
    with _db._conexion_directa(timeout=10) as conn:
        ctx = _cargar_ficha_agente(conn, cid)
    if not ctx:
        abort(404)
    ctx["token"] = token
    ctx["flash"] = flash
    return render_template("petra_agente_detalle.html", **ctx)


@petra_panel_bp.post("/agente/<int:cid>/responder")
def accion_agente_responder(cid):
    _check_token()
    texto = (request.form.get("texto") or "").strip()
    token = request.form.get("token", "")
    if not texto:
        return redirect(url_for("petra_panel.ficha_agente", cid=cid,
                                token=token, flash="vacio"))
    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT telefono FROM petra_contactos WHERE id = %s", (cid,)
        ).fetchone()
    if not row:
        abort(404)
    tel = row[0]
    from app import petra_wa
    r = petra_wa.send_text(tel, texto)
    flash = "enviado" if r.get("ok") else f"error:{str(r)[:80]}"
    return redirect(url_for("petra_panel.ficha_agente", cid=cid,
                            token=token, flash=flash))


def _set_bot_pausado(cid, pausado):
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute(
            "UPDATE petra_contacto_roles "
            "SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), "
            "                         '{bot_pausado}', to_jsonb(%s::boolean)) "
            "WHERE contacto_id = %s AND rol = 'agente_petra'",
            (pausado, cid))


@petra_panel_bp.post("/agente/<int:cid>/pausar_bot")
def accion_agente_pausar_bot(cid):
    _check_token()
    _set_bot_pausado(cid, True)
    return redirect(url_for("petra_panel.ficha_agente", cid=cid,
                            token=request.form.get("token", ""),
                            flash="bot_pausado"))


@petra_panel_bp.post("/agente/<int:cid>/reactivar_bot")
def accion_agente_reactivar_bot(cid):
    _check_token()
    _set_bot_pausado(cid, False)
    return redirect(url_for("petra_panel.ficha_agente", cid=cid,
                            token=request.form.get("token", ""),
                            flash="bot_reactivado"))


@petra_panel_bp.post("/agente/<int:cid>/reenviar_ultimo_paso")
def accion_agente_reenviar_ultimo_paso(cid):
    """Dispara _enviar_recordatorio_retorno del router: manda a la agente
    el call-to-action del paso del flow en el que está, para destrabarla
    si quedó parada sin saber qué hacer."""
    _check_token()
    token = request.form.get("token", "")
    volver = (request.form.get("volver") or "detalle").strip()
    try:
        with _db._conexion_directa(timeout=5) as conn:
            row = conn.execute(
                "SELECT telefono FROM petra_contactos WHERE id = %s", (cid,)
            ).fetchone()
    except Exception:
        row = None
    if row and row[0]:
        try:
            from app.petra_agents.router import _enviar_recordatorio_retorno
            _enviar_recordatorio_retorno(row[0])
        except Exception as exc:  # noqa: BLE001
            import logging as _lg
            _lg.getLogger("petra").exception(
                "[panel] reenviar_ultimo_paso cid=%s fallo", cid)
    if volver == "home":
        return redirect(url_for("petra_panel.panel_home",
                                tab="agentes", token=token,
                                flash="recordatorio_enviado"))
    return redirect(url_for("petra_panel.ficha_agente", cid=cid,
                            token=token, flash="recordatorio_enviado"))


# =============================================================================
# Corregir dato de agente: resetea el campo + estado=recolectando + mensaje
# proactivo. El flow determinístico vuelve a capturar la respuesta y avanza.
# =============================================================================

_CAMPOS_CORREGIBLES = {"nombre", "cedula", "ciudad", "barrio", "conjunto"}

_MSG_PEDIR_CAMPO = {
    "nombre":   "Hola! 👋 ¿Me podrías decir tu nombre completo?",
    "cedula":   "Hola {nombre}! Me faltó tu cédula. ¿Me la mandás? (solo números)",
    "ciudad":   "Hola {nombre}! ¿En qué ciudad vivís?",
    "barrio":   "Hola {nombre}! ¿En qué barrio vivís?",
    "conjunto": "Hola {nombre}! ¿Cómo se llama el conjunto o edificio donde vivís?",
}


@petra_panel_bp.post("/agente/<int:cid>/pedir_corregir/<campo>")
def accion_agente_pedir_corregir(cid, campo):
    """Resetea un campo específico de la agente, cierra la sesión activa
    para que el flow arranque limpio, y manda un mensaje proactivo
    pidiendo ese dato. Cuando la agente responda, el handler determinístico
    vuelve a capturar el valor y avanza al resumen de confirmación."""
    _check_token()
    token = request.form.get("token", "")
    if campo not in _CAMPOS_CORREGIBLES:
        return redirect(url_for("petra_panel.ficha_agente", cid=cid,
                                token=token, flash=f"error:campo invalido"))

    import json as _json
    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT c.telefono, r.metadata "
            "FROM petra_contactos c "
            "JOIN petra_contacto_roles r ON r.contacto_id = c.id "
            "WHERE c.id = %s AND r.rol = 'agente_petra'",
            (cid,)
        ).fetchone()
        if not row:
            abort(404)
        tel, meta = row[0], (row[1] or {})
        nombre_actual = str(meta.get("nombre") or "").split(" ")[0] or ""
        # Reset del campo + volver a estado recolectando
        meta.pop(campo, None)
        meta["estado_agente"] = "recolectando"
        conn.execute(
            "UPDATE petra_contacto_roles SET metadata = %s::jsonb "
            "WHERE contacto_id = %s AND rol = 'agente_petra'",
            (_json.dumps(meta), cid),
        )
        # Cerrar sesión activa para que la próxima respuesta arranque limpia
        conn.execute(
            "UPDATE petra_sesion_chat SET cerrada_at = NOW(), "
            "razon_cierre = 'correccion_manual_panel' "
            "WHERE telefono = %s AND cerrada_at IS NULL",
            (tel,),
        )

    # Mensaje proactivo
    plantilla = _MSG_PEDIR_CAMPO[campo]
    mensaje = plantilla.format(nombre=nombre_actual) if "{nombre}" in plantilla else plantilla
    # Si no tenemos nombre y la plantilla lo pide, limpiar los dobles espacios
    mensaje = mensaje.replace("Hola ! ", "Hola! ").replace("Hola !", "Hola!")
    from app import petra_wa
    r = petra_wa.send_text(tel, mensaje)
    flash = f"correccion_{campo}_ok" if r.get("ok") else f"error:{str(r)[:80]}"
    return redirect(url_for("petra_panel.ficha_agente", cid=cid,
                            token=token, flash=flash))


# =============================================================================
# Admin: ejecutar manualmente el cron de capacitación.
# Útil para pruebas o cuando se cargan videos nuevos y no se quiere esperar
# hasta las 8am del día siguiente. Respeta PETRA_CRON_CAPACITACION_OFF y
# PETRA_PILOTO_WHITELIST, igual que el cron automático.
# =============================================================================


@petra_panel_bp.post("/admin/run_capacitacion")
def accion_admin_run_capacitacion():
    _check_token()
    token = request.form.get("token", "")
    ignore_date = (request.form.get("ignore_date") or "").strip().lower() in ("1", "true", "on", "yes")
    try:
        from app.scheduler import _petra_enviar_capacitacion_diaria
        _petra_enviar_capacitacion_diaria(ignorar_dedup_fecha=ignore_date)
        flash = "cap_cron_ok"
    except Exception as exc:
        flash = f"error:{str(exc)[:80]}"
    return redirect(url_for("petra_panel.panel_home", tab="contenidos",
                            token=token, flash=flash))


@petra_panel_bp.post("/admin/revertir_cap_hoy")
def accion_admin_revertir_cap_hoy():
    """Resetea cap_dia_enviado y cap_ultimo_envio_fecha de todos los agentes
    cuyo cap_ultimo_envio_fecha es HOY (zona Bogotá) EXCEPTO los que están
    en PETRA_PILOTO_WHITELIST (ellos sí recibieron). Útil para deshacer el
    efecto de un disparo del cron hecho mientras la whitelist estaba
    activa (los demás agentes quedaron con cap_dia_enviado incrementado
    pero sin haber recibido nada)."""
    _check_token()
    token = request.form.get("token", "")
    import datetime as _dt, json as _json
    try:
        from zoneinfo import ZoneInfo
        hoy = _dt.datetime.now(ZoneInfo("America/Bogota")).date().isoformat()
    except Exception:
        hoy = _dt.datetime.utcnow().date().isoformat()
    wl = (os.environ.get("PETRA_PILOTO_WHITELIST") or "").strip()
    whitelist = {s.strip() for s in wl.split(",") if s.strip()}
    revertidos = []
    try:
        with _db._conexion_directa(timeout=15) as conn:
            cur = conn.execute(
                "SELECT c.id, c.telefono, r.metadata "
                "FROM petra_contactos c "
                "JOIN petra_contacto_roles r ON r.contacto_id=c.id "
                "WHERE r.rol='agente_petra' "
                "AND r.metadata->>'cap_ultimo_envio_fecha' = %s", (hoy,)
            )
            filas = cur.fetchall()
            for cid, tel, meta in filas:
                if tel in whitelist:
                    continue  # a esta sí le llegó realmente
                m = meta or {}
                m.pop("cap_dia_enviado", None)
                m.pop("cap_ultimo_envio_fecha", None)
                # Si el envío la había promovido de esperando_capacitacion_dia1,
                # también revertimos el estado.
                if m.get("estado_agente") == "en_capacitacion" and not m.get("videos_vistos"):
                    m["estado_agente"] = "esperando_capacitacion_dia1"
                conn.execute(
                    "UPDATE petra_contacto_roles SET metadata = %s::jsonb "
                    "WHERE contacto_id=%s AND rol='agente_petra'",
                    (_json.dumps(m), cid),
                )
                revertidos.append(tel)
        flash = f"revertidos_{len(revertidos)}"
    except Exception as exc:
        flash = f"error:{str(exc)[:80]}"
    return redirect(url_for("petra_panel.panel_home", tab="contenidos",
                            token=token, flash=flash))


@petra_panel_bp.post("/agente/<int:cid>/revertir_cap")
def accion_agente_revertir_cap(cid):
    """Resetea el progreso de capacitación de UNA agente: cap_dia_enviado,
    cap_ultimo_envio_fecha, videos_vistos. Si estaba en 'en_capacitacion'
    vuelve a 'esperando_capacitacion_dia1'. Útil cuando un envío falló
    (ventana 24h de Meta) pero el metadata quedó avanzado, o para
    reiniciar la capacitación."""
    _check_token()
    token = request.form.get("token", "")
    import json as _json
    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT r.metadata FROM petra_contacto_roles r "
            "WHERE r.contacto_id=%s AND r.rol='agente_petra'", (cid,)
        ).fetchone()
        if not row:
            abort(404)
        m = row[0] or {}
        m.pop("cap_dia_enviado", None)
        m.pop("cap_ultimo_envio_fecha", None)
        m.pop("videos_vistos", None)
        if m.get("estado_agente") == "en_capacitacion":
            m["estado_agente"] = "esperando_capacitacion_dia1"
        conn.execute(
            "UPDATE petra_contacto_roles SET metadata=%s::jsonb "
            "WHERE contacto_id=%s AND rol='agente_petra'",
            (_json.dumps(m), cid),
        )
    return redirect(url_for("petra_panel.ficha_agente", cid=cid,
                            token=token, flash="cap_reseteada"))


# =============================================================================
# FASE A — Textos editables desde panel
# =============================================================================

@petra_panel_bp.post("/textos/upsert")
def textos_upsert():
    """Crea o actualiza un texto editable del bot."""
    _check_token()
    token = request.form.get("token", "")
    clave = (request.form.get("clave") or "").strip()
    contenido = request.form.get("contenido") or ""
    descripcion = request.form.get("descripcion") or None
    categoria = (request.form.get("categoria") or "otro").strip()
    variables_raw = (request.form.get("variables") or "").strip()
    variables = [v.strip() for v in variables_raw.split(",") if v.strip()]
    if not clave or not contenido:
        abort(400, "clave y contenido son obligatorios")
    from app.petra_agents import textos as _textos_mod
    ok = _textos_mod.upsert_texto(
        clave=clave, contenido=contenido, descripcion=descripcion,
        variables=variables, categoria=categoria, updated_by="panel",
    )
    return redirect(url_for(
        "petra_panel.panel_home", tab="textos", token=token,
        flash=("texto_guardado" if ok else "texto_error"),
    ))


@petra_panel_bp.post("/textos/eliminar")
def textos_eliminar():
    """Borra una clave. El flow volverá a usar su fallback hardcodeado."""
    _check_token()
    token = request.form.get("token", "")
    clave = (request.form.get("clave") or "").strip()
    if not clave:
        abort(400)
    from app.petra_agents import textos as _textos_mod
    _textos_mod.eliminar_texto(clave)
    return redirect(url_for(
        "petra_panel.panel_home", tab="textos", token=token,
        flash="texto_eliminado",
    ))


@petra_panel_bp.post("/textos/toggle_activo")
def textos_toggle_activo():
    """Oculta (activo=false) o re-activa un mensaje. Más suave que eliminar
    porque la config queda guardada."""
    _check_token()
    token = request.form.get("token", "")
    clave = (request.form.get("clave") or "").strip()
    activo = request.form.get("activo", "0") in ("1", "true", "on")
    preset = (request.form.get("preset") or "").strip()
    if not clave:
        abort(400)
    from app.petra_agents import textos as _textos_mod
    _textos_mod.set_activo(clave, activo)
    return redirect(url_for(
        "petra_panel.panel_home", tab="simulador", token=token,
        preset=preset,
        flash=("texto_activado" if activo else "texto_ocultado"),
    ))


@petra_panel_bp.post("/mensajes_extra/crear")
def mensajes_extra_crear():
    """Agrega un mensaje extra que el bot envía DESPUÉS del mensaje padre."""
    _check_token()
    token = request.form.get("token", "")
    parent_clave = (request.form.get("parent_clave") or "").strip()
    contenido = (request.form.get("contenido") or "").strip()
    preset = (request.form.get("preset") or "").strip()
    from app.petra_agents import mensajes_extra as _mex
    try:
        _mex.crear(parent_clave, contenido)
    except ValueError as e:
        abort(400, str(e))
    return redirect(url_for(
        "petra_panel.panel_home", tab="simulador", token=token,
        preset=preset, flash="extra_agregado",
    ))


@petra_panel_bp.post("/mensajes_extra/<int:mid>/eliminar")
def mensajes_extra_eliminar(mid):
    _check_token()
    token = request.form.get("token", "")
    preset = (request.form.get("preset") or "").strip()
    from app.petra_agents import mensajes_extra as _mex
    _mex.eliminar(mid)
    return redirect(url_for(
        "petra_panel.panel_home", tab="simulador", token=token,
        preset=preset, flash="extra_eliminado",
    ))


@petra_panel_bp.post("/textos/seed")
def textos_seed():
    """Carga los textos baseline (idempotente: no sobrescribe lo editado)."""
    _check_token()
    token = request.form.get("token", "")
    forzar = request.form.get("forzar") == "1"
    from app.petra_agents import textos as _textos_mod
    ins, upd, omi = _textos_mod.seed_textos(forzar_sobrescribir=forzar)
    return redirect(url_for(
        "petra_panel.panel_home", tab="textos", token=token,
        flash=f"seed_{ins}_{upd}_{omi}",
    ))


# =============================================================================
# FASE A — FAQ dedicada (knowledge base del LLM)
# =============================================================================

@petra_panel_bp.post("/faq/crear")
def faq_crear():
    """Crea una nueva entrada FAQ. Internamente es petra_contenidos tipo='faq'."""
    _check_token()
    token = request.form.get("token", "")
    titulo = (request.form.get("titulo") or "").strip()
    texto = (request.form.get("texto") or "").strip()
    descripcion = (request.form.get("descripcion") or "").strip() or None
    if not titulo or not texto:
        abort(400, "titulo y texto son obligatorios")
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute(
            "INSERT INTO petra_contenidos "
            "(tipo, titulo, texto, descripcion, activo) "
            "VALUES ('faq', %s, %s, %s, TRUE)",
            (titulo, texto, descripcion),
        )
    return redirect(url_for(
        "petra_panel.panel_home", tab="faq", token=token, flash="faq_creada",
    ))


@petra_panel_bp.post("/faq/<int:fid>/editar")
def faq_editar(fid):
    """Edita una FAQ existente."""
    _check_token()
    token = request.form.get("token", "")
    titulo = (request.form.get("titulo") or "").strip()
    texto = (request.form.get("texto") or "").strip()
    descripcion = (request.form.get("descripcion") or "").strip() or None
    if not titulo or not texto:
        abort(400)
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute(
            "UPDATE petra_contenidos "
            "SET titulo=%s, texto=%s, descripcion=%s, updated_at=NOW() "
            "WHERE id=%s AND tipo IN ('faq','otro')",
            (titulo, texto, descripcion, fid),
        )
    return redirect(url_for(
        "petra_panel.panel_home", tab="faq", token=token, flash="faq_actualizada",
    ))


@petra_panel_bp.post("/faq/<int:fid>/toggle")
def faq_toggle(fid):
    """Activa/desactiva una FAQ sin borrarla."""
    _check_token()
    token = request.form.get("token", "")
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute(
            "UPDATE petra_contenidos SET activo = NOT activo, updated_at=NOW() "
            "WHERE id=%s AND tipo IN ('faq','otro')", (fid,),
        )
    return redirect(url_for(
        "petra_panel.panel_home", tab="faq", token=token, flash="faq_toggled",
    ))


@petra_panel_bp.post("/faq/<int:fid>/eliminar")
def faq_eliminar(fid):
    """Borra una FAQ definitivamente."""
    _check_token()
    token = request.form.get("token", "")
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute(
            "DELETE FROM petra_contenidos "
            "WHERE id=%s AND tipo IN ('faq','otro')", (fid,),
        )
    return redirect(url_for(
        "petra_panel.panel_home", tab="faq", token=token, flash="faq_eliminada",
    ))


# =============================================================================
# FASE C — Activación: envío manual del kit
# =============================================================================

@petra_panel_bp.post("/kit/<int:cid>/marcar_enviado")
def kit_marcar_enviado(cid):
    """Humano confirma que el kit físico fue despachado. Dispara envío
    del QR + video 'cómo usar' + botón 'Ya instalé el QR' por WhatsApp."""
    import datetime as _dt
    import json as _json
    _check_token()
    token = request.form.get("token", "")
    with _db._conexion_directa(timeout=15) as conn:
        row = conn.execute(
            "SELECT c.telefono, r.metadata "
            "FROM petra_contactos c "
            "JOIN petra_contacto_roles r "
            "  ON r.contacto_id=c.id AND r.rol='agente_petra' "
            "WHERE c.id=%s", (cid,),
        ).fetchone()
        if not row:
            abort(404)
        telefono, meta = row[0], (row[1] or {})
        if meta.get("estado_agente") != "kit_pendiente":
            return redirect(url_for(
                "petra_panel.panel_home", tab="kits", token=token,
                flash="kit_estado_invalido",
            ))
        meta["estado_agente"] = "kit_enviado"
        meta["kit_enviado_at"] = _dt.datetime.utcnow().isoformat()
        conn.execute(
            "UPDATE petra_contacto_roles SET metadata=%s::jsonb, "
            "updated_at=NOW() WHERE contacto_id=%s AND rol='agente_petra'",
            (_json.dumps(meta), cid),
        )
        # QR del edificio (si está asignado). URL público (QR → WhatsApp agente)
        qr_url = None
        try:
            qr_row = conn.execute(
                "SELECT e.qr_slug FROM petra_edificios e "
                "JOIN petra_contacto_roles r "
                "  ON r.edificio_id = e.id AND r.contacto_id = %s "
                "  AND r.rol = 'agente_petra' "
                "WHERE e.qr_slug IS NOT NULL LIMIT 1",
                (cid,),
            ).fetchone()
            if qr_row and qr_row[0]:
                qr_url = f"https://petra-inmuebles.onrender.com/qr/{qr_row[0]}"
        except Exception:
            pass
        # Video "cómo usar QR" si está cargado
        vid_row = conn.execute(
            "SELECT titulo, url FROM petra_contenidos "
            "WHERE tipo='video_como_usar_qr' AND activo=TRUE "
            "ORDER BY orden LIMIT 1"
        ).fetchone()

    # Envío fuera de la transacción
    from app import petra_wa as _pwa
    from app.petra_agents.textos import t as _t
    import time as _tm
    from app.petra_agents.flow_onboarding_agente import _sm as _sm_flow
    _sm_flow(telefono,
        "activacion.msg_kit_enviado",
        fallback=(
            "📦 *¡Tu kit ya está en camino!*\n\n"
            "Dentro vas a encontrar tu QR + un regalito 💜. "
            "Cuando lo recibas, instalá el QR en la cartelera del conjunto "
            "o donde lo autorizó el administrador.\n\n"
            "Te paso un video rápido con el paso a paso 👇"
        ),
    )
    _tm.sleep(1)
    if vid_row:
        _pwa.send_text(telefono, f"📲 *{vid_row[0]}*\n\n{vid_row[1]}")
        _tm.sleep(2)
    if qr_url:
        _pwa.send_text(telefono, f"🔗 Tu QR único: {qr_url}")
        _tm.sleep(1)
    _pwa.send_buttons(telefono, _t(
        "activacion.msg_qr_boton",
        fallback="Cuando lo tengas instalado, pulsá el botón 👇",
    ), [{"id": "ONBAG_QR_INSTALADO", "title": "✅ Ya instalé el QR"}])

    return redirect(url_for(
        "petra_panel.panel_home", tab="kits", token=token,
        flash="kit_marcado_enviado",
    ))


# =============================================================================
# FASE D — Citas (reutiliza petra_visitas)
# =============================================================================

@petra_panel_bp.post("/cita/crear")
def cita_crear():
    """Crea una cita (petra_visitas tipo='comercial') y notifica a la
    agente por WhatsApp con botones. Hoy se usa manualmente desde el
    panel; mañana el flujo del interesado puede llamar al mismo endpoint
    internamente para enganchar sin re-trabajo."""
    import datetime as _dt
    _check_token()
    token = request.form.get("token", "")
    agente_cid = int(request.form.get("agente_cid") or 0)
    interesado_nombre = (request.form.get("interesado_nombre") or "").strip()
    interesado_tel = (request.form.get("interesado_tel") or "").strip() or None
    inmueble_desc = (request.form.get("inmueble_desc") or "").strip()
    fecha_hora = (request.form.get("fecha_hora") or "").strip()
    nota = (request.form.get("nota") or "").strip() or None
    if not agente_cid or not interesado_nombre or not fecha_hora:
        abort(400, "agente, interesado y fecha son obligatorios")
    try:
        programada_at = _dt.datetime.fromisoformat(fecha_hora)
    except Exception:
        abort(400, "fecha inválida (formato esperado: YYYY-MM-DDTHH:MM)")
    with _db._conexion_directa(timeout=15) as conn:
        # Agente debe estar en estado activa
        row = conn.execute(
            "SELECT c.telefono, c.nombre, r.metadata->>'estado_agente' "
            "FROM petra_contactos c "
            "JOIN petra_contacto_roles r "
            "  ON r.contacto_id=c.id AND r.rol='agente_petra' "
            "WHERE c.id=%s", (agente_cid,),
        ).fetchone()
        if not row:
            abort(404, "agente no encontrada")
        agente_tel, agente_nombre, estado = row
        if estado not in ("activa",):
            return redirect(url_for(
                "petra_panel.panel_home", tab="visitas", token=token,
                flash="cita_agente_no_activa",
            ))
        # Interesado: crea contacto si trae tel, si no deja NULL
        interesado_id = None
        if interesado_tel:
            r2 = conn.execute(
                "INSERT INTO petra_contactos (telefono, nombre) "
                "VALUES (%s, %s) "
                "ON CONFLICT (telefono) DO UPDATE SET "
                "  nombre = COALESCE(petra_contactos.nombre, EXCLUDED.nombre) "
                "RETURNING id",
                (interesado_tel, interesado_nombre),
            ).fetchone()
            interesado_id = r2[0] if r2 else None
        # Insert visita
        vid_row = conn.execute(
            "INSERT INTO petra_visitas "
            "(agente_id, interesado_id, tipo, programada_at, estado, observaciones) "
            "VALUES (%s, %s, 'comercial', %s, 'programada', %s) "
            "RETURNING id",
            (agente_cid, interesado_id, programada_at,
             (f"Inmueble: {inmueble_desc}\n" if inmueble_desc else "") + (nota or "")),
        ).fetchone()
        visita_id = vid_row[0]

    # Notificar a la agente con botones (fuera de la transacción)
    from app import petra_wa as _pwa
    from app.petra_agents.textos import t as _t
    _pwa.send_buttons(
        agente_tel,
        _t(
            "citas.msg_nueva_cita",
            fallback=(
                "📅 *¡Nueva cita!*\n\n"
                "👤 *Interesado:* {interesado}\n"
                "🏠 *Inmueble:* {inmueble}\n"
                "🕒 *Propuesta:* {cuando}\n"
                "\n¿Podés atenderla?"
            ),
            interesado=interesado_nombre,
            inmueble=inmueble_desc or "a confirmar",
            cuando=_fmt_fecha_es(programada_at),
        ),
        [
            {"id": f"ONBAG_CITA_PUEDO_{visita_id}", "title": "✅ Puedo"},
            {"id": f"ONBAG_CITA_PROPONER_{visita_id}", "title": "📅 Proponer otro día"},
            {"id": f"ONBAG_CITA_NO_PUEDO_{visita_id}", "title": "❌ No puedo"},
        ],
    )
    return redirect(url_for(
        "petra_panel.panel_home", tab="visitas", token=token,
        flash=f"cita_creada_{visita_id}",
    ))


def _fmt_fecha_es(dt):
    """Formato humano colombiano para fechas."""
    if not dt:
        return "fecha por confirmar"
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
             "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    return (f"{dias[dt.weekday()]} {dt.day} de {meses[dt.month-1]} "
            f"a las {dt.hour:02d}:{dt.minute:02d}")


# =============================================================================
# FASE E — Inmuebles (validación / edición desde panel)
# =============================================================================

@petra_panel_bp.post("/inmueble/<int:iid>/estado")
def inmueble_cambiar_estado(iid):
    """Cambia el estado de un inmueble (borrador → activo → cerrado)."""
    _check_token()
    token = request.form.get("token", "")
    nuevo_estado = (request.form.get("estado") or "").strip()
    if nuevo_estado not in ("borrador", "activo", "pausado", "cerrado"):
        abort(400, "estado inválido")
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute(
            "UPDATE petra_inmuebles SET estado=%s, updated_at=NOW(), "
            "publicado_at = CASE WHEN %s='activo' AND publicado_at IS NULL "
            "                    THEN NOW() ELSE publicado_at END "
            "WHERE id=%s",
            (nuevo_estado, nuevo_estado, iid),
        )
    return redirect(url_for(
        "petra_panel.panel_home", tab="inmuebles", token=token,
        flash=f"inmueble_estado_{nuevo_estado}",
    ))


@petra_panel_bp.post("/inmueble/<int:iid>/foto_url")
def inmueble_agregar_foto_url(iid):
    """Agrega una foto al inmueble vía URL externa (link). Para subir
    archivo local, usar POST /inmueble/<id>/foto_upload (abajo)."""
    import json as _json
    _check_token()
    token = request.form.get("token", "")
    url_foto = (request.form.get("url_foto") or "").strip()
    if not url_foto or not url_foto.startswith(("http://", "https://")):
        abort(400, "URL inválida")
    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT fotos FROM petra_inmuebles WHERE id=%s", (iid,),
        ).fetchone()
        if not row:
            abort(404)
        fotos = list(row[0] or [])
        fotos.append({"url": url_foto, "fuente": "panel_url"})
        conn.execute(
            "UPDATE petra_inmuebles SET fotos=%s::jsonb, updated_at=NOW() "
            "WHERE id=%s", (_json.dumps(fotos), iid),
        )
    return redirect(url_for(
        "petra_panel.panel_home", tab="inmuebles", token=token,
        flash="inmueble_foto_agregada",
    ))


@petra_panel_bp.post("/inmueble/<int:iid>/foto_upload")
def inmueble_subir_foto(iid):
    """Sube un archivo de imagen local al inmueble (reutiliza media dir)."""
    import json as _json
    _check_token()
    token = request.form.get("token", "")
    file = request.files.get("archivo")
    if not file or not file.filename:
        abort(400, "sin archivo")
    url_servida = _guardar_archivo_media(file)
    if not url_servida:
        abort(400, "archivo inválido")
    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT fotos FROM petra_inmuebles WHERE id=%s", (iid,),
        ).fetchone()
        if not row:
            abort(404)
        fotos = list(row[0] or [])
        fotos.append({"url": url_servida, "fuente": "panel_upload"})
        conn.execute(
            "UPDATE petra_inmuebles SET fotos=%s::jsonb, updated_at=NOW() "
            "WHERE id=%s", (_json.dumps(fotos), iid),
        )
    return redirect(url_for(
        "petra_panel.panel_home", tab="inmuebles", token=token,
        flash="inmueble_foto_subida",
    ))


@petra_panel_bp.post("/inmueble/<int:iid>/nota")
def inmueble_nota(iid):
    """Edita el campo de notas del inmueble."""
    _check_token()
    token = request.form.get("token", "")
    notas = (request.form.get("notas") or "").strip() or None
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute(
            "UPDATE petra_inmuebles SET notas=%s, updated_at=NOW() WHERE id=%s",
            (notas, iid),
        )
    return redirect(url_for(
        "petra_panel.panel_home", tab="inmuebles", token=token,
        flash="inmueble_nota",
    ))


# =============================================================================
# FASE F — Nudges (reglas de remarketing)
# =============================================================================

@petra_panel_bp.post("/nudges/upsert")
def nudges_upsert():
    """Crea o actualiza una regla de nudge."""
    _check_token()
    token = request.form.get("token", "")
    clave = (request.form.get("clave") or "").strip()
    estado_origen = (request.form.get("estado_origen") or "").strip()
    texto_clave = (request.form.get("texto_clave") or "").strip()
    horas = int(request.form.get("horas") or "12")
    descripcion = (request.form.get("descripcion") or "").strip() or None
    activo = request.form.get("activo") == "on" or not request.form.get("clave_existente")
    if not clave or not estado_origen or not texto_clave or horas < 1:
        abort(400)
    from app.petra_agents import nudges as _n
    _n.upsert_regla(clave, estado_origen, horas, texto_clave,
                    descripcion=descripcion, activo=activo)
    return redirect(url_for(
        "petra_panel.panel_home", tab="nudges", token=token,
        flash="nudge_guardado",
    ))


@petra_panel_bp.post("/nudges/<clave>/toggle")
def nudges_toggle(clave):
    _check_token()
    token = request.form.get("token", "")
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute(
            "UPDATE petra_nudges SET activo = NOT activo, updated_at=NOW() "
            "WHERE clave=%s", (clave,),
        )
    return redirect(url_for(
        "petra_panel.panel_home", tab="nudges", token=token,
        flash="nudge_toggled",
    ))


@petra_panel_bp.post("/nudges/<clave>/eliminar")
def nudges_eliminar(clave):
    _check_token()
    token = request.form.get("token", "")
    from app.petra_agents import nudges as _n
    _n.eliminar_regla(clave)
    return redirect(url_for(
        "petra_panel.panel_home", tab="nudges", token=token,
        flash="nudge_eliminado",
    ))


@petra_panel_bp.post("/nudges/seed")
def nudges_seed():
    """Carga las reglas base. Idempotente."""
    _check_token()
    token = request.form.get("token", "")
    forzar = request.form.get("forzar") == "1"
    from app.petra_agents import nudges as _n
    ins, upd, omi = _n.seed_reglas(forzar=forzar)
    return redirect(url_for(
        "petra_panel.panel_home", tab="nudges", token=token,
        flash=f"nudges_seed_{ins}_{upd}_{omi}",
    ))


@petra_panel_bp.post("/nudges/evaluar_ahora")
def nudges_evaluar_ahora():
    """Dispara la evaluación de nudges manualmente (bypass del cron)."""
    _check_token()
    token = request.form.get("token", "")
    from app.petra_agents import nudges as _n
    try:
        reglas, enviados = _n.evaluar_y_enviar()
        flash = f"nudges_evaluado_{reglas}_{enviados}"
    except Exception:
        flash = "nudges_evaluacion_error"
    return redirect(url_for(
        "petra_panel.panel_home", tab="nudges", token=token, flash=flash,
    ))


# =============================================================================
# Simulador — probar flujo sin WhatsApp real
# =============================================================================

@petra_panel_bp.post("/simulador/reset")
def simulador_reset():
    """Resetea el contacto SIM al preset elegido."""
    _check_token()
    token = request.form.get("token", "")
    preset = (request.form.get("preset") or "nuevo").strip()
    from app.petra_agents import simulador as _sim
    _sim.reset(preset)
    return redirect(url_for(
        "petra_panel.panel_home", tab="simulador", token=token,
        preset=preset, flash="sim_reset",
    ))


@petra_panel_bp.post("/simulador/send")
def simulador_send():
    """Envía input al simulador (texto o botón). Reutiliza el router real."""
    _check_token()
    token = request.form.get("token", "")
    texto = request.form.get("texto") or ""
    button_id = request.form.get("button_id") or None
    preset = request.form.get("preset") or ""
    from app.petra_agents import simulador as _sim
    _sim.tick(texto=texto, button_id=button_id)
    return redirect(url_for(
        "petra_panel.panel_home", tab="simulador", token=token, preset=preset,
    ))


# =============================================================================
# Media adjunto por estación (Nivel 2 — editable visualmente)
# =============================================================================

@petra_panel_bp.post("/media_estacion/crear")
def media_estacion_crear():
    """Crea una entrada de media para un estado del flow. Puede ser upload
    o URL externa."""
    _check_token()
    token = request.form.get("token", "")
    estado_key = (request.form.get("estado_key") or "").strip()
    clave_texto = (request.form.get("clave_texto") or "").strip() or None
    titulo = (request.form.get("titulo") or "").strip() or None
    url_externa = (request.form.get("url_externa") or "").strip() or None
    archivo = request.files.get("archivo")
    if not estado_key:
        abort(400, "estado_key requerido")
    # Fuente: upload o URL
    url_final = None
    if archivo and archivo.filename:
        url_final = _guardar_archivo_media(archivo)
        if not url_final:
            abort(400, "archivo inválido")
    elif url_externa and url_externa.startswith(("http://", "https://")):
        url_final = url_externa
    else:
        abort(400, "sube un archivo o pega una URL")
    with _db._conexion_directa(timeout=10) as conn:
        # orden incremental: por clave_texto si está, sino por estación
        if clave_texto:
            row = conn.execute(
                "SELECT COALESCE(MAX(orden), 0) + 1 FROM petra_media_estacion "
                "WHERE clave_texto=%s", (clave_texto,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(MAX(orden), 0) + 1 FROM petra_media_estacion "
                "WHERE estado_key=%s AND clave_texto IS NULL", (estado_key,),
            ).fetchone()
        orden = row[0] if row else 1
        conn.execute(
            "INSERT INTO petra_media_estacion "
            "(estado_key, clave_texto, orden, titulo, url, activo) "
            "VALUES (%s, %s, %s, %s, %s, TRUE)",
            (estado_key, clave_texto, orden, titulo, url_final),
        )
    return redirect(url_for(
        "petra_panel.panel_home", tab="simulador", token=token,
        preset=estado_key, flash="media_agregada",
    ))


@petra_panel_bp.post("/media_estacion/<int:mid>/eliminar")
def media_estacion_eliminar(mid):
    """Borra una media adjunta."""
    _check_token()
    token = request.form.get("token", "")
    estado_key = (request.form.get("estado_key") or "").strip()
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute("DELETE FROM petra_media_estacion WHERE id=%s", (mid,))
    return redirect(url_for(
        "petra_panel.panel_home", tab="simulador", token=token,
        preset=estado_key, flash="media_eliminada",
    ))


# =============================================================================
# Botones editables por estación (crear nuevos / ocultar defaults)
# =============================================================================

@petra_panel_bp.post("/botones_estacion/custom/crear")
def botones_custom_crear():
    """Agrega un botón custom a una estación (cuando el agente lo pulsa,
    el bot responde con `texto_respuesta` y se queda en el mismo estado)."""
    _check_token()
    token = request.form.get("token", "")
    estado_key = (request.form.get("estado_key") or "").strip()
    clave_texto = (request.form.get("clave_texto") or "").strip() or None
    titulo = (request.form.get("titulo") or "").strip()
    texto_respuesta = (request.form.get("texto_respuesta") or "").strip()
    accion = (request.form.get("accion") or "enviar_texto").strip()
    payload = (request.form.get("payload") or "").strip() or None
    from app.petra_agents import botones_estacion as _b_est
    try:
        _b_est.crear_custom(estado_key, titulo, texto_respuesta,
                            clave_texto=clave_texto,
                            accion=accion, payload=payload)
    except ValueError as e:
        abort(400, str(e))
    return redirect(url_for(
        "petra_panel.panel_home", tab="simulador", token=token,
        preset=estado_key, flash="boton_agregado",
    ))


@petra_panel_bp.post("/botones_estacion/<int:bid>/eliminar")
def botones_eliminar(bid):
    """Borra una regla (custom o override)."""
    _check_token()
    token = request.form.get("token", "")
    estado_key = (request.form.get("estado_key") or "").strip()
    from app.petra_agents import botones_estacion as _b_est
    _b_est.eliminar(bid)
    return redirect(url_for(
        "petra_panel.panel_home", tab="simulador", token=token,
        preset=estado_key, flash="boton_eliminado",
    ))


@petra_panel_bp.post("/botones_estacion/override")
def botones_override_toggle():
    """Oculta o desoculta un botón default."""
    _check_token()
    token = request.form.get("token", "")
    estado_key = (request.form.get("estado_key") or "").strip()
    boton_id = (request.form.get("boton_id") or "").strip()
    ocultar = request.form.get("ocultar", "0") in ("1", "true", "on")
    from app.petra_agents import botones_estacion as _b_est
    try:
        _b_est.toggle_override(estado_key, boton_id, ocultar)
    except ValueError as e:
        abort(400, str(e))
    return redirect(url_for(
        "petra_panel.panel_home", tab="simulador", token=token,
        preset=estado_key,
        flash=("boton_ocultado" if ocultar else "boton_restaurado"),
    ))


# =============================================================================
# Observabilidad: tool-calls (vista "qué está haciendo el bot por dentro")
# =============================================================================

@petra_panel_bp.get("/tool-calls")
def tool_calls_vista():
    """Dashboard mínimo de observabilidad del tool-calling.
    Query params:
      - fase, tool, telefono: filtros (opcionales, substring/exact)
      - limit: cantidad de filas (default 100, max 500)
    """
    _check_token()
    token = (request.args.get("token") or "").strip()
    fase = (request.args.get("fase") or "").strip()
    tool = (request.args.get("tool") or "").strip()
    telefono = (request.args.get("telefono") or "").strip()
    solo_errores = request.args.get("errores", "") in ("1", "true", "on")
    try:
        limit = max(1, min(500, int(request.args.get("limit") or 100)))
    except Exception:
        limit = 100

    where = []
    params = []
    if fase:
        where.append("fase = %s")
        params.append(fase)
    if tool:
        where.append("tool_nombre = %s")
        params.append(tool)
    if telefono:
        where.append("telefono = %s")
        params.append(telefono)
    if solo_errores:
        where.append("ok = FALSE")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    with _db._conexion_directa(timeout=10) as conn:
        filas = _rows(conn,
            "SELECT id, created_at, telefono, cid, fase, mensaje_user, "
            "       tool_nombre, tool_args, tool_resultado, ok, latencia_ms, modelo "
            "FROM petra_tool_calls" + where_sql + " "
            "ORDER BY id DESC LIMIT %s",
            tuple(params) + (limit,))
        # Métricas agregadas de las últimas 24h
        resumen = _rows(conn,
            "SELECT fase, tool_nombre, "
            "       COUNT(*) AS total, "
            "       COUNT(*) FILTER (WHERE NOT ok) AS errores, "
            "       ROUND(AVG(latencia_ms)::numeric, 0) AS lat_prom_ms "
            "FROM petra_tool_calls "
            "WHERE created_at >= NOW() - INTERVAL '24 hours' "
            "GROUP BY fase, tool_nombre "
            "ORDER BY total DESC")
        total_24h = conn.execute(
            "SELECT COUNT(*), COUNT(*) FILTER (WHERE NOT ok) "
            "FROM petra_tool_calls WHERE created_at >= NOW() - INTERVAL '24 hours'"
        ).fetchone()
    import json as _json
    for f in filas:
        try:
            f["tool_args_str"] = _json.dumps(f.get("tool_args") or {},
                                             ensure_ascii=False)
        except Exception:
            f["tool_args_str"] = str(f.get("tool_args"))
        try:
            f["tool_resultado_str"] = _json.dumps(f.get("tool_resultado") or {},
                                                   ensure_ascii=False)
        except Exception:
            f["tool_resultado_str"] = str(f.get("tool_resultado"))
    return render_template(
        "petra_tool_calls.html",
        filas=filas,
        resumen=resumen,
        total_24h=(total_24h[0] if total_24h else 0),
        errores_24h=(total_24h[1] if total_24h else 0),
        filtros={"fase": fase, "tool": tool, "telefono": telefono,
                 "errores": solo_errores, "limit": limit},
        token=token,
    )


# =============================================================================
# Evals: correr golden set contra el LLM real desde el panel
# =============================================================================

@petra_panel_bp.get("/evals")
def evals_vista():
    """Lista los casos de evals disponibles y permite correrlos."""
    _check_token()
    token = (request.args.get("token") or "").strip()
    from pathlib import Path
    import json as _json
    golden_dir = Path(__file__).parent / "evals" / "golden"
    casos = []
    for p in sorted(golden_dir.glob("*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                data = _json.load(f)
            casos.append({"file": p.name, "name": data.get("name"),
                          "fase": data.get("fase"),
                          "turnos": len(data.get("turnos") or [])})
        except Exception as e:
            casos.append({"file": p.name, "error": str(e)[:120]})
    return render_template("petra_evals.html", casos=casos, token=token,
                           resultado=None)


@petra_panel_bp.post("/evals/run")
def evals_run():
    """Corre todos los evals contra el LLM real y devuelve resultado."""
    _check_token()
    token = (request.form.get("token") or "").strip()
    import io, contextlib
    from app.evals.runner import GOLDEN_DIR, _correr_caso
    paths = sorted(GOLDEN_DIR.glob("*.json"))
    buffer = io.StringIO()
    resultados = []
    with contextlib.redirect_stdout(buffer):
        for p in paths:
            try:
                ok, nombre, errs = _correr_caso(p)
                resultados.append({"file": p.name, "name": nombre,
                                   "ok": ok, "errors": errs})
            except Exception as e:
                resultados.append({"file": p.name, "name": p.stem,
                                   "ok": False,
                                   "errors": [f"excepcion: {type(e).__name__}: {str(e)[:200]}"]})
    log_text = buffer.getvalue()
    pasaron = sum(1 for r in resultados if r["ok"])
    from pathlib import Path
    import json as _json
    casos = []
    for p in sorted(GOLDEN_DIR.glob("*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                data = _json.load(f)
            casos.append({"file": p.name, "name": data.get("name"),
                          "fase": data.get("fase"),
                          "turnos": len(data.get("turnos") or [])})
        except Exception:
            casos.append({"file": p.name})
    return render_template("petra_evals.html", casos=casos, token=token,
                           resultado={"resultados": resultados,
                                      "pasaron": pasaron,
                                      "total": len(resultados),
                                      "log": log_text})


# =============================================================================
# Admin: reset de un agente por teléfono (uso puntual de testing)
# =============================================================================

@petra_panel_bp.get("/flow")
def flow_vista():
    """Flow explorer: documento vivo del flow del bot.
    Lee todas las claves de petra_textos activas, las agrupa por fase
    (prefijo), y muestra el contenido actual. Si el admin edita un
    texto en el panel de textos, acá se refleja al toque."""
    _check_token()
    token = (request.args.get("token") or "").strip()
    try:
        with _db._conexion_directa(timeout=10) as conn:
            rows = _rows(conn,
                "SELECT clave, contenido, activo, updated_at "
                "FROM petra_textos "
                "ORDER BY clave")
    except Exception as e:
        return f"❌ Error leyendo petra_textos: {e}", 500
    import html as _html
    import re as _re
    orden_fases = [
        "inscripcion", "capacitacion", "autorizacion", "activacion",
        "citas", "inventario", "nudges", "fallback", "boton", "otros",
    ]
    # Pre-procesamos cada item: HTML-escape + resaltar variables {X} +
    # convertir saltos de línea en <br> + resaltar *bold*.
    for r in rows:
        raw = r.get("contenido") or ""
        esc = _html.escape(raw)
        esc = _re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}",
                      r'<span class="var">{\1}</span>', esc)
        esc = _re.sub(r"\*([^*\n]+?)\*", r"<b>\1</b>", esc)
        esc = esc.replace("\n", "<br>")
        r["contenido_html"] = esc
    # Agrupar por prefijo (fase)
    secciones = {}
    for r in rows:
        clave = r.get("clave") or ""
        pref = clave.split(".", 1)[0] if "." in clave else "otros"
        pref = pref if pref in orden_fases else "otros"
        secciones.setdefault(pref, []).append(r)
    secciones_ordenadas = [(f, secciones[f]) for f in orden_fases if f in secciones]
    return render_template(
        "petra_flow.html",
        secciones=secciones_ordenadas,
        total=len(rows),
        activos=sum(1 for r in rows if r.get("activo")),
        token=token,
    )


@petra_panel_bp.get("/_admin/run-evals")
def admin_run_evals():
    """Corre los goldens end-to-end (app/evals_e2e) contra la DB prod,
    usando teléfonos de prueba +99999* que se auto-limpian.

    Query params:
      - name: nombre del golden a correr (opcional; si no, corre todos)
      - baseline: 1 para guardar baseline.json (snapshot del output)
    Devuelve resumen JSON con pass/fail por golden.
    """
    _check_token()
    import json as _json
    import io, contextlib
    from pathlib import Path
    from app.evals_e2e import runner as _runner
    name = (request.args.get("name") or "").strip()
    guardar_baseline = request.args.get("baseline") == "1"
    goldens = _runner._listar()
    if name:
        goldens = [p for p in goldens if p.name == name or p.stem == name]
    if not goldens:
        return "No hay goldens coincidentes", 404
    resultados = []
    failed = 0
    for p in goldens:
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ok, res = _runner.run_golden(p, verbose=True)
            res["log"] = buf.getvalue()
            resultados.append(res)
            if not ok:
                failed += 1
        except Exception as e:
            import traceback
            resultados.append({"name": p.stem, "ok": False,
                               "errores_total": 1,
                               "exception": f"{type(e).__name__}: {e}",
                               "traceback": traceback.format_exc()})
            failed += 1
    if guardar_baseline:
        try:
            with open(_runner.BASELINE_FILE, "w") as f:
                _json.dump(resultados, f, indent=2, ensure_ascii=False, default=str)
        except Exception:
            pass
    return (_json.dumps({
        "total": len(goldens),
        "ok": len(goldens) - failed,
        "failed": failed,
        "resultados": resultados,
    }, indent=2, ensure_ascii=False, default=str), 200, {"Content-Type": "application/json"})


@petra_panel_bp.get("/_admin/briefing-valentina")
def admin_briefing_valentina():
    """Dispara el briefing diario a Valentina ahora, sin esperar al cron 8am."""
    _check_token()
    try:
        from app.scheduler import _petra_briefing_valentina
        _petra_briefing_valentina()
        return "OK ✅ Briefing enviado a VALENTINA_PHONE.", 200
    except Exception as e:
        import traceback
        return (f"❌ Error: {type(e).__name__}: {e}\n{traceback.format_exc()}",
                500)


@petra_panel_bp.get("/_admin/enviar-plantilla-volvemos")
def admin_enviar_plantilla_volvemos():
    """INHABILITADO. El envío one-shot ya se hizo el 2026-10-28.
    La URL quedó expuesta a prefetch/preview del navegador y disparó
    envíos duplicados; se bloquea para que no vuelva a pasar.
    """
    return ("Endpoint inhabilitado. El envío del template volvemos ya se "
            "ejecutó el 28/10/2026. Para una nueva campaña, hablar con "
            "ingeniería y crear una ruta nueva con lock de concurrencia.",
            410, {"Content-Type": "text/plain; charset=utf-8"})

    # ------------------------------------------------------------------
    # Código original preservado abajo (muerto) por si hace falta
    # resucitarlo manualmente. NO se ejecuta: el return de arriba corta.
    # ------------------------------------------------------------------
    _check_token()
    from app import petra_wa as _pwa_tpl
    IDS = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 19, 20,
           21, 22, 23, 24, 26, 27, 29, 30]
    dry = (request.args.get("dry") or "").lower() in ("1", "true", "yes")
    # ?limit=N → corta después de N envíos.  ?offset=N → salta los primeros N.
    # Útil para mandar primero sólo al que querés probar (limit=1) y después
    # al resto (offset=1).
    def _int_arg(name, default=0):
        v = (request.args.get(name) or "").strip()
        try:
            return int(v) if v else default
        except ValueError:
            return default
    limit = _int_arg("limit", 0)
    offset = _int_arg("offset", 0)
    enviados = []
    fallos = []
    try:
        with _db._conexion_directa(timeout=15) as conn:
            rows = conn.execute(
                "SELECT id, telefono, nombre FROM petra_contactos "
                "WHERE id = ANY(%s) ORDER BY id",
                (IDS,),
            ).fetchall()
    except Exception as e:
        return f"❌ Error leyendo contactos: {type(e).__name__}: {e}", 500

    # Aplicar offset + limit
    if offset:
        rows = rows[offset:]
    if limit:
        rows = rows[:limit]

    for cid, tel, nombre in rows:
        if dry:
            enviados.append(f"[DRY] #{cid} {nombre or '(sin nombre)'} → {tel}")
            continue
        try:
            r = _pwa_tpl.send_template(
                to=tel,
                template_name="petra_volvemos_v2",
                language="es_CO",
                components=[{
                    "type": "header",
                    "parameters": [{
                        "type": "image",
                        "image": {
                            "link": "https://petra-inmuebles.onrender.com/static/petra_volvemos.png",
                        },
                    }],
                }],
            )
            if r.get("ok"):
                nota = ""
                if r.get("stub"):
                    nota = " [STUB — sin credenciales]"
                elif r.get("whitelist_skip"):
                    nota = " [SKIP whitelist]"
                elif r.get("simulated"):
                    nota = " [SIM]"
                enviados.append(
                    f"#{cid} {nombre or '(sin nombre)'} → {tel}{nota}"
                )
            else:
                fallos.append(
                    f"#{cid} {nombre or '(sin nombre)'} → {tel} "
                    f"ERROR {r.get('status')}: {str(r.get('body', ''))[:200]}"
                )
        except Exception as e:
            fallos.append(
                f"#{cid} {nombre or '(sin nombre)'} → {tel} "
                f"EXCEPTION: {type(e).__name__}: {e}"
            )

    out = []
    out.append(f"Enviados ({len(enviados)}):")
    out.extend(enviados)
    out.append("")
    out.append(f"Fallos ({len(fallos)}):")
    out.extend(fallos)
    if dry:
        out.append("")
        out.append("(dry-run: no se envió nada. Pasá ?dry=0 para enviar.)")
    return "\n".join(out), 200, {"Content-Type": "text/plain; charset=utf-8"}


@petra_panel_bp.post("/_admin/enviar-pregunta-reactivacion")
def admin_enviar_pregunta_reactivacion():
    """Envía el template `petra_pregunta_dos_opciones` a las 10 agentes que
    leyeron pero no respondieron el template `petra_volvemos_v2` del 28/10.

    POST only (evita prefetch del navegador que nos duplicó envíos la vez
    anterior). Protección adicional: NO re-envía a quienes ya tengan un
    mensaje OUT de este mismo template en últimos 7 días.

    Params:
      ?dry=1        → preview sin enviar
      ?cids=6,14    → sólo a esos CIDs (default: los 10 pre-aprobados)
      ?limit=N      → corta después de N envíos (para probar con 1)
      ?offset=N     → salta los primeros N
    """
    _check_token()
    from app import petra_wa as _pwa_tpl
    import time as _time

    TEMPLATE = "petra_pregunta_dos_opciones"
    # Las 10 agentes que leyeron pero no respondieron el petra_volvemos_v2
    # (query ejecutada el 28/10/2026 — ver conversación con Fabricio).
    IDS_DEFAULT = [6, 14, 8, 26, 20, 23, 21, 3, 30, 15]

    dry = (request.args.get("dry") or "").lower() in ("1", "true", "yes")

    def _int_arg(name, default=0):
        v = (request.args.get(name) or "").strip()
        try:
            return int(v) if v else default
        except ValueError:
            return default
    limit = _int_arg("limit", 0)
    offset = _int_arg("offset", 0)

    cids_param = (request.args.get("cids") or "").strip()
    if cids_param:
        try:
            ids = [int(x) for x in cids_param.split(",") if x.strip()]
        except ValueError:
            return ("Param cids inválido. Formato: cids=1,2,3",
                    400, {"Content-Type": "text/plain; charset=utf-8"})
    else:
        ids = list(IDS_DEFAULT)

    enviados, fallos, skipped = [], [], []
    try:
        with _db._conexion_directa(timeout=15) as conn:
            rows = conn.execute(
                "SELECT id, telefono, nombre FROM petra_contactos "
                "WHERE id = ANY(%s) ORDER BY id",
                (ids,),
            ).fetchall()
            # Protección anti-duplicado: cualquier OUT template a este
            # mismo telefono en los últimos 7 días cuyo payload mencione
            # el nombre del template.
            ya_enviados = conn.execute(
                "SELECT DISTINCT telefono FROM mensajes "
                "WHERE direccion='out' AND tipo='template' "
                "AND fecha > NOW() - INTERVAL '7 days' "
                "AND payload::text LIKE %s",
                (f"%{TEMPLATE}%",),
            ).fetchall()
            ya_set = {r[0] for r in ya_enviados}
    except Exception as e:
        return (f"❌ Error leyendo contactos: {type(e).__name__}: {e}",
                500, {"Content-Type": "text/plain; charset=utf-8"})

    if offset:
        rows = rows[offset:]
    if limit:
        rows = rows[:limit]

    for cid, tel, nombre in rows:
        primer_nombre = ((nombre or "").split() or ["amiga"])[0]
        if tel in ya_set:
            skipped.append(
                f"#{cid} {nombre or '(sin nombre)'} → {tel} [YA ENVIADO en últimos 7d]"
            )
            continue
        if dry:
            enviados.append(
                f"[DRY] #{cid} {nombre or '(sin nombre)'} → {tel} · {{1}}={primer_nombre}"
            )
            continue
        try:
            r = _pwa_tpl.send_template(
                to=tel,
                template_name=TEMPLATE,
                language="es_CO",
                components=[{
                    "type": "body",
                    "parameters": [{"type": "text", "text": primer_nombre}],
                }],
            )
            if r.get("ok"):
                nota = ""
                if r.get("stub"):
                    nota = " [STUB]"
                elif r.get("whitelist_skip"):
                    nota = " [SKIP whitelist]"
                enviados.append(
                    f"#{cid} {nombre or '(sin nombre)'} → {tel}{nota}"
                )
                # Rate-limit entre envíos reales para no fundir el número.
                _time.sleep(2)
            else:
                fallos.append(
                    f"#{cid} {nombre or '(sin nombre)'} → {tel} "
                    f"ERROR {r.get('status')}: {str(r.get('body', ''))[:200]}"
                )
        except Exception as e:
            fallos.append(
                f"#{cid} {nombre or '(sin nombre)'} → {tel} "
                f"EXCEPTION: {type(e).__name__}: {e}"
            )

    out = []
    out.append(f"Template: {TEMPLATE}")
    out.append(f"Total CIDs considerados: {len(ids)}")
    out.append("")
    out.append(f"✅ Enviados ({len(enviados)}):")
    out.extend(enviados)
    if skipped:
        out.append("")
        out.append(f"⏭  Skipped — ya enviados en 7d ({len(skipped)}):")
        out.extend(skipped)
    if fallos:
        out.append("")
        out.append(f"❌ Fallos ({len(fallos)}):")
        out.extend(fallos)
    if dry:
        out.append("")
        out.append("(dry-run: no se envió nada. POST sin ?dry para enviar.)")
    return ("\n".join(out), 200, {"Content-Type": "text/plain; charset=utf-8"})


@petra_panel_bp.get("/_admin/nudges-run")
def admin_nudges_run():
    """Dispara la evaluación de todos los nudges manualmente, sin esperar
    al cron. Usa el panel token. Devuelve resumen de reglas evaluadas +
    nudges enviados.
    NO borra NI modifica agentes — solo envía mensajes de nudge a las que
    cumplan la condición de tiempo + estado.

    Antes de evaluar, carga las reglas default si la tabla está vacía
    (idempotente). Esto garantiza que funcione aunque nadie haya ido
    manualmente al panel a sembrarlas."""
    _check_token()
    from app.petra_agents import nudges as _n
    try:
        ins, upd, omi = _n.seed_reglas(forzar=False)
        reglas, enviados = _n.evaluar_y_enviar()
        return (f"OK ✅ Reglas seed: {ins} nuevas, {upd} actualizadas, {omi} ya existían.\n"
                f"Nudges evaluados: {reglas} reglas activas → "
                f"{enviados} mensajes enviados a agentes paradas.\n"
                f"Revisá /panel/petra/tool-calls o los logs de Render "
                f"para ver el detalle."), 200
    except Exception as e:
        import traceback
        return f"❌ Error: {type(e).__name__}: {e}\n{traceback.format_exc()}", 500


@petra_panel_bp.post("/_admin/recordatorios-auto-run")
def admin_recordatorios_auto_run():
    """Dispara manualmente la pasada de recordatorios automáticos del paso
    actual (equivalente al 📣 Recordar del panel para cada agente atascada
    >72h sin OUT reciente). El cron corre cada 4h; este endpoint es para
    forzar una pasada ad-hoc. POST only, respeta la ventana horaria por
    default; pasá ?forzar=1 para ignorarla."""
    _check_token()
    from app.petra_agents import nudges as _n
    forzar = (request.args.get("forzar") or "").lower() in ("1", "true", "yes")
    try:
        candidatas, enviados = _n.evaluar_recordatorios_automaticos(
            forzar_horario=forzar)
        return (f"OK ✅ Recordatorios automáticos: {candidatas} candidatas "
                f"evaluadas → {enviados} enviados.\n"
                f"(Las no-enviadas están en cooldown 72h, o el bot ya les habló "
                f"en últimas 48h, o estamos fuera de horario laboral — "
                f"usá ?forzar=1 para ignorar el horario.)"), 200
    except Exception as e:
        import traceback
        return (f"❌ Error: {type(e).__name__}: {e}\n{traceback.format_exc()}",
                500)


@petra_panel_bp.route("/_admin/sembrar-propietarios-dummy", methods=["POST", "GET"])
def admin_sembrar_propietarios_dummy():
    """Setea un teléfono dummy en inmuebles_visitante.propietario_telefono
    para los inmuebles activos que no tienen uno. Útil para probar el flow
    end-to-end del wa.me del propietario sin tener propietarios reales.

    Query params:
      telefono (requerido): E164 sin + (ej. 573227057375).
      solo_vacios (opcional, default=1): si 1, solo pisa los inmuebles sin
        propietario_telefono. Si 0, pisa TODOS los activos.
    """
    _check_token()
    tel = (request.args.get("telefono") or "").strip().lstrip("+")
    if not tel.isdigit() or len(tel) < 10:
        return "❌ Falta ?telefono=<E164 sin +>", 400
    solo_vacios = (request.args.get("solo_vacios") or "1").lower() in ("1", "true", "yes")

    try:
        with _db._conexion_directa(timeout=10) as conn:
            if solo_vacios:
                cur = conn.execute(
                    "UPDATE inmuebles_visitante SET propietario_telefono=%s "
                    "WHERE activo=TRUE AND (propietario_telefono IS NULL "
                    "   OR propietario_telefono='') "
                    "RETURNING id",
                    (tel,),
                )
            else:
                cur = conn.execute(
                    "UPDATE inmuebles_visitante SET propietario_telefono=%s "
                    "WHERE activo=TRUE RETURNING id",
                    (tel,),
                )
            filas = cur.fetchall()
            n = len(filas)
        return (f"OK ✅ {n} inmueble(s) actualizados con "
                f"propietario_telefono={tel} "
                f"(solo_vacios={solo_vacios}).\n"), 200
    except Exception as e:
        import traceback
        return (f"❌ Error: {type(e).__name__}: {e}\n{traceback.format_exc()}",
                500)


@petra_panel_bp.get("/_admin/enviar-nudge")
def admin_enviar_nudge_manual():
    """Envía UN nudge específico a UN teléfono, sin importar hace cuánto
    está en ese estado ni si ya lo recibió antes. Útil para probar que
    el envío funciona desde una agente puntual.

    Query params:
      - tel: teléfono (ej. 573227057375)
      - clave: clave del nudge (ej. kit_enviado_72h). Si no se pasa, usa
               la clave default del estado actual de la agente.
    NO toca el flag nudges_enviados — es puramente para test."""
    _check_token()
    tel = (request.args.get("tel") or "").strip().lstrip("+").replace(" ", "")
    clave_pedida = (request.args.get("clave") or "").strip()
    if not tel or not tel.isdigit():
        return "Falta ?tel=<telefono> (solo dígitos)", 400
    from app.petra_agents import nudges as _n
    from app.petra_agents.textos import t as _t
    from app import petra_wa, db as _db2
    try:
        with _db2._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                "SELECT c.id, c.nombre, r.metadata->>'estado_agente' "
                "FROM petra_contactos c "
                "JOIN petra_contacto_roles r ON r.contacto_id=c.id "
                "AND r.rol='agente_petra' "
                "WHERE c.telefono=%s", (tel,)
            ).fetchone()
        if not row:
            return f"Sin agente con telefono={tel}", 404
        cid, nombre, estado = row
        # Busco la regla: si no se pidió clave específica, uso la más
        # chica (menor horas) para el estado actual.
        reglas = [r for r in _n.listar_reglas(solo_activas=True)
                  if (clave_pedida and r["clave"] == clave_pedida)
                  or (not clave_pedida and r["estado_origen"] == estado)]
        if not reglas:
            return (f"Sin regla de nudge para estado={estado} / clave={clave_pedida or '(auto)'}. "
                    f"Reglas disponibles: {[r['clave'] for r in _n.listar_reglas(solo_activas=True)]}"), 404
        reglas.sort(key=lambda r: int(r.get("horas_sin_avanzar") or 0))
        regla = reglas[0]
        texto = _t(
            regla["texto_clave"],
            fallback=f"[nudge {regla['clave']} — texto no cargado]",
            nombre=(nombre or "").split()[0] if nombre else "",
        )
        petra_wa.send_text(tel, texto)
        return (f"OK ✅ Nudge '{regla['clave']}' enviado manualmente a tel={tel} "
                f"(estado actual: {estado}). NO se marcó dedup — el próximo "
                f"run del cron también se lo puede volver a mandar."), 200
    except Exception as e:
        import traceback
        return f"❌ Error: {type(e).__name__}: {e}\n{traceback.format_exc()}", 500


@petra_panel_bp.get("/_admin/reset-agente")
def admin_reset_agente_por_tel():
    """Elimina el rol agente_petra + progreso de un contacto por teléfono.
    Usa el panel token como auth (mismo mecanismo que el resto del panel).
    Útil para re-probar flujos desde cero sin tener que navegar la UI."""
    _check_token()
    tel = (request.args.get("tel") or "").strip().lstrip("+").replace(" ", "")
    if not tel or not tel.isdigit():
        return "Falta ?tel=<telefono> (solo dígitos)", 400
    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT id FROM petra_contactos WHERE telefono=%s", (tel,)
        ).fetchone()
        if not row:
            return f"Sin contacto con telefono={tel}", 404
        cid = row[0]
        n_rol = conn.execute(
            "DELETE FROM petra_contacto_roles "
            "WHERE contacto_id=%s AND rol='agente_petra'",
            (cid,),
        ).rowcount
        n_cap = 0
        try:
            n_cap = conn.execute(
                "DELETE FROM petra_capacitacion_progreso WHERE contacto_id=%s",
                (cid,),
            ).rowcount
        except Exception:
            pass
        n_intent = 0
        try:
            n_intent = conn.execute(
                "DELETE FROM petra_intent_store WHERE telefono=%s", (tel,)
            ).rowcount
        except Exception:
            pass
        n_sess = 0
        try:
            n_sess = conn.execute(
                "UPDATE petra_sesiones SET cerrado_at=NOW() "
                "WHERE telefono=%s AND cerrado_at IS NULL", (tel,)
            ).rowcount
        except Exception:
            pass
    return (f"OK ✅ Agente reseteado — tel={tel}, cid={cid}, "
            f"rol_eliminado={n_rol}, cap_eliminada={n_cap}, "
            f"intent_eliminado={n_intent}, sesiones_cerradas={n_sess}. "
            f"Ya podés escribirle desde tu WhatsApp y arranca de cero."), 200


# =============================================================================
# M4.A — Endpoints humano-in-the-loop para el flow determinístico del kit.
# =============================================================================


def _buscar_agente_por_tel(tel):
    """Devuelve (cid, meta) o (None, None). Normaliza el teléfono."""
    import json as _json
    tel = (tel or "").strip().lstrip("+").replace(" ", "")
    if not tel or not tel.isdigit():
        return None, None, None
    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT c.id, r.metadata FROM petra_contactos c "
            "JOIN petra_contacto_roles r ON r.contacto_id=c.id "
            "AND r.rol='agente_petra' WHERE c.telefono=%s",
            (tel,),
        ).fetchone()
    if not row:
        return None, None, tel
    cid = row[0]
    meta = row[1]
    if isinstance(meta, str):
        try:
            meta = _json.loads(meta)
        except Exception:
            meta = {}
    meta = dict(meta or {})
    return cid, meta, tel


@petra_panel_bp.get("/_admin/kit-qr-cargado")
def admin_kit_qr_cargado():
    """Humano confirma: fotos del libro OK + QR cargado en el sistema.
    Transiciona el estado del agente al sub-paso `qr_cargado` y envía
    por WhatsApp el mensaje con el botón ONBAG_QR_CARGADO.

    Uso:
        GET /panel/petra/_admin/kit-qr-cargado?tel=<tel>&token=<PANEL_TOKEN>
    """
    _check_token()
    cid, meta, tel = _buscar_agente_por_tel(request.args.get("tel") or "")
    if cid is None:
        return (f"Sin agente_petra con telefono={tel!r}", 404)
    estado = (meta.get("estado_agente") or "").strip()
    if estado not in ("kit_pendiente", "kit_enviado"):
        return (f"Estado inválido para QR cargado: {estado!r} "
                f"(se esperaba kit_pendiente / kit_enviado)", 400)
    try:
        from app.petra_agents import kit_v3 as _kv3
        with _db._conexion_directa(timeout=10) as conn:
            ok = _kv3.disparar_qr_cargado(conn, cid, tel, meta)
    except Exception as e:
        return f"Error disparando qr-cargado: {e!r}", 500
    if not ok:
        return "No se pudo disparar (estado no válido)", 400
    return (f"OK ✅ kit-qr-cargado disparado — tel={tel}, cid={cid}, "
            f"paso=qr_cargado. Se envió msg al chat con botón "
            f"ONBAG_QR_CARGADO."), 200


@petra_panel_bp.get("/_admin/kit-guia")
def admin_kit_guia():
    """Humano setea el número de guía del envío del kit. Guarda
    `meta.kit_guia`, transiciona a `guia_enviada` y manda al chat el
    mensaje con el número de guía.

    Uso:
        GET /panel/petra/_admin/kit-guia?tel=<tel>&guia=<N>&token=<TK>

    Si guia= viene vacío, se usa el msg "guía te la compartimos cuando
    esté disponible" (igual que el flow interno). Si meta.kit_guia ya
    existía, se sobrescribe con el nuevo valor.
    """
    _check_token()
    cid, meta, tel = _buscar_agente_por_tel(request.args.get("tel") or "")
    if cid is None:
        return (f"Sin agente_petra con telefono={tel!r}", 404)
    guia = (request.args.get("guia") or "").strip()
    estado = (meta.get("estado_agente") or "").strip()
    if estado not in ("kit_pendiente", "kit_enviado"):
        return (f"Estado inválido para kit-guia: {estado!r}", 400)
    try:
        from app.petra_agents import kit_v3 as _kv3
        with _db._conexion_directa(timeout=10) as conn:
            ok = _kv3.disparar_guia(conn, cid, tel, meta, guia)
    except Exception as e:
        return f"Error disparando kit-guia: {e!r}", 500
    if not ok:
        return "No se pudo disparar (estado no válido)", 400
    return (f"OK ✅ kit-guia seteado — tel={tel}, cid={cid}, "
            f"guia={guia!r}, paso=guia_enviada."), 200


# =============================================================================
# Visitante QR — Admin de conjuntos, inmuebles y solicitudes de contacto
# =============================================================================
# Este bloque administra el inventario que consume el flujo WhatsApp
# `app/petra_visitante/*` (tablas `conjuntos_visitante`, `inmuebles_visitante`,
# `visitante_contactos`). Reutiliza `_check_token` y `_rows` ya definidos
# arriba y el mismo patrón de conexión directa que usa el router visitante.


def _fmt_precio_cop(valor):
    """Formato $ 1.950.000 — reutiliza textos.formatear_precio del visitante
    para no divergir del formato que ve el usuario en WhatsApp.
    Devuelve '—' cuando el valor es 0/None (precio no definido)."""
    try:
        if valor is None or int(valor) == 0:
            return "—"
    except (ValueError, TypeError):
        pass
    try:
        from app.petra_visitante.textos import formatear_precio
        return formatear_precio(valor)
    except Exception:
        try:
            n = int(valor)
            if n <= 0:
                return "Sin precio"
            return "$ " + f"{n:,}".replace(",", ".")
        except (TypeError, ValueError):
            return "Sin precio"


@petra_panel_bp.get("/visitante")
def visitante_conjuntos():
    """Index de conjuntos registrados en el flujo visitante QR.
    Trae también KPIs del embudo por conjunto para el dashboard arriba."""
    _check_token()
    token = (request.args.get("token") or "").strip()
    flash = (request.args.get("flash") or "").strip()
    with _db._conexion_directa(timeout=10) as conn:
        conjuntos = _rows(conn,
            """SELECT c.id, c.codigo, c.nombre, c.ciudad, c.creado_en,
                      COALESCE(SUM(CASE WHEN i.activo AND i.operacion='arriendo'
                                        THEN 1 ELSE 0 END), 0) AS arr_activos,
                      COALESCE(SUM(CASE WHEN i.activo AND i.operacion='venta'
                                        THEN 1 ELSE 0 END), 0) AS ven_activos,
                      COALESCE(SUM(CASE WHEN i.activo THEN 1 ELSE 0 END), 0) AS act,
                      COALESCE(SUM(CASE WHEN i.activo AND i.datos_confirmado_at IS NOT NULL
                                        THEN 1 ELSE 0 END), 0) AS n_conf,
                      COALESCE(SUM(CASE WHEN i.activo AND i.bienvenida_at IS NOT NULL
                                        THEN 1 ELSE 0 END), 0) AS n_bv,
                      COALESCE(SUM(CASE WHEN i.activo AND i.ofrecer_petra_at IS NOT NULL
                                        THEN 1 ELSE 0 END), 0) AS n_op,
                      COALESCE(SUM(CASE WHEN i.activo AND i.ofrecer_credito_at IS NOT NULL
                                        THEN 1 ELSE 0 END), 0) AS n_cr,
                      COALESCE(SUM(CASE WHEN i.activo AND i.mandato_at IS NOT NULL
                                        THEN 1 ELSE 0 END), 0) AS n_mn
                 FROM conjuntos_visitante c
                 LEFT JOIN inmuebles_visitante i ON i.conjunto_id = c.id
                GROUP BY c.id
                ORDER BY c.creado_en DESC""")
    return render_template(
        "petra_visitante_conjuntos.html",
        conjuntos=conjuntos, token=token, flash=flash,
    )


@petra_panel_bp.post("/visitante/conjuntos/nuevo")
def visitante_conjunto_nuevo():
    _check_token()
    token = (request.form.get("token") or "").strip()
    codigo = (request.form.get("codigo") or "").strip()
    nombre = (request.form.get("nombre") or "").strip()
    ciudad = (request.form.get("ciudad") or "").strip() or None
    if not codigo or not nombre:
        return redirect(url_for("petra_panel.visitante_conjuntos",
                                token=token, flash="falta_campo"))
    try:
        with _db._conexion_directa(timeout=10) as conn:
            dup = conn.execute(
                "SELECT 1 FROM conjuntos_visitante WHERE codigo=%s",
                (codigo,),
            ).fetchone()
            if dup:
                return redirect(url_for("petra_panel.visitante_conjuntos",
                                        token=token, flash="codigo_dup"))
            conn.execute(
                "INSERT INTO conjuntos_visitante (codigo, nombre, ciudad) "
                "VALUES (%s, %s, %s)",
                (codigo, nombre, ciudad),
            )
            conn.commit()
    except Exception:
        return redirect(url_for("petra_panel.visitante_conjuntos",
                                token=token, flash="error"))
    # Invalidar cache del bot para que el nuevo conjunto se vea al toque.
    try:
        from app.petra_visitante.router import _conjunto_cache_bust
        _conjunto_cache_bust()
    except Exception:
        pass
    return redirect(url_for("petra_panel.visitante_conjuntos",
                            token=token, flash="creado"))


@petra_panel_bp.post("/visitante/conjuntos/<int:cid>/editar")
def visitante_conjunto_editar(cid):
    _check_token()
    token = (request.form.get("token") or "").strip()
    nombre = (request.form.get("nombre") or "").strip()
    ciudad = (request.form.get("ciudad") or "").strip() or None
    if not nombre:
        return redirect(url_for("petra_panel.visitante_conjuntos",
                                token=token, flash="falta_campo"))
    try:
        with _db._conexion_directa(timeout=10) as conn:
            conn.execute(
                "UPDATE conjuntos_visitante SET nombre=%s, ciudad=%s "
                "WHERE id=%s",
                (nombre, ciudad, cid),
            )
            conn.commit()
        # Invalidar cache del bot para reflejar el cambio al toque.
        try:
            from app.petra_visitante.router import _conjunto_cache_bust
            _conjunto_cache_bust()
        except Exception:
            pass
    except Exception:
        return redirect(url_for("petra_panel.visitante_conjuntos",
                                token=token, flash="error"))
    return redirect(url_for("petra_panel.visitante_conjuntos",
                            token=token, flash="editado"))


@petra_panel_bp.get("/visitante/conjuntos/<int:cid>/qr")
def visitante_conjunto_qr(cid):
    """QR generator client-side para pegar en el edificio."""
    _check_token()
    token = (request.args.get("token") or "").strip()
    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT id, codigo, nombre, ciudad FROM conjuntos_visitante "
            "WHERE id=%s", (cid,),
        ).fetchone()
    if not row:
        abort(404)
    conjunto = {"id": row[0], "codigo": row[1], "nombre": row[2],
                "ciudad": row[3] or ""}
    wa_phone = (os.environ.get("WA_PILOT_PHONE") or "").strip()
    # wa.me espera el número sin el signo + (ej. 573001234567).
    wa_phone_clean = wa_phone.lstrip("+").replace(" ", "")
    from urllib.parse import quote as _urlquote
    texto_prefill = (
        f"Estoy en el edificio {conjunto['nombre']} en "
        f"{conjunto['ciudad'] or 'Colombia'}. Cod: {conjunto['codigo']}"
    )
    wa_link = (f"https://wa.me/{wa_phone_clean}?text="
               f"{_urlquote(texto_prefill)}") if wa_phone_clean else ""
    return render_template(
        "petra_visitante_qr.html",
        conjunto=conjunto, token=token,
        wa_phone=wa_phone, wa_link=wa_link,
        texto_prefill=texto_prefill,
    )


@petra_panel_bp.get("/visitante/qrs/imprimir")
def visitante_qrs_imprimir():
    """Grilla imprimible con los QRs de todos los conjuntos activos."""
    _check_token()
    token = (request.args.get("token") or "").strip()
    from urllib.parse import quote as _urlquote
    wa_phone = (os.environ.get("WA_PILOT_PHONE") or "").strip()
    wa_phone_clean = wa_phone.lstrip("+").replace(" ", "")
    with _db._conexion_directa(timeout=10) as conn:
        filas = _rows(conn,
            "SELECT id, codigo, nombre, ciudad FROM conjuntos_visitante "
            "ORDER BY codigo ASC")
    conjuntos = []
    for r in filas:
        nombre = r["nombre"]
        ciudad = r["ciudad"] or "Colombia"
        texto = (
            f"Estoy en el edificio {nombre} en {ciudad}. Cod: {r['codigo']}"
        )
        wa_link = (f"https://wa.me/{wa_phone_clean}?text={_urlquote(texto)}"
                   if wa_phone_clean else "")
        conjuntos.append({
            "id": r["id"], "codigo": r["codigo"], "nombre": nombre,
            "ciudad": r["ciudad"] or "", "wa_link": wa_link,
        })
    return render_template(
        "petra_visitante_qrs_imprimir.html",
        conjuntos=conjuntos, token=token, wa_phone=wa_phone,
    )


@petra_panel_bp.get("/visitante/conjuntos/<int:cid>/inmuebles")
def visitante_inmuebles(cid):
    """Lista + alta de inmuebles para un conjunto."""
    _check_token()
    token = (request.args.get("token") or "").strip()
    flash = (request.args.get("flash") or "").strip()
    op_filtro = (request.args.get("op") or "todos").strip()
    estado_filtro = (request.args.get("estado") or "todos").strip()

    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT id, codigo, nombre, ciudad FROM conjuntos_visitante "
            "WHERE id=%s", (cid,),
        ).fetchone()
        if not row:
            abort(404)
        conjunto = {"id": row[0], "codigo": row[1],
                    "nombre": row[2], "ciudad": row[3] or ""}

        where = ["conjunto_id=%s"]
        params = [cid]
        if op_filtro in ("arriendo", "venta"):
            where.append("operacion=%s")
            params.append(op_filtro)
        if estado_filtro == "activos":
            where.append("activo=TRUE")
        elif estado_filtro == "inactivos":
            where.append("activo=FALSE")
        sql = (
            "SELECT id, tipo, nro, operacion, habitaciones, banos, m2, precio, "
            "       fuente, activo, creado_en, propietario_telefono, "
            "       gestionado_por_petra, propietario_telefono_original, "
            "       datos_confirmado_at, bienvenida_at, ofrecer_petra_at, "
            "       ofrecer_credito_at, mandato_at, es_inmobiliaria "
            "  FROM inmuebles_visitante "
            " WHERE " + " AND ".join(where) +
            " ORDER BY activo DESC, creado_en DESC"
        )
        inmuebles = _rows(conn, sql, tuple(params))

    # Enriquecer cada inmueble con etapa + wa.me URLs listas para los botones.
    for i in inmuebles:
        i["precio_fmt"] = _fmt_precio_cop(i.get("precio"))
        i["codigo"] = f"P{i['id']:04d}"
        i["conjunto_nombre"] = conjunto["nombre"]
        i["etapa"] = _etapa_inmueble(i)
        cel = _normalizar_celular_co(i.get("propietario_telefono"))
        i["celular_e164"] = cel
        i["celular_valido"] = bool(cel)
        # Pre-computar texto + wa_url para cada botón habilitado
        i["wa_confirmar"] = _wa_url(cel, _texto_confirmar_datos(i))
        i["wa_bienvenida"] = _wa_url(cel, _texto_bienvenida(i))
        i["wa_ofrecer_petra"] = _wa_url(cel, _texto_ofrecer_petra(i))
        i["wa_ofrecer_credito"] = _wa_url(cel, _texto_ofrecer_credito(i))
        # Imagen asociada a ofrecer_petra
        op = (i.get("operacion") or "").lower()
        if op == "venta":
            i["img_ofrecer"] = _IMG_ETAPA['ofrecer_petra_venta']
            i["img_ofrecer_nombre"] = "venta.webp"
        else:
            i["img_ofrecer"] = _IMG_ETAPA['ofrecer_petra_arriendo']
            i["img_ofrecer_nombre"] = "arriendo.webp"
        i["img_credito"] = _IMG_ETAPA['ofrecer_credito']

    # Filtro por etapa (query param ?etapa=...). Si está, filtra en memoria.
    etapa_filtro = (request.args.get("etapa") or "").strip()
    if etapa_filtro:
        inmuebles = [i for i in inmuebles if i["etapa"] == etapa_filtro]

    # Contadores por etapa para los chips (sobre el universo activo).
    cont = {k: 0 for k in (
        'pendiente_confirmar', 'confirmado_sin_datos', 'confirmado_completo',
        'post_bienvenida', 'post_petra', 'post_credito', 'mandato',
    )}
    with _db._conexion_directa(timeout=10) as conn:
        all_rows = _rows(conn,
            """SELECT activo, precio, m2,
                      datos_confirmado_at, bienvenida_at,
                      ofrecer_petra_at, ofrecer_credito_at, mandato_at
                 FROM inmuebles_visitante WHERE conjunto_id=%s""", (cid,))
    for r in all_rows:
        e = _etapa_inmueble(r)
        if e in cont:
            cont[e] += 1

    return render_template(
        "petra_visitante_inmuebles.html",
        conjunto=conjunto, inmuebles=inmuebles, token=token, flash=flash,
        op_filtro=op_filtro, estado_filtro=estado_filtro,
        etapa_filtro=etapa_filtro, cont=cont,
    )


def _parse_int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@petra_panel_bp.post("/visitante/conjuntos/<int:cid>/inmuebles/nuevo")
def visitante_inmueble_nuevo(cid):
    _check_token()
    token = (request.form.get("token") or "").strip()
    nro = (request.form.get("nro") or "").strip()
    operacion = (request.form.get("operacion") or "").strip()
    tipo = (request.form.get("tipo") or "apartamento").strip().lower()
    hab = _parse_int_or_none(request.form.get("habitaciones"))
    banos = _parse_int_or_none(request.form.get("banos"))
    m2 = _parse_int_or_none(request.form.get("m2"))
    precio = _parse_int_or_none(request.form.get("precio"))
    tel = (request.form.get("propietario_telefono") or "").strip() or None

    tipos_validos = {"apartamento", "casa", "oficina", "parqueadero",
                      "deposito", "habitacion", "local"}
    if tipo not in tipos_validos:
        tipo = "apartamento"

    if operacion not in ("arriendo", "venta"):
        return redirect(url_for("petra_panel.visitante_inmuebles",
                                cid=cid, token=token, flash="falta_campo"))
    if precio is None or precio < 0:
        precio = 0
    if not nro:
        nro = "(sin especificar)"
    for v in (hab, banos, m2):
        if v is not None and v < 0:
            return redirect(url_for("petra_panel.visitante_inmuebles",
                                    cid=cid, token=token,
                                    flash="valores_invalidos"))
    try:
        with _db._conexion_directa(timeout=10) as conn:
            conn.execute(
                "INSERT INTO inmuebles_visitante "
                "(conjunto_id, tipo, nro, operacion, habitaciones, banos, m2, "
                " precio, propietario_telefono, fuente, activo) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'petra', TRUE)",
                (cid, tipo, nro, operacion, hab, banos, m2, precio, tel),
            )
            conn.commit()
    except Exception:
        return redirect(url_for("petra_panel.visitante_inmuebles",
                                cid=cid, token=token, flash="error"))
    return redirect(url_for("petra_panel.visitante_inmuebles",
                            cid=cid, token=token, flash="creado"))


@petra_panel_bp.post("/visitante/inmuebles/<int:iid>/editar")
def visitante_inmueble_editar(iid):
    _check_token()
    token = (request.form.get("token") or "").strip()
    nro = (request.form.get("nro") or "").strip()
    operacion = (request.form.get("operacion") or "").strip()
    hab = _parse_int_or_none(request.form.get("habitaciones"))
    banos = _parse_int_or_none(request.form.get("banos"))
    m2 = _parse_int_or_none(request.form.get("m2"))
    precio = _parse_int_or_none(request.form.get("precio"))
    tel = (request.form.get("propietario_telefono") or "").strip() or None

    if operacion not in ("arriendo", "venta"):
        return redirect(request.referrer or url_for(
            "petra_panel.visitante_conjuntos", token=token))
    if not nro:
        nro = "(sin especificar)"
    if precio is None or precio < 0:
        precio = 0
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                "SELECT conjunto_id FROM inmuebles_visitante WHERE id=%s",
                (iid,),
            ).fetchone()
            if not row:
                abort(404)
            cid = row[0]
            conn.execute(
                "UPDATE inmuebles_visitante SET nro=%s, operacion=%s, "
                "habitaciones=%s, banos=%s, m2=%s, precio=%s, "
                "propietario_telefono=%s WHERE id=%s",
                (nro, operacion, hab, banos, m2, precio, tel, iid),
            )
            conn.commit()
    except Exception:
        return redirect(request.referrer or url_for(
            "petra_panel.visitante_conjuntos", token=token))
    return redirect(url_for("petra_panel.visitante_inmuebles",
                            cid=cid, token=token, flash="editado"))


@petra_panel_bp.post("/visitante/inmuebles/<int:iid>/eliminar")
def visitante_inmueble_eliminar(iid):
    """Elimina DEFINITIVAMENTE un inmueble (DELETE, no toggle).
    Vs 'Desactivar' (inactivo pero vive en la base), esto borra la fila
    y no se puede deshacer. Doble confirm en el frontend."""
    _check_token()
    token = (request.form.get("token") or "").strip()
    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT conjunto_id FROM inmuebles_visitante WHERE id=%s",
            (iid,)).fetchone()
        if not row:
            abort(404)
        cid = row[0]
        conn.execute("DELETE FROM inmuebles_visitante WHERE id=%s", (iid,))
        conn.commit()
    return redirect(url_for("petra_panel.visitante_inmuebles",
                            cid=cid, token=token, flash="eliminado"))


@petra_panel_bp.post("/visitante/inmuebles/<int:iid>/toggle")
def visitante_inmueble_toggle(iid):
    _check_token()
    token = (request.form.get("token") or "").strip()
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                "SELECT conjunto_id, activo FROM inmuebles_visitante "
                "WHERE id=%s", (iid,),
            ).fetchone()
            if not row:
                abort(404)
            cid = row[0]
            conn.execute(
                "UPDATE inmuebles_visitante SET activo = NOT activo "
                "WHERE id=%s", (iid,),
            )
            conn.commit()
    except Exception:
        return redirect(request.referrer or url_for(
            "petra_panel.visitante_conjuntos", token=token))
    return redirect(url_for("petra_panel.visitante_inmuebles",
                            cid=cid, token=token, flash="toggled"))


@petra_panel_bp.post("/visitante/inmuebles/<int:iid>/toggle-gestion")
def visitante_inmueble_toggle_gestion(iid):
    """Toggle del flag gestionado_por_petra.
    - FALSE → TRUE: guarda propietario_telefono actual en
      propietario_telefono_original y lo reemplaza por PETRA_OPERADOR_PHONE.
    - TRUE → FALSE: restaura propietario_telefono desde el original y
      limpia la columna de backup.
    """
    _check_token()
    token = (request.form.get("token") or "").strip()
    operador = (os.environ.get("PETRA_OPERADOR_PHONE") or "").strip()
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                "SELECT conjunto_id, gestionado_por_petra, propietario_telefono, "
                "       propietario_telefono_original "
                "FROM inmuebles_visitante WHERE id=%s", (iid,),
            ).fetchone()
            if not row:
                abort(404)
            cid_conj, gestionado, prop_actual, prop_orig = row
            if not gestionado:
                # FALSE → TRUE
                if not operador:
                    return redirect(url_for(
                        "petra_panel.visitante_inmuebles",
                        cid=cid_conj, token=token,
                        flash="falta_operador_env"))
                conn.execute(
                    "UPDATE inmuebles_visitante "
                    "   SET gestionado_por_petra = TRUE, "
                    "       propietario_telefono_original = COALESCE(propietario_telefono_original, propietario_telefono), "
                    "       propietario_telefono = %s "
                    " WHERE id = %s",
                    (operador, iid),
                )
            else:
                # TRUE → FALSE: restaurar el original si lo hay
                conn.execute(
                    "UPDATE inmuebles_visitante "
                    "   SET gestionado_por_petra = FALSE, "
                    "       propietario_telefono = COALESCE(propietario_telefono_original, propietario_telefono), "
                    "       propietario_telefono_original = NULL "
                    " WHERE id = %s",
                    (iid,),
                )
            conn.commit()
    except Exception:
        return redirect(request.referrer or url_for(
            "petra_panel.visitante_conjuntos", token=token))
    return redirect(url_for("petra_panel.visitante_inmuebles",
                            cid=cid_conj, token=token, flash="gestion_toggled"))


@petra_panel_bp.post("/visitante/inmuebles/<int:iid>/toggle-inmobiliaria")
def visitante_inmueble_toggle_inmobiliaria(iid):
    """Toggle del flag es_inmobiliaria. Solo marca/desmarca; no toca
    teléfonos. Para taguear comercialmente."""
    _check_token()
    token = (request.form.get("token") or "").strip()
    cid_conj = None
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                "SELECT conjunto_id FROM inmuebles_visitante WHERE id=%s",
                (iid,),
            ).fetchone()
            if not row:
                abort(404)
            cid_conj = row[0]
            conn.execute(
                "UPDATE inmuebles_visitante "
                "   SET es_inmobiliaria = NOT es_inmobiliaria "
                " WHERE id=%s", (iid,),
            )
            conn.commit()
    except Exception:
        return redirect(request.referrer or url_for(
            "petra_panel.visitante_conjuntos", token=token))
    return redirect(url_for("petra_panel.visitante_inmuebles",
                            cid=cid_conj, token=token, flash="inmob_toggled"))


@petra_panel_bp.get("/visitante/interesados")
def visitante_interesados_vista():
    """Dashboard de interesados que llegaron vía el QR, usaron el Flow
    multi-select y pidieron contacto por N inmuebles a la vez.

    Alimentado por el flujo visitante cuando llega el nfm_reply del Flow
    (ver PR #65 — pendiente). Mientras tanto, la tabla puede estar vacía
    sin ser un bug."""
    _check_token()
    token = (request.args.get("token") or "").strip()
    conj_filtro = (request.args.get("conjunto") or "").strip()
    estado_filtro = (request.args.get("estado") or "").strip()
    where = []
    params = []
    if conj_filtro.isdigit():
        where.append("i.conjunto_id = %s")
        params.append(int(conj_filtro))
    if estado_filtro in ("enviado", "respondido", "cerrado"):
        where.append("i.estado = %s")
        params.append(estado_filtro)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    with _db._conexion_directa(timeout=10) as conn:
        rows = _rows(conn,
            "SELECT i.id, i.creado_en, i.interesado_telefono, "
            "       i.interesado_nombre, i.interesado_username, "
            "       i.interesado_wa_id, i.conjunto_id, "
            "       cv.codigo AS conjunto_codigo, cv.nombre AS conjunto_nombre, "
            "       i.inmueble_ids, i.propietarios_notificados, "
            "       i.estado, i.actualizado_en, "
            "       (SELECT array_agg(iv.nro ORDER BY iv.nro) "
            "          FROM inmuebles_visitante iv "
            "         WHERE iv.id = ANY(i.inmueble_ids)) AS inmueble_nros, "
            "       (SELECT bool_or(iv.gestionado_por_petra) "
            "          FROM inmuebles_visitante iv "
            "         WHERE iv.id = ANY(i.inmueble_ids)) AS hay_petra_gestionados "
            "  FROM petra_interesados_qr i "
            "  LEFT JOIN conjuntos_visitante cv ON cv.id = i.conjunto_id "
            + where_sql +
            " ORDER BY i.creado_en DESC LIMIT 500",
            tuple(params))
        conjuntos = _rows(conn,
            "SELECT id, codigo, nombre FROM conjuntos_visitante "
            " WHERE id IN (SELECT DISTINCT conjunto_id FROM petra_interesados_qr "
            "              WHERE conjunto_id IS NOT NULL) "
            " ORDER BY codigo")
    return render_template(
        "petra_visitante_interesados.html",
        interesados=rows, conjuntos=conjuntos, token=token,
        conj_filtro=conj_filtro, estado_filtro=estado_filtro,
    )


@petra_panel_bp.get("/visitante/contactos")
def visitante_contactos_vista():
    """Solicitudes de contacto generadas desde el flujo visitante."""
    _check_token()
    token = (request.args.get("token") or "").strip()
    with _db._conexion_directa(timeout=10) as conn:
        rows = _rows(conn,
            """SELECT vc.id, vc.creado_en, vc.telefono, vc.contacto_nombre,
                      cj.id AS conjunto_id, cj.codigo AS conjunto_codigo,
                      cj.nombre AS conjunto_nombre,
                      im.id AS inmueble_id, im.nro AS inmueble_nro,
                      im.operacion, im.precio
                 FROM visitante_contactos vc
                 JOIN conjuntos_visitante cj ON cj.id = vc.conjunto_id
                 JOIN inmuebles_visitante im ON im.id = vc.inmueble_id
                ORDER BY vc.creado_en DESC
                LIMIT 500""")
    for r in rows:
        r["precio_fmt"] = _fmt_precio_cop(r.get("precio"))
    return render_template(
        "petra_visitante_contactos.html",
        contactos=rows, token=token,
    )


@petra_panel_bp.get("/visitante/usuarios")
def visitante_usuarios_vista():
    """Lista todos los usuarios que han interactuado con el flujo visitante.

    Por cada teléfono muestra:
      - número
      - nombre (si lo capturamos)
      - última vez conectado (updated_at de petra_contactos)
      - anunciante = publicó al menos 1 inmueble activo
      - buscando = pidió contacto por al menos 1 inmueble
      - edificio sticky (si lo tiene en meta.visitante_conjunto_id)
    """
    _check_token()
    token = (request.args.get("token") or "").strip()
    filtro = (request.args.get("rol") or "").strip()  # all|anun|busc
    with _db._conexion_directa(timeout=15) as conn:
        rows = _rows(conn,
            """
            WITH anunciantes AS (
              SELECT DISTINCT propietario_telefono AS tel, COUNT(*) AS n_inm
                FROM inmuebles_visitante
               WHERE activo = TRUE
                 AND propietario_telefono IS NOT NULL
                 AND propietario_telefono <> ''
               GROUP BY propietario_telefono
            ),
            buscando AS (
              SELECT DISTINCT telefono AS tel, COUNT(*) AS n_cont
                FROM visitante_contactos
               WHERE telefono IS NOT NULL AND telefono <> ''
               GROUP BY telefono
            ),
            interesados_qr AS (
              SELECT DISTINCT interesado_telefono AS tel, COUNT(*) AS n_int
                FROM petra_interesados_qr
               WHERE interesado_telefono IS NOT NULL
                 AND interesado_telefono <> ''
               GROUP BY interesado_telefono
            ),
            telefonos AS (
              SELECT telefono FROM petra_contactos
                WHERE meta ? 'visitante_estado'
                   OR meta ? 'visitante_conjunto_id'
                   OR meta ? 'flow_publicar_conjunto_id'
              UNION
              -- Todas las agentes Petra (tengan o no interacción visitante).
              SELECT pc.telefono
                FROM petra_contactos pc
                JOIN petra_contacto_roles r ON r.contacto_id = pc.id
               WHERE r.rol = 'agente_petra'
              UNION
              SELECT tel AS telefono FROM anunciantes
              UNION
              SELECT tel AS telefono FROM buscando
              UNION
              SELECT tel AS telefono FROM interesados_qr
            )
            SELECT t.telefono,
                   pc.nombre,
                   pc.username,
                   pc.updated_at AS ultima_conexion,
                   cv.ciudad AS ciudad,
                   COALESCE(a.n_inm, 0) AS n_inmuebles_publicados,
                   COALESCE(b.n_cont, 0) + COALESCE(i.n_int, 0) AS n_contactos_pedidos,
                   EXISTS (
                     SELECT 1 FROM petra_contacto_roles r
                      WHERE r.contacto_id = pc.id
                        AND r.rol = 'agente_petra'
                   ) AS es_agente
              FROM telefonos t
              LEFT JOIN petra_contactos pc ON pc.telefono = t.telefono
              LEFT JOIN anunciantes a ON a.tel = t.telefono
              LEFT JOIN buscando b ON b.tel = t.telefono
              LEFT JOIN interesados_qr i ON i.tel = t.telefono
              LEFT JOIN conjuntos_visitante cv
                ON cv.id = NULLIF(pc.meta->>'visitante_conjunto_id', '')::int
             ORDER BY pc.updated_at DESC NULLS LAST, t.telefono
             LIMIT 1000
            """)
    for r in rows:
        r["es_anunciante"] = (r.get("n_inmuebles_publicados") or 0) > 0
        r["es_buscando"] = (r.get("n_contactos_pedidos") or 0) > 0
    if filtro == "anun":
        rows = [r for r in rows if r["es_anunciante"]]
    elif filtro == "busc":
        rows = [r for r in rows if r["es_buscando"]]
    elif filtro == "agente":
        rows = [r for r in rows if r.get("es_agente")]
    total = len(rows)
    n_anun = sum(1 for r in rows if r["es_anunciante"])
    n_busc = sum(1 for r in rows if r["es_buscando"])
    n_agente = sum(1 for r in rows if r.get("es_agente"))
    return render_template(
        "petra_visitante_usuarios.html",
        usuarios=rows, token=token, filtro=filtro,
        total=total, n_anun=n_anun, n_busc=n_busc, n_agente=n_agente,
    )


@petra_panel_bp.get("/visitante/usuario/<telefono>")
def visitante_usuario_detalle(telefono):
    """Historial completo de lo que ha hecho un usuario del flujo visitante.

    Muestra:
      - datos básicos + roles
      - inmuebles que publicó
      - solicitudes de contacto que hizo (visitante_contactos)
      - interesados QR (multi-select del Flow)
      - estado agente (si lo es)
      - línea de tiempo unificada ordenada por fecha
    """
    _check_token()
    token = (request.args.get("token") or "").strip()
    tel = (telefono or "").strip()
    if not tel:
        return redirect(url_for("petra_panel.visitante_usuarios_vista",
                                 token=token))
    with _db._conexion_directa(timeout=15) as conn:
        _contacto_rows = _rows(conn,
            """SELECT pc.id, pc.telefono, pc.nombre, pc.username, pc.updated_at,
                      pc.created_at,
                      pc.meta->>'visitante_conjunto_id' AS conjunto_id_sticky,
                      pc.meta->>'visitante_estado' AS estado_visitante,
                      cv.nombre AS conjunto_nombre,
                      COALESCE(cv.ciudad, ra.metadata->>'ciudad') AS ciudad,
                      ra.metadata->>'estado_agente' AS estado_agente,
                      COALESCE((ra.metadata->>'videos_vistos')::int, 0) AS videos_vistos,
                      ra.metadata->>'inscrito_at' AS inscrito_at,
                      ra.metadata->>'origen' AS origen_agente,
                      ra.metadata->>'bot_pausado' AS bot_pausado,
                      (ra.id IS NOT NULL) AS es_agente,
                      (SELECT COUNT(*) FROM conjuntos_visitante
                        WHERE creado_por_cid = pc.id) AS n_edificios_agente,
                      (SELECT COUNT(*) FROM inmuebles_visitante iv
                         JOIN conjuntos_visitante cv2 ON cv2.id = iv.conjunto_id
                        WHERE cv2.creado_por_cid = pc.id
                          AND iv.activo = TRUE) AS n_inmuebles_agente
                 FROM petra_contactos pc
                 LEFT JOIN conjuntos_visitante cv
                   ON cv.id = NULLIF(pc.meta->>'visitante_conjunto_id', '')::int
                 LEFT JOIN petra_contacto_roles ra
                   ON ra.contacto_id = pc.id AND ra.rol = 'agente_petra'
                WHERE pc.telefono = %s""",
            (tel,))
        contacto = _contacto_rows[0] if _contacto_rows else None
        if not contacto:
            contacto = {"telefono": tel, "nombre": None,
                        "updated_at": None, "created_at": None,
                        "es_agente": False, "conjunto_nombre": None,
                        "ciudad": None, "estado_visitante": None}

        publicados = _rows(conn,
            """SELECT iv.id, iv.tipo, iv.nro, iv.operacion, iv.precio,
                      iv.m2, iv.admin_valor, iv.admin_incluida,
                      iv.activo, iv.creado_en,
                      cv.nombre AS conjunto_nombre, cv.codigo AS conjunto_codigo
                 FROM inmuebles_visitante iv
                 LEFT JOIN conjuntos_visitante cv ON cv.id = iv.conjunto_id
                WHERE iv.propietario_telefono = %s
                ORDER BY iv.creado_en DESC""",
            (tel,))
        for p in publicados:
            p["precio_fmt"] = _fmt_precio_cop(p.get("precio"))

        contactos_pedidos = _rows(conn,
            """SELECT vc.id, vc.creado_en,
                      cv.nombre AS conjunto_nombre, cv.codigo AS conjunto_codigo,
                      iv.id AS inmueble_id, iv.tipo, iv.nro,
                      iv.operacion, iv.precio
                 FROM visitante_contactos vc
                 LEFT JOIN conjuntos_visitante cv ON cv.id = vc.conjunto_id
                 LEFT JOIN inmuebles_visitante iv ON iv.id = vc.inmueble_id
                WHERE vc.telefono = %s
                ORDER BY vc.creado_en DESC""",
            (tel,))
        for c in contactos_pedidos:
            c["precio_fmt"] = _fmt_precio_cop(c.get("precio"))

        edificios_agente = []
        inmuebles_agente_total = 0
        if contacto.get("es_agente"):
            edificios_agente = _rows(conn,
                """SELECT id, codigo, nombre, ciudad, creado_en,
                          (SELECT COUNT(*) FROM inmuebles_visitante iv
                            WHERE iv.conjunto_id = cv.id AND iv.activo = TRUE)
                          AS n_inmuebles
                     FROM conjuntos_visitante cv
                    WHERE creado_por_cid = %s
                    ORDER BY creado_en DESC""",
                (contacto.get("id"),))
            inmuebles_agente_total = sum(e.get("n_inmuebles") or 0
                                         for e in edificios_agente)

        mensajes = _rows(conn,
            """SELECT direccion, tipo, resumen, fecha,
                      wa_msg_id, estado_entrega, estado_entrega_at,
                      estado_entrega_error
                 FROM mensajes
                WHERE telefono = %s
                ORDER BY fecha DESC
                LIMIT 200""",
            (tel,))
        # Invertimos para render (oldest first dentro del chat).
        mensajes = list(reversed(mensajes))

        interesados = _rows(conn,
            """SELECT id, creado_en, estado, inmueble_ids,
                      conjunto_id,
                      (SELECT cv.nombre FROM conjuntos_visitante cv
                        WHERE cv.id = i.conjunto_id) AS conjunto_nombre
                 FROM petra_interesados_qr i
                WHERE interesado_telefono = %s
                ORDER BY creado_en DESC""",
            (tel,))

        # Timeline unificada: mezcla eventos por fecha descendente.
        timeline = []
        for p in publicados:
            tipo_lbl = (p.get("tipo") or "inmueble").capitalize()
            timeline.append({
                "fecha": p["creado_en"],
                "icono": "🏠",
                "titulo": f"Publicó {tipo_lbl}"
                          + (f" #{p['nro']}" if p.get("nro")
                             and p["nro"] != "(sin especificar)" else ""),
                "detalle": (f"{p.get('conjunto_nombre') or '—'} · "
                            f"{p.get('operacion') or ''} · "
                            f"{p.get('precio_fmt') or ''}"),
                "activo": p.get("activo"),
            })
        for c in contactos_pedidos:
            tipo_lbl = (c.get("tipo") or "inmueble").capitalize()
            timeline.append({
                "fecha": c["creado_en"],
                "icono": "💬",
                "titulo": f"Pidió hablar por {tipo_lbl}"
                          + (f" #{c['nro']}" if c.get("nro")
                             and c["nro"] != "(sin especificar)" else ""),
                "detalle": (f"{c.get('conjunto_nombre') or '—'} · "
                            f"{c.get('operacion') or ''} · "
                            f"{c.get('precio_fmt') or ''}"),
                "activo": True,
            })
        for i in interesados:
            n = len(i.get("inmueble_ids") or [])
            timeline.append({
                "fecha": i["creado_en"],
                "icono": "✨",
                "titulo": f"Pidió contacto por {n} inmuebles (Flow)",
                "detalle": (f"{i.get('conjunto_nombre') or '—'} · "
                            f"estado: {i.get('estado') or 'enviado'}"),
                "activo": True,
            })
        timeline.sort(key=lambda x: x["fecha"], reverse=True)

    return render_template(
        "petra_visitante_usuario_detalle.html",
        contacto=contacto, publicados=publicados,
        contactos_pedidos=contactos_pedidos, interesados=interesados,
        timeline=timeline, token=token,
        edificios_agente=edificios_agente,
        inmuebles_agente_total=inmuebles_agente_total,
        mensajes=mensajes,
    )


_DIAS_SEMANA = [
    ("", "Diario"),
    ("mon", "Lunes"), ("tue", "Martes"), ("wed", "Miércoles"),
    ("thu", "Jueves"), ("fri", "Viernes"), ("sat", "Sábado"), ("sun", "Domingo"),
]


def _dia_label(dow):
    m = {k: v for k, v in _DIAS_SEMANA}
    return m.get(dow or "", "Diario")


_DISPARADORES_CATALOGO = [
    {
        "id": "viernes_disponible",
        "nombre": "¿Sigue disponible? (viernes)",
        "descripcion": ("Cada viernes 9:00 AM Bogotá pregunta a los "
                         "propietarios si su inmueble sigue disponible."),
        "tipo": "cron",
    },
    {
        "id": "buscador_followup",
        "nombre": "Follow-up buscador (5 días)",
        "descripcion": ("Cada día 10:30 AM pregunta a buscadores (5+ días "
                         "sin respuesta) si siguen buscando o ya encontraron."),
        "tipo": "cron",
    },
    {
        "id": "mejora_busqueda",
        "nombre": "Mejorar búsqueda (tras contacto)",
        "descripcion": ("N minutos después de que el buscador toque 'Hablar "
                         "al dueño', le pide zona, presupuesto y m² para "
                         "mejorar la recomendación."),
        "tipo": "delay",
    },
]


@petra_panel_bp.get("/visitante/disparadores")
def visitante_disparadores_vista():
    """Lista de disparadores (cron jobs) con posibilidad de pausar/reanudar."""
    _check_token()
    token = (request.args.get("token") or "").strip()
    # Horarios default por catálogo (sobrescritos por DB si existe).
    defaults = {
        "viernes_disponible": {"hora": 9, "minuto": 0, "dia_semana": "fri"},
        "buscador_followup":  {"hora": 10, "minuto": 30, "dia_semana": None},
        "mejora_busqueda":    {"delay_minutos": 15},
    }
    with _db._conexion_directa(timeout=10) as conn:
        try:
            rows = _rows(conn,
                "SELECT id, pausado, hora, minuto, dia_semana, "
                "       delay_minutos, updated_at "
                "  FROM petra_disparadores")
        except Exception:
            rows = []
    estado_map = {r["id"]: r for r in rows}
    disparadores = []
    for d in _DISPARADORES_CATALOGO:
        prev = estado_map.get(d["id"], {})
        default = defaults.get(d["id"], {})
        item = {
            **d,
            "pausado": bool(prev.get("pausado", False)),
            "updated_at": prev.get("updated_at"),
        }
        if d["tipo"] == "delay":
            delay = (prev.get("delay_minutos")
                     if prev.get("delay_minutos") is not None
                     else default.get("delay_minutos", 15))
            item["delay_minutos"] = int(delay)
        else:
            hora = prev.get("hora") if prev.get("hora") is not None else default.get("hora", 9)
            minuto = prev.get("minuto") if prev.get("minuto") is not None else default.get("minuto", 0)
            dow = prev.get("dia_semana") if "dia_semana" in prev else default.get("dia_semana")
            if hora == 0:
                hora_12, ampm = 12, "AM"
            elif hora < 12:
                hora_12, ampm = hora, "AM"
            elif hora == 12:
                hora_12, ampm = 12, "PM"
            else:
                hora_12, ampm = hora - 12, "PM"
            item["hora"] = hora
            item["hora_12"] = hora_12
            item["ampm"] = ampm
            item["minuto"] = minuto
            item["dia_semana"] = dow
        disparadores.append(item)
    return render_template(
        "petra_disparadores.html",
        disparadores=disparadores, token=token, dias_semana=_DIAS_SEMANA,
    )


@petra_panel_bp.post("/visitante/disparadores/<disp_id>/toggle")
def visitante_disparadores_toggle(disp_id):
    _check_token()
    token = (request.args.get("token") or "").strip()
    valido = next((d for d in _DISPARADORES_CATALOGO if d["id"] == disp_id),
                  None)
    if not valido:
        abort(404)
    pausado = request.form.get("pausado") == "1"
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute(
            "INSERT INTO petra_disparadores (id, nombre, descripcion, pausado) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE "
            "SET pausado = EXCLUDED.pausado, updated_at = NOW()",
            (valido["id"], valido["nombre"], valido["descripcion"], pausado),
        )
    return redirect(url_for("petra_panel.visitante_disparadores_vista",
                             token=token))


@petra_panel_bp.post("/visitante/disparadores/<disp_id>/horario")
def visitante_disparadores_horario(disp_id):
    _check_token()
    token = (request.args.get("token") or "").strip()
    valido = next((d for d in _DISPARADORES_CATALOGO if d["id"] == disp_id),
                  None)
    if not valido:
        abort(404)
    try:
        hora_12 = max(1, min(12, int(request.form.get("hora", "9"))))
        minuto = max(0, min(59, int(request.form.get("minuto", "0"))))
    except (ValueError, TypeError):
        hora_12, minuto = 9, 0
    ampm = (request.form.get("ampm") or "AM").upper()
    # Convierte 12h → 24h.
    if ampm == "PM":
        hora = 12 if hora_12 == 12 else hora_12 + 12
    else:
        hora = 0 if hora_12 == 12 else hora_12
    dow = (request.form.get("dia_semana") or "").strip() or None
    valid_dows = {k for k, _ in _DIAS_SEMANA if k}
    if dow and dow not in valid_dows:
        dow = None
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute(
            "INSERT INTO petra_disparadores (id, nombre, descripcion, "
            "  hora, minuto, dia_semana) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE "
            "SET hora = EXCLUDED.hora, minuto = EXCLUDED.minuto, "
            "    dia_semana = EXCLUDED.dia_semana, updated_at = NOW()",
            (valido["id"], valido["nombre"], valido["descripcion"],
             hora, minuto, dow),
        )
    # Reprograma el job APScheduler en caliente.
    try:
        from app import scheduler as _sch
        _sch.reprogramar_disparador(disp_id)
    except Exception:
        pass
    return redirect(url_for("petra_panel.visitante_disparadores_vista",
                             token=token))


@petra_panel_bp.post("/visitante/disparadores/<disp_id>/disparar")
def visitante_disparadores_disparar(disp_id):
    """Dispara el job inmediatamente sin esperar el horario."""
    _check_token()
    token = (request.args.get("token") or "").strip()
    valido = next((d for d in _DISPARADORES_CATALOGO if d["id"] == disp_id),
                  None)
    if not valido:
        abort(404)
    try:
        from app import scheduler as _sch
        fn_map = {
            "viernes_disponible": _sch._petra_nudge_viernes_disponibles,
            "buscador_followup":  _sch._petra_nudge_buscador_seguimiento,
            "mejora_busqueda":    _sch._petra_nudge_mejora_busqueda,
        }
        fn = fn_map.get(disp_id)
        if fn:
            fn()
    except Exception:
        import logging
        logging.getLogger("petra").exception(
            "[disparadores] disparar manual fallo id=%s", disp_id)
    return redirect(url_for("petra_panel.visitante_disparadores_vista",
                             token=token))


@petra_panel_bp.post("/visitante/disparadores/<disp_id>/delay")
def visitante_disparadores_delay(disp_id):
    _check_token()
    token = (request.args.get("token") or "").strip()
    valido = next((d for d in _DISPARADORES_CATALOGO if d["id"] == disp_id),
                  None)
    if not valido or valido.get("tipo") != "delay":
        abort(404)
    try:
        minutos = max(1, min(1440, int(request.form.get("minutos", "15"))))
    except (ValueError, TypeError):
        minutos = 15
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute(
            "INSERT INTO petra_disparadores (id, nombre, descripcion, "
            "  tipo, delay_minutos) "
            "VALUES (%s, %s, %s, 'delay', %s) "
            "ON CONFLICT (id) DO UPDATE "
            "SET delay_minutos = EXCLUDED.delay_minutos, "
            "    tipo = 'delay', updated_at = NOW()",
            (valido["id"], valido["nombre"], valido["descripcion"], minutos),
        )
    return redirect(url_for("petra_panel.visitante_disparadores_vista",
                             token=token))


@petra_panel_bp.get("/visitante/listado")
def visitante_listado_inmuebles():
    """Listado completo de inmuebles con código tipo P0001."""
    _check_token()
    token = (request.args.get("token") or "").strip()
    filtro = (request.args.get("f") or "").strip()  # all|activo|inactivo|arriendo|venta
    with _db._conexion_directa(timeout=15) as conn:
        rows = _rows(conn,
            """SELECT iv.id, iv.tipo, iv.nro, iv.operacion, iv.precio,
                      iv.m2, iv.admin_valor, iv.admin_incluida, iv.activo,
                      iv.fuente, iv.propietario_telefono, iv.creado_en,
                      cv.codigo AS conjunto_codigo, cv.nombre AS conjunto_nombre,
                      cv.ciudad AS conjunto_ciudad
                 FROM inmuebles_visitante iv
                 LEFT JOIN conjuntos_visitante cv ON cv.id = iv.conjunto_id
                ORDER BY iv.id DESC""")
    for r in rows:
        r["codigo"] = f"P{r['id']:04d}"
        r["precio_fmt"] = _fmt_precio_cop(r.get("precio"))
    if filtro == "activo":
        rows = [r for r in rows if r.get("activo")]
    elif filtro == "inactivo":
        rows = [r for r in rows if not r.get("activo")]
    elif filtro == "arriendo":
        rows = [r for r in rows if r.get("operacion") == "arriendo"]
    elif filtro == "venta":
        rows = [r for r in rows if r.get("operacion") == "venta"]
    stats = {
        "total": len(rows),
        "activos": sum(1 for r in rows if r.get("activo")),
        "arriendos": sum(1 for r in rows if r.get("operacion") == "arriendo"),
        "ventas": sum(1 for r in rows if r.get("operacion") == "venta"),
    }
    return render_template(
        "petra_visitante_listado.html",
        inmuebles=rows, token=token, filtro=filtro, stats=stats,
    )


# =============================================================================
# Embudo de contacto a propietarios (wa.me click-to-chat).
# 5 etapas gated:
#   1 confirmar_datos  → Sebastián
#   2 bienvenida       → Fabricio (texto + video)
#   3 ofrecer_petra    → Fabricio (texto + imagen)
#   4 ofrecer_credito  → Fabricio (texto + imagen)
#   5 mandato          → Fabricio (toggle, no envía)
# =============================================================================

def _urlencode_min(s):
    import urllib.parse
    return urllib.parse.quote(s or "", safe="")


def _normalizar_celular_co(raw):
    """Devuelve '57XXXXXXXXXX' (listo para wa.me). None si no parseable.
    Si vienen 10 dígitos empezando en 3, agrega '57' adelante.
    """
    if not raw:
        return None
    dig = "".join(c for c in str(raw) if c.isdigit())
    if not dig:
        return None
    if dig.startswith("57") and 12 <= len(dig) <= 13:
        return dig[:12]
    if len(dig) == 10 and dig.startswith("3"):
        return "57" + dig
    if 10 <= len(dig) <= 15:
        return dig
    return None


def _tipo_label(inm):
    return (inm.get("tipo") or "inmueble").lower()


# Tipos de inmueble femeninos (para elegir 'la'/'el'). Todo lo demás masculino.
_TIPOS_FEM = {"casa", "oficina", "bodega", "habitacion", "habitación"}


def _articulo(tipo):
    return "la" if (tipo or "").lower() in _TIPOS_FEM else "el"


def _op_label(op):
    op = (op or "").lower()
    if op == "arriendo":
        return "arrendar"
    if op == "venta":
        return "vender"
    return "arrendar o vender"


# --- Textos de cada etapa ----------------------------------------------------

def _texto_confirmar_datos(inm):
    """Mensaje ① — Sebastián sondeando datos. Sin marca, sin mencionar admin."""
    tipo = _tipo_label(inm)
    art = _articulo(tipo)
    conjunto = inm.get("conjunto_nombre") or "el conjunto"
    return (
        f"Hola, buen día.\n"
        f"Quería preguntarte por {art} {tipo} que tenés en {conjunto}. "
        f"¿Me puedes contar de cuántos metros es y qué precio tiene?"
    )


def _texto_bienvenida(inm):
    """Mensaje ② — Fabricio dando bienvenida + video (lo adjunta a mano)."""
    conjunto = inm.get("conjunto_nombre") or "tu conjunto"
    return (
        f"Hola 👋\n"
        f"Soy Fabricio de *Petra*.\n\n"
        f"Te escribo para contarte que {conjunto} acaba de activar Petra "
        f"para ayudar a los propietarios a vender o arrendar más rápido 🏠\n\n"
        f"Mirá este video de 1 min y enterate cómo funciona 👇"
    )


def _texto_ofrecer_petra(inm):
    """Mensaje ③ — Fabricio ofreciendo servicio inmobiliario. Caption de imagen."""
    tipo = _tipo_label(inm)
    conjunto = inm.get("conjunto_nombre") or "tu conjunto"
    op = (inm.get("operacion") or "").lower()
    if op == "venta":
        return (
            f"Hola, soy Fabricio, asesor inmobiliario de *Petra*. "
            f"Vi que quieres vender tu {tipo} en {conjunto}.\n\n"
            f"La mayoría de propietarios tardan meses en el proceso de venta. "
            f"En Petra nos encargamos de todo y podemos hacerlo más rápido.\n\n"
            f"En la imagen te explico cómo.\n\n"
            f"¿Te interesa?"
        )
    # arriendo (o default)
    return (
        f"Hola, soy Fabricio, asesor inmobiliario de *Petra*. "
        f"Vi que querés arrendar tu {tipo} en {conjunto}.\n\n"
        f"Arrendar por tu cuenta toma tiempo y riesgos. Inquilinos que no "
        f"pagan, visitas infinitas, trámites de administración. En Petra "
        f"lo hacemos rápido y seguro.\n\n"
        f"En la imagen te explico cómo.\n\n"
        f"¿Te interesa?"
    )


def _texto_ofrecer_credito(inm):
    """Mensaje ④ — Fabricio ofreciendo crédito. Caption de imagen."""
    return (
        f"Hola, soy Fabricio, asesor inmobiliario de *Petra*.\n\n"
        f"Dale impulso a eso que tienes en mente. ¿Necesitas liquidez? "
        f"En la imagen te muestro cómo te podemos ayudar.\n\n"
        f"¿Te interesa?"
    )


# --- Lookup de wa.me URL por etapa -------------------------------------------

def _wa_url(cel_e164, texto):
    """URL de WhatsApp pre-cargado con el texto, sin página intermedia.
    web.whatsapp.com/send abre WhatsApp Web directo en escritorio y la app
    en mobile (redirect transparente). vs wa.me que pasa por
    api.whatsapp.com y muestra una landing con 'Abrir aplicación'."""
    return (f"https://web.whatsapp.com/send?phone={cel_e164}"
            f"&text={_urlencode_min(texto)}") if cel_e164 else ""


def _etapa_inmueble(inm):
    """Clasifica el inmueble en la etapa actual del embudo.
    Flag embudo_requiere_bienvenida: si está OFF, se salta la etapa
    bienvenida — un inmueble con datos completos pasa directo al estado
    'post_bienvenida' (listo para ofrecer Petra). Reversible desde
    /panel/petra/flags.
    """
    if not inm.get("activo"):
        return 'desactivado'
    if inm.get("mandato_at"):
        return 'mandato'
    if inm.get("ofrecer_credito_at"):
        return 'post_credito'
    if inm.get("ofrecer_petra_at"):
        return 'post_petra'
    if inm.get("bienvenida_at"):
        return 'post_bienvenida'
    if inm.get("datos_confirmado_at"):
        tiene_precio = (inm.get("precio") or 0) > 0
        tiene_m2 = (inm.get("m2") or 0) > 0
        if tiene_precio and tiene_m2:
            # Si el flag está OFF, saltar bienvenida: tratar como listo
            # para ofrecer Petra directamente.
            if not _flag("embudo_requiere_bienvenida", default=True):
                return 'post_bienvenida'
            return 'confirmado_completo'
        return 'confirmado_sin_datos'
    return 'pendiente_confirmar'


# Imagen asociada por etapa (para el thumbnail del botón).
_IMG_ETAPA = {
    'ofrecer_petra_venta': '/static/images/propietarios/venta.webp',
    'ofrecer_petra_arriendo': '/static/images/propietarios/arriendo.webp',
    'ofrecer_credito': '/static/images/propietarios/credito.webp',
}


# -----------------------------------------------------------------------------
# Embudo: endpoint único para avanzar una etapa. El gate real es el PIN por
# rol (PIN_SEBASTIAN para confirmar_datos; PIN_FABRICIO para el resto).
# Token del panel = acceso a la pantalla; PIN = permiso de ejecución.
# -----------------------------------------------------------------------------

_ACCIONES_EMBUDO = {
    # accion         : (rol_requerido, columna_timestamp)
    'confirmar_datos':  ('sebastian', 'datos_confirmado_at'),
    'bienvenida':       ('fabricio',  'bienvenida_at'),
    'ofrecer_petra':    ('fabricio',  'ofrecer_petra_at'),
    'ofrecer_credito':  ('fabricio',  'ofrecer_credito_at'),
    'mandato':          ('fabricio',  'mandato_at'),
}


def _verificar_pin(rol):
    """PIN de 4 dígitos por rol, configurable en env:
      PIN_SEBASTIAN, PIN_FABRICIO
    El PIN viaja en el form POST como 'pin'. Si el env no está configurado,
    no se exige (dev mode). Devuelve True/False.
    """
    env_key = 'PIN_SEBASTIAN' if rol == 'sebastian' else 'PIN_FABRICIO'
    expected = (os.environ.get(env_key) or "").strip()
    if not expected:
        return True  # dev mode: sin PIN exigido
    got = (request.form.get("pin") or "").strip()
    return got == expected


@petra_panel_bp.post("/pin/verificar")
def pin_verificar():
    """AJAX: valida PIN sin ejecutar acción. Usado por el JS para decidir si
    guardar el PIN en sessionStorage antes de enviar el form real.
    Request body: rol=fabricio|sebastian, pin=1234
    Response: 200 {"ok": true} o 401 {"ok": false}
    """
    from flask import jsonify
    rol = (request.form.get("rol") or "").strip()
    pin = (request.form.get("pin") or "").strip()
    env_key = 'PIN_SEBASTIAN' if rol == 'sebastian' else 'PIN_FABRICIO'
    expected = (os.environ.get(env_key) or "").strip()
    if not expected:
        return jsonify({"ok": True, "dev": True})
    if pin == expected:
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 401


@petra_panel_bp.post("/inmueble/<int:iid>/etapa/<accion>")
def inmueble_etapa_avanzar(iid, accion):
    """Marca la etapa como completada (timestamp = NOW()). Idempotente:
    si ya estaba marcada, no la pisa. El PIN es el gate de ejecución."""
    _check_token()
    if accion not in _ACCIONES_EMBUDO:
        abort(400)
    req_role, col = _ACCIONES_EMBUDO[accion]
    token = (request.form.get("token") or "").strip()
    if not _verificar_pin(req_role):
        ref = request.form.get("volver") or request.referrer or url_for(
            "petra_panel.visitante_conjuntos", token=token)
        sep = "&" if "?" in ref else "?"
        return redirect(f"{ref}{sep}flash=pin_malo")
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute(
            f"UPDATE inmuebles_visitante "
            f"   SET {col} = NOW() "
            f" WHERE id = %s AND {col} IS NULL",
            (iid,))
    ref = request.form.get("volver") or request.referrer
    if ref:
        return redirect(ref)
    return redirect(url_for("petra_panel.visitante_conjuntos", token=token))


@petra_panel_bp.post("/inmueble/<int:iid>/etapa/<accion>/deshacer")
def inmueble_etapa_deshacer(iid, accion):
    _check_token()
    if accion not in _ACCIONES_EMBUDO:
        abort(400)
    req_role, col = _ACCIONES_EMBUDO[accion]
    if not _verificar_pin(req_role):
        ref = request.form.get("volver") or request.referrer or "/"
        sep = "&" if "?" in ref else "?"
        return redirect(f"{ref}{sep}flash=pin_malo")
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute(
            f"UPDATE inmuebles_visitante SET {col} = NULL WHERE id = %s",
            (iid,))
    token = (request.form.get("token") or "").strip()
    ref = request.form.get("volver") or request.referrer
    if ref:
        return redirect(ref)
    return redirect(url_for("petra_panel.visitante_conjuntos", token=token))


# =============================================================================
# SALUD — Dashboard de estado tipo githubstatus.com
# =============================================================================

def _check_db():
    import time
    try:
        t0 = time.time()
        with _db._conexion_directa(timeout=3) as conn:
            conn.execute("SELECT 1").fetchone()
        ms = int((time.time() - t0) * 1000)
        return {"nombre": "Base de datos (Postgres)",
                "status": "ok" if ms < 500 else "warn",
                "detalle": f"respondió en {ms} ms",
                "bars": 4 if ms < 150 else (3 if ms < 500 else 2)}
    except Exception as e:
        return {"nombre": "Base de datos (Postgres)",
                "status": "down", "detalle": f"error: {type(e).__name__}",
                "bars": 0}


def _check_whatsapp_api():
    import requests
    tok = (os.environ.get("META_ACCESS_TOKEN") or "").strip()
    pid = (os.environ.get("META_PHONE_NUMBER_ID") or "").strip()
    if not tok or not pid:
        return {"nombre": "WhatsApp API (Meta)",
                "status": "down",
                "detalle": "faltan credenciales en env",
                "bars": 0,
                "diagnostico": {
                    "titulo": "Variables de entorno faltantes",
                    "lineas": [
                        f"• META_ACCESS_TOKEN: {'OK' if tok else 'VACÍO'}",
                        f"• META_PHONE_NUMBER_ID: {'OK' if pid else 'VACÍO'}",
                    ],
                    "accion": ("Configurá las dos variables en Render → "
                               "Environment → Add Environment Variable. "
                               "Las obtenés en Meta Business Manager → "
                               "WhatsApp → API Setup."),
                    "link": "https://dashboard.render.com",
                    "link_label": "Render dashboard",
                }}
    try:
        r = requests.get(
            f"https://graph.facebook.com/v20.0/{pid}",
            params={"fields": "verified_name,quality_rating,code_verification_status"},
            headers={"Authorization": f"Bearer {tok}"},
            timeout=5)
        if r.status_code != 200:
            body = r.text[:300]
            return {"nombre": "WhatsApp API (Meta)",
                    "status": "down",
                    "detalle": f"HTTP {r.status_code}",
                    "bars": 0,
                    "diagnostico": {
                        "titulo": f"Meta devolvió HTTP {r.status_code}",
                        "lineas": [f"• {body}"],
                        "accion": ("Probablemente el token expiró o fue "
                                   "revocado. Regenerá META_ACCESS_TOKEN "
                                   "en Meta Business Manager."),
                        "link": "https://business.facebook.com",
                        "link_label": "Meta Business Manager",
                    }}
        d = r.json()
        name = d.get("verified_name", "?")
        qual = d.get("quality_rating", "?")
        result = {"nombre": "WhatsApp API (Meta)",
                  "status": "ok" if qual.upper() in ("GREEN", "UNKNOWN") else "warn",
                  "detalle": f"{name} · quality: {qual}",
                  "bars": 4 if qual.upper() == "GREEN" else 3}
        if qual.upper() not in ("GREEN", "UNKNOWN"):
            result["diagnostico"] = {
                "titulo": f"Quality rating degradado: {qual}",
                "lineas": [
                    "• Los propietarios bloquean/reportan los mensajes",
                    "• O enviás a gente que no te escribió primero sin template",
                ],
                "accion": ("Bajá el volumen outbound, mejorá los templates, "
                           "y nunca mandes mensajes libres fuera de 24h. "
                           "Meta puede bajarte a amarillo/rojo si sigue."),
                "link": "https://business.facebook.com",
                "link_label": "Meta Business Manager",
            }
        return result
    except Exception as e:
        return {"nombre": "WhatsApp API (Meta)",
                "status": "down", "detalle": f"{type(e).__name__}",
                "bars": 0,
                "diagnostico": {
                    "titulo": "No se pudo contactar a Meta",
                    "lineas": [f"• {type(e).__name__}: {str(e)[:200]}"],
                    "accion": ("Puede ser timeout de red, token revocado, o "
                               "Meta caído. Reintentá en 1 min."),
                }}


def _check_scheduler():
    try:
        from app import scheduler as _sch
        s = getattr(_sch, "_scheduler", None)
        if s is None or not getattr(s, "running", False):
            return {"nombre": "Scheduler (APScheduler)",
                    "status": "down", "detalle": "no corre",
                    "bars": 0,
                    "diagnostico": {
                        "titulo": "APScheduler no está corriendo",
                        "lineas": ["• Todos los disparadores quedan congelados",
                                   "• Normalmente se arranca al boot de la app"],
                        "accion": ("Redeploy en Render para reiniciar la app. "
                                   "Si persiste, revisá los logs al boot "
                                   "— puede haber un job mal configurado."),
                        "link": "https://dashboard.render.com",
                        "link_label": "Render dashboard",
                    }}
        jobs = s.get_jobs()
        n = len(jobs)
        proximos = [j.next_run_time for j in jobs if j.next_run_time]
        prox = min(proximos).strftime("%Y-%m-%d %H:%M") if proximos else "—"
        return {"nombre": "Scheduler (APScheduler)",
                "status": "ok" if n > 0 else "warn",
                "detalle": f"{n} jobs · próximo {prox}",
                "bars": 4 if n > 0 else 2}
    except Exception as e:
        return {"nombre": "Scheduler (APScheduler)",
                "status": "down", "detalle": f"{type(e).__name__}",
                "bars": 0}


def _check_webhook_queue():
    try:
        with _db._conexion_directa(timeout=3) as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM webhook_queue WHERE estado='pending'"
            ).fetchone()[0] or 0
            atrasado = conn.execute(
                "SELECT COUNT(*) FROM webhook_queue "
                "WHERE estado='pending' AND recibido_at < NOW() - INTERVAL '2 minutes'"
            ).fetchone()[0] or 0
            ultimo_error = conn.execute(
                "SELECT ultimo_error FROM webhook_queue "
                "WHERE ultimo_error IS NOT NULL "
                "ORDER BY recibido_at DESC LIMIT 1").fetchone()
        if atrasado > 0:
            return {"nombre": "Cola de webhooks",
                    "status": "warn",
                    "detalle": f"{pending} pendientes · {atrasado} atrasados",
                    "bars": 1 if atrasado > 20 else 2,
                    "diagnostico": {
                        "titulo": "El worker no está drenando",
                        "lineas": ([f"• último error: {ultimo_error[0][:200]}"]
                                   if ultimo_error else
                                   ["• sin errores registrados — probablemente el worker está parado"]),
                        "accion": ("Si el worker está parado, un redeploy en "
                                   "Render lo re-arranca. Si hay errores, "
                                   "revisá los logs del servicio."),
                        "link": "https://dashboard.render.com",
                        "link_label": "Render logs",
                    }}
        return {"nombre": "Cola de webhooks",
                "status": "ok",
                "detalle": f"{pending} en cola · drena bien",
                "bars": 4}
    except Exception as e:
        return {"nombre": "Cola de webhooks",
                "status": "down", "detalle": f"{type(e).__name__}",
                "bars": 0}


def _check_disco():
    import shutil
    try:
        t, u, f = shutil.disk_usage("/")
        pct_libre = int(f * 100 / t)
        gb_libre = f / (1024 ** 3)
        if pct_libre < 10:
            return {"nombre": "Disco",
                    "status": "down" if pct_libre < 5 else "warn",
                    "detalle": f"solo {pct_libre}% libre ({gb_libre:.1f} GB)",
                    "bars": 1 if pct_libre < 5 else 2}
        return {"nombre": "Disco",
                "status": "ok",
                "detalle": f"{pct_libre}% libre ({gb_libre:.1f} GB)",
                "bars": 4}
    except Exception as e:
        return {"nombre": "Disco", "status": "down",
                "detalle": str(e), "bars": 0}


def _check_mensajes_out_24h():
    try:
        with _db._conexion_directa(timeout=3) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM mensajes WHERE direccion='out' "
                "AND fecha > NOW() - INTERVAL '24 hours'").fetchone()[0] or 0
            fallidos = conn.execute(
                "SELECT COUNT(*) FROM mensajes WHERE direccion='out' "
                "AND fecha > NOW() - INTERVAL '24 hours' AND estado_entrega='failed'"
            ).fetchone()[0] or 0
        if total == 0:
            return {"nombre": "Mensajes saliendo (24h)",
                    "status": "warn", "detalle": "0 enviados — flujo quieto",
                    "bars": 2}
        pct_fail = int(fallidos * 100 / total) if total else 0
        status = "ok" if pct_fail < 10 else ("warn" if pct_fail < 50 else "down")
        bars = 4 if pct_fail < 5 else (3 if pct_fail < 10 else (2 if pct_fail < 50 else 1))
        result = {"nombre": "Mensajes saliendo (24h)",
                  "status": status,
                  "detalle": f"{total} enviados · {pct_fail}% fallidos",
                  "bars": bars}
        if status != "ok":
            # Diagnóstico: contar por tipo de error
            with _db._conexion_directa(timeout=3) as conn:
                errores = _rows(conn,
                    """SELECT COALESCE(estado_entrega_error, 'sin detalle') AS err,
                              COUNT(*) AS n
                         FROM mensajes
                        WHERE direccion='out'
                          AND fecha > NOW() - INTERVAL '24 hours'
                          AND estado_entrega='failed'
                        GROUP BY err ORDER BY n DESC LIMIT 5""")
            diag_lineas = [f"• {e['n']}× {e['err'][:100]}" for e in errores]
            # Sugerencia según el error dominante
            dominante = (errores[0]["err"] if errores else "").lower()
            if "re-engagement" in dominante or "re_engagement" in dominante:
                accion = ("Fuera de la ventana de 24h. Meta rechaza mensajes "
                          "libres a propietarios que no te escribieron en las "
                          "últimas 24h. Solución: crear un **template** "
                          "aprobado en Meta Business y usarlo para el primer "
                          "contacto outbound.")
            elif "undeliverable" in dominante:
                accion = ("Número inválido o no registrado en WhatsApp. "
                          "Revisá que los teléfonos tengan '57' adelante y "
                          "que sean celulares reales.")
            elif "rate" in dominante or "limit" in dominante:
                accion = ("Rate-limit de Meta. Esperá o aumentá tu tier en "
                          "Meta Business Manager.")
            else:
                accion = ("Revisá el detalle de error arriba y consultá la "
                          "doc de Meta Cloud API.")
            result["diagnostico"] = {
                "titulo": "Errores dominantes (últimas 24h):",
                "lineas": diag_lineas,
                "accion": accion,
                "link": "https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes",
                "link_label": "Códigos de error Meta",
            }
        return result
    except Exception as e:
        return {"nombre": "Mensajes saliendo (24h)",
                "status": "down", "detalle": str(e), "bars": 0}


def _check_embudo():
    try:
        with _db._conexion_directa(timeout=3) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM inmuebles_visitante WHERE activo=TRUE"
            ).fetchone()[0] or 0
            confirmados = conn.execute(
                "SELECT COUNT(*) FROM inmuebles_visitante WHERE activo=TRUE "
                "AND datos_confirmado_at IS NOT NULL").fetchone()[0] or 0
            mandatos = conn.execute(
                "SELECT COUNT(*) FROM inmuebles_visitante WHERE activo=TRUE "
                "AND mandato_at IS NOT NULL").fetchone()[0] or 0
        return {"nombre": "Embudo propietarios",
                "status": "ok",
                "detalle": f"{total} activos · {confirmados} confirmados · {mandatos} mandatos",
                "bars": 4}
    except Exception as e:
        return {"nombre": "Embudo propietarios",
                "status": "down", "detalle": str(e), "bars": 0}


def _check_deploy_git():
    try:
        import subprocess
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=2).decode().strip()
        ts = subprocess.check_output(
            ["git", "log", "-1", "--format=%cr"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=2).decode().strip()
        return {"nombre": "Deploy actual",
                "status": "ok",
                "detalle": f"commit {sha} · {ts}",
                "bars": 4}
    except Exception as e:
        return {"nombre": "Deploy actual",
                "status": "warn", "detalle": str(e)[:60], "bars": 2}


def _run_checks():
    """Corre todos los checks. Cada uno es isolated — si uno falla no tumba al resto."""
    checks = []
    for fn in [_check_db, _check_whatsapp_api, _check_scheduler,
               _check_webhook_queue, _check_disco, _check_mensajes_out_24h,
               _check_embudo, _check_deploy_git]:
        try:
            checks.append(fn())
        except Exception as e:
            checks.append({"nombre": fn.__name__,
                           "status": "down",
                           "detalle": f"check crasheó: {e}",
                           "bars": 0})
    return checks


def _global_status(checks):
    """Agrega el estado general: OK si todos OK, WARN si algún warn, DOWN si alguno down."""
    if any(c["status"] == "down" for c in checks):
        return "down", "Algo está caído"
    if any(c["status"] == "warn" for c in checks):
        return "warn", "Operativo con advertencias"
    return "ok", "Todo operativo"


@petra_panel_bp.get("/salud")
def salud_dashboard():
    """Dashboard de estado tipo githubstatus.com. Protegido por PANEL_TOKEN."""
    _check_token()
    token = (request.args.get("token") or "").strip()
    checks = _run_checks()
    funnels = _run_funnels()
    estado, mensaje = _global_status(checks)
    from datetime import datetime
    return render_template(
        "petra_salud.html",
        checks=checks, funnels=funnels,
        estado=estado, mensaje=mensaje,
        verificado_en=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        token=token,
    )


@petra_panel_bp.get("/salud.json")
def salud_json():
    """Mismo check pero en JSON para que el frontend haga auto-refresh sin recargar."""
    _check_token()
    from flask import jsonify
    from datetime import datetime
    checks = _run_checks()
    funnels = _run_funnels()
    estado, mensaje = _global_status(checks)
    return jsonify({
        "estado": estado,
        "mensaje": mensaje,
        "verificado_en": datetime.now().strftime("%H:%M:%S"),
        "checks": checks,
        "funnels": funnels,
    })


@petra_panel_bp.post("/salud/smoke/wa")
def salud_smoke_wa():
    """Smoke test: manda un WA de prueba al número de Fabricio."""
    _check_token()
    if not _verificar_pin("fabricio"):
        from flask import jsonify
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    destino = (os.environ.get("PETRA_OPERADOR_PHONE") or "").strip()
    if not destino:
        from flask import jsonify
        return jsonify({"ok": False, "error": "PETRA_OPERADOR_PHONE no configurado"}), 400
    try:
        from app import whatsapp as _wa
        from datetime import datetime
        marca = datetime.now().strftime("%H:%M:%S")
        resp = _wa.send_text(
            destino,
            f"🧪 Smoke test de Petra — {marca}\n\nSi ves este mensaje, la salida WA funciona end-to-end.")
        from flask import jsonify
        wa_id = (resp or {}).get("messages", [{}])[0].get("id", "?")
        return jsonify({"ok": True, "wa_msg_id": wa_id,
                        "destino": destino, "hora": marca})
    except Exception as e:
        from flask import jsonify
        return jsonify({"ok": False, "error": str(e)}), 500


# =============================================================================
# SALUD — Funnels de progreso por tipo de persona
# =============================================================================

def _funnel_propietarios():
    sql = """
      SELECT
        COUNT(*) FILTER (WHERE activo)                                          AS total,
        COUNT(*) FILTER (WHERE activo AND datos_confirmado_at IS NULL)          AS pendiente,
        COUNT(*) FILTER (WHERE activo AND datos_confirmado_at IS NOT NULL
                         AND bienvenida_at IS NULL)                             AS confirmado,
        COUNT(*) FILTER (WHERE activo AND bienvenida_at IS NOT NULL
                         AND ofrecer_petra_at IS NULL)                          AS bienvenida,
        COUNT(*) FILTER (WHERE activo AND ofrecer_petra_at IS NOT NULL
                         AND ofrecer_credito_at IS NULL
                         AND mandato_at IS NULL)                                AS ofrecer_petra,
        COUNT(*) FILTER (WHERE activo AND ofrecer_credito_at IS NOT NULL
                         AND mandato_at IS NULL)                                AS ofrecer_credito,
        COUNT(*) FILTER (WHERE activo AND mandato_at IS NOT NULL)               AS mandato,
        COUNT(*) FILTER (WHERE NOT activo)                                      AS desactivados
      FROM inmuebles_visitante
    """
    with _db._conexion_directa(timeout=5) as conn:
        r = conn.execute(sql).fetchone()
    total = r[0] or 0
    requiere_bienvenida = _flag("embudo_requiere_bienvenida", default=True)
    if requiere_bienvenida:
        pasos = [
            ("⚪ Pendiente contacto (Sebas)",   r[1] or 0),
            ("🟡 Contactado · faltan datos",    r[2] or 0),
            ("✉️ Bienvenida enviada",           r[3] or 0),
            ("🏠 Ofrecida Petra",               r[4] or 0),
            ("💵 Ofrecida Crédito",             r[5] or 0),
            ("🤝 Mandato firmado",              r[6] or 0),
        ]
    else:
        # Flag OFF: ocultar paso bienvenida. Lo que estaba en "bienvenida"
        # se visualiza como "listo para ofrecer" (sin cambiar la DB).
        pasos = [
            ("⚪ Pendiente contacto (Sebas)",   r[1] or 0),
            ("🟡 Contactado · faltan datos",    r[2] or 0),
            ("🏠 Ofrecida Petra",               (r[4] or 0) + (r[3] or 0)),
            ("💵 Ofrecida Crédito",             r[5] or 0),
            ("🤝 Mandato firmado",              r[6] or 0),
        ]
    extra = f"{r[7] or 0} desactivados (no cuentan)"
    if not requiere_bienvenida:
        extra += " · paso bienvenida desactivado (flag)"
    return {
        "titulo": "🏠 Propietarios — Embudo",
        "total": total, "subtotal_label": "activos",
        "extra": extra,
        "pasos": [{"nombre": n, "n": v,
                   "pct": int(v * 100 / total) if total else 0} for n, v in pasos],
    }


def _funnel_agentes():
    # El rol y estado_agente viven en petra_contacto_roles (no en petra_contactos).
    sql = """
      SELECT
        COUNT(*)                                                                  AS total,
        COUNT(*) FILTER (WHERE r.metadata->>'estado_agente' = 'recolectando')             AS recolec,
        COUNT(*) FILTER (WHERE r.metadata->>'estado_agente' = 'en_capacitacion')          AS capa,
        COUNT(*) FILTER (WHERE r.metadata->>'estado_agente' = 'autorizacion_pendiente')   AS auto,
        COUNT(*) FILTER (WHERE r.metadata->>'estado_agente' IN ('activa','activo'))       AS activa,
        COUNT(*) FILTER (WHERE r.metadata->>'estado_agente' = 'pausada')                  AS pausada,
        COUNT(*) FILTER (WHERE r.metadata->>'estado_agente' IS NULL
                         OR r.metadata->>'estado_agente' = '')                             AS sin_estado
      FROM petra_contacto_roles r
      WHERE r.rol = 'agente_petra'
    """
    with _db._conexion_directa(timeout=5) as conn:
        r = conn.execute(sql).fetchone()
    total = r[0] or 0
    pasos = [
        ("🌱 Recolectando datos",        r[1] or 0),
        ("🎓 En capacitación",           r[2] or 0),
        ("📋 Autorización pendiente",    r[3] or 0),
        ("✅ Activa",                    r[4] or 0),
        ("⏸ Pausada",                   r[5] or 0),
    ]
    extra = ""
    if (r[6] or 0) > 0:
        extra = f"{r[6]} sin estado definido"
    return {
        "titulo": "👤 Agentes Petra — Flujo",
        "total": total, "subtotal_label": "agentes",
        "extra": extra,
        "pasos": [{"nombre": n, "n": v,
                   "pct": int(v * 100 / total) if total else 0} for n, v in pasos],
    }


def _funnel_visitantes():
    # Visitantes = contactos que NO tienen rol agente_petra.
    sql = """
      SELECT
        COUNT(DISTINCT pc.id)                                                    AS total,
        COUNT(DISTINCT pc.id) FILTER (WHERE pc.updated_at > NOW() - INTERVAL '7 days') AS activos_7d,
        COUNT(DISTINCT pc.id) FILTER (WHERE pc.meta->>'visitante_conjunto_id' IS NOT NULL) AS escaneo_qr,
        COUNT(DISTINCT i.interesado_telefono)                                    AS pidieron_contacto,
        COUNT(DISTINCT i.interesado_telefono) FILTER (WHERE i.estado = 'enviado') AS enviados_al_prop
      FROM petra_contactos pc
      LEFT JOIN petra_interesados_qr i ON i.interesado_telefono = pc.telefono
      WHERE NOT EXISTS (
        SELECT 1 FROM petra_contacto_roles r
        WHERE r.contacto_id = pc.id AND r.rol = 'agente_petra'
      )
    """
    try:
        with _db._conexion_directa(timeout=5) as conn:
            r = conn.execute(sql).fetchone()
    except Exception as e:
        return {"titulo": "👀 Visitantes QR", "total": 0, "subtotal_label": "",
                "extra": f"error: {type(e).__name__}", "pasos": []}
    total = r[0] or 0
    pasos = [
        ("👋 Escaneó QR en algún conjunto",          r[2] or 0),
        ("💬 Pidió contacto al propietario",          r[3] or 0),
        ("📤 Contacto enviado al propietario",        r[4] or 0),
    ]
    return {
        "titulo": "👀 Visitantes QR",
        "total": total, "subtotal_label": "contactos totales",
        "extra": f"{r[1] or 0} activos últimos 7 días",
        "pasos": [{"nombre": n, "n": v,
                   "pct": int(v * 100 / total) if total else 0} for n, v in pasos],
    }


def _run_funnels():
    """Corre los 3 funnels aislados."""
    fallbacks = {
        "_funnel_propietarios": "🏠 Propietarios — Embudo",
        "_funnel_agentes":      "👤 Agentes Petra — Flujo",
        "_funnel_visitantes":   "👀 Visitantes QR",
    }
    funnels = []
    for fn in [_funnel_propietarios, _funnel_agentes, _funnel_visitantes]:
        try:
            funnels.append(fn())
        except Exception as e:
            funnels.append({"titulo": fallbacks.get(fn.__name__, fn.__name__),
                            "total": 0, "subtotal_label": "",
                            "extra": f"error al consultar: {type(e).__name__}",
                            "pasos": []})
    return funnels


# =============================================================================
# FEATURE FLAGS — switches on/off desde /panel/petra/flags
# =============================================================================

import time as _time
_FLAGS_CACHE = {"data": {}, "ts": 0.0, "ttl": 30.0}


def _flag(nombre, default=False):
    """Lee un feature flag. Cachea 30s para no hammer DB en hot paths.
    Si el flag no existe en DB, devuelve `default`.
    Uso típico:
        from app.petra_panel import _flag
        if _flag("disparador_viernes_propietarios"):
            ...
    """
    now = _time.time()
    cache = _FLAGS_CACHE
    if now - cache["ts"] > cache["ttl"] or not cache["data"]:
        try:
            with _db._conexion_directa(timeout=3) as conn:
                rows = conn.execute(
                    "SELECT nombre, activo FROM petra_flags").fetchall()
            cache["data"] = {r[0]: bool(r[1]) for r in rows}
            cache["ts"] = now
        except Exception:
            # Si falla la DB, usar lo que haya cacheado (o default)
            pass
    return cache["data"].get(nombre, default)


def _flags_cache_bust():
    """Fuerza la próxima lectura a ir a DB. Se llama después de toggle."""
    _FLAGS_CACHE["ts"] = 0.0


@petra_panel_bp.get("/flags")
def flags_dashboard():
    """UI de feature flags. Grupos por categoría, toggle con PIN."""
    _check_token()
    token = (request.args.get("token") or "").strip()
    flash = (request.args.get("flash") or "").strip()
    with _db._conexion_directa(timeout=5) as conn:
        rows = _rows(conn,
            "SELECT nombre, activo, categoria, descripcion, updated_at "
            "FROM petra_flags ORDER BY categoria, nombre")
    # Agrupar por categoría
    grupos = {}
    for f in rows:
        grupos.setdefault(f["categoria"], []).append(f)
    return render_template(
        "petra_flags.html",
        grupos=grupos, token=token, flash=flash,
    )


@petra_panel_bp.post("/flags/<nombre>/toggle")
def flags_toggle(nombre):
    """Toggle activo/inactivo. Requiere PIN de Fabricio."""
    _check_token()
    token = (request.form.get("token") or "").strip()
    if not _verificar_pin("fabricio"):
        return redirect(url_for("petra_panel.flags_dashboard",
                                token=token, flash="pin_malo"))
    with _db._conexion_directa(timeout=5) as conn:
        row = conn.execute(
            "SELECT activo FROM petra_flags WHERE nombre=%s", (nombre,)
        ).fetchone()
        if not row:
            return redirect(url_for("petra_panel.flags_dashboard",
                                    token=token, flash="no_existe"))
        nuevo = not bool(row[0])
        conn.execute(
            "UPDATE petra_flags SET activo=%s, updated_at=NOW() "
            "WHERE nombre=%s", (nuevo, nombre))
    _flags_cache_bust()
    return redirect(url_for("petra_panel.flags_dashboard",
                            token=token, flash="ok"))


# =============================================================================
# PASOS — Detalle persona-a-persona de propietarios, agentes y visitantes
# con su etapa actual, agrupados por paso del embudo.
# =============================================================================

def _detalle_propietarios():
    """Lista inmuebles activos con dueño + paso actual + última actividad."""
    sql = """
      SELECT iv.id, iv.tipo, iv.nro, iv.operacion, iv.precio, iv.m2,
             iv.propietario_telefono,
             iv.datos_confirmado_at, iv.bienvenida_at,
             iv.ofrecer_petra_at, iv.ofrecer_credito_at, iv.mandato_at,
             iv.creado_en,
             cv.nombre AS conjunto_nombre,
             cv.codigo AS conjunto_codigo
        FROM inmuebles_visitante iv
        LEFT JOIN conjuntos_visitante cv ON cv.id = iv.conjunto_id
       WHERE iv.activo = TRUE
       ORDER BY iv.creado_en DESC
    """
    with _db._conexion_directa(timeout=5) as conn:
        rows = _rows(conn, sql)
    for r in rows:
        if r.get("mandato_at"):
            r["etapa"], r["etapa_label"], r["etapa_cls"] = \
                "mandato", "🤝 Mandato firmado", "mandato"
            r["ult_actividad"] = r["mandato_at"]
        elif r.get("ofrecer_credito_at"):
            r["etapa"], r["etapa_label"], r["etapa_cls"] = \
                "credito", "💵 Ofrecido Crédito", "credito"
            r["ult_actividad"] = r["ofrecer_credito_at"]
        elif r.get("ofrecer_petra_at"):
            r["etapa"], r["etapa_label"], r["etapa_cls"] = \
                "ofrecer", "🏠 Ofrecida Petra", "ofrecer"
            r["ult_actividad"] = r["ofrecer_petra_at"]
        elif r.get("bienvenida_at"):
            r["etapa"], r["etapa_label"], r["etapa_cls"] = \
                "bienvenida", "✉️ Bienvenida enviada", "bienvenida"
            r["ult_actividad"] = r["bienvenida_at"]
        elif r.get("datos_confirmado_at"):
            tiene_datos = (r.get("precio") or 0) > 0 and (r.get("m2") or 0) > 0
            r["etapa"] = "confirmado"
            r["etapa_label"] = "📝 Datos completos · listo bienvenida" if tiene_datos \
                else "🟡 Contactado · faltan datos"
            r["etapa_cls"] = "confirmado"
            r["ult_actividad"] = r["datos_confirmado_at"]
        else:
            r["etapa"], r["etapa_label"], r["etapa_cls"] = \
                "pendiente", "⚪ Pendiente contacto (Sebas)", "pendiente"
            r["ult_actividad"] = r["creado_en"]
        r["codigo"] = f"P{r['id']:04d}"
    # Agrupar por etapa en orden del embudo
    orden = ["pendiente", "confirmado", "bienvenida", "ofrecer",
             "credito", "mandato"]
    grupos = {k: [] for k in orden}
    for r in rows:
        grupos[r["etapa"]].append(r)
    return [(k, grupos[k]) for k in orden if grupos[k]]


def _detalle_agentes():
    """Lista agentes Petra con su estado + última actividad."""
    sql = """
      SELECT c.telefono, c.nombre, c.updated_at,
             r.metadata->>'estado_agente' AS estado_agente,
             r.metadata->>'ciudad' AS ciudad,
             r.metadata->>'videos_vistos' AS videos_vistos,
             c.created_at
        FROM petra_contacto_roles r
        JOIN petra_contactos c ON c.id = r.contacto_id
       WHERE r.rol = 'agente_petra'
       ORDER BY c.updated_at DESC NULLS LAST
    """
    with _db._conexion_directa(timeout=5) as conn:
        rows = _rows(conn, sql)
    for r in rows:
        est = (r.get("estado_agente") or "").lower()
        if est == "recolectando":
            r["etapa"], r["etapa_label"], r["etapa_cls"] = \
                "recolectando", "🌱 Recolectando datos", "pendiente"
        elif est == "en_capacitacion":
            r["etapa"], r["etapa_label"], r["etapa_cls"] = \
                "capacitacion", "🎓 En capacitación", "confirmado"
        elif est == "autorizacion_pendiente":
            r["etapa"], r["etapa_label"], r["etapa_cls"] = \
                "autorizacion", "📋 Autorización pendiente", "bienvenida"
        elif est in ("activa", "activo"):
            r["etapa"], r["etapa_label"], r["etapa_cls"] = \
                "activa", "✅ Activa", "mandato"
        elif est == "pausada":
            r["etapa"], r["etapa_label"], r["etapa_cls"] = \
                "pausada", "⏸ Pausada", "credito"
        else:
            r["etapa"], r["etapa_label"], r["etapa_cls"] = \
                "sin_estado", "❓ Sin estado", "pendiente"
    orden = ["recolectando", "capacitacion", "autorizacion", "activa",
             "pausada", "sin_estado"]
    grupos = {k: [] for k in orden}
    for r in rows:
        grupos[r["etapa"]].append(r)
    return [(k, grupos[k]) for k in orden if grupos[k]]


def _detalle_visitantes():
    """Visitantes QR (contactos NO-agentes) activos último mes."""
    sql = """
      SELECT pc.telefono, pc.nombre, pc.updated_at, pc.created_at,
             pc.meta->>'visitante_conjunto_id' AS conjunto_id,
             pc.meta->>'visitante_estado' AS estado_v,
             (SELECT COUNT(*) FROM petra_interesados_qr i
               WHERE i.interesado_telefono = pc.telefono) AS n_pidio
        FROM petra_contactos pc
       WHERE NOT EXISTS (
         SELECT 1 FROM petra_contacto_roles r
         WHERE r.contacto_id = pc.id AND r.rol = 'agente_petra'
       )
         AND pc.updated_at > NOW() - INTERVAL '30 days'
       ORDER BY pc.updated_at DESC
       LIMIT 500
    """
    try:
        with _db._conexion_directa(timeout=5) as conn:
            rows = _rows(conn, sql)
    except Exception:
        rows = []
    for r in rows:
        if (r.get("n_pidio") or 0) > 0:
            r["etapa"], r["etapa_label"], r["etapa_cls"] = \
                "pidio_contacto", "💬 Pidió contacto a propietario", "mandato"
            r["ult_actividad"] = r["updated_at"]
        elif r.get("conjunto_id"):
            r["etapa"], r["etapa_label"], r["etapa_cls"] = \
                "escaneo", "👋 Escaneó QR", "confirmado"
            r["ult_actividad"] = r["updated_at"]
        else:
            r["etapa"], r["etapa_label"], r["etapa_cls"] = \
                "sin_actividad", "👀 Sin actividad clara", "pendiente"
            r["ult_actividad"] = r["updated_at"]
    orden = ["pidio_contacto", "escaneo", "sin_actividad"]
    grupos = {k: [] for k in orden}
    for r in rows:
        grupos[r["etapa"]].append(r)
    return [(k, grupos[k]) for k in orden if grupos[k]]


@petra_panel_bp.get("/pasos")
def pasos_personas():
    """Detalle persona-a-persona de propietarios, agentes y visitantes QR."""
    _check_token()
    token = (request.args.get("token") or "").strip()
    tipo = (request.args.get("tipo") or "propietarios").strip()
    if tipo == "agentes":
        grupos = _detalle_agentes()
        titulo = "👤 Agentes Petra"
    elif tipo == "visitantes":
        grupos = _detalle_visitantes()
        titulo = "👀 Visitantes QR"
    else:
        tipo = "propietarios"
        grupos = _detalle_propietarios()
        titulo = "🏠 Propietarios"
    total = sum(len(g[1]) for g in grupos)
    return render_template(
        "petra_pasos.html",
        tipo=tipo, grupos=grupos, titulo=titulo,
        total=total, token=token,
    )


# =============================================================================
# DESPLIEGUES — Botón "Promover staging → prod"
# =============================================================================

def _gh_compare(base="main", head="develop"):
    """GitHub API: compara base vs head. Devuelve dict con ahead_by,
    behind_by, commits (lista). Sin auth necesaria si el repo es público,
    pero con GITHUB_TOKEN evita rate-limit de 60 req/h."""
    import requests as _rq
    tok = (os.environ.get("GITHUB_TOKEN") or "").strip()
    headers = {"Accept": "application/vnd.github+json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    r = _rq.get(
        f"https://api.github.com/repos/fabricio754/petra-inmuebles/compare/{base}...{head}",
        headers=headers, timeout=10,
    )
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
    d = r.json()
    return {
        "ahead_by": d.get("ahead_by", 0),
        "behind_by": d.get("behind_by", 0),
        "commits": [
            {
                "sha": c.get("sha", "")[:7],
                "message": (c.get("commit", {}).get("message") or "").split("\n")[0][:120],
                "author": c.get("commit", {}).get("author", {}).get("name", ""),
                "date": c.get("commit", {}).get("author", {}).get("date", "")[:10],
            }
            for c in d.get("commits", [])[-30:]  # máximo 30 commits
        ],
    }


def _gh_merge_dev_into_main(commit_message=None):
    """Mergea develop → main via GitHub API. Requires GITHUB_TOKEN con
    permiso de push a main."""
    import requests as _rq
    tok = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if not tok:
        return {"ok": False, "error": "GITHUB_TOKEN no configurado en env"}
    from datetime import datetime
    msg = commit_message or f"🚀 Promoción staging → prod · {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    r = _rq.post(
        "https://api.github.com/repos/fabricio754/petra-inmuebles/merges",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
        },
        json={"base": "main", "head": "develop", "commit_message": msg},
        timeout=20,
    )
    if r.status_code == 201:
        d = r.json()
        return {"ok": True, "sha": d.get("sha", "")[:7], "message": msg}
    if r.status_code == 204:
        return {"ok": True, "noop": True, "message": "Nada para mergear — ya está todo en prod"}
    if r.status_code == 409:
        return {"ok": False, "error": "Conflicto de merge. Resolvé a mano."}
    return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}


# Ventana horaria de despliegue: por default 23:00–06:00 Colombia (America/Bogota).
# Configurable con PETRA_DEPLOY_WINDOW="23-6" o similar. Fuera de la ventana el
# botón promover se deshabilita (el bot está siendo usado, no queremos caídas).
# Override de emergencia: pasar fuerza=1 en el POST (requiere PIN igual).
_DEPLOY_WINDOW_DEFAULT = "20-6"


def _parse_window(spec):
    """'23-6' → (23, 6). Rango cruza medianoche si inicio > fin."""
    try:
        a, b = spec.split("-")
        return int(a) % 24, int(b) % 24
    except Exception:
        return 23, 6


def _ahora_bogota_hora():
    """Devuelve la hora (0-23) en America/Bogota (UTC-5, sin DST)."""
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=-5)).hour


def _en_ventana_despliegue():
    """True si la hora local (Bogotá) cae en la ventana configurada."""
    spec = (os.environ.get("PETRA_DEPLOY_WINDOW") or _DEPLOY_WINDOW_DEFAULT).strip()
    inicio, fin = _parse_window(spec)
    h = _ahora_bogota_hora()
    if inicio <= fin:
        return inicio <= h < fin
    # Cruza medianoche: 23-6 = [23,24) ∪ [0,6)
    return h >= inicio or h < fin


def _gh_prs_abiertos_a_main():
    """Lista PRs abiertos con base=main. Devuelve [{number, title, head, url}]."""
    import requests as _rq
    tok = (os.environ.get("GITHUB_TOKEN_FABRICIO")
           or os.environ.get("GITHUB_TOKEN") or "").strip()
    headers = {"Accept": "application/vnd.github+json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    r = _rq.get(
        "https://api.github.com/repos/fabricio754/petra-inmuebles/pulls"
        "?state=open&base=main", headers=headers, timeout=10,
    )
    if r.status_code != 200:
        return []
    return [{
        "number": p.get("number"),
        "title": p.get("title", "")[:120],
        "head": (p.get("head") or {}).get("ref", ""),
        "url": p.get("html_url", ""),
    } for p in r.json()]


@petra_panel_bp.get("/deploys")
def deploys_dashboard():
    """Pantalla: ver pendientes + PRs abiertos para aprobar+mergear."""
    _check_token()
    token = (request.args.get("token") or "").strip()
    cmp = _gh_compare(base="main", head="develop")
    spec = (os.environ.get("PETRA_DEPLOY_WINDOW") or _DEPLOY_WINDOW_DEFAULT).strip()
    inicio, fin = _parse_window(spec)
    return render_template(
        "petra_deploys.html",
        cmp=cmp, token=token,
        is_staging=(os.environ.get("MODO_STAGING") or "").strip() == "1",
        ventana_abierta=_en_ventana_despliegue(),
        ventana_inicio=inicio,
        ventana_fin=fin,
        hora_bogota=_ahora_bogota_hora(),
        prs_abiertos=_gh_prs_abiertos_a_main(),
    )


def _gh_aprobar_y_mergear_pr(pr_number):
    """Aprueba el PR como @fabricio754 y lo mergea. Requiere
    GITHUB_TOKEN_FABRICIO (PAT personal, scope repo). Si falta, falla."""
    import requests as _rq
    tok = (os.environ.get("GITHUB_TOKEN_FABRICIO")
           or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not tok:
        return {"ok": False, "error": "GITHUB_TOKEN_FABRICIO no configurado en env"}
    repo = "fabricio754/petra-inmuebles"
    headers = {"Accept": "application/vnd.github+json",
               "Authorization": f"Bearer {tok}",
               "Content-Type": "application/json"}
    # 1) Aprobar como Fabricio.
    r = _rq.post(
        f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews",
        headers=headers, json={"event": "APPROVE"}, timeout=20,
    )
    if r.status_code not in (200, 201):
        return {"ok": False, "error": f"Approve fallo HTTP {r.status_code}: {r.text[:200]}"}
    # 2) Mergear.
    r = _rq.put(
        f"https://api.github.com/repos/{repo}/pulls/{pr_number}/merge",
        headers=headers, json={"merge_method": "merge"}, timeout=20,
    )
    if r.status_code == 200:
        return {"ok": True, "sha": (r.json().get("sha") or "")[:7]}
    return {"ok": False, "error": f"Merge fallo HTTP {r.status_code}: {r.text[:200]}"}


@petra_panel_bp.post("/deploys/aprobar")
def deploys_aprobar():
    """Aprueba+mergea un PR develop→main. Requiere PIN de Fabricio.

    Este es el ÚNICO camino para mergear a prod. GitHub tiene branch
    protection con CODEOWNERS=@fabricio754 → ningún otro merge pasa.
    El panel usa GITHUB_TOKEN_FABRICIO (PAT personal) para aprobar
    como Fabricio y mergear en una sola acción atómica."""
    _check_token()
    from flask import jsonify
    if not _verificar_pin("fabricio"):
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    if (os.environ.get("MODO_STAGING") or "").strip() == "1":
        return jsonify({"ok": False,
                        "error": "No se puede aprobar desde staging. Hacelo desde prod."}), 400
    if not _en_ventana_despliegue() and not (request.form.get("fuerza") or "").strip() == "1":
        return jsonify({"ok": False, "error": "Fuera de ventana (20–06 Bogotá). "
                        "Si es emergencia, mandá fuerza=1.", "fuera_ventana": True}), 423
    try:
        pr_number = int((request.form.get("pr") or "").strip())
    except Exception:
        return jsonify({"ok": False, "error": "Falta ?pr=NNN"}), 400
    return jsonify(_gh_aprobar_y_mergear_pr(pr_number))


# =============================================================================
# OBSERVABILIDAD — dashboard que agrega petra_paso_eventos
# =============================================================================

def _stats_por_etapa(ambiente, horas):
    """Devuelve lista de etapas con stats (últimas N horas), ordenadas por
    volumen descendente."""
    sql = """
      SELECT etapa,
             COUNT(*)                                       AS total,
             COUNT(*) FILTER (WHERE resultado='ok')         AS ok,
             COUNT(*) FILTER (WHERE resultado='fallo')      AS fallo,
             COUNT(*) FILTER (WHERE resultado='timeout')    AS timeout,
             PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY tiempo_ms)   AS p50,
             PERCENTILE_DISC(0.95) WITHIN GROUP (ORDER BY tiempo_ms)  AS p95,
             MAX(tiempo_ms)                                 AS p_max,
             (SELECT error FROM petra_paso_eventos p2
               WHERE p2.etapa = p.etapa AND p2.ambiente = p.ambiente
                 AND p2.resultado = 'fallo'
                 AND p2.ts > NOW() - INTERVAL '%s hours'
               ORDER BY p2.ts DESC LIMIT 1)                 AS ultimo_error
        FROM petra_paso_eventos p
       WHERE ambiente = %s
         AND ts > NOW() - INTERVAL '%s hours'
       GROUP BY etapa, ambiente
       ORDER BY total DESC
       LIMIT 50
    """
    try:
        with _db._conexion_directa(timeout=5) as conn:
            rows = _rows(conn, sql, (horas, ambiente, horas))
    except Exception as e:
        return [{"error_global": str(e)}]
    for r in rows:
        total = r.get("total") or 0
        ok = r.get("ok") or 0
        r["pct_ok"] = int(ok * 100 / total) if total else 0
        if r["pct_ok"] >= 95:
            r["status"] = "ok"
        elif r["pct_ok"] >= 80:
            r["status"] = "warn"
        else:
            r["status"] = "down"
    return rows


@petra_panel_bp.get("/observabilidad")
def observabilidad_dashboard():
    """Dashboard de observabilidad por paso. Muestra comparación lado a
    lado prod vs staging con % éxito, latencias, último error por etapa."""
    _check_token()
    token = (request.args.get("token") or "").strip()
    horas = int(request.args.get("horas") or 24)
    prod = _stats_por_etapa("prod", horas)
    staging = _stats_por_etapa("staging", horas)
    # Unificar lista de etapas que apararecen en cualquiera de los dos
    etapas = sorted(set(
        [r["etapa"] for r in prod if "etapa" in r] +
        [r["etapa"] for r in staging if "etapa" in r]
    ))
    prod_by = {r["etapa"]: r for r in prod if "etapa" in r}
    staging_by = {r["etapa"]: r for r in staging if "etapa" in r}
    rows = []
    for e in etapas:
        rows.append({
            "etapa": e,
            "prod": prod_by.get(e),
            "staging": staging_by.get(e),
        })
    return render_template(
        "petra_observabilidad.html",
        rows=rows, horas=horas, token=token,
        total_eventos_prod=sum(r.get("total", 0) for r in prod if "etapa" in r),
        total_eventos_staging=sum(r.get("total", 0) for r in staging if "etapa" in r),
    )


@petra_panel_bp.get("/observabilidad.json")
def observabilidad_json():
    """JSON para auto-refresh del dashboard sin recargar."""
    _check_token()
    from flask import jsonify
    horas = int(request.args.get("horas") or 24)
    return jsonify({
        "horas": horas,
        "prod": _stats_por_etapa("prod", horas),
        "staging": _stats_por_etapa("staging", horas),
    })


# =============================================================================
# STAGING WHITELIST — gestión desde el panel
# =============================================================================

@petra_panel_bp.get("/staging-whitelist")
def staging_whitelist_dashboard():
    """Pantalla para gestionar la whitelist del router staging."""
    _check_token()
    token = (request.args.get("token") or "").strip()
    flash = (request.args.get("flash") or "").strip()
    with _db._conexion_directa(timeout=5) as conn:
        rows = _rows(conn,
            "SELECT telefono, nota, creado_en "
            "FROM petra_staging_whitelist ORDER BY creado_en DESC")
    router_url = (os.environ.get("PETRA_STAGING_URL") or "").strip()
    is_staging_panel = (os.environ.get("MODO_STAGING") or "").strip() == "1"
    return render_template(
        "petra_staging_whitelist.html",
        rows=rows, token=token, flash=flash,
        router_url=router_url, is_staging_panel=is_staging_panel,
    )


@petra_panel_bp.post("/staging-whitelist/agregar")
def staging_whitelist_agregar():
    """Agrega un teléfono a la whitelist. Requiere PIN de Fabricio."""
    _check_token()
    token = (request.form.get("token") or "").strip()
    if not _verificar_pin("fabricio"):
        return redirect(url_for("petra_panel.staging_whitelist_dashboard",
                                token=token, flash="pin_malo"))
    telefono = (request.form.get("telefono") or "").strip()
    nota = (request.form.get("nota") or "").strip() or None
    # Normalizar: solo dígitos, debe empezar con 57 (Colombia)
    import re as _re
    tel_limpio = _re.sub(r"\D", "", telefono)
    if not tel_limpio or len(tel_limpio) < 10:
        return redirect(url_for("petra_panel.staging_whitelist_dashboard",
                                token=token, flash="telefono_invalido"))
    if len(tel_limpio) == 10 and tel_limpio.startswith("3"):
        tel_limpio = "57" + tel_limpio
    try:
        with _db._conexion_directa(timeout=5) as conn:
            conn.execute(
                "INSERT INTO petra_staging_whitelist (telefono, nota) "
                "VALUES (%s, %s) ON CONFLICT (telefono) DO UPDATE "
                "SET nota = EXCLUDED.nota",
                (tel_limpio, nota))
        # Invalidar cache del router
        try:
            from app.staging_router import whitelist_cache_bust
            whitelist_cache_bust()
        except Exception:
            pass
    except Exception:
        return redirect(url_for("petra_panel.staging_whitelist_dashboard",
                                token=token, flash="error"))
    return redirect(url_for("petra_panel.staging_whitelist_dashboard",
                            token=token, flash="agregado"))


@petra_panel_bp.post("/staging-whitelist/<telefono>/eliminar")
def staging_whitelist_eliminar(telefono):
    """Quita un teléfono de la whitelist. Requiere PIN de Fabricio."""
    _check_token()
    token = (request.form.get("token") or "").strip()
    if not _verificar_pin("fabricio"):
        return redirect(url_for("petra_panel.staging_whitelist_dashboard",
                                token=token, flash="pin_malo"))
    try:
        with _db._conexion_directa(timeout=5) as conn:
            conn.execute("DELETE FROM petra_staging_whitelist WHERE telefono=%s",
                         (telefono,))
        try:
            from app.staging_router import whitelist_cache_bust
            whitelist_cache_bust()
        except Exception:
            pass
    except Exception:
        return redirect(url_for("petra_panel.staging_whitelist_dashboard",
                                token=token, flash="error"))
    return redirect(url_for("petra_panel.staging_whitelist_dashboard",
                            token=token, flash="eliminado"))
