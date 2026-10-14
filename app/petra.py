"""PETRA plataforma inmobiliaria — Sprint 1.

Endpoints para edificios, contactos (multi-rol) e inmuebles del inventario
por conjunto. Base del flujo QR portería (publicar / ver ventas / ver
arriendos). Aislado del esquema legacy — todo bajo prefijo petra_*.

Auth: header `X-Tools-Token` contra env `TOOLS_API_TOKEN` (mismo que /tools).

Sprint 1 scope:
  - CRUD edificios
  - CRUD contactos + roles (sin lógica de router WA todavía)
  - CRUD inmuebles con transiciones de estado seguras
NO incluye todavía: router WA inbound, state machine del chat publicar,
matching engine, remarketing. Esos van en Sprints 2 y 3.
"""
import logging
import os
import re
from functools import wraps

from flask import Blueprint, jsonify, request

from app import db as _db

log = logging.getLogger("petra")

petra_bp = Blueprint("petra", __name__, url_prefix="/petra")

_TEL_RE = re.compile(r"^\+?\d{10,15}$")
_TEL_STRIP_RE = re.compile(r"[\s\-\.]+")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,63}$")

_INMUEBLE_TIPO_VALID = {"apartamento", "casa", "local", "oficina", "habitacion", "parqueadero"}
_INMUEBLE_MODALIDAD_VALID = {"venta", "arriendo", "ambas"}
_INMUEBLE_ESTADO_VALID = {"borrador", "activo", "pausado", "vendido", "arrendado", "eliminado"}
_INMUEBLE_CIERRE_VALID = {"vendido", "arrendado", "eliminado"}

_ROLES_VALID = {
    "interesado", "anunciante", "lead_credito", "cliente_credito",
    "agente_petra", "cliente_servicios", "prospecto_servicios",
}


def _norm_tel(raw):
    if raw is None:
        return None
    s = _TEL_STRIP_RE.sub("", str(raw))
    return s if _TEL_RE.match(s) else None


def _num(raw):
    if raw is None or raw == "":
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def _bool(raw):
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in ("true", "1", "si", "sí", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None


def _int_pos(raw, minv=1, maxv=None):
    if raw is None or raw == "":
        return None
    try:
        v = int(raw)
        if v < minv:
            return "invalid"
        if maxv is not None and v > maxv:
            return "invalid"
        return v
    except (ValueError, TypeError):
        return "invalid"


def _require_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = os.environ.get("TOOLS_API_TOKEN", "")
        if not token or request.headers.get("X-Tools-Token") != token:
            return jsonify({"error": "no_autorizado"}), 401
        return fn(*args, **kwargs)
    return wrapper


def _row_to_dict(row, cols):
    d = dict(zip(cols, row))
    for k, v in list(d.items()):
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif isinstance(v, (int, float)) or v is None or isinstance(v, (str, bool, dict, list)):
            continue
        else:
            d[k] = float(v) if hasattr(v, "__float__") else str(v)
    return d


# ============================================================================
# EDIFICIOS
# ============================================================================

_EDIF_COLS = (
    "id", "nombre", "ciudad", "direccion", "barrio", "estrato",
    "qr_slug", "agente_petra_contacto_id", "activo",
    "created_at", "updated_at",
)
_EDIF_SELECT = ", ".join(_EDIF_COLS)


@petra_bp.post("/edificios")
@_require_token
def crear_edificio():
    body = request.get_json(silent=True) or {}
    nombre = (body.get("nombre") or "").strip()
    ciudad = (body.get("ciudad") or "").strip()
    if not nombre or not ciudad:
        return jsonify({"error": "nombre y ciudad requeridos"}), 400

    direccion = (body.get("direccion") or "").strip() or None
    barrio = (body.get("barrio") or "").strip() or None
    estrato = _int_pos(body.get("estrato"), 1, 6)
    if estrato == "invalid":
        return jsonify({"error": "estrato invalido (1-6)"}), 400

    qr_slug = body.get("qr_slug")
    if qr_slug is not None:
        qr_slug = str(qr_slug).strip().lower() or None
        if qr_slug and not _SLUG_RE.match(qr_slug):
            return jsonify({"error": "qr_slug invalido (a-z, 0-9, guiones; 2-64 chars)"}), 400

    agente_id = _int_pos(body.get("agente_petra_contacto_id"))
    if agente_id == "invalid":
        return jsonify({"error": "agente_petra_contacto_id invalido"}), 400

    log.info("[petra] POST /edificios nombre=%s ciudad=%s", nombre, ciudad)
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                f"INSERT INTO petra_edificios "
                f"(nombre, ciudad, direccion, barrio, estrato, qr_slug, agente_petra_contacto_id) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s) "
                f"RETURNING {_EDIF_SELECT}",
                (nombre, ciudad, direccion, barrio, estrato, qr_slug, agente_id),
            ).fetchone()
    except Exception as exc:
        log.exception("[petra] /edificios fallo")
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            return jsonify({"error": "qr_slug ya existe"}), 409
        return jsonify({"error": str(exc)}), 500
    return jsonify(_row_to_dict(row, _EDIF_COLS)), 200


