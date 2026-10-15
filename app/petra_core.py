"""Helpers CRUD y de dominio para las entidades transaccionales de Petra.

Separa la lógica de persistencia por entidad. Cada función es intencionadamente
pequeña y usa transacciones cortas. La idea es que petra_router, petra_match,
petra_llm, el panel y los webhooks externos (Auco, Sureti, ElevenLabs) llamen
a estas funciones en vez de escribir SQL ad-hoc.

Entidades que cubre:
  - petra_oportunidades
  - petra_contratos
  - petra_visitas
  - petra_casos_soporte
  - petra_comisiones
  - petra_consentimientos
  - petra_auditoria_eventos
  - utilidades de inmueble (dedup_hash, transiciones de estado)

Reglas:
  - Toda mutación relevante escribe un evento en petra_auditoria_eventos.
  - Las transiciones de estado se validan contra tablas de permitidos.
  - Las comisiones usan UNIQUE INDEX para no contar dos veces el mismo evento.
"""
import hashlib
import json
import logging
import os
import re
from flask import Blueprint, jsonify, request

from app import db as _db

log = logging.getLogger("petra")

petra_core_bp = Blueprint("petra_core", __name__, url_prefix="/petra")


# =============================================================================
# Transiciones permitidas
# =============================================================================

INMUEBLE_TRANSICIONES = {
    "borrador": {"activo", "eliminado"},
    "activo": {"pausado", "reservado", "vendido", "arrendado", "vencido", "eliminado"},
    "reservado": {"activo", "vendido", "arrendado", "pausado"},
    "pausado": {"activo", "eliminado"},
    "vencido": {"activo", "eliminado"},
    "vendido": set(),
    "arrendado": set(),
    "eliminado": set(),
}

OPORTUNIDAD_TRANSICIONES = {
    "lead": {"contactado", "cerrado_perdido", "pausado"},
    "contactado": {"visita_agendada", "negociando", "cerrado_perdido", "pausado"},
    "visita_agendada": {"visita_completa", "cerrado_perdido", "pausado"},
    "visita_completa": {"oferta", "negociando", "cerrado_perdido", "pausado"},
    "oferta": {"negociando", "cerrado_ganado", "cerrado_perdido"},
    "negociando": {"oferta", "cerrado_ganado", "cerrado_perdido", "pausado"},
    "pausado": {"contactado", "cerrado_perdido"},
    "cerrado_ganado": set(),
    "cerrado_perdido": {"lead"},
}

CONTRATO_TRANSICIONES = {
    "borrador": {"enviado_firma", "cancelado"},
    "enviado_firma": {"firmado", "cancelado"},
    "firmado": {"activo", "cancelado"},
    "activo": {"vencido", "cancelado"},
    "vencido": set(),
    "cancelado": set(),
}

CASO_TRANSICIONES = {
    "abierto": {"en_gestion", "escalado", "resuelto", "cerrado"},
    "en_gestion": {"pendiente_cliente", "escalado", "resuelto"},
    "pendiente_cliente": {"en_gestion", "resuelto", "cerrado"},
    "escalado": {"en_gestion", "resuelto"},
    "resuelto": {"cerrado"},
    "cerrado": set(),
}


def transicion_permitida(mapa, actual, nuevo):
    return nuevo in mapa.get(actual, set())


# =============================================================================
# Auditoría
# =============================================================================


