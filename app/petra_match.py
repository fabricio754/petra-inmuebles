"""PETRA matching engine L1 + outbound queue + remarketing (Sprint 3).

Matching determinístico por reglas duras:
  - mismo edificio (preferente) o misma ciudad
  - mismo tipo (o tipo compatible)
  - modalidad compatible (venta/arriendo/ambas)
  - precio del inmueble dentro del rango de la búsqueda (±20% de holgura)
  - área del inmueble dentro del rango de la búsqueda (si especificado)
  - parqueadero (si búsqueda lo pide)

Scoring simple: puntos por overlap de criterios. 100% explicable.

Outbound queue:
  - Al publicar un inmueble, los interesados que matchean reciben un
    "mensaje encolado" en petra_outbound_queue estado='pendiente'.
  - La función drain_outbound_queue() procesa la cola. Hoy es stub (marca
    como enviado + log) hasta que el helper WhatsApp real esté activo.

Remarketing engine:
  - escanear_pendientes() recorre reglas y agenda outbound items. Para
    cada regla, aplica guardia multi-rol: si contacto es cliente activo
    de cualquier servicio, skipea (no spam a clientes).
  - Reglas iniciales:
      * anuncio_sin_actualizar_30d: ping "¿sigue disponible?"
      * busqueda_sin_matches_14d: ping "aún estamos buscando"
      * contacto_qr_sin_accion_72h: ping "¿viste el QR?"

Endpoints:
  - POST   /petra/busquedas               crear/actualizar búsqueda
  - GET    /petra/busquedas/<id>
  - GET    /petra/contactos/<tel>/busquedas
  - POST   /petra/busquedas/<id>/cerrar
  - GET    /petra/matches/inmueble/<id>   quién debe ser notificado
  - GET    /petra/matches/busqueda/<id>   qué inmuebles cumplen esta búsqueda
  - POST   /petra/outbound/drain          procesa la cola (admin)
  - GET    /petra/outbound/pendientes
  - POST   /petra/remarketing/escanear    ejecuta reglas (admin)
"""
import json
import logging
import os
from functools import wraps

from flask import Blueprint, jsonify, request

from app import db as _db

log = logging.getLogger("petra")

petra_match_bp = Blueprint("petra_match", __name__, url_prefix="/petra")


def _require_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = os.environ.get("TOOLS_API_TOKEN", "")
        if not token or request.headers.get("X-Tools-Token") != token:
            return jsonify({"error": "no_autorizado"}), 401
        return fn(*args, **kwargs)
    return wrapper


# =============================================================================
# BÚSQUEDAS (criterios de interesado)
# =============================================================================

_BUS_COLS = (
    "id", "contacto_id", "edificio_id", "modalidad", "tipo",
    "precio_min_millones", "precio_max_millones",
    "area_min_m2", "area_max_m2", "parqueadero", "activa",
    "created_at", "updated_at",
)
_BUS_SELECT = ", ".join(_BUS_COLS)

_MODALIDADES = {"venta", "arriendo", "ambas"}
_TIPOS = {"apartamento", "casa", "local", "oficina", "habitacion", "parqueadero"}


def _row_dict(row, cols):
    d = dict(zip(cols, row))
    for k, v in list(d.items()):
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d


def _num(raw):
    if raw is None or raw == "":
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def _bool_coerce(raw):
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