@petra_bp.get("/edificios/<int:edificio_id>")
@_require_token
def obtener_edificio(edificio_id):
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                f"SELECT {_EDIF_SELECT} FROM petra_edificios WHERE id = %s",
                (edificio_id,),
            ).fetchone()
    except Exception as exc:
        log.exception("[petra] GET /edificios/%s fallo", edificio_id)
        return jsonify({"error": str(exc)}), 500
    if not row:
        return jsonify({"error": "no encontrado"}), 404
    return jsonify(_row_to_dict(row, _EDIF_COLS)), 200


@petra_bp.get("/edificios/by-slug/<slug>")
@_require_token
def obtener_edificio_por_slug(slug):
    slug_norm = (slug or "").strip().lower()
    if not _SLUG_RE.match(slug_norm):
        return jsonify({"error": "slug invalido"}), 400
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                f"SELECT {_EDIF_SELECT} FROM petra_edificios WHERE qr_slug = %s",
                (slug_norm,),
            ).fetchone()
    except Exception as exc:
        log.exception("[petra] GET /edificios/by-slug/%s fallo", slug_norm)
        return jsonify({"error": str(exc)}), 500
    if not row:
        return jsonify({"error": "no encontrado"}), 404
    return jsonify(_row_to_dict(row, _EDIF_COLS)), 200


# ============================================================================
# CONTACTOS + ROLES (modelo multi-rol)
# ============================================================================

_CONT_COLS = (
    "id", "telefono", "nombre", "email", "opt_out", "opt_out_motivo",
    "opt_out_at", "created_at", "updated_at",
)
_CONT_SELECT = ", ".join(_CONT_COLS)

_ROL_COLS = (
    "id", "contacto_id", "rol", "estado", "edificio_id", "metadata",
    "estado_desde", "proximo_contacto_at", "created_at", "updated_at",
)
_ROL_SELECT = ", ".join(_ROL_COLS)


def _upsert_contacto(conn, telefono, nombre=None, email=None):
    """Upsert por teléfono. Devuelve la fila completa."""
    if email and not _EMAIL_RE.match(email):
        email = None
    return conn.execute(
        f"INSERT INTO petra_contactos (telefono, nombre, email) VALUES (%s, %s, %s) "
        f"ON CONFLICT (telefono) DO UPDATE SET "
        f"  nombre = COALESCE(EXCLUDED.nombre, petra_contactos.nombre), "
        f"  email = COALESCE(EXCLUDED.email, petra_contactos.email), "
        f"  updated_at = NOW() "
        f"RETURNING {_CONT_SELECT}",
        (telefono, nombre, email),
    ).fetchone()


@petra_bp.post("/contactos")
@_require_token
def crear_o_actualizar_contacto():
    body = request.get_json(silent=True) or {}
    tel = _norm_tel(body.get("telefono"))
    if not tel:
        return jsonify({"error": "telefono invalido"}), 400
    nombre = (body.get("nombre") or "").strip() or None
    email = (body.get("email") or "").strip() or None
    log.info("[petra] POST /contactos tel=%s", tel)
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = _upsert_contacto(conn, tel, nombre, email)
    except Exception as exc:
        log.exception("[petra] /contactos fallo tel=%s", tel)
        return jsonify({"error": str(exc)}), 500
    return jsonify(_row_to_dict(row, _CONT_COLS)), 200