def audit(conn, actor_tipo, actor_id, actor_label, accion,
          entidad_tipo, entidad_id, antes=None, despues=None, metadata=None):
    """Escribe un evento en petra_auditoria_eventos. Nunca lanza."""
    try:
        conn.execute(
            """INSERT INTO petra_auditoria_eventos
               (actor_tipo, actor_id, actor_label, accion, entidad_tipo,
                entidad_id, antes, despues, metadata)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (actor_tipo, actor_id, actor_label, accion, entidad_tipo, entidad_id,
             json.dumps(antes, ensure_ascii=False, default=str) if antes else None,
             json.dumps(despues, ensure_ascii=False, default=str) if despues else None,
             json.dumps(metadata, ensure_ascii=False, default=str) if metadata else None),
        )
    except Exception:
        log.exception("[petra_core] fallo escribiendo auditoría")


# =============================================================================
# Deduplicación de inmueble
# =============================================================================


def _norm(s):
    if s is None:
        return ""
    return re.sub(r"\s+", "", str(s).strip().lower())


def dedup_hash_inmueble(edificio_id, torre, piso, apto, tipo, modalidad):
    """Hash estable para detectar duplicados obvios."""
    base = "|".join([
        str(edificio_id or ""),
        _norm(torre), _norm(piso), _norm(apto),
        _norm(tipo), _norm(modalidad),
    ])
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]


def rellenar_dedup_hash(conn, inmueble_id):
    """Calcula y guarda dedup_hash para un inmueble existente."""
    row = conn.execute(
        "SELECT edificio_id, torre, piso, apto, tipo, modalidad FROM petra_inmuebles WHERE id=%s",
        (inmueble_id,),
    ).fetchone()
    if not row:
        return None
    h = dedup_hash_inmueble(*row)
    conn.execute("UPDATE petra_inmuebles SET dedup_hash=%s WHERE id=%s", (h, inmueble_id))
    return h


# =============================================================================
# Oportunidades
# =============================================================================


def crear_oportunidad(conn, contacto_id, tipo, origen=None, inmueble_id=None,
                     valor_cop=None, origen_metadata=None,
                     responsable_contacto_id=None):
    row = conn.execute(
        """INSERT INTO petra_oportunidades
           (contacto_id, inmueble_id, tipo, origen, origen_metadata,
            valor_cop, responsable_contacto_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (contacto_id, inmueble_id, tipo, origen,
         json.dumps(origen_metadata, ensure_ascii=False) if origen_metadata else None,
         valor_cop, responsable_contacto_id),
    ).fetchone()
    oid = row[0]
    audit(conn, "sistema", None, "petra_core", "crear_oportunidad",
          "oportunidad", oid,
          despues={"contacto_id": contacto_id, "tipo": tipo, "estado": "lead"})
    return oid


def cambiar_estado_oportunidad(conn, oportunidad_id, nuevo_estado,
                              motivo=None, actor=None):
    row = conn.execute(
        "SELECT estado FROM petra_oportunidades WHERE id=%s",
        (oportunidad_id,),
    ).fetchone()
    if not row:
        return False, "no_existe"
    actual = row[0]
    if actual == nuevo_estado:
        return True, "sin_cambios"
    if not transicion_permitida(OPORTUNIDAD_TRANSICIONES, actual, nuevo_estado):
        return False, f"transicion_invalida {actual}->{nuevo_estado}"
    cerrado_at = "NOW()" if nuevo_estado.startswith("cerrado_") else None
    if cerrado_at:
        conn.execute(
            """UPDATE petra_oportunidades
               SET estado=%s, cerrado_motivo=%s, cerrado_at=NOW(), updated_at=NOW()
               WHERE id=%s""",
            (nuevo_estado, motivo, oportunidad_id),
        )
    else:
        conn.execute(
            "UPDATE petra_oportunidades SET estado=%s, updated_at=NOW() WHERE id=%s",
            (nuevo_estado, oportunidad_id),
        )
    audit(conn, "sistema", None, actor or "petra_core", "cambiar_estado",
          "oportunidad", oportunidad_id,
          antes={"estado": actual}, despues={"estado": nuevo_estado, "motivo": motivo})
    return True, "ok"


# =============================================================================
# Contratos
# =============================================================================


def crear_contrato(conn, contacto_id, tipo, oportunidad_id=None,
                  condiciones=None, inicio=None, fin=None):
    row = conn.execute(
        """INSERT INTO petra_contratos
           (contacto_id, tipo, oportunidad_id, condiciones, inicio, fin)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
        (contacto_id, tipo, oportunidad_id,
         json.dumps(condiciones, ensure_ascii=False) if condiciones else '{}',
         inicio, fin),
    ).fetchone()
    cid = row[0]
    audit(conn, "sistema", None, "petra_core", "crear_contrato",
          "contrato", cid,
          despues={"contacto_id": contacto_id, "tipo": tipo})
    return cid


def marcar_contrato_firmado(conn, contrato_id, firma_proveedor, firma_tx_id,
                            firma_tipo="electronica_simple"):
    row = conn.execute(
        "SELECT estado FROM petra_contratos WHERE id=%s", (contrato_id,),
    ).fetchone()
    if not row:
        return False
    if not transicion_permitida(CONTRATO_TRANSICIONES, row[0], "firmado"):
        return False
    conn.execute(
        """UPDATE petra_contratos
           SET estado='firmado', firma_proveedor=%s, firma_proveedor_tx_id=%s,
               firma_tipo=%s, firmado_at=NOW(), updated_at=NOW()
           WHERE id=%s""",
        (firma_proveedor, firma_tx_id, firma_tipo, contrato_id),
    )
    audit(conn, "integracion_externa", None, firma_proveedor,
          "firmar_contrato", "contrato", contrato_id,
          antes={"estado": row[0]}, despues={"estado": "firmado", "tx": firma_tx_id})
    return True


# =============================================================================
# Visitas
# =============================================================================


def crear_visita(conn, tipo, inmueble_id=None, oportunidad_id=None,
                 agente_id=None, interesado_id=None, programada_at=None,
                 checklist=None):
    row = conn.execute(
        """INSERT INTO petra_visitas
           (tipo, inmueble_id, oportunidad_id, agente_id, interesado_id,
            programada_at, checklist)
           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (tipo, inmueble_id, oportunidad_id, agente_id, interesado_id,
         programada_at,
         json.dumps(checklist, ensure_ascii=False) if checklist else '{}'),
    ).fetchone()
    vid = row[0]
    audit(conn, "sistema", None, "petra_core", "crear_visita",
          "visita", vid,
          despues={"tipo": tipo, "inmueble_id": inmueble_id, "agente_id": agente_id})
    # A5. Notificar automáticamente al agente con 3 botones.
    if agente_id:
        try:
            encolar_notificacion_visita_agente(conn, vid)
        except Exception:
            log.exception("[petra_core] encolar_notificacion_visita_agente fallo vid=%s", vid)
    return vid


def agregar_evidencia_visita(conn, visita_id, evidencia):
    """evidencia = {tipo, url, categoria, descripcion}"""
    conn.execute(
        """UPDATE petra_visitas
           SET evidencias = COALESCE(evidencias,'[]'::jsonb) || %s::jsonb,
               updated_at = NOW()
           WHERE id=%s""",
        (json.dumps([evidencia], ensure_ascii=False), visita_id),
    )


# =============================================================================
# Casos de soporte
# =============================================================================


SLA_HORAS = {"baja": 24, "media": 8, "alta": 4, "critica": 1}


def crear_caso_soporte(conn, contacto_id, categoria, descripcion,
                       prioridad="media", contrato_id=None,
                       responsable_humano=None):
    sla_h = SLA_HORAS.get(prioridad, 8)
    row = conn.execute(
        """INSERT INTO petra_casos_soporte
           (contacto_id, contrato_id, categoria, prioridad, descripcion,
            responsable_humano,
            sla_primera_respuesta_at, sla_resolucion_at)
           VALUES (%s,%s,%s,%s,%s,%s,
                   NOW() + (%s || ' hours')::interval,
                   NOW() + (%s || ' hours')::interval)
           RETURNING id""",
        (contacto_id, contrato_id, categoria, prioridad, descripcion,
         responsable_humano, sla_h, sla_h * 3),
    ).fetchone()
    cid = row[0]
    audit(conn, "sistema", None, "petra_core", "crear_caso",
          "caso", cid,
          despues={"contacto_id": contacto_id, "categoria": categoria, "prioridad": prioridad})
    return cid


# =============================================================================
# Comisiones
# =============================================================================


def causar_comision(conn, agente_id, evento_tipo, evento_entidad,
                   evento_entidad_id, monto_cop, base_calculo=None, nota=None):
    """Idempotente: si ya existe la combinación, no hace nada."""
    try:
        row = conn.execute(
            """INSERT INTO petra_comisiones
               (agente_id, evento_tipo, evento_entidad, evento_entidad_id,
                monto_cop, base_calculo, nota)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT DO NOTHING
               RETURNING id""",
            (agente_id, evento_tipo, evento_entidad, evento_entidad_id,
             monto_cop,
             json.dumps(base_calculo, ensure_ascii=False) if base_calculo else None,
             nota),
        ).fetchone()
    except Exception:
        log.exception("[petra_core] fallo causando comisión")
        return None
    if not row:
        return None
    cid = row[0]
    audit(conn, "sistema", None, "petra_core", "causar_comision",
          "comision", cid,
          despues={"agente_id": agente_id, "evento": evento_tipo, "monto": monto_cop})
    return cid


def aprobar_comision(conn, comision_id, aprobada_por):
    conn.execute(
        """UPDATE petra_comisiones
           SET estado='aprobada', aprobada_at=NOW(), aprobada_por=%s
           WHERE id=%s AND estado='causada'""",
        (aprobada_por, comision_id),
    )
    audit(conn, "humano_panel", None, aprobada_por, "aprobar_comision",
          "comision", comision_id, despues={"estado": "aprobada"})


# =============================================================================
# Consentimientos
# =============================================================================


def otorgar_consentimiento(conn, contacto_id, finalidad, canal="whatsapp",
                           evidencia=None, version_politica=None):
    """Si ya existía uno activo para esta finalidad, lo deja; si no, crea."""
    row = conn.execute(
        """SELECT id FROM petra_consentimientos
           WHERE contacto_id=%s AND finalidad=%s AND estado='otorgado'""",
        (contacto_id, finalidad),
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        """INSERT INTO petra_consentimientos
           (contacto_id, finalidad, canal, evidencia, version_politica)
           VALUES (%s,%s,%s,%s,%s) RETURNING id""",
        (contacto_id, finalidad, canal,
         json.dumps(evidencia, ensure_ascii=False) if evidencia else None,
         version_politica),
    ).fetchone()
    cid = row[0]
    audit(conn, "contacto", contacto_id, None, "otorgar_consentimiento",
          "consentimiento", cid,
          despues={"finalidad": finalidad, "canal": canal})
    return cid


def revocar_consentimiento(conn, contacto_id, finalidad, motivo=None):
    conn.execute(
        """UPDATE petra_consentimientos
           SET estado='revocado', revocado_at=NOW(), revocado_motivo=%s
           WHERE contacto_id=%s AND finalidad=%s AND estado='otorgado'""",
        (motivo, contacto_id, finalidad),
    )
    audit(conn, "contacto", contacto_id, None, "revocar_consentimiento",
          "consentimiento", None,
          despues={"finalidad": finalidad, "motivo": motivo})


def control_calidad_inventario(conn, dias_vencimiento=60):
    """Flujo 9 — Control de calidad del inventario.
    1) Mueve a 'borrador' los activos con datos clave faltantes.
    2) Mueve a 'vencido' los activos que llevan >N días sin tocarse.
    Devuelve dict con contadores."""
    # 1) Incompletos → borrador
    row_incompletos = conn.execute(
        """UPDATE petra_inmuebles
              SET estado = 'borrador', motivo_pausa = 'datos_incompletos',
                  updated_at = NOW()
            WHERE estado = 'activo'
              AND (
                tipo IS NULL OR modalidad IS NULL OR
                (modalidad = 'venta' AND precio_millones IS NULL) OR
                (modalidad = 'arriendo' AND precio_arriendo_mensual_millones IS NULL) OR
                telefono_contacto IS NULL
              )
            RETURNING id"""
    ).fetchall()
    movidos_borrador = len(row_incompletos)

    # 2) Vencidos
    row_vencidos = conn.execute(
        """UPDATE petra_inmuebles
              SET estado = 'vencido', vence_at = NOW(), updated_at = NOW()
            WHERE estado = 'activo'
              AND publicado_at < NOW() - (%s || ' days')::interval
            RETURNING id""",
        (dias_vencimiento,),
    ).fetchall()
    movidos_vencidos = len(row_vencidos)

    audit(conn, "sistema", None, "scheduler", "control_calidad",
          "inmueble", None,
          metadata={"a_borrador": movidos_borrador, "a_vencido": movidos_vencidos})
    return {"a_borrador": movidos_borrador, "a_vencido": movidos_vencidos}


def tiene_consentimiento(conn, contacto_id, finalidad):
    row = conn.execute(
        """SELECT 1 FROM petra_consentimientos
           WHERE contacto_id=%s AND finalidad=%s AND estado='otorgado'
           LIMIT 1""",
        (contacto_id, finalidad),
    ).fetchone()
    return row is not None


# =============================================================================
# Endpoints HTTP mínimos (list + estado) — el panel/paneles externos los usan
# =============================================================================


def _ok(data=None, **extra):
    out = {"ok": True}
    if data is not None:
        out["data"] = data
    out.update(extra)
    return jsonify(out)


def _rows_to_dicts(cursor, rows):
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in rows]