@petra_match_bp.post("/busquedas")
@_require_token
def crear_busqueda():
    """Alta de criterio de búsqueda de un interesado. Upsert por (contacto_id,
    edificio_id, modalidad) si querés dedupe suave, pero por simplicidad
    permitimos múltiples activas."""
    body = request.get_json(silent=True) or {}
    contacto_id = body.get("contacto_id")
    telefono = (body.get("telefono") or "").strip()
    if not contacto_id and telefono:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                "SELECT id FROM petra_contactos WHERE telefono = %s",
                (telefono,),
            ).fetchone()
            contacto_id = row[0] if row else None
    if not contacto_id:
        return jsonify({"error": "contacto_id o telefono valido requerido"}), 400

    modalidad = (body.get("modalidad") or "").strip().lower()
    if modalidad not in _MODALIDADES:
        return jsonify({"error": f"modalidad invalida (opciones: {sorted(_MODALIDADES)})"}), 400

    tipo = body.get("tipo")
    if tipo:
        tipo = str(tipo).strip().lower()
        if tipo not in _TIPOS:
            return jsonify({"error": f"tipo invalido"}), 400

    edificio_id = body.get("edificio_id")
    try:
        edificio_id = int(edificio_id) if edificio_id else None
    except (ValueError, TypeError):
        return jsonify({"error": "edificio_id invalido"}), 400

    log.info("[petra_match] POST /busquedas contacto=%s edif=%s modal=%s",
             contacto_id, edificio_id, modalidad)
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                f"INSERT INTO petra_busquedas "
                f"(contacto_id, edificio_id, modalidad, tipo, "
                f" precio_min_millones, precio_max_millones, "
                f" area_min_m2, area_max_m2, parqueadero) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                f"RETURNING {_BUS_SELECT}",
                (contacto_id, edificio_id, modalidad, tipo,
                 _num(body.get("precio_min_millones")),
                 _num(body.get("precio_max_millones")),
                 _num(body.get("area_min_m2")),
                 _num(body.get("area_max_m2")),
                 _bool_coerce(body.get("parqueadero"))),
            ).fetchone()
            # Rol interesado
            conn.execute(
                "INSERT INTO petra_contacto_roles "
                "(contacto_id, rol, estado, edificio_id, metadata) "
                "VALUES (%s, 'interesado', 'activo', %s, %s::jsonb) "
                "ON CONFLICT (contacto_id, rol, edificio_id) DO UPDATE SET "
                "  estado = 'activo', updated_at = NOW()",
                (contacto_id, edificio_id, json.dumps({"ultima_busqueda_id": row[0]})),
            )
    except Exception as exc:
        log.exception("[petra_match] /busquedas fallo")
        return jsonify({"error": str(exc)}), 500
    return jsonify(_row_dict(row, _BUS_COLS)), 200


@petra_match_bp.get("/busquedas/<int:bid>")
@_require_token
def obtener_busqueda(bid):
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                f"SELECT {_BUS_SELECT} FROM petra_busquedas WHERE id = %s", (bid,)
            ).fetchone()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    if not row:
        return jsonify({"error": "no encontrado"}), 404
    return jsonify(_row_dict(row, _BUS_COLS)), 200


@petra_match_bp.get("/contactos/<telefono>/busquedas")
@_require_token
def listar_busquedas_contacto(telefono):
    try:
        with _db._conexion_directa(timeout=10) as conn:
            cont = conn.execute(
                "SELECT id FROM petra_contactos WHERE telefono = %s", (telefono,),
            ).fetchone()
            if not cont:
                return jsonify({"busquedas": []}), 200
            rows = conn.execute(
                f"SELECT {_BUS_SELECT} FROM petra_busquedas "
                f"WHERE contacto_id = %s AND activa = TRUE "
                f"ORDER BY created_at DESC", (cont[0],),
            ).fetchall()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"busquedas": [_row_dict(r, _BUS_COLS) for r in rows]}), 200


@petra_match_bp.post("/busquedas/<int:bid>/cerrar")
@_require_token
def cerrar_busqueda(bid):
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                f"UPDATE petra_busquedas SET activa = FALSE, updated_at = NOW() "
                f"WHERE id = %s RETURNING {_BUS_SELECT}", (bid,)
            ).fetchone()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    if not row:
        return jsonify({"error": "no encontrado"}), 404
    return jsonify(_row_dict(row, _BUS_COLS)), 200


# =============================================================================
# MATCHING ENGINE L1
# =============================================================================

PRECIO_TOLERANCIA = 0.20  # ±20% de holgura sobre rango especificado


