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
    # Carga toda la media_estacion de una vez agrupada por estado real
    media_por_estado = {}
    try:
        with _db._conexion_directa(timeout=10) as conn:
            rows = conn.execute(
                "SELECT id, estado_key, titulo, url, activo, created_at "
                "FROM petra_media_estacion ORDER BY estado_key, orden, id"
            ).fetchall()
        for r in rows:
            media_por_estado.setdefault(r[1], []).append({
                "id": r[0], "titulo": r[2], "url": r[3],
                "activo": r[4], "created_at": r[5],
            })
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
            })
        estado_real = _sim_mod.estado_real_del_preset(k)
        presets_extended.append({
            "key": k, "label": v["label"], "contexto": v.get("contexto", ""),
            "textos": textos_resueltos,
            "transiciones": v.get("transiciones", []),
            "estado_real": estado_real,
            "media": media_por_estado.get(estado_real, []) if estado_real else [],
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
    # URL pública relativa (el server tiene ruta /petra/media/<name>)
    base = (public_base_url or os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
    return f"{base}/petra/media/{nuevo}" if base else f"/petra/media/{nuevo}"


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
        "       c.created_at AS contacto_desde, "
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
    _pwa.send_text(telefono, _t(
        "activacion.msg_kit_enviado",
        fallback=(
            "📦 *¡Tu kit ya está en camino!*\n\n"
            "Dentro vas a encontrar tu QR + un regalito 💜. "
            "Cuando lo recibas, instalá el QR en la cartelera del conjunto "
            "o donde lo autorizó el administrador.\n\n"
            "Te paso un video rápido con el paso a paso 👇"
        ),
    ))
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
        # orden incremental dentro del estado
        row = conn.execute(
            "SELECT COALESCE(MAX(orden), 0) + 1 FROM petra_media_estacion "
            "WHERE estado_key=%s", (estado_key,),
        ).fetchone()
        orden = row[0] if row else 1
        conn.execute(
            "INSERT INTO petra_media_estacion "
            "(estado_key, orden, titulo, url, activo) "
            "VALUES (%s, %s, %s, %s, TRUE)",
            (estado_key, orden, titulo, url_final),
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