@petra_core_bp.get("/oportunidades")
def listar_oportunidades():
    estado = request.args.get("estado")
    contacto_id = request.args.get("contacto_id", type=int)
    limit = min(request.args.get("limit", 50, type=int), 500)
    sql = ["SELECT * FROM petra_oportunidades WHERE 1=1"]
    params = []
    if estado:
        sql.append("AND estado=%s"); params.append(estado)
    if contacto_id:
        sql.append("AND contacto_id=%s"); params.append(contacto_id)
    sql.append("ORDER BY updated_at DESC LIMIT %s"); params.append(limit)
    with _db._conexion_directa(timeout=10) as conn:
        cur = conn.execute(" ".join(sql), tuple(params))
        rows = cur.fetchall()
        return _ok(_rows_to_dicts(cur, rows))


@petra_core_bp.post("/oportunidades")
def crear_oportunidad_http():
    data = request.get_json(force=True) or {}
    if not data.get("contacto_id") or not data.get("tipo"):
        return jsonify({"ok": False, "error": "contacto_id y tipo requeridos"}), 400
    with _db._conexion_directa(timeout=10) as conn:
        oid = crear_oportunidad(
            conn,
            contacto_id=data["contacto_id"],
            tipo=data["tipo"],
            origen=data.get("origen"),
            inmueble_id=data.get("inmueble_id"),
            valor_cop=data.get("valor_cop"),
            origen_metadata=data.get("origen_metadata"),
            responsable_contacto_id=data.get("responsable_contacto_id"),
        )
    return _ok({"id": oid})


@petra_core_bp.post("/oportunidades/<int:oid>/estado")
def cambiar_estado_oportunidad_http(oid):
    data = request.get_json(force=True) or {}
    nuevo = data.get("estado")
    if not nuevo:
        return jsonify({"ok": False, "error": "estado requerido"}), 400
    with _db._conexion_directa(timeout=10) as conn:
        ok, msg = cambiar_estado_oportunidad(conn, oid, nuevo,
                                             motivo=data.get("motivo"),
                                             actor=data.get("actor"))
    status = 200 if ok else 400
    return jsonify({"ok": ok, "msg": msg}), status


@petra_core_bp.get("/casos")
def listar_casos():
    estado = request.args.get("estado")
    prioridad = request.args.get("prioridad")
    limit = min(request.args.get("limit", 50, type=int), 500)
    sql = ["SELECT * FROM petra_casos_soporte WHERE 1=1"]
    params = []
    if estado:
        sql.append("AND estado=%s"); params.append(estado)
    if prioridad:
        sql.append("AND prioridad=%s"); params.append(prioridad)
    sql.append("ORDER BY prioridad DESC, created_at ASC LIMIT %s")
    params.append(limit)
    with _db._conexion_directa(timeout=10) as conn:
        cur = conn.execute(" ".join(sql), tuple(params))
        rows = cur.fetchall()
        return _ok(_rows_to_dicts(cur, rows))


@petra_core_bp.get("/comisiones")
def listar_comisiones():
    agente_id = request.args.get("agente_id", type=int)
    estado = request.args.get("estado")
    limit = min(request.args.get("limit", 100, type=int), 500)
    sql = ["SELECT * FROM petra_comisiones WHERE 1=1"]
    params = []
    if agente_id:
        sql.append("AND agente_id=%s"); params.append(agente_id)
    if estado:
        sql.append("AND estado=%s"); params.append(estado)
    sql.append("ORDER BY created_at DESC LIMIT %s"); params.append(limit)
    with _db._conexion_directa(timeout=10) as conn:
        cur = conn.execute(" ".join(sql), tuple(params))
        rows = cur.fetchall()
        return _ok(_rows_to_dicts(cur, rows))


@petra_core_bp.get("/auditoria")
def listar_auditoria():
    entidad_tipo = request.args.get("entidad_tipo")
    entidad_id = request.args.get("entidad_id", type=int)
    limit = min(request.args.get("limit", 100, type=int), 500)
    sql = ["SELECT * FROM petra_auditoria_eventos WHERE 1=1"]
    params = []
    if entidad_tipo:
        sql.append("AND entidad_tipo=%s"); params.append(entidad_tipo)
    if entidad_id:
        sql.append("AND entidad_id=%s"); params.append(entidad_id)
    sql.append("ORDER BY created_at DESC LIMIT %s"); params.append(limit)
    with _db._conexion_directa(timeout=10) as conn:
        cur = conn.execute(" ".join(sql), tuple(params))
        rows = cur.fetchall()
        return _ok(_rows_to_dicts(cur, rows))


@petra_core_bp.get("/integracion_errores")
def listar_errores_integracion():
    estado = request.args.get("estado")
    proveedor = request.args.get("proveedor")
    limit = min(request.args.get("limit", 50, type=int), 500)
    sql = ["SELECT * FROM petra_integracion_errores WHERE 1=1"]
    params = []
    if estado:
        sql.append("AND estado=%s"); params.append(estado)
    if proveedor:
        sql.append("AND proveedor=%s"); params.append(proveedor)
    sql.append("ORDER BY created_at DESC LIMIT %s"); params.append(limit)
    with _db._conexion_directa(timeout=10) as conn:
        cur = conn.execute(" ".join(sql), tuple(params))
        return _ok(_rows_to_dicts(cur, cur.fetchall()))


@petra_core_bp.post("/integracion_errores/<int:eid>/resolver")
def resolver_error_http(eid):
    data = request.get_json(force=True) or {}
    with _db._conexion_directa(timeout=10) as conn:
        resolver_error_integracion(conn, eid, nota=data.get("nota"))
    return _ok({"id": eid, "resuelto": True})


@petra_core_bp.get("/agente-link")
def agente_link_publico():
    """Link público para campañas de reclutamiento de agentes Petra.
    Redirige a wa.me/<numero_petra> con mensaje pre-llenado + UTM.
    No requiere auth (es un link de marketing).

    Query params opcionales:
      utm_source, utm_campaign, utm_medium, utm_content
    """
    from urllib.parse import quote

    numero = (os.environ.get("WA_PILOT_NUMERO_E164") or "573001278631").lstrip("+")
    utm_source = request.args.get("utm_source", "directo")
    utm_campaign = request.args.get("utm_campaign", "agentes")
    utm_medium = request.args.get("utm_medium", "")

    # Mensaje limpio (sin tag visible). El origen UTM se registra en
    # petra_agente_link_hits (si existe); el detector usa keywords.
    msg = "Hola, quiero ser agente Petra."
    url = f"https://wa.me/{numero}?text={quote(msg)}"
    try:
        from app import db as _db
        with _db._conexion_directa(timeout=5) as conn:
            conn.execute(
                """INSERT INTO petra_agente_link_hits
                   (utm_source, utm_campaign, utm_medium, user_agent, ip)
                   VALUES (%s, %s, %s, %s, %s)""",
                (utm_source, utm_campaign, utm_medium,
                 (request.headers.get("User-Agent") or "")[:300],
                 (request.headers.get("X-Forwarded-For") or request.remote_addr or "")[:64]),
            )
    except Exception:
        log.exception("[agente-link] no se pudo registrar hit")
    from flask import redirect
    return redirect(url, code=302)


@petra_core_bp.post("/agentes/inscribir")
def inscribir_agente_http():
    """Entry point del Flujo 4 (link anuncio IG → quiero ser agente)."""
    data = request.get_json(force=True) or {}
    if not data.get("telefono"):
        return jsonify({"ok": False, "error": "telefono requerido"}), 400
    with _db._conexion_directa(timeout=10) as conn:
        cid, estado = inscribir_agente(
            conn,
            telefono=data["telefono"],
            nombre=data.get("nombre"),
            ciudad=data.get("ciudad"),
            num_edificios_conocidos=data.get("num_edificios_conocidos"),
            origen=data.get("origen", "link_recruiting"),
        )
    return _ok({"contacto_id": cid, "estado_agente": estado})


@petra_core_bp.post("/agentes/<int:cid>/estado")
def avanzar_agente_http(cid):
    data = request.get_json(force=True) or {}
    nuevo = data.get("estado")
    if not nuevo:
        return jsonify({"ok": False, "error": "estado requerido"}), 400
    with _db._conexion_directa(timeout=10) as conn:
        ok, msg = avanzar_estado_agente(conn, cid, nuevo,
                                        metadata_extra=data.get("metadata"))
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 400)