def _match_score(busqueda, inmueble):
    """Devuelve (score 0-100, razones) o (0, 'motivo_descarte') si no matchea."""
    razones = []
    # 1) Edificio (si búsqueda ancla a edificio, debe coincidir)
    if busqueda["edificio_id"] and busqueda["edificio_id"] != inmueble["edificio_id"]:
        return 0, "edificio_distinto"
    if busqueda["edificio_id"] == inmueble["edificio_id"]:
        razones.append("mismo_edificio")

    # 2) Modalidad
    b_mod = busqueda["modalidad"]
    i_mod = inmueble["modalidad"]
    if b_mod != "ambas" and i_mod != "ambas" and b_mod != i_mod:
        return 0, "modalidad_distinta"
    razones.append("modalidad_ok")

    # 3) Tipo
    if busqueda["tipo"] and inmueble["tipo"] and busqueda["tipo"] != inmueble["tipo"]:
        return 0, "tipo_distinto"
    if busqueda["tipo"] and busqueda["tipo"] == inmueble["tipo"]:
        razones.append("tipo_coincide")

    # 4) Precio (modalidad apropiada)
    precio = None
    if b_mod == "venta" or (b_mod == "ambas" and inmueble.get("precio_millones")):
        precio = inmueble.get("precio_millones")
    elif b_mod == "arriendo":
        precio = inmueble.get("precio_arriendo_mensual_millones")

    if precio is not None:
        pmin = busqueda["precio_min_millones"]
        pmax = busqueda["precio_max_millones"]
        if pmin is not None:
            holgura = pmin * (1 - PRECIO_TOLERANCIA)
            if precio < holgura:
                return 0, "precio_bajo_rango"
        if pmax is not None:
            holgura = pmax * (1 + PRECIO_TOLERANCIA)
            if precio > holgura:
                return 0, "precio_sobre_rango"
        razones.append("precio_en_rango")

    # 5) Área
    area = inmueble.get("area_m2")
    if area is not None:
        amin = busqueda["area_min_m2"]
        amax = busqueda["area_max_m2"]
        if amin is not None and area < amin:
            return 0, "area_insuficiente"
        if amax is not None and area > amax:
            return 0, "area_excede"
        if amin or amax:
            razones.append("area_en_rango")

    # 6) Parqueadero
    if busqueda["parqueadero"] is True and inmueble.get("parqueadero") is False:
        return 0, "sin_parqueadero"
    if busqueda["parqueadero"] is True and inmueble.get("parqueadero") is True:
        razones.append("parqueadero_coincide")

    # Score: 20 pts por cada razón positiva, cap 100
    score = min(100, len(razones) * 20)
    return score, ",".join(razones)


def _dict_from_cols(row, cols):
    return dict(zip(cols, row))


_INM_FETCH_COLS = (
    "id", "edificio_id", "contacto_id", "tipo", "modalidad",
    "torre", "piso", "area_m2", "parqueadero",
    "precio_millones", "precio_arriendo_mensual_millones",
    "nombre_anunciante", "telefono_contacto", "estado",
)


def encontrar_matches_para_inmueble(conn, inmueble_id):
    """Dado un inmueble, encuentra búsquedas activas que lo matchean. Devuelve lista de {busqueda, score, razones}."""
    inm_row = conn.execute(
        f"SELECT {', '.join(_INM_FETCH_COLS)} FROM petra_inmuebles "
        f"WHERE id = %s AND estado = 'activo'", (inmueble_id,),
    ).fetchone()
    if not inm_row:
        return []
    inm = _dict_from_cols(inm_row, _INM_FETCH_COLS)
    for k in ("area_m2", "precio_millones", "precio_arriendo_mensual_millones"):
        if inm.get(k) is not None:
            inm[k] = float(inm[k])

    # Candidatas: búsquedas activas que anclan al mismo edificio, O sin edificio
    # (búsqueda global, cruzamos luego por ciudad vía edificio del inmueble).
    busqs = conn.execute(
        f"SELECT {_BUS_SELECT} FROM petra_busquedas "
        f"WHERE activa = TRUE "
        f"  AND (edificio_id = %s OR edificio_id IS NULL) "
        f"  AND contacto_id != %s "
        f"ORDER BY created_at DESC",
        (inm["edificio_id"], inm["contacto_id"]),
    ).fetchall()

    matches = []
    for b in busqs:
        bd = _dict_from_cols(b, _BUS_COLS)
        for k in ("precio_min_millones", "precio_max_millones", "area_min_m2", "area_max_m2"):
            if bd.get(k) is not None:
                bd[k] = float(bd[k])
        score, razones = _match_score(bd, inm)
        if score > 0:
            matches.append({"busqueda_id": bd["id"], "contacto_id": bd["contacto_id"],
                            "score": score, "razones": razones})
    return matches