@petra_bp.get("/contactos/<telefono>")
@_require_token
def obtener_contacto(telefono):
    tel = _norm_tel(telefono)
    if not tel:
        return jsonify({"error": "telefono invalido"}), 400
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                f"SELECT {_CONT_SELECT} FROM petra_contactos WHERE telefono = %s",
                (tel,),
            ).fetchone()
            if not row:
                return jsonify({"error": "no encontrado"}), 404
            contacto_id = row[0]
            roles = conn.execute(
                f"SELECT {_ROL_SELECT} FROM petra_contacto_roles "
                f"WHERE contacto_id = %s ORDER BY estado_desde DESC",
                (contacto_id,),
            ).fetchall()
    except Exception as exc:
        log.exception("[petra] GET /contactos/%s fallo", tel)
        return jsonify({"error": str(exc)}), 500
    out = _row_to_dict(row, _CONT_COLS)
    out["roles"] = [_row_to_dict(r, _ROL_COLS) for r in roles]
    return jsonify(out), 200


@petra_bp.post("/contactos/<telefono>/roles")
@_require_token
def agregar_o_actualizar_rol(telefono):
    tel = _norm_tel(telefono)
    if not tel:
        return jsonify({"error": "telefono invalido"}), 400
    body = request.get_json(silent=True) or {}
    rol = (body.get("rol") or "").strip().lower()
    if rol not in _ROLES_VALID:
        return jsonify({"error": f"rol invalido (opciones: {sorted(_ROLES_VALID)})"}), 400
    estado = (body.get("estado") or "activo").strip().lower()
    edificio_id = _int_pos(body.get("edificio_id"))
    if edificio_id == "invalid":
        return jsonify({"error": "edificio_id invalido"}), 400
    metadata = body.get("metadata") or {}
    if not isinstance(metadata, dict):
        return jsonify({"error": "metadata debe ser objeto"}), 400
    log.info("[petra] POST roles tel=%s rol=%s estado=%s", tel, rol, estado)
    try:
        with _db._conexion_directa(timeout=10) as conn:
            cont = _upsert_contacto(conn, tel)
            contacto_id = cont[0]
            import json as _json
            row = conn.execute(
                f"INSERT INTO petra_contacto_roles "
                f"(contacto_id, rol, estado, edificio_id, metadata) "
                f"VALUES (%s, %s, %s, %s, %s::jsonb) "
                f"ON CONFLICT (contacto_id, rol, edificio_id) DO UPDATE SET "
                f"  estado = EXCLUDED.estado, "
                f"  metadata = petra_contacto_roles.metadata || EXCLUDED.metadata, "
                f"  updated_at = NOW() "
                f"RETURNING {_ROL_SELECT}",
                (contacto_id, rol, estado, edificio_id, _json.dumps(metadata)),
            ).fetchone()
    except Exception as exc:
        log.exception("[petra] /contactos/%s/roles fallo", tel)
        return jsonify({"error": str(exc)}), 500
    return jsonify(_row_to_dict(row, _ROL_COLS)), 200


def _tiene_rol_cliente_activo(conn, contacto_id):
    """True si el contacto tiene cualquier rol de cliente activo.
    Usado por remarketing para suprimir follow-ups genéricos a clientes nuestros."""
    row = conn.execute(
        "SELECT 1 FROM petra_contacto_roles "
        "WHERE contacto_id = %s "
        "  AND rol IN ('cliente_credito', 'cliente_servicios') "
        "  AND estado = 'activo' "
        "LIMIT 1",
        (contacto_id,),
    ).fetchone()
    return row is not None


# ============================================================================
# INMUEBLES
# ============================================================================