@petra_core_bp.get("/agentes")
def listar_agentes():
    estado = request.args.get("estado_agente")
    limit = min(request.args.get("limit", 100, type=int), 500)
    sql = """SELECT r.contacto_id, c.telefono, c.nombre,
                    r.metadata->>'estado_agente' AS estado_agente,
                    r.metadata->>'ciudad' AS ciudad,
                    r.metadata, r.updated_at
             FROM petra_contacto_roles r
             JOIN petra_contactos c ON c.id = r.contacto_id
             WHERE r.rol = 'agente_petra'"""
    params = []
    if estado:
        sql += " AND r.metadata->>'estado_agente' = %s"
        params.append(estado)
    sql += " ORDER BY r.updated_at DESC LIMIT %s"
    params.append(limit)
    with _db._conexion_directa(timeout=10) as conn:
        cur = conn.execute(sql, tuple(params))
        return _ok(_rows_to_dicts(cur, cur.fetchall()))


@petra_core_bp.get("/visitas")
def listar_visitas():
    estado = request.args.get("estado")
    agente_id = request.args.get("agente_id", type=int)
    inmueble_id = request.args.get("inmueble_id", type=int)
    limit = min(request.args.get("limit", 50, type=int), 500)
    sql = ["SELECT * FROM petra_visitas WHERE 1=1"]
    params = []
    if estado:
        sql.append("AND estado=%s"); params.append(estado)
    if agente_id:
        sql.append("AND agente_id=%s"); params.append(agente_id)
    if inmueble_id:
        sql.append("AND inmueble_id=%s"); params.append(inmueble_id)
    sql.append("ORDER BY programada_at DESC NULLS LAST LIMIT %s")
    params.append(limit)
    with _db._conexion_directa(timeout=10) as conn:
        cur = conn.execute(" ".join(sql), tuple(params))
        return _ok(_rows_to_dicts(cur, cur.fetchall()))


@petra_core_bp.post("/visitas")
def crear_visita_http():
    data = request.get_json(force=True) or {}
    if not data.get("tipo"):
        return jsonify({"ok": False, "error": "tipo requerido (comercial|entrega|recepcion|inspeccion)"}), 400
    with _db._conexion_directa(timeout=10) as conn:
        vid = crear_visita(
            conn,
            tipo=data["tipo"],
            inmueble_id=data.get("inmueble_id"),
            oportunidad_id=data.get("oportunidad_id"),
            agente_id=data.get("agente_id"),
            interesado_id=data.get("interesado_id"),
            programada_at=data.get("programada_at"),
            checklist=data.get("checklist"),
        )
    return _ok({"id": vid})


@petra_core_bp.post("/visitas/<int:vid>")
def actualizar_visita_http(vid):
    """Transiciona estado o agrega evidencias/observaciones/acta."""
    data = request.get_json(force=True) or {}
    sets, params = [], []
    nuevo_estado = data.get("estado")
    with _db._conexion_directa(timeout=10) as conn:
        if nuevo_estado:
            row = conn.execute("SELECT estado FROM petra_visitas WHERE id=%s", (vid,)).fetchone()
            if not row:
                return jsonify({"ok": False, "error": "no_existe"}), 404
            # Transiciones simples para visitas
            permitidas = {
                "programada": {"confirmada", "cancelada", "realizada", "no_show"},
                "confirmada": {"realizada", "cancelada", "no_show"},
                "realizada": set(),
                "cancelada": set(),
                "no_show": {"programada"},
            }
            if nuevo_estado not in permitidas.get(row[0], set()) and nuevo_estado != row[0]:
                return jsonify({"ok": False, "error": f"transicion_invalida {row[0]}->{nuevo_estado}"}), 400
            sets.append("estado=%s"); params.append(nuevo_estado)
        if data.get("observaciones"):
            sets.append("observaciones=%s"); params.append(data["observaciones"])
        if data.get("acta_url"):
            sets.append("acta_url=%s"); params.append(data["acta_url"])
        if sets:
            sets.append("updated_at=NOW()")
            params.append(vid)
            conn.execute(f"UPDATE petra_visitas SET {', '.join(sets)} WHERE id=%s", tuple(params))
        if data.get("evidencia"):
            agregar_evidencia_visita(conn, vid, data["evidencia"])
        audit(conn, "humano_panel", None, data.get("actor"),
              "actualizar_visita", "visita", vid, despues=data)
    return _ok({"id": vid, "actualizado": True})


@petra_core_bp.post("/casos/<int:cid>")
def actualizar_caso_http(cid):
    """PATCH-like: asignar responsable, registrar primera_respuesta, resolver.
    Body: {responsable_humano?, primera_respuesta?: bool, estado?, resolucion_nota?}
    """
    data = request.get_json(force=True) or {}
    sets, params = [], []
    nuevo_estado = data.get("estado")
    if data.get("responsable_humano"):
        sets.append("responsable_humano=%s"); params.append(data["responsable_humano"])
    if data.get("primera_respuesta"):
        sets.append("primera_respuesta_at=NOW()")
    if nuevo_estado:
        with _db._conexion_directa(timeout=5) as conn:
            row = conn.execute("SELECT estado FROM petra_casos_soporte WHERE id=%s", (cid,)).fetchone()
            if not row:
                return jsonify({"ok": False, "error": "no_existe"}), 404
            if not transicion_permitida(CASO_TRANSICIONES, row[0], nuevo_estado):
                return jsonify({"ok": False, "error": f"transicion_invalida {row[0]}->{nuevo_estado}"}), 400
        sets.append("estado=%s"); params.append(nuevo_estado)
        if nuevo_estado == "resuelto":
            sets.append("resuelto_at=NOW()")
            if data.get("resolucion_nota"):
                sets.append("resolucion_nota=%s"); params.append(data["resolucion_nota"])
    if not sets:
        return jsonify({"ok": False, "error": "nada que actualizar"}), 400
    sets.append("updated_at=NOW()")
    params.append(cid)
    with _db._conexion_directa(timeout=10) as conn:
        conn.execute(f"UPDATE petra_casos_soporte SET {', '.join(sets)} WHERE id=%s", tuple(params))
        audit(conn, "humano_panel", None, data.get("responsable_humano"),
              "actualizar_caso", "caso", cid, despues=data)
    return _ok({"id": cid, "actualizado": True})


AGENTE_PETRA_TRANSICIONES = {
    "lead": {"inscrito", "descartado"},
    "inscrito": {"en_capacitacion", "descartado"},
    "en_capacitacion": {"certificado", "descartado"},
    "certificado": {"kit_asignado", "descartado"},
    "kit_asignado": {"operativo", "descartado"},
    "operativo": {"pausado", "descartado"},
    "pausado": {"operativo", "descartado"},
    "descartado": set(),
}


def _rol_agente_metadata(conn, contacto_id):
    row = conn.execute(
        """SELECT metadata FROM petra_contacto_roles
           WHERE contacto_id=%s AND rol='agente_petra' LIMIT 1""",
        (contacto_id,),
    ).fetchone()
    return (row[0] if row else {}) or {}


def inscribir_agente(conn, telefono, nombre=None, ciudad=None,
                     num_edificios_conocidos=None, origen="link_recruiting"):
    """Crea contacto si falta y registra rol agente_petra con estado 'lead'
    (o 'inscrito' si ya trae datos mínimos). Idempotente por teléfono."""
    row = conn.execute(
        "SELECT id FROM petra_contactos WHERE telefono=%s", (telefono,),
    ).fetchone()
    if row:
        cid = row[0]
        if nombre:
            conn.execute(
                "UPDATE petra_contactos SET nombre=COALESCE(nombre,%s), updated_at=NOW() WHERE id=%s",
                (nombre, cid),
            )
    else:
        row = conn.execute(
            "INSERT INTO petra_contactos (telefono, nombre) VALUES (%s,%s) RETURNING id",
            (telefono, nombre),
        ).fetchone()
        cid = row[0]

    datos_min = bool(nombre and ciudad)
    estado_inicial = "inscrito" if datos_min else "lead"
    meta = {
        "ciudad": ciudad, "num_edificios_conocidos": num_edificios_conocidos,
        "origen": origen, "estado_agente": estado_inicial,
        "videos_vistos": 0, "examen_resultado": None,
        "kit_tracking": None, "edificios_activados": [],
    }
    conn.execute(
        """INSERT INTO petra_contacto_roles
           (contacto_id, rol, estado, metadata)
           VALUES (%s, 'agente_petra', 'activo', %s::jsonb)
           ON CONFLICT (contacto_id, rol, edificio_id) DO UPDATE SET
             estado='activo', updated_at=NOW()""",
        (cid, json.dumps(meta)),
    )
    audit(conn, "sistema", None, "petra_core", "inscribir_agente",
          "contacto", cid, despues={"estado_agente": estado_inicial})
    return cid, estado_inicial