def encontrar_matches_para_busqueda(conn, busqueda_id):
    """Dada una búsqueda, encuentra inmuebles activos que la cumplen."""
    bus_row = conn.execute(
        f"SELECT {_BUS_SELECT} FROM petra_busquedas WHERE id = %s AND activa = TRUE",
        (busqueda_id,),
    ).fetchone()
    if not bus_row:
        return []
    bus = _dict_from_cols(bus_row, _BUS_COLS)
    for k in ("precio_min_millones", "precio_max_millones", "area_min_m2", "area_max_m2"):
        if bus.get(k) is not None:
            bus[k] = float(bus[k])

    where = "estado = 'activo' AND contacto_id != %s"
    params = [bus["contacto_id"]]
    if bus["edificio_id"]:
        where += " AND edificio_id = %s"
        params.append(bus["edificio_id"])
    inms = conn.execute(
        f"SELECT {', '.join(_INM_FETCH_COLS)} FROM petra_inmuebles "
        f"WHERE {where} ORDER BY publicado_at DESC LIMIT 50",
        params,
    ).fetchall()

    matches = []
    for ir in inms:
        inm = _dict_from_cols(ir, _INM_FETCH_COLS)
        for k in ("area_m2", "precio_millones", "precio_arriendo_mensual_millones"):
            if inm.get(k) is not None:
                inm[k] = float(inm[k])
        score, razones = _match_score(bus, inm)
        if score > 0:
            matches.append({"inmueble_id": inm["id"], "score": score, "razones": razones})
    return matches


@petra_match_bp.get("/matches/inmueble/<int:inm_id>")
@_require_token
def matches_inmueble(inm_id):
    try:
        with _db._conexion_directa(timeout=10) as conn:
            matches = encontrar_matches_para_inmueble(conn, inm_id)
    except Exception as exc:
        log.exception("[petra_match] matches/inmueble/%s fallo", inm_id)
        return jsonify({"error": str(exc)}), 500
    return jsonify({"matches": matches}), 200


@petra_match_bp.get("/matches/busqueda/<int:bid>")
@_require_token
def matches_busqueda(bid):
    try:
        with _db._conexion_directa(timeout=10) as conn:
            matches = encontrar_matches_para_busqueda(conn, bid)
    except Exception as exc:
        log.exception("[petra_match] matches/busqueda/%s fallo", bid)
        return jsonify({"error": str(exc)}), 500
    return jsonify({"matches": matches}), 200


# =============================================================================
# OUTBOUND QUEUE
# =============================================================================

def encolar_mensaje(conn, contacto_id, tipo, payload, origen=None):
    """Encola un mensaje para enviar al contacto. Devuelve id del item."""
    row = conn.execute(
        "SELECT telefono FROM petra_contactos WHERE id = %s", (contacto_id,),
    ).fetchone()
    if not row:
        return None
    telefono = row[0]
    out = conn.execute(
        "INSERT INTO petra_outbound_queue "
        "(contacto_id, telefono, tipo, payload, origen, estado) "
        "VALUES (%s, %s, %s, %s::jsonb, %s, 'pendiente') RETURNING id",
        (contacto_id, telefono, tipo, json.dumps(payload), origen),
    ).fetchone()
    return out[0]


def notificar_matches_al_publicar(conn, inmueble_id):
    """Al publicar un inmueble, encola notificación a cada búsqueda que
    matchea. Devuelve cuántas notificaciones encoladas."""
    matches = encontrar_matches_para_inmueble(conn, inmueble_id)
    if not matches:
        return 0
    inm = conn.execute(
        "SELECT tipo, modalidad, torre, piso, area_m2, precio_millones, "
        "       precio_arriendo_mensual_millones, edificio_id "
        "FROM petra_inmuebles WHERE id = %s", (inmueble_id,),
    ).fetchone()
    if not inm:
        return 0
    tipo, modal, torre, piso, area, pv, pa, edif_id = inm
    edif = conn.execute(
        "SELECT nombre FROM petra_edificios WHERE id = %s", (edif_id,),
    ).fetchone()
    edif_nombre = edif[0] if edif else "edificio"
    precio = pv if modal in ("venta", "ambas") else pa
    precio_txt = f"${float(precio):.0f}M" if precio else "a convenir"
    if modal == "arriendo":
        precio_txt = f"${float(precio):.1f}M/mes" if precio else "a convenir"
    cuerpo = (
        f"🏡 Nuevo {modal} que podría interesarte en {edif_nombre}:\n"
        f"{tipo.capitalize()} T{torre or '?'} P{piso or '?'} · "
        f"{float(area):.0f}m²" if area else ""
    )
    cuerpo += f"\nPrecio: {precio_txt}"

    enviados = 0
    for m in matches:
        encolar_mensaje(
            conn,
            m["contacto_id"],
            tipo="match_nuevo",
            payload={
                "tipo_mensaje": "text",
                "texto": cuerpo,
                "inmueble_id": inmueble_id,
                "busqueda_id": m["busqueda_id"],
                "score": m["score"],
            },
            origen=f"match_inmueble_{inmueble_id}",
        )
        enviados += 1
    return enviados