_INM_COLS = (
    "id", "edificio_id", "contacto_id", "tipo", "modalidad",
    "torre", "piso", "area_m2", "parqueadero",
    "precio_millones", "precio_arriendo_mensual_millones",
    "nombre_anunciante", "telefono_contacto",
    "estado", "publicado_at", "cerrado_at", "cerrado_motivo",
    "created_at", "updated_at",
)
_INM_SELECT = ", ".join(_INM_COLS)


def _validar_payload_inmueble(body, require_tipo_modalidad=True):
    tipo = (body.get("tipo") or "").strip().lower() or None
    if require_tipo_modalidad and tipo not in _INMUEBLE_TIPO_VALID:
        return None, f"tipo invalido (opciones: {sorted(_INMUEBLE_TIPO_VALID)})"
    if tipo is not None and tipo not in _INMUEBLE_TIPO_VALID:
        return None, f"tipo invalido"

    modalidad = (body.get("modalidad") or "").strip().lower() or None
    if require_tipo_modalidad and modalidad not in _INMUEBLE_MODALIDAD_VALID:
        return None, f"modalidad invalida (opciones: {sorted(_INMUEBLE_MODALIDAD_VALID)})"
    if modalidad is not None and modalidad not in _INMUEBLE_MODALIDAD_VALID:
        return None, f"modalidad invalida"

    area = _num(body.get("area_m2"))
    if area is not None and (area < 5 or area > 10000):
        return None, "area_m2 fuera de rango (5-10000)"
    parq = _bool(body.get("parqueadero"))
    precio_venta = _num(body.get("precio_millones"))
    precio_arr = _num(body.get("precio_arriendo_mensual_millones"))
    nombre = (body.get("nombre_anunciante") or "").strip() or None
    tel_cont = _norm_tel(body.get("telefono_contacto"))
    if body.get("telefono_contacto") and not tel_cont:
        return None, "telefono_contacto invalido"
    torre = (body.get("torre") or "").strip() or None
    piso = (body.get("piso") or "").strip() or None

    return {
        "tipo": tipo,
        "modalidad": modalidad,
        "torre": torre,
        "piso": piso,
        "area_m2": area,
        "parqueadero": parq,
        "precio_millones": precio_venta,
        "precio_arriendo_mensual_millones": precio_arr,
        "nombre_anunciante": nombre,
        "telefono_contacto": tel_cont,
    }, None


@petra_bp.post("/inmuebles")
@_require_token
def crear_inmueble():
    """Crear inmueble publicado. Alta directa (sin flujo chat).
    Para el flujo chat del Sprint 2, el state machine construye el payload
    campo-a-campo y llama este endpoint al final con estado='activo'."""
    body = request.get_json(silent=True) or {}
    edif_id = _int_pos(body.get("edificio_id"))
    if edif_id == "invalid" or edif_id is None:
        return jsonify({"error": "edificio_id requerido"}), 400
    tel_anunciante = _norm_tel(body.get("telefono_anunciante") or body.get("telefono"))
    if not tel_anunciante:
        return jsonify({"error": "telefono_anunciante invalido"}), 400

    payload, err = _validar_payload_inmueble(body, require_tipo_modalidad=True)
    if err:
        return jsonify({"error": err}), 400

    estado = (body.get("estado") or "activo").strip().lower()
    if estado not in {"borrador", "activo"}:
        return jsonify({"error": "estado inicial debe ser borrador o activo"}), 400

    log.info("[petra] POST /inmuebles edif=%s tel=%s tipo=%s modalidad=%s",
             edif_id, tel_anunciante, payload["tipo"], payload["modalidad"])
    try:
        with _db._conexion_directa(timeout=10) as conn:
            edif = conn.execute(
                "SELECT 1 FROM petra_edificios WHERE id = %s", (edif_id,)
            ).fetchone()
            if not edif:
                return jsonify({"error": "edificio no existe"}), 404
            cont = _upsert_contacto(
                conn, tel_anunciante,
                nombre=payload["nombre_anunciante"],
            )
            contacto_id = cont[0]
            tel_contacto = payload["telefono_contacto"] or tel_anunciante
            publicado_at = "NOW()" if estado == "activo" else "NULL"
            row = conn.execute(
                f"INSERT INTO petra_inmuebles "
                f"(edificio_id, contacto_id, tipo, modalidad, torre, piso, area_m2, "
                f" parqueadero, precio_millones, precio_arriendo_mensual_millones, "
                f" nombre_anunciante, telefono_contacto, estado, publicado_at) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, {publicado_at}) "
                f"RETURNING {_INM_SELECT}",
                (edif_id, contacto_id, payload["tipo"], payload["modalidad"],
                 payload["torre"], payload["piso"], payload["area_m2"],
                 payload["parqueadero"], payload["precio_millones"],
                 payload["precio_arriendo_mensual_millones"],
                 payload["nombre_anunciante"], tel_contacto, estado),
            ).fetchone()
            # Rol anunciante en ese edificio.
            import json as _json
            conn.execute(
                "INSERT INTO petra_contacto_roles "
                "(contacto_id, rol, estado, edificio_id, metadata) "
                "VALUES (%s, 'anunciante', 'activo', %s, %s::jsonb) "
                "ON CONFLICT (contacto_id, rol, edificio_id) DO UPDATE SET "
                "  estado = 'activo', updated_at = NOW()",
                (contacto_id, edif_id, _json.dumps({"ultimo_inmueble_id": row[0]})),
            )
    except Exception as exc:
        log.exception("[petra] /inmuebles fallo")
        return jsonify({"error": str(exc)}), 500
    return jsonify(_row_to_dict(row, _INM_COLS)), 200