def avanzar_estado_agente(conn, contacto_id, nuevo_estado, metadata_extra=None):
    """Transiciona el estado_agente dentro del rol agente_petra.
    metadata_extra se mergea en el JSONB del rol."""
    meta = _rol_agente_metadata(conn, contacto_id)
    actual = meta.get("estado_agente") or "lead"
    if nuevo_estado == actual:
        return False, "sin_cambios"
    if nuevo_estado not in AGENTE_PETRA_TRANSICIONES.get(actual, set()):
        return False, f"transicion_invalida {actual}->{nuevo_estado}"
    meta["estado_agente"] = nuevo_estado
    if metadata_extra:
        meta.update(metadata_extra)
    conn.execute(
        """UPDATE petra_contacto_roles
              SET metadata = %s::jsonb, updated_at = NOW()
            WHERE contacto_id=%s AND rol='agente_petra'""",
        (json.dumps(meta), contacto_id),
    )
    audit(conn, "sistema", None, "petra_core", "avanzar_agente",
          "contacto", contacto_id,
          antes={"estado_agente": actual},
          despues={"estado_agente": nuevo_estado, "extra": metadata_extra})
    return True, "ok"


def registrar_error_integracion(conn, proveedor, operacion, error_mensaje,
                                entidad_tipo=None, entidad_id=None,
                                payload=None, error_codigo=None,
                                max_intentos=5):
    """Flujo 13 — encola un error para reintento con backoff. Idempotente
    por (proveedor, operacion, entidad_tipo, entidad_id) activo."""
    try:
        row = conn.execute(
            """SELECT id, intentos FROM petra_integracion_errores
               WHERE proveedor=%s AND operacion=%s
                 AND COALESCE(entidad_tipo,'')=COALESCE(%s,'')
                 AND COALESCE(entidad_id,0)=COALESCE(%s,0)
                 AND estado IN ('pendiente_retry','reintentando')
               ORDER BY created_at DESC LIMIT 1""",
            (proveedor, operacion, entidad_tipo, entidad_id),
        ).fetchone()
        if row:
            # Incrementa intentos del existente; backoff exponencial 2^n min
            new_intentos = row[1] + 1
            delay_min = min(2 ** new_intentos, 240)  # tope 4h
            if new_intentos >= max_intentos:
                conn.execute(
                    """UPDATE petra_integracion_errores
                       SET estado='dead_letter', intentos=%s,
                           error_mensaje=%s, updated_at=NOW()
                       WHERE id=%s""",
                    (new_intentos, error_mensaje[:2000], row[0]),
                )
            else:
                conn.execute(
                    """UPDATE petra_integracion_errores
                       SET intentos=%s, error_mensaje=%s,
                           proximo_intento_at=NOW() + make_interval(mins=>%s),
                           estado='pendiente_retry', updated_at=NOW()
                       WHERE id=%s""",
                    (new_intentos, error_mensaje[:2000], delay_min, row[0]),
                )
            return row[0]
        row = conn.execute(
            """INSERT INTO petra_integracion_errores
               (proveedor, operacion, entidad_tipo, entidad_id, payload,
                error_mensaje, error_codigo, max_intentos,
                proximo_intento_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s, NOW() + interval '2 minutes')
               RETURNING id""",
            (proveedor, operacion, entidad_tipo, entidad_id,
             json.dumps(payload, ensure_ascii=False, default=str) if payload else None,
             (error_mensaje or "")[:2000], error_codigo, max_intentos),
        ).fetchone()
        audit(conn, "integracion_externa", None, proveedor,
              "error_integracion", "integracion_error", row[0],
              despues={"operacion": operacion, "codigo": error_codigo})
        return row[0]
    except Exception:
        log.exception("[petra_core] registrar_error_integracion fallo")
        return None


def resolver_error_integracion(conn, err_id, nota=None):
    conn.execute(
        """UPDATE petra_integracion_errores
           SET estado='resuelto', resuelto_at=NOW(), nota_humano=%s,
               updated_at=NOW()
           WHERE id=%s""",
        (nota, err_id),
    )


def reintentar_integracion(err_id, proveedor, operacion, payload, intentos):
    """Flujo 13 — ejecuta el reintento real de un error encolado.
    Devuelve (ok: bool, nota: str). No propaga excepciones.

    Rutea por (proveedor, operacion). Añadir handlers nuevos acá cuando
    se construyan integraciones adicionales (Sureti, Auco, ElevenLabs).
    El scheduler llama a esto y luego llama resolver/registrar según
    el resultado — así el estado backoff queda consistente."""
    try:
        if proveedor == "meta_whatsapp" and operacion == "enviar_mensaje":
            from app import whatsapp as _wa
            if not isinstance(payload, dict):
                return False, "payload inválido"
            # Reusa _dispatch; si falla registrará solo (OK — es idempotente
            # por (prov, op, entidad_tipo, entidad_id)).
            resumen = payload.get("text", {}).get("body") or payload.get("type", "msg")
            res = _wa._dispatch(payload, f"[retry #{intentos}] {resumen}", log_out=True)
            status = res.get("status")
            if isinstance(status, int) and 200 <= status < 300:
                return True, f"retry OK (status={status})"
            if status == "paused":
                return True, "destino pausado — se descarta retry"
            return False, f"retry falló (status={status}) body={res.get('body','')[:200]}"
        return False, f"sin handler para {proveedor}/{operacion}"
    except Exception as exc:
        log.exception("[petra_core] reintentar_integracion fallo")
        return False, f"excepción: {exc}"


def errores_pendientes_de_retry(conn, limit=50):
    rows = conn.execute(
        """SELECT id, proveedor, operacion, entidad_tipo, entidad_id,
                  payload, intentos
           FROM petra_integracion_errores
           WHERE estado='pendiente_retry' AND proximo_intento_at <= NOW()
           ORDER BY proximo_intento_at ASC LIMIT %s""",
        (limit,),
    ).fetchall()
    return rows


def puede_enviar_remarketing(conn, contacto_id, regla, dentro_dias=7,
                             requiere_consentimiento="comunicaciones_comerciales"):
    """Gatekeeper central (Flujo 8). Devuelve (ok: bool, motivo: str).
    Verifica en orden:
      1) Consentimiento activo para la finalidad.
      2) No es cliente activo (ya le vendimos).
      3) No se le disparó esta misma regla en los últimos N días.
      4) No tiene un caso de soporte abierto (no es momento comercial).
    """
    if requiere_consentimiento:
        if not tiene_consentimiento(conn, contacto_id, requiere_consentimiento):
            return False, "sin_consentimiento"
    row = conn.execute(
        """SELECT 1 FROM petra_contacto_roles
           WHERE contacto_id=%s
             AND rol IN ('cliente_credito','cliente_servicios')
             AND estado='activo' LIMIT 1""",
        (contacto_id,),
    ).fetchone()
    if row:
        return False, "cliente_activo"
    row = conn.execute(
        """SELECT 1 FROM petra_remarketing_eventos
           WHERE contacto_id=%s AND regla=%s
             AND disparado_at > NOW() - make_interval(days => %s)
           LIMIT 1""",
        (contacto_id, regla, dentro_dias),
    ).fetchone()
    if row:
        return False, "ya_disparado_recientemente"
    row = conn.execute(
        """SELECT 1 FROM petra_casos_soporte
           WHERE contacto_id=%s
             AND estado IN ('abierto','en_gestion','escalado') LIMIT 1""",
        (contacto_id,),
    ).fetchone()
    if row:
        return False, "caso_soporte_abierto"
    return True, "ok"