def _wa_send_real(telefono, payload):
    """Despacha un item de la outbound queue vía Cloud API del piloto.
    El payload tiene {tipo_mensaje, texto, ...meta}. Soportamos text y
    buttons/list cuando el payload los lleva. Si no hay credenciales, el
    helper hace stub (ok=True, stub=True) y la cola marca como enviado."""
    from app.petra_wa import send_text, send_buttons, send_list
    tm = (payload or {}).get("tipo_mensaje", "text")
    try:
        if tm == "text":
            return send_text(telefono, payload.get("texto", ""))
        if tm == "buttons":
            return send_buttons(telefono, payload.get("texto", ""),
                                payload.get("buttons", []))
        if tm == "list":
            return send_list(telefono, payload.get("texto", ""),
                             payload.get("button_label", "Elegir"),
                             payload.get("sections", []))
    except Exception as exc:
        log.exception("[petra_match] _wa_send_real exc")
        return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": f"tipo_mensaje desconocido: {tm}"}


def drain_outbound(conn, limit=50):
    """Procesa items pendientes. Devuelve estadísticas."""
    pend = conn.execute(
        "SELECT id, contacto_id, telefono, tipo, payload FROM petra_outbound_queue "
        "WHERE estado = 'pendiente' ORDER BY created_at ASC LIMIT %s", (limit,),
    ).fetchall()
    enviados = 0
    fallos = 0
    for item_id, cid, tel, tipo, payload in pend:
        try:
            res = _wa_send_real(tel, payload)
            if res.get("ok"):
                conn.execute(
                    "UPDATE petra_outbound_queue SET estado = 'enviado', "
                    "enviado_at = NOW(), intentos = intentos + 1, "
                    "error = NULL, updated_at = NOW() WHERE id = %s",
                    (item_id,),
                )
                enviados += 1
            else:
                conn.execute(
                    "UPDATE petra_outbound_queue SET estado = 'error', "
                    "intentos = intentos + 1, error = %s, updated_at = NOW() "
                    "WHERE id = %s",
                    (str(res.get("error", "unknown")), item_id),
                )
                fallos += 1
        except Exception as exc:
            log.exception("[petra_match] drain item=%s fallo", item_id)
            conn.execute(
                "UPDATE petra_outbound_queue SET estado = 'error', "
                "intentos = intentos + 1, error = %s, updated_at = NOW() "
                "WHERE id = %s",
                (str(exc)[:500], item_id),
            )
            fallos += 1
    return {"procesados": len(pend), "enviados": enviados, "fallos": fallos}


@petra_match_bp.post("/outbound/drain")
@_require_token
def admin_drain():
    limit = int(request.args.get("limit", 50))
    try:
        with _db._conexion_directa(timeout=15) as conn:
            stats = drain_outbound(conn, limit=limit)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(stats), 200


@petra_match_bp.get("/outbound/pendientes")
@_require_token
def listar_pendientes():
    try:
        with _db._conexion_directa(timeout=10) as conn:
            rows = conn.execute(
                "SELECT id, contacto_id, telefono, tipo, origen, created_at, intentos "
                "FROM petra_outbound_queue WHERE estado = 'pendiente' "
                "ORDER BY created_at ASC LIMIT 100"
            ).fetchall()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    out = []
    for r in rows:
        d = {"id": r[0], "contacto_id": r[1], "telefono": r[2], "tipo": r[3],
             "origen": r[4], "intentos": r[6]}
        d["created_at"] = r[5].isoformat() if r[5] else None
        out.append(d)
    return jsonify({"pendientes": out, "count": len(out)}), 200


