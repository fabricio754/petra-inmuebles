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