def visitas_proximas_a_recordar(conn, horas_antes=24):
    """Devuelve visitas 'programada'/'confirmada' que están próximas y a las
    que no se les ha mandado recordatorio aún (checklist.recordada=true).
    No manda nada: encola el recordatorio en petra_outbound_queue."""
    rows = conn.execute(
        """SELECT v.id, v.programada_at, v.tipo, v.agente_id, v.interesado_id,
                  v.inmueble_id, i.torre, i.piso, e.nombre
             FROM petra_visitas v
        LEFT JOIN petra_inmuebles i ON i.id = v.inmueble_id
        LEFT JOIN petra_edificios  e ON e.id = i.edificio_id
            WHERE v.estado IN ('programada','confirmada')
              AND v.programada_at IS NOT NULL
              AND v.programada_at BETWEEN NOW() AND NOW() + (%s || ' hours')::interval
              AND COALESCE((v.checklist->>'recordada')::bool, false) = false""",
        (horas_antes,),
    ).fetchall()
    encoladas = 0
    for (vid, pa, tipo, agente_id, interesado_id, inm_id, torre, piso, edif) in rows:
        cuerpo = (f"Recordatorio: visita {tipo} el "
                  f"{pa.strftime('%Y-%m-%d %H:%M') if pa else '?'} "
                  f"en {edif or 'edificio'} T{torre or '?'} P{piso or '?'}. "
                  f"¿Confirmas?")
        payload = {
            "tipo_mensaje": "buttons",
            "texto": cuerpo[:1024],
            "buttons": [
                {"id": f"VISITA_SI_{vid}", "title": "Sí, confirmo"},
                {"id": f"VISITA_REAGENDAR_{vid}", "title": "Reagendar"},
                {"id": f"VISITA_NO_{vid}", "title": "Cancelar"},
            ],
        }
        for cid in filter(None, [agente_id, interesado_id]):
            tel = conn.execute(
                "SELECT telefono FROM petra_contactos WHERE id=%s", (cid,),
            ).fetchone()
            if tel and tel[0]:
                conn.execute(
                    """INSERT INTO petra_outbound_queue
                       (contacto_id, telefono, tipo, payload, origen, estado)
                       VALUES (%s, %s, 'buttons', %s::jsonb,
                               'recordatorio_visita', 'pendiente')""",
                    (cid, tel[0],
                     json.dumps(payload, ensure_ascii=False)),
                )
                encoladas += 1
        conn.execute(
            """UPDATE petra_visitas
                  SET checklist = jsonb_set(COALESCE(checklist,'{}'::jsonb),
                                            '{recordada}', 'true'::jsonb)
                WHERE id=%s""", (vid,),
        )
    return encoladas


def escalar_casos_vencidos(conn):
    """Flujo 7 — pasa a 'escalado' los casos cuyo SLA de primera respuesta
    ya venció y nadie respondió. Devuelve lista de IDs escalados."""
    rows = conn.execute(
        """UPDATE petra_casos_soporte
              SET estado = 'escalado', updated_at = NOW()
            WHERE estado IN ('abierto','en_gestion','pendiente_cliente')
              AND primera_respuesta_at IS NULL
              AND sla_primera_respuesta_at < NOW()
            RETURNING id, contacto_id, categoria, prioridad"""
    ).fetchall()
    for cid, con_id, cat, prio in rows:
        audit(conn, "sistema", None, "scheduler", "escalar_caso",
              "caso", cid, metadata={"motivo": "sla_vencido", "categoria": cat, "prioridad": prio})
    return [r[0] for r in rows]


@petra_core_bp.post("/control_calidad")
def control_calidad_http():
    dias = request.args.get("dias_vencimiento", 60, type=int)
    with _db._conexion_directa(timeout=30) as conn:
        out = control_calidad_inventario(conn, dias_vencimiento=dias)
    return _ok(out)


@petra_core_bp.post("/consentimientos/otorgar")
def otorgar_consentimiento_http():
    data = request.get_json(force=True) or {}
    if not data.get("contacto_id") or not data.get("finalidad"):
        return jsonify({"ok": False, "error": "contacto_id y finalidad requeridos"}), 400
    with _db._conexion_directa(timeout=10) as conn:
        cid = otorgar_consentimiento(
            conn,
            contacto_id=data["contacto_id"],
            finalidad=data["finalidad"],
            canal=data.get("canal", "whatsapp"),
            evidencia=data.get("evidencia"),
            version_politica=data.get("version_politica"),
        )
    return _ok({"id": cid})


# =============================================================================
# Flujo 4 — Importación de contactos legacy (contactos → petra_contactos)
# =============================================================================
@petra_core_bp.post("/importar/contactos-legacy")
def importar_contactos_legacy():
    """Copia contactos del esquema viejo a petra_contactos con auditoría.

    - Idempotente: NO sobre-escribe filas existentes (dedup por teléfono).
    - Opt-out: contactos.no_contactar=TRUE → petra_contactos.opt_out=TRUE
      con motivo 'legacy_no_contactar'.
    - Audita cada inserción como integracion_externa/import_legacy.
    - Protección: requiere Authorization: Bearer PETRA_LLM_TOKEN si está
      seteado. ?dry_run=1 solo cuenta sin escribir.
    """
    required = (os.environ.get("PETRA_LLM_TOKEN") or "").strip()
    if required:
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        token = token or (request.args.get("token") or "").strip()
        if token != required:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

    dry = (request.args.get("dry_run") or "").lower() in ("1", "true", "yes")
    limit = request.args.get("limit", type=int)

    with _db._conexion_directa(timeout=60) as conn:
        sql = """SELECT telefono, nombre, no_contactar
                 FROM contactos
                 WHERE telefono IS NOT NULL AND telefono <> ''
                 ORDER BY id ASC"""
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql).fetchall()

        total = len(rows)
        creados = 0
        actualizados_opt_out = 0
        ya_existian = 0
        errores = 0

        for (tel, nombre, no_contactar) in rows:
            tel_norm = re.sub(r"\D", "", str(tel) or "")
            if not tel_norm or len(tel_norm) < 10:
                errores += 1
                continue
            try:
                existing = conn.execute(
                    "SELECT id, opt_out FROM petra_contactos WHERE telefono=%s",
                    (tel_norm,),
                ).fetchone()
                if existing:
                    cid, cur_opt = existing
                    if no_contactar and not cur_opt and not dry:
                        conn.execute(
                            """UPDATE petra_contactos
                               SET opt_out=TRUE,
                                   opt_out_motivo=COALESCE(opt_out_motivo,'legacy_no_contactar'),
                                   opt_out_at=COALESCE(opt_out_at, NOW()),
                                   updated_at=NOW()
                               WHERE id=%s""",
                            (cid,),
                        )
                        audit(conn, "integracion_externa", None, "import_legacy",
                              "opt_out_propagado", "contacto", cid,
                              metadata={"telefono": tel_norm})
                        actualizados_opt_out += 1
                    else:
                        ya_existian += 1
                    continue
                if dry:
                    creados += 1
                    continue
                new_row = conn.execute(
                    """INSERT INTO petra_contactos
                       (telefono, nombre, opt_out, opt_out_motivo, opt_out_at)
                       VALUES (%s, %s, %s, %s,
                               CASE WHEN %s THEN NOW() ELSE NULL END)
                       RETURNING id""",
                    (tel_norm, (nombre or "").strip() or None,
                     bool(no_contactar),
                     "legacy_no_contactar" if no_contactar else None,
                     bool(no_contactar)),
                ).fetchone()
                audit(conn, "integracion_externa", None, "import_legacy",
                      "contacto_creado", "contacto", new_row[0],
                      metadata={"telefono": tel_norm,
                                "opt_out": bool(no_contactar)})
                creados += 1
            except Exception:
                log.exception("[import_legacy] fallo tel=%s", tel_norm)
                errores += 1

    return _ok({
        "total_leidos": total,
        "creados": creados,
        "actualizados_opt_out": actualizados_opt_out,
        "ya_existian": ya_existian,
        "errores": errores,
        "dry_run": dry,
    })


# =============================================================================
# FASE A — Agente Petra operativo
# =============================================================================

def _rol_agente_metadata(conn, contacto_id):
    """Devuelve el JSONB metadata del rol agente_petra, o {} si no existe."""
    row = conn.execute(
        """SELECT metadata FROM petra_contacto_roles
           WHERE contacto_id=%s AND rol='agente_petra' LIMIT 1""",
        (contacto_id,),
    ).fetchone()
    return row[0] if row and row[0] else {}


def iniciar_capacitacion(conn, contacto_id):
    """A1. Encola los videos activos y transiciona el agente a en_capacitacion.
    Devuelve: lista de (contenido_id, titulo, url, orden)."""
    rows = conn.execute(
        """SELECT id, titulo, url, orden FROM petra_contenidos
           WHERE tipo='video_capacitacion' AND activo=TRUE
           ORDER BY orden ASC, created_at ASC"""
    ).fetchall()
    for (vid, _t, _u, _o) in rows:
        conn.execute(
            """INSERT INTO petra_capacitacion_progreso (contacto_id, contenido_id)
               VALUES (%s,%s) ON CONFLICT (contacto_id, contenido_id) DO NOTHING""",
            (contacto_id, vid),
        )
    avanzar_estado_agente(conn, contacto_id, "en_capacitacion")
    return [{"id": r[0], "titulo": r[1], "url": r[2], "orden": r[3]} for r in rows]