@petra_bp.get("/inmuebles/<int:inm_id>")
@_require_token
def obtener_inmueble(inm_id):
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                f"SELECT {_INM_SELECT} FROM petra_inmuebles WHERE id = %s",
                (inm_id,),
            ).fetchone()
    except Exception as exc:
        log.exception("[petra] GET /inmuebles/%s fallo", inm_id)
        return jsonify({"error": str(exc)}), 500
    if not row:
        return jsonify({"error": "no encontrado"}), 404
    return jsonify(_row_to_dict(row, _INM_COLS)), 200


@petra_bp.get("/edificios/<int:edif_id>/inmuebles")
@_require_token
def listar_inmuebles_edificio(edif_id):
    modalidad = (request.args.get("modalidad") or "").strip().lower()
    estado = (request.args.get("estado") or "activo").strip().lower()
    where = ["edificio_id = %s", "estado = %s"]
    params = [edif_id, estado]
    if modalidad in {"venta", "arriendo", "ambas"}:
        where.append("(modalidad = %s OR modalidad = 'ambas')")
        params.append(modalidad)
    sql = (f"SELECT {_INM_SELECT} FROM petra_inmuebles "
           f"WHERE {' AND '.join(where)} ORDER BY publicado_at DESC NULLS LAST LIMIT 100")
    try:
        with _db._conexion_directa(timeout=10) as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception as exc:
        log.exception("[petra] listar edif=%s fallo", edif_id)
        return jsonify({"error": str(exc)}), 500
    return jsonify({"inmuebles": [_row_to_dict(r, _INM_COLS) for r in rows]}), 200


@petra_bp.get("/contactos/<telefono>/inmuebles")
@_require_token
def listar_inmuebles_contacto(telefono):
    tel = _norm_tel(telefono)
    if not tel:
        return jsonify({"error": "telefono invalido"}), 400
    try:
        with _db._conexion_directa(timeout=10) as conn:
            cont = conn.execute(
                "SELECT id FROM petra_contactos WHERE telefono = %s", (tel,)
            ).fetchone()
            if not cont:
                return jsonify({"inmuebles": []}), 200
            rows = conn.execute(
                f"SELECT {_INM_SELECT} FROM petra_inmuebles "
                f"WHERE contacto_id = %s AND estado NOT IN ('eliminado') "
                f"ORDER BY publicado_at DESC NULLS LAST",
                (cont[0],),
            ).fetchall()
    except Exception as exc:
        log.exception("[petra] /contactos/%s/inmuebles fallo", tel)
        return jsonify({"error": str(exc)}), 500
    return jsonify({"inmuebles": [_row_to_dict(r, _INM_COLS) for r in rows]}), 200