# Hook llamado explícitamente desde el router al terminar "publicar".
@petra_match_bp.post("/inmuebles/<int:inm_id>/trigger-matching")
@_require_token
def trigger_matching(inm_id):
    try:
        with _db._conexion_directa(timeout=15) as conn:
            n = notificar_matches_al_publicar(conn, inm_id)
    except Exception as exc:
        log.exception("[petra_match] trigger-matching/%s fallo", inm_id)
        return jsonify({"error": str(exc)}), 500
    return jsonify({"notificaciones_encoladas": n}), 200


# =============================================================================
# REMARKETING ENGINE
# =============================================================================

def _tiene_rol_cliente_activo(conn, contacto_id):
    row = conn.execute(
        "SELECT 1 FROM petra_contacto_roles "
        "WHERE contacto_id = %s "
        "  AND rol IN ('cliente_credito', 'cliente_servicios') "
        "  AND estado = 'activo' LIMIT 1",
        (contacto_id,),
    ).fetchone()
    return row is not None


def _puede_enviar(conn, contacto_id, regla, dentro_dias=7):
    """Delega al gatekeeper central (Sprint 12) manteniendo la firma legacy."""
    try:
        from app import petra_core
        ok, motivo = petra_core.puede_enviar_remarketing(
            conn, contacto_id, regla, dentro_dias=dentro_dias,
            # Las reglas automáticas de inventario no exigen consentimiento
            # comercial explícito aún (fase piloto); se activa cuando los
            # templates estén aprobados y pidamos consent al registrarse.
            requiere_consentimiento=None,
        )
        return ok, motivo
    except Exception:
        return True, "fallback"


def _ya_disparado(conn, contacto_id, regla, dentro_dias):
    row = conn.execute(
        "SELECT 1 FROM petra_remarketing_eventos "
        "WHERE contacto_id = %s AND regla = %s "
        "  AND disparado_at > NOW() - make_interval(days => %s) LIMIT 1",
        (contacto_id, regla, dentro_dias),
    ).fetchone()
    return row is not None


def _log_remarketing(conn, contacto_id, regla, entidad_tipo=None, entidad_id=None,
                     resultado="enviado", skipped_motivo=None):
    conn.execute(
        "INSERT INTO petra_remarketing_eventos "
        "(contacto_id, regla, entidad_tipo, entidad_id, resultado, skipped_motivo) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (contacto_id, regla, entidad_tipo, entidad_id, resultado, skipped_motivo),
    )


def regla_anuncio_sin_actualizar_30d(conn):
    """Pinga propietarios de anuncios activos sin actualizar hace >30d.
    Skipea si el contacto es cliente activo."""
    nombre = "anuncio_sin_actualizar_30d"
    rows = conn.execute(
        "SELECT i.id, i.contacto_id, i.tipo, i.modalidad, "
        "       EXTRACT(DAY FROM NOW() - i.updated_at)::int AS dias "
        "FROM petra_inmuebles i "
        "WHERE i.estado = 'activo' "
        "  AND i.updated_at < NOW() - INTERVAL '30 days'"
    ).fetchall()
    disparados = 0
    skipped = 0
    for inm_id, cid, tipo, modal, dias in rows:
        if _ya_disparado(conn, cid, nombre, 30):
            continue
        if _tiene_rol_cliente_activo(conn, cid):
            _log_remarketing(conn, cid, nombre, "inmueble", inm_id,
                             resultado="skipped", skipped_motivo="cliente_activo")
            skipped += 1
            continue
        texto = (
            f"Hola 👋 Tu anuncio de {tipo} en {modal} lleva {dias} días sin actualizar. "
            f"¿Sigue disponible? Si no, puedes marcarlo como vendido/arrendado y liberar el espacio."
        )
        encolar_mensaje(conn, cid, "remarketing",
                        {"tipo_mensaje": "text", "texto": texto, "inmueble_id": inm_id},
                        origen=f"regla_{nombre}")
        _log_remarketing(conn, cid, nombre, "inmueble", inm_id, resultado="enviado")
        disparados += 1
    return {"regla": nombre, "disparados": disparados, "skipped": skipped}