def siguiente_video_pendiente(conn, contacto_id):
    """A1. Devuelve el próximo video no visto por este agente, o None."""
    row = conn.execute(
        """SELECT c.id, c.titulo, c.url, c.descripcion, c.duracion_seg, p.id AS prog_id
           FROM petra_contenidos c
           LEFT JOIN petra_capacitacion_progreso p
             ON p.contenido_id = c.id AND p.contacto_id = %s
           WHERE c.tipo='video_capacitacion' AND c.activo=TRUE
             AND (p.visto_at IS NULL)
           ORDER BY c.orden ASC, c.created_at ASC LIMIT 1""",
        (contacto_id,),
    ).fetchone()
    if not row:
        return None
    return {"contenido_id": row[0], "titulo": row[1], "url": row[2],
            "descripcion": row[3], "duracion_seg": row[4], "progreso_id": row[5]}


def marcar_video_enviado(conn, contacto_id, contenido_id):
    conn.execute(
        """INSERT INTO petra_capacitacion_progreso (contacto_id, contenido_id, enviado_at)
           VALUES (%s,%s,NOW())
           ON CONFLICT (contacto_id, contenido_id)
             DO UPDATE SET enviado_at = COALESCE(petra_capacitacion_progreso.enviado_at, NOW())""",
        (contacto_id, contenido_id),
    )


def marcar_video_visto(conn, contacto_id, contenido_id, comentario=None):
    """A1. Marca visto. Si fue el último, promueve a 'certificado'."""
    conn.execute(
        """INSERT INTO petra_capacitacion_progreso (contacto_id, contenido_id, visto_at, comentario)
           VALUES (%s,%s,NOW(),%s)
           ON CONFLICT (contacto_id, contenido_id) DO UPDATE
             SET visto_at = COALESCE(petra_capacitacion_progreso.visto_at, NOW()),
                 comentario = COALESCE(EXCLUDED.comentario, petra_capacitacion_progreso.comentario)""",
        (contacto_id, contenido_id, comentario),
    )
    # Actualiza contador en el rol
    meta = _rol_agente_metadata(conn, contacto_id)
    vistos = conn.execute(
        """SELECT COUNT(*) FROM petra_capacitacion_progreso
           WHERE contacto_id=%s AND visto_at IS NOT NULL""",
        (contacto_id,),
    ).fetchone()[0]
    total_activos = conn.execute(
        "SELECT COUNT(*) FROM petra_contenidos WHERE tipo='video_capacitacion' AND activo=TRUE"
    ).fetchone()[0]
    meta["videos_vistos"] = vistos
    conn.execute(
        """UPDATE petra_contacto_roles SET metadata=%s::jsonb, updated_at=NOW()
           WHERE contacto_id=%s AND rol='agente_petra'""",
        (json.dumps(meta), contacto_id),
    )
    if total_activos > 0 and vistos >= total_activos:
        avanzar_estado_agente(conn, contacto_id, "certificado")
        return True, "certificado"
    return False, f"vistos {vistos}/{total_activos}"


# A2. Autorizaciones del administrador

def crear_autorizacion_admin(conn, agente_id, conjunto_nombre, edificio_id=None, nota=None):
    row = conn.execute(
        """INSERT INTO petra_autorizaciones_admin
           (agente_id, conjunto_nombre, edificio_id, nota, estado)
           VALUES (%s,%s,%s,%s,'pendiente')
           RETURNING id""",
        (agente_id, conjunto_nombre, edificio_id, nota),
    ).fetchone()
    audit(conn, "sistema", None, "petra_core", "crear_autorizacion",
          "autorizacion_admin", row[0],
          despues={"agente_id": agente_id, "conjunto": conjunto_nombre})
    return row[0]


def marcar_kit_autorizacion_enviado(conn, autorizacion_id):
    conn.execute(
        """UPDATE petra_autorizaciones_admin
           SET estado='kit_enviado', kit_enviado_at=NOW(), updated_at=NOW()
           WHERE id=%s AND estado='pendiente'""",
        (autorizacion_id,),
    )


def actualizar_estado_autorizacion(conn, autorizacion_id, estado, nota=None):
    """Transiciones: pendiente → kit_enviado → en_gestion → aprobada|rechazada."""
    valid = {"pendiente", "kit_enviado", "en_gestion", "aprobada", "rechazada", "descartada"}
    if estado not in valid:
        return False, "estado invalido"
    extra = ", aprobada_at=NOW()" if estado == "aprobada" else ""
    conn.execute(
        f"""UPDATE petra_autorizaciones_admin
           SET estado=%s, nota=COALESCE(%s, nota), updated_at=NOW(){extra}
           WHERE id=%s""",
        (estado, nota, autorizacion_id),
    )
    return True, "ok"


# A3. Generación de QR del edificio

def generar_qr_png_bytes(url):
    """Genera un PNG del QR y devuelve bytes."""
    import qrcode
    import io
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


def asegurar_edificio_para_agente(conn, agente_id, conjunto_nombre, ciudad=None):
    """Crea petra_edificios si no existe, usa qr_slug derivado del nombre + agente."""
    slug_base = re.sub(r"[^a-z0-9]+", "-",
                       (conjunto_nombre or "edificio").lower()).strip("-")[:40]
    slug = f"{slug_base}-{agente_id}"
    row = conn.execute(
        "SELECT id FROM petra_edificios WHERE qr_slug=%s", (slug,),
    ).fetchone()
    if row:
        return row[0], slug
    row = conn.execute(
        """INSERT INTO petra_edificios (nombre, ciudad, qr_slug, activo)
           VALUES (%s,%s,%s,TRUE) RETURNING id""",
        (conjunto_nombre, ciudad, slug),
    ).fetchone()
    return row[0], slug


# A5. Notificación al agente cuando se agenda una visita

def encolar_notificacion_visita_agente(conn, visita_id):
    """Encola botones de confirmación al agente de la visita."""
    row = conn.execute(
        """SELECT v.id, v.programada_at, v.tipo, v.agente_id,
                  i.torre, i.piso, e.nombre, c.telefono
             FROM petra_visitas v
        LEFT JOIN petra_inmuebles i ON i.id = v.inmueble_id
        LEFT JOIN petra_edificios  e ON e.id = i.edificio_id
             JOIN petra_contactos  c ON c.id = v.agente_id
            WHERE v.id = %s""",
        (visita_id,),
    ).fetchone()
    if not row:
        return False
    (vid, pa, tipo, agente_id, torre, piso, edif, tel) = row
    cuerpo = (f"Nueva visita {tipo} agendada el "
              f"{pa.strftime('%Y-%m-%d %H:%M') if pa else '?'} "
              f"en {edif or 'edificio'} T{torre or '?'} P{piso or '?'}. "
              f"¿Puedes asistir?")
    payload = {
        "tipo_mensaje": "buttons",
        "texto": cuerpo[:1024],
        "buttons": [
            {"id": f"VISITA_AGENTE_SI_{vid}", "title": "Sí, asisto"},
            {"id": f"VISITA_AGENTE_REAGENDAR_{vid}", "title": "Reagendar"},
            {"id": f"VISITA_AGENTE_NO_{vid}", "title": "No puedo"},
        ],
    }
    conn.execute(
        """INSERT INTO petra_outbound_queue
           (contacto_id, telefono, tipo, payload, origen, estado)
           VALUES (%s,%s,'buttons',%s::jsonb,'notificacion_visita_agente','pendiente')""",
        (agente_id, tel, json.dumps(payload, ensure_ascii=False)),
    )
    return True


# =============================================================================
# FASE B — Mandatos (venta / arriendo / administración)
# =============================================================================