@petra_bp.patch("/inmuebles/<int:inm_id>")
@_require_token
def editar_inmueble(inm_id):
    body = request.get_json(silent=True) or {}
    payload, err = _validar_payload_inmueble(body, require_tipo_modalidad=False)
    if err:
        return jsonify({"error": err}), 400
    # Solo updates sobre campos presentes en el body.
    sets = []
    params = []
    for k, v in payload.items():
        if k in body:
            sets.append(f"{k} = %s")
            params.append(v)
    if not sets:
        return jsonify({"error": "nada que actualizar"}), 400
    sets.append("updated_at = NOW()")
    params.append(inm_id)
    sql = (f"UPDATE petra_inmuebles SET {', '.join(sets)} "
           f"WHERE id = %s AND estado NOT IN ('vendido', 'arrendado', 'eliminado') "
           f"RETURNING {_INM_SELECT}")
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(sql, params).fetchone()
    except Exception as exc:
        log.exception("[petra] PATCH /inmuebles/%s fallo", inm_id)
        return jsonify({"error": str(exc)}), 500
    if not row:
        return jsonify({"error": "no encontrado o inmueble ya cerrado"}), 404
    return jsonify(_row_to_dict(row, _INM_COLS)), 200


@petra_bp.post("/inmuebles/<int:inm_id>/estado")
@_require_token
def cambiar_estado_inmueble(inm_id):
    """Transiciones permitidas:
      borrador → activo (publicar)
      activo → pausado | vendido | arrendado | eliminado
      pausado → activo | eliminado
    Transiciones a estados terminales (vendido/arrendado/eliminado) son
    irreversibles."""
    body = request.get_json(silent=True) or {}
    nuevo = (body.get("estado") or "").strip().lower()
    motivo = (body.get("motivo") or "").strip() or None
    if nuevo not in _INMUEBLE_ESTADO_VALID:
        return jsonify({"error": f"estado invalido (opciones: {sorted(_INMUEBLE_ESTADO_VALID)})"}), 400

    transiciones = {
        "borrador": {"activo", "eliminado"},
        "activo": {"pausado", "vendido", "arrendado", "eliminado"},
        "pausado": {"activo", "eliminado"},
    }
    terminales = {"vendido", "arrendado", "eliminado"}

    try:
        with _db._conexion_directa(timeout=10) as conn:
            cur = conn.execute(
                "SELECT estado FROM petra_inmuebles WHERE id = %s", (inm_id,)
            ).fetchone()
            if not cur:
                return jsonify({"error": "no encontrado"}), 404
            estado_actual = cur[0]
            if estado_actual in terminales:
                return jsonify({"error": f"inmueble en estado terminal {estado_actual}"}), 409
            if nuevo not in transiciones.get(estado_actual, set()):
                return jsonify({
                    "error": f"transicion no permitida {estado_actual} -> {nuevo}"
                }), 400

            if nuevo == "activo" and estado_actual == "borrador":
                row = conn.execute(
                    f"UPDATE petra_inmuebles "
                    f"SET estado = %s, publicado_at = COALESCE(publicado_at, NOW()), "
                    f"    updated_at = NOW() "
                    f"WHERE id = %s RETURNING {_INM_SELECT}",
                    (nuevo, inm_id),
                ).fetchone()
            elif nuevo in terminales:
                row = conn.execute(
                    f"UPDATE petra_inmuebles "
                    f"SET estado = %s, cerrado_at = NOW(), cerrado_motivo = %s, "
                    f"    updated_at = NOW() "
                    f"WHERE id = %s RETURNING {_INM_SELECT}",
                    (nuevo, motivo, inm_id),
                ).fetchone()
            else:
                row = conn.execute(
                    f"UPDATE petra_inmuebles "
                    f"SET estado = %s, updated_at = NOW() "
                    f"WHERE id = %s RETURNING {_INM_SELECT}",
                    (nuevo, inm_id),
                ).fetchone()
    except Exception as exc:
        log.exception("[petra] POST /inmuebles/%s/estado fallo", inm_id)
        return jsonify({"error": str(exc)}), 500
    return jsonify(_row_to_dict(row, _INM_COLS)), 200