def regla_busqueda_sin_matches_14d(conn):
    """Pinga interesados con búsqueda activa sin resultado hace >14d."""
    nombre = "busqueda_sin_matches_14d"
    rows = conn.execute(
        "SELECT b.id, b.contacto_id, b.modalidad "
        "FROM petra_busquedas b "
        "WHERE b.activa = TRUE "
        "  AND b.updated_at < NOW() - INTERVAL '14 days'"
    ).fetchall()
    disparados = 0
    skipped = 0
    for bid, cid, modal in rows:
        if _ya_disparado(conn, cid, nombre, 14):
            continue
        if _tiene_rol_cliente_activo(conn, cid):
            _log_remarketing(conn, cid, nombre, "busqueda", bid,
                             resultado="skipped", skipped_motivo="cliente_activo")
            skipped += 1
            continue
        matches = encontrar_matches_para_busqueda(conn, bid)
        if matches:
            # Si hay matches nuevos, mejor usar notificación de match.
            continue
        texto = (
            f"👀 Seguimos atentos a tu búsqueda de {modal}. "
            f"Aún no hay inmuebles que cumplan tus criterios, pero te avisaremos apenas aparezcan. "
            f"Si cambió tu necesidad, escríbeme y la actualizamos."
        )
        encolar_mensaje(conn, cid, "remarketing",
                        {"tipo_mensaje": "text", "texto": texto, "busqueda_id": bid},
                        origen=f"regla_{nombre}")
        _log_remarketing(conn, cid, nombre, "busqueda", bid, resultado="enviado")
        disparados += 1
    return {"regla": nombre, "disparados": disparados, "skipped": skipped}


REMARKETING_REGLAS = [
    regla_anuncio_sin_actualizar_30d,
    regla_busqueda_sin_matches_14d,
]


def escanear_remarketing(conn):
    """Ejecuta todas las reglas y devuelve resumen."""
    resultados = []
    for regla_fn in REMARKETING_REGLAS:
        try:
            r = regla_fn(conn)
        except Exception as exc:
            log.exception("[petra_match] regla %s fallo", regla_fn.__name__)
            r = {"regla": regla_fn.__name__, "error": str(exc)}
        resultados.append(r)
    return resultados


@petra_match_bp.post("/remarketing/escanear")
@_require_token
def admin_escanear_remarketing():
    try:
        with _db._conexion_directa(timeout=30) as conn:
            resultados = escanear_remarketing(conn)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"resultados": resultados}), 200


@petra_match_bp.post("/cron/tick")
@_require_token
def admin_cron_tick():
    """Un cron externo (ej. Render cron, cron-job.org, github actions) llama
    este endpoint cada N minutos. Drena cola de salida + opcionalmente
    ejecuta reglas de remarketing si el query param `remarketing=1` o
    es el primer tick del día (sin estado, se puede llamar siempre)."""
    run_remarketing = request.args.get("remarketing", "0") in ("1", "true", "yes")
    try:
        with _db._conexion_directa(timeout=30) as conn:
            drain_stats = drain_outbound(conn, limit=100)
            rm_resultados = None
            if run_remarketing:
                rm_resultados = escanear_remarketing(conn)
                # Después de encolar, drenar de nuevo para despachar.
                drain2 = drain_outbound(conn, limit=100)
                drain_stats["segundo_drain"] = drain2
    except Exception as exc:
        log.exception("[petra_match] cron tick fallo")
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True, "drain": drain_stats, "remarketing": rm_resultados}), 200


@petra_match_bp.get("/remarketing/eventos")
@_require_token
def listar_eventos_remarketing():
    limit = int(request.args.get("limit", 100))
    try:
        with _db._conexion_directa(timeout=10) as conn:
            rows = conn.execute(
                "SELECT id, contacto_id, regla, entidad_tipo, entidad_id, "
                "       resultado, skipped_motivo, disparado_at "
                "FROM petra_remarketing_eventos "
                "ORDER BY disparado_at DESC LIMIT %s", (limit,),
            ).fetchall()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    out = []
    for r in rows:
        d = {"id": r[0], "contacto_id": r[1], "regla": r[2],
             "entidad_tipo": r[3], "entidad_id": r[4],
             "resultado": r[5], "skipped_motivo": r[6]}
        d["disparado_at"] = r[7].isoformat() if r[7] else None
        out.append(d)
    return jsonify({"eventos": out, "count": len(out)}), 200