def registrar_mandato(conn, contacto_id, tipo, inmueble_id=None,
                     comision_pct=None, canon_acordado=None,
                     exclusivo=False, plazo_meses=None, nota=None):
    """Crea un petra_mandato en estado 'propuesto' y agrega el rol."""
    if tipo not in ("venta", "arriendo", "administracion"):
        return None, "tipo invalido"
    row = conn.execute(
        """INSERT INTO petra_mandatos
           (contacto_id, inmueble_id, tipo, comision_pct, canon_acordado,
            exclusivo, plazo_meses, nota, estado)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'propuesto')
           RETURNING id""",
        (contacto_id, inmueble_id, tipo, comision_pct, canon_acordado,
         bool(exclusivo), plazo_meses, nota),
    ).fetchone()
    rol = {
        "venta": "propietario_vendedor",
        "arriendo": "propietario_arrendador",
        "administracion": "propietario_administrado",
    }[tipo]
    conn.execute(
        """INSERT INTO petra_contacto_roles (contacto_id, rol, estado, metadata)
           VALUES (%s,%s,'activo','{}'::jsonb)
           ON CONFLICT (contacto_id, rol, edificio_id) DO UPDATE SET
             estado='activo', updated_at=NOW()""",
        (contacto_id, rol),
    )
    audit(conn, "sistema", None, "petra_core", "registrar_mandato",
          "mandato", row[0],
          despues={"contacto_id": contacto_id, "tipo": tipo})
    return row[0], "ok"


def firmar_mandato(conn, mandato_id, auco_doc_id=None):
    conn.execute(
        """UPDATE petra_mandatos
           SET estado='firmado', firmado_at=NOW(),
               auco_doc_id=COALESCE(%s, auco_doc_id),
               updated_at=NOW()
           WHERE id=%s""",
        (auco_doc_id, mandato_id),
    )


def crear_administracion_arriendo(conn, mandato_id, propietario_id,
                                   inmueble_id, canon, dia_corte=5,
                                   inquilino_tel=None, inquilino_nombre=None,
                                   cuenta_bancaria=None):
    row = conn.execute(
        """INSERT INTO petra_administraciones_arriendo
           (mandato_id, propietario_id, inmueble_id, inquilino_tel,
            inquilino_nombre, canon, dia_corte, cuenta_bancaria, estado)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'activa') RETURNING id""",
        (mandato_id, propietario_id, inmueble_id, inquilino_tel,
         inquilino_nombre, canon, dia_corte, cuenta_bancaria),
    ).fetchone()
    audit(conn, "sistema", None, "petra_core", "crear_admin_arriendo",
          "administracion", row[0], despues={"canon": canon, "dia_corte": dia_corte})
    return row[0]


# =============================================================================
# FASE C — Ofertas, slots de visita, postulaciones
# =============================================================================

def proponer_slots_visita(conn, oportunidad_id, slots_iso, propuesto_por="panel"):
    """slots_iso: lista de strings ISO8601. Crea una fila en petra_slots_visita."""
    row = conn.execute(
        """INSERT INTO petra_slots_visita (oportunidad_id, slots, propuesto_por, estado)
           VALUES (%s, %s::jsonb, %s, 'propuesto') RETURNING id""",
        (oportunidad_id, json.dumps(slots_iso), propuesto_por),
    ).fetchone()
    return row[0]


def confirmar_slot_visita(conn, slot_row_id, elegido_idx):
    row = conn.execute(
        """UPDATE petra_slots_visita
           SET elegido_idx=%s, elegido_at=NOW(), estado='confirmado', updated_at=NOW()
           WHERE id=%s RETURNING oportunidad_id, slots""",
        (elegido_idx, slot_row_id),
    ).fetchone()
    return row


def registrar_oferta(conn, oportunidad_id, interesado_id, monto, condiciones=None):
    row = conn.execute(
        """INSERT INTO petra_ofertas (oportunidad_id, interesado_id, monto, condiciones, estado)
           VALUES (%s,%s,%s,%s,'presentada') RETURNING id""",
        (oportunidad_id, interesado_id, monto, condiciones),
    ).fetchone()
    audit(conn, "sistema", None, "petra_core", "registrar_oferta",
          "oferta", row[0], despues={"monto": monto})
    return row[0]


def responder_oferta(conn, oferta_id, estado, respuesta=None):
    """estado: aceptada | rechazada | contraofertada."""
    if estado not in ("aceptada", "rechazada", "contraofertada", "retirada"):
        return False, "estado invalido"
    conn.execute(
        """UPDATE petra_ofertas
           SET estado=%s, respuesta_propietario=COALESCE(%s, respuesta_propietario),
               respondida_at=NOW(), updated_at=NOW()
           WHERE id=%s""",
        (estado, respuesta, oferta_id),
    )
    return True, "ok"


def registrar_postulacion_arriendo(conn, interesado_id, inmueble_id=None,
                                    oportunidad_id=None, codeudor_nombre=None,
                                    codeudor_tel=None, ingresos_decl=None,
                                    docs=None):
    row = conn.execute(
        """INSERT INTO petra_postulaciones_arriendo
           (interesado_id, inmueble_id, oportunidad_id, codeudor_nombre,
            codeudor_tel, ingresos_decl, docs_entregados, estado)
           VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,'recibida') RETURNING id""",
        (interesado_id, inmueble_id, oportunidad_id, codeudor_nombre,
         codeudor_tel, ingresos_decl, json.dumps(docs or [])),
    ).fetchone()
    return row[0]


# =============================================================================
# FASE D — Pagos mensuales de arriendo
# =============================================================================

def generar_pagos_mes(conn, mes_anio=None):
    """Para cada administración activa, crea la fila de pago del mes si no existe.
    mes_anio: 'YYYY-MM' (default: mes actual UTC)."""
    import datetime as _dt
    if not mes_anio:
        mes_anio = _dt.datetime.utcnow().strftime("%Y-%m")
    rows = conn.execute(
        "SELECT id, canon FROM petra_administraciones_arriendo WHERE estado='activa'"
    ).fetchall()
    creados = 0
    for (aid, canon) in rows:
        try:
            conn.execute(
                """INSERT INTO petra_pagos_arriendo
                   (administracion_id, mes_anio, monto_esperado, estado)
                   VALUES (%s,%s,%s,'pendiente')
                   ON CONFLICT (administracion_id, mes_anio) DO NOTHING""",
                (aid, mes_anio, canon),
            )
            creados += 1
        except Exception:
            log.exception("[petra_core] generar_pagos_mes fallo admin=%s", aid)
    return {"mes": mes_anio, "creados": creados, "admins_activas": len(rows)}


def reportar_pago(conn, pago_id, monto_pagado, comprobante_url=None, nota=None):
    estado = "pagado" if monto_pagado else "pendiente"
    conn.execute(
        """UPDATE petra_pagos_arriendo
           SET monto_pagado=%s, estado=%s, pagado_at=NOW(),
               comprobante_url=COALESCE(%s, comprobante_url),
               nota=COALESCE(%s, nota), updated_at=NOW()
           WHERE id=%s""",
        (monto_pagado, estado, comprobante_url, nota, pago_id),
    )


# =============================================================================
# Dashboard pública (readonly) por token
# =============================================================================
import secrets as _secrets


def crear_dashboard_token(conn, contacto_id, rol_vista):
    """Crea un token de 32 chars para dashboard readonly. Reusa si ya hay activo."""
    if rol_vista not in ("agente", "propietario"):
        return None
    existing = conn.execute(
        """SELECT token FROM petra_dashboard_tokens
           WHERE contacto_id=%s AND rol_vista=%s AND activo=TRUE LIMIT 1""",
        (contacto_id, rol_vista),
    ).fetchone()
    if existing:
        return existing[0]
    tok = _secrets.token_urlsafe(24)
    conn.execute(
        """INSERT INTO petra_dashboard_tokens (token, contacto_id, rol_vista)
           VALUES (%s,%s,%s)""",
        (tok, contacto_id, rol_vista),
    )
    return tok


@petra_core_bp.get("/edificios/<int:edif_id>/qr.png")
def servir_qr_edificio(edif_id):
    """Devuelve el PNG del QR del edificio. URL pública para imprimir."""
    from flask import send_file
    import io
    with _db._conexion_directa(timeout=5) as conn:
        row = conn.execute(
            "SELECT qr_slug FROM petra_edificios WHERE id=%s", (edif_id,),
        ).fetchone()
    if not row:
        return jsonify({"error": "edificio no encontrado"}), 404
    slug = row[0]
    base = os.environ.get("WA_PILOT_PHONE_NUMBER", "").lstrip("+")
    wa_url = f"https://wa.me/{base}?text=Hola%20quiero%20publicar%20en%20{slug}" if base \
             else f"https://petra-inmuebles.onrender.com/petra/qr/{slug}"
    png = generar_qr_png_bytes(wa_url)
    return send_file(io.BytesIO(png), mimetype="image/png",
                     as_attachment=False,
                     download_name=f"qr-{slug}.png")
