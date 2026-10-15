"""HTTP tools consumidas por el agente de ElevenLabs ("Massi", piloto).

Aislado de produccion: todo bajo esquema de tablas `pilot_*`. Nunca
tocar `contactos`, `mensajes`, `recordatorios` (bot productivo).
Auth por header `X-Tools-Token` contra env `TOOLS_API_TOKEN`.
"""
import logging
import os
import re
from datetime import datetime
from functools import wraps

from flask import Blueprint, jsonify, request

from app import db as _db
from app import petra_core

log = logging.getLogger("petra")

tools_bp = Blueprint("tools", __name__, url_prefix="/tools")


# =============================================================================
# Puente lead crédito ⇄ petra_oportunidades (Sprint 7)
# =============================================================================


def _bridge_credito_asegurar_oportunidad(conn, telefono, nombre=None,
                                         origen="template", valor_cop=None):
    """Garantiza que exista un petra_contacto + petra_oportunidad tipo=credito
    para este teléfono. Idempotente. Devuelve (contacto_id, oportunidad_id).

    Nunca lanza: si algo falla devuelve (None, None) y loguea."""
    try:
        # Contacto
        row = conn.execute(
            "SELECT id FROM petra_contactos WHERE telefono=%s", (telefono,),
        ).fetchone()
        if row:
            contacto_id = row[0]
            if nombre:
                conn.execute(
                    "UPDATE petra_contactos SET nombre=COALESCE(nombre,%s), updated_at=NOW() WHERE id=%s",
                    (nombre, contacto_id),
                )
        else:
            row = conn.execute(
                "INSERT INTO petra_contactos (telefono, nombre) VALUES (%s,%s) RETURNING id",
                (telefono, nombre),
            ).fetchone()
            contacto_id = row[0]

        # Oportunidad crédito abierta
        row = conn.execute(
            """SELECT id FROM petra_oportunidades
               WHERE contacto_id=%s AND tipo='credito'
                 AND estado NOT IN ('cerrado_ganado','cerrado_perdido')
               ORDER BY created_at DESC LIMIT 1""",
            (contacto_id,),
        ).fetchone()
        if row:
            oportunidad_id = row[0]
            if valor_cop is not None:
                conn.execute(
                    "UPDATE petra_oportunidades SET valor_cop=COALESCE(valor_cop,%s), updated_at=NOW() WHERE id=%s",
                    (valor_cop, oportunidad_id),
                )
        else:
            oportunidad_id = petra_core.crear_oportunidad(
                conn, contacto_id=contacto_id, tipo="credito",
                origen=origen, valor_cop=valor_cop,
            )
        return contacto_id, oportunidad_id
    except Exception:
        log.exception("[tools] bridge_credito fallo tel=%s", telefono)
        return None, None

_TEL_RE = re.compile(r"^\+?\d{10,15}$")
_TEL_STRIP_RE = re.compile(r"[\s\-\.]+")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CEDULA_STRIP_RE = re.compile(r"[\s\-\.]+")
_CEDULA_RE = re.compile(r"^\d{5,20}$")

_PATRIMONIO_VALID = {"SI", "NO", "NO_SE"}
_TIPO_PERSONA_VALID = {"NATURAL", "JURIDICA"}
_BOOL_TRUE = {"true", "1", "si", "sí", "yes", "y", "t"}
_BOOL_FALSE = {"false", "0", "no", "n", "f"}


def _normalizar_telefono(raw):
    if raw is None:
        return None
    s = _TEL_STRIP_RE.sub("", str(raw))
    if not _TEL_RE.match(s):
        return None
    return s


def _parse_iso(raw):
    if not raw:
        return None
    try:
        s = str(raw).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return None
        return dt
    except (ValueError, TypeError):
        return None


def _num(raw):
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def _coerce_bool(raw):
    """Devuelve True/False si válido, None si no se puede interpretar."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        if raw in (0, 1):
            return bool(raw)
        return None
    s = str(raw).strip().lower()
    if s in _BOOL_TRUE:
        return True
    if s in _BOOL_FALSE:
        return False
    return None


def _coerce_estrato(raw):
    if raw is None or raw == "":
        return None
    try:
        v = int(raw)
        return v if 1 <= v <= 6 else "invalid"
    except (ValueError, TypeError):
        return "invalid"


def _coerce_edad(raw):
    if raw is None or raw == "":
        return None
    try:
        v = int(raw)
        return v if 1 <= v < 120 else "invalid"
    except (ValueError, TypeError):
        return "invalid"


def _coerce_email(raw):
    if raw is None or raw == "":
        return None
    s = str(raw).strip()
    return s if _EMAIL_RE.match(s) else "invalid"


def _coerce_cedula(raw):
    if raw is None or raw == "":
        return None
    s = _CEDULA_STRIP_RE.sub("", str(raw))
    return s if _CEDULA_RE.match(s) else "invalid"


def _coerce_choice(raw, valid_set):
    if raw is None or raw == "":
        return None
    s = str(raw).strip().upper()
    return s if s in valid_set else "invalid"


def _require_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = os.environ.get("TOOLS_API_TOKEN", "")
        if not token or request.headers.get("X-Tools-Token") != token:
            return jsonify({"error": "no_autorizado"}), 401
        return fn(*args, **kwargs)
    return wrapper


# Rate limiting en memoria (gunicorn -w 1 → un worker, un dict). Protege de
# un bucle del LLM que spamee tools. Buckets por (ip, endpoint), ventana
# deslizante de 60s, 30 hits máximo. Si se excede → 429.
import time as _time
import threading as _threading
_RL_LOCK = _threading.Lock()
_RL_HITS: dict = {}
_RL_LIMIT = int(os.environ.get("TOOLS_RL_LIMIT_PER_MIN", "30"))


def _rate_limit(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        ip = (request.headers.get("X-Forwarded-For") or
              request.remote_addr or "unknown").split(",")[0].strip()
        key = (ip, request.endpoint)
        now = _time.time()
        cutoff = now - 60.0
        with _RL_LOCK:
            hits = [t for t in _RL_HITS.get(key, ()) if t >= cutoff]
            if len(hits) >= _RL_LIMIT:
                return jsonify({
                    "error": "rate_limited",
                    "limit_per_min": _RL_LIMIT,
                    "retry_after_s": int(60 - (now - hits[0])) + 1,
                }), 429
            hits.append(now)
            _RL_HITS[key] = hits
            # GC: si crece mucho, limpia keys muertas
            if len(_RL_HITS) > 1000:
                for k in list(_RL_HITS.keys()):
                    _RL_HITS[k] = [t for t in _RL_HITS[k] if t >= cutoff]
                    if not _RL_HITS[k]:
                        del _RL_HITS[k]
        return fn(*args, **kwargs)
    return wrapper


_LEAD_COLS = (
    "id", "telefono", "nombre", "ciudad", "tipo_inmueble", "avaluo",
    "monto_solicitado", "destino_credito", "requiere_humano",
    "requiere_humano_motivo", "requiere_humano_at", "enviado_sureti_at",
    "created_at", "updated_at",
    "direccion", "apto", "barrio", "estrato", "es_ph", "hipoteca",
    "patrimonio_familia", "propietario_mayor_75", "paz_salvo",
    "tipo_persona", "cedula_nit", "edad", "email",
)
_LEAD_SELECT = ", ".join(_LEAD_COLS)


def _row_lead(row, cols):
    d = dict(zip(cols, row))
    for k in ("created_at", "updated_at", "requiere_humano_at", "enviado_sureti_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    for k in ("avaluo", "monto_solicitado"):
        if d.get(k) is not None:
            d[k] = float(d[k])
    return d


@tools_bp.post("/leads")
@_require_token
@_rate_limit
def crear_o_actualizar_lead():
    body = request.get_json(silent=True) or {}
    tel = _normalizar_telefono(body.get("telefono"))
    if not tel:
        return jsonify({"error": "telefono invalido"}), 400
    log.info("[tools] POST /leads tel=%s", tel)

    nombre = body.get("nombre")
    ciudad = body.get("ciudad")
    tipo = body.get("tipo_inmueble")
    avaluo = _num(body.get("avaluo"))
    monto = _num(body.get("monto_solicitado"))
    destino = body.get("destino_credito")

    direccion = body.get("direccion")
    apto = body.get("apto")
    barrio = body.get("barrio")

    estrato = _coerce_estrato(body.get("estrato"))
    if estrato == "invalid":
        return jsonify({"error": "estrato invalido (1-6)"}), 400

    es_ph = _coerce_bool(body.get("es_ph"))
    hipoteca = _coerce_bool(body.get("hipoteca"))
    paz_salvo = _coerce_bool(body.get("paz_salvo"))
    propietario_mayor_75 = _coerce_bool(body.get("propietario_mayor_75"))

    patrimonio = _coerce_choice(body.get("patrimonio_familia"), _PATRIMONIO_VALID)
    if patrimonio == "invalid":
        return jsonify({"error": "patrimonio_familia invalido (SI|NO|NO_SE)"}), 400

    tipo_persona = _coerce_choice(body.get("tipo_persona"), _TIPO_PERSONA_VALID)
    if tipo_persona == "invalid":
        return jsonify({"error": "tipo_persona invalido (NATURAL|JURIDICA)"}), 400

    cedula = _coerce_cedula(body.get("cedula_nit"))
    if cedula == "invalid":
        return jsonify({"error": "cedula_nit invalido (5-20 digitos)"}), 400

    edad = _coerce_edad(body.get("edad"))
    if edad == "invalid":
        return jsonify({"error": "edad invalida (1-119)"}), 400

    email = _coerce_email(body.get("email"))
    if email == "invalid":
        return jsonify({"error": "email invalido"}), 400

    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                f"INSERT INTO pilot_leads "
                f"(telefono, nombre, ciudad, tipo_inmueble, avaluo, monto_solicitado, destino_credito, "
                f" direccion, apto, barrio, estrato, es_ph, hipoteca, patrimonio_familia, "
                f" propietario_mayor_75, paz_salvo, tipo_persona, cedula_nit, edad, email) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                f"ON CONFLICT (telefono) DO UPDATE SET "
                f"  nombre = COALESCE(EXCLUDED.nombre, pilot_leads.nombre), "
                f"  ciudad = COALESCE(EXCLUDED.ciudad, pilot_leads.ciudad), "
                f"  tipo_inmueble = COALESCE(EXCLUDED.tipo_inmueble, pilot_leads.tipo_inmueble), "
                f"  avaluo = COALESCE(EXCLUDED.avaluo, pilot_leads.avaluo), "
                f"  monto_solicitado = COALESCE(EXCLUDED.monto_solicitado, pilot_leads.monto_solicitado), "
                f"  destino_credito = COALESCE(EXCLUDED.destino_credito, pilot_leads.destino_credito), "
                f"  direccion = COALESCE(EXCLUDED.direccion, pilot_leads.direccion), "
                f"  apto = COALESCE(EXCLUDED.apto, pilot_leads.apto), "
                f"  barrio = COALESCE(EXCLUDED.barrio, pilot_leads.barrio), "
                f"  estrato = COALESCE(EXCLUDED.estrato, pilot_leads.estrato), "
                f"  es_ph = COALESCE(EXCLUDED.es_ph, pilot_leads.es_ph), "
                f"  hipoteca = COALESCE(EXCLUDED.hipoteca, pilot_leads.hipoteca), "
                f"  patrimonio_familia = COALESCE(EXCLUDED.patrimonio_familia, pilot_leads.patrimonio_familia), "
                f"  propietario_mayor_75 = COALESCE(EXCLUDED.propietario_mayor_75, pilot_leads.propietario_mayor_75), "
                f"  paz_salvo = COALESCE(EXCLUDED.paz_salvo, pilot_leads.paz_salvo), "
                f"  tipo_persona = COALESCE(EXCLUDED.tipo_persona, pilot_leads.tipo_persona), "
                f"  cedula_nit = COALESCE(EXCLUDED.cedula_nit, pilot_leads.cedula_nit), "
                f"  edad = COALESCE(EXCLUDED.edad, pilot_leads.edad), "
                f"  email = COALESCE(EXCLUDED.email, pilot_leads.email), "
                f"  updated_at = NOW() "
                f"RETURNING {_LEAD_SELECT}",
                (tel, nombre, ciudad, tipo, avaluo, monto, destino,
                 direccion, apto, barrio, estrato, es_ph, hipoteca, patrimonio,
                 propietario_mayor_75, paz_salvo, tipo_persona, cedula, edad, email),
            ).fetchone()
            # --- Sprint 7: puente a petra_oportunidades + consentimiento habeas data ---
            _bridge_credito_asegurar_oportunidad(
                conn, tel, nombre=nombre, origen="template",
                valor_cop=int(monto) if monto else None,
            )
            if _coerce_bool(body.get("habeas_data")) is True:
                contacto_row = conn.execute(
                    "SELECT id FROM petra_contactos WHERE telefono=%s", (tel,),
                ).fetchone()
                if contacto_row:
                    petra_core.otorgar_consentimiento(
                        conn, contacto_id=contacto_row[0],
                        finalidad="tratamiento_datos", canal="whatsapp",
                        evidencia={"tool": "/leads", "telefono": tel},
                    )
    except Exception as exc:
        log.exception("[tools] /leads fallo tel=%s", tel)
        return jsonify({"error": str(exc)}), 500
    return jsonify(_row_lead(row, _LEAD_COLS)), 200


@tools_bp.get("/leads/<telefono>")
@_require_token
@_rate_limit
def obtener_lead(telefono):
    tel = _normalizar_telefono(telefono)
    if not tel:
        return jsonify({"error": "telefono invalido"}), 400
    log.info("[tools] GET /leads/%s", tel)
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                f"SELECT {_LEAD_SELECT} FROM pilot_leads WHERE telefono = %s",
                (tel,),
            ).fetchone()
            if not row:
                return jsonify({"error": "no encontrado"}), 404
            notas = conn.execute(
                "SELECT id, telefono, autor, texto, created_at FROM pilot_notas "
                "WHERE telefono = %s ORDER BY created_at DESC LIMIT 10",
                (tel,),
            ).fetchall()
            recs = conn.execute(
                "SELECT id, telefono, disparar_at, texto, disparado, created_at "
                "FROM pilot_recordatorios WHERE telefono = %s AND disparado = FALSE "
                "ORDER BY disparar_at ASC",
                (tel,),
            ).fetchall()
    except Exception as exc:
        log.exception("[tools] GET /leads/%s fallo", tel)
        return jsonify({"error": str(exc)}), 500

    lead = _row_lead(row, _LEAD_COLS)
    nota_cols = ("id", "telefono", "autor", "texto", "created_at")
    rec_cols = ("id", "telefono", "disparar_at", "texto", "disparado", "created_at")
    lead["notas"] = [
        {**dict(zip(nota_cols, n)),
         "created_at": n[4].isoformat() if n[4] else None}
        for n in notas
    ]
    lead["recordatorios"] = [
        {**dict(zip(rec_cols, r)),
         "disparar_at": r[2].isoformat() if r[2] else None,
         "created_at": r[5].isoformat() if r[5] else None}
        for r in recs
    ]
    return jsonify(lead), 200


@tools_bp.post("/notas")
@_require_token
@_rate_limit
def crear_nota():
    body = request.get_json(silent=True) or {}
    tel = _normalizar_telefono(body.get("telefono"))
    if not tel:
        return jsonify({"error": "telefono invalido"}), 400
    texto = (body.get("texto") or "").strip()
    if not texto:
        return jsonify({"error": "texto requerido"}), 400
    autor = (body.get("autor") or "massi").strip() or "massi"
    log.info("[tools] POST /notas tel=%s autor=%s", tel, autor)
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                "INSERT INTO pilot_notas (telefono, autor, texto) "
                "VALUES (%s, %s, %s) "
                "RETURNING id, telefono, autor, texto, created_at",
                (tel, autor, texto),
            ).fetchone()
    except Exception as exc:
        log.exception("[tools] /notas fallo tel=%s", tel)
        return jsonify({"error": str(exc)}), 500
    cols = ("id", "telefono", "autor", "texto", "created_at")
    out = dict(zip(cols, row))
    out["created_at"] = out["created_at"].isoformat() if out["created_at"] else None
    return jsonify(out), 200


@tools_bp.post("/recordatorios")
@_require_token
@_rate_limit
def crear_recordatorio():
    body = request.get_json(silent=True) or {}
    tel = _normalizar_telefono(body.get("telefono"))
    if not tel:
        return jsonify({"error": "telefono invalido"}), 400
    disparar = _parse_iso(body.get("disparar_at"))
    if disparar is None:
        return jsonify({"error": "disparar_at invalido (requiere ISO8601 con tz)"}), 400
    texto = (body.get("texto") or "").strip()
    if not texto:
        return jsonify({"error": "texto requerido"}), 400
    log.info("[tools] POST /recordatorios tel=%s at=%s", tel, disparar.isoformat())
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                "INSERT INTO pilot_recordatorios (telefono, disparar_at, texto) "
                "VALUES (%s, %s, %s) "
                "RETURNING id, telefono, disparar_at, texto, disparado, created_at",
                (tel, disparar, texto),
            ).fetchone()
    except Exception as exc:
        log.exception("[tools] /recordatorios fallo tel=%s", tel)
        return jsonify({"error": str(exc)}), 500
    cols = ("id", "telefono", "disparar_at", "texto", "disparado", "created_at")
    out = dict(zip(cols, row))
    out["disparar_at"] = out["disparar_at"].isoformat() if out["disparar_at"] else None
    out["created_at"] = out["created_at"].isoformat() if out["created_at"] else None
    return jsonify(out), 200


@tools_bp.post("/escalar")
@_require_token
@_rate_limit
def escalar():
    body = request.get_json(silent=True) or {}
    tel = _normalizar_telefono(body.get("telefono"))
    if not tel:
        return jsonify({"error": "telefono invalido"}), 400
    motivo = (body.get("motivo") or "").strip()
    if not motivo:
        return jsonify({"error": "motivo requerido"}), 400
    log.info("[tools] POST /escalar tel=%s", tel)
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                f"UPDATE pilot_leads SET "
                f"  requiere_humano = TRUE, "
                f"  requiere_humano_motivo = %s, "
                f"  requiere_humano_at = NOW(), "
                f"  updated_at = NOW() "
                f"WHERE telefono = %s "
                f"RETURNING {_LEAD_SELECT}",
                (motivo, tel),
            ).fetchone()
    except Exception as exc:
        log.exception("[tools] /escalar fallo tel=%s", tel)
        return jsonify({"error": str(exc)}), 500
    if not row:
        return jsonify({"error": "no encontrado"}), 404
    return jsonify(_row_lead(row, _LEAD_COLS)), 200


_SURETI_REQUIRED = (
    "nombre", "ciudad", "direccion", "barrio", "estrato", "es_ph", "hipoteca",
    "patrimonio_familia", "propietario_mayor_75", "paz_salvo", "avaluo",
    "destino_credito", "tipo_persona", "cedula_nit", "edad", "email",
)


@tools_bp.post("/sureti")
@_require_token
@_rate_limit
def enviar_sureti():
    body = request.get_json(silent=True) or {}
    tel = _normalizar_telefono(body.get("telefono"))
    if not tel:
        return jsonify({"error": "telefono invalido"}), 400
    log.info("[tools] POST /sureti tel=%s", tel)
    try:
        with _db._conexion_directa(timeout=10) as conn:
            lead_row = conn.execute(
                f"SELECT {_LEAD_SELECT} FROM pilot_leads WHERE telefono = %s",
                (tel,),
            ).fetchone()
            if not lead_row:
                return jsonify({"error": "no encontrado"}), 404
            lead = dict(zip(_LEAD_COLS, lead_row))
            faltantes = [c for c in _SURETI_REQUIRED if lead.get(c) is None]
            if faltantes:
                return jsonify({
                    "error": "Faltan datos para enviar a Sureti",
                    "faltantes": faltantes,
                }), 400
            conn.execute(
                "INSERT INTO pilot_notas (telefono, autor, texto) "
                "VALUES (%s, 'sistema', '[SURETI] Enviado a Sureti')",
                (tel,),
            )
            row = conn.execute(
                "UPDATE pilot_leads SET enviado_sureti_at = NOW(), updated_at = NOW() "
                "WHERE telefono = %s RETURNING enviado_sureti_at",
                (tel,),
            ).fetchone()
            # --- Sprint 7: oportunidad crédito → cerrado_ganado al enviar a Sureti ---
            contacto_id, oportunidad_id = _bridge_credito_asegurar_oportunidad(
                conn, tel, nombre=lead.get("nombre"),
                valor_cop=lead.get("monto_solicitado"),
            )
            if oportunidad_id:
                petra_core.cambiar_estado_oportunidad(
                    conn, oportunidad_id, "cerrado_ganado",
                    motivo="enviado_a_sureti", actor="massi",
                )
    except Exception as exc:
        log.exception("[tools] /sureti fallo tel=%s", tel)
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True, "enviado_at": row[0].isoformat() if row and row[0] else None}), 200


# =============================================================================
# Server Tool: inscribir agente Petra (Flujo 4 - Reclutamiento)
# =============================================================================

@tools_bp.post("/agente_inscribir")
@_require_token
@_rate_limit
def agente_inscribir():
    """Tool para que Massimiliano inscriba a un agente Petra.
    ElevenLabs envía automáticamente `telefono` (del system__caller_id).
    Payload: {telefono, nombre, ciudad, conjunto, disponibilidad?}
    """
    body = request.get_json(silent=True) or {}
    tel = _normalizar_telefono(body.get("telefono"))
    if not tel:
        return jsonify({"error": "telefono invalido"}), 400
    nombre = (body.get("nombre") or "").strip() or None
    ciudad = (body.get("ciudad") or "").strip() or None
    conjunto = (body.get("conjunto") or "").strip() or None
    disponibilidad = (body.get("disponibilidad") or "").strip() or None

    log.info("[tools] POST /agente_inscribir tel=%s nombre=%s ciudad=%s conjunto=%s",
             tel, nombre, ciudad, conjunto)
    try:
        with _db._conexion_directa(timeout=10) as conn:
            cid, estado = petra_core.inscribir_agente(
                conn, telefono=tel, nombre=nombre, ciudad=ciudad,
                origen=body.get("origen", "massimiliano_wa_chat"),
            )
            # Enriquecer metadata del rol con conjunto y disponibilidad
            if cid and (conjunto or disponibilidad):
                try:
                    import json as _json
                    row = conn.execute(
                        "SELECT metadata FROM petra_contacto_roles "
                        "WHERE contacto_id=%s AND rol='agente_petra' LIMIT 1",
                        (cid,),
                    ).fetchone()
                    meta = (row[0] if row else {}) or {}
                    if conjunto:
                        meta["conjunto"] = conjunto
                    if disponibilidad:
                        meta["disponibilidad"] = disponibilidad
                    conn.execute(
                        "UPDATE petra_contacto_roles SET metadata=%s::jsonb, "
                        "updated_at=NOW() WHERE contacto_id=%s AND rol='agente_petra'",
                        (_json.dumps(meta), cid),
                    )
                except Exception:
                    log.exception("[tools] /agente_inscribir meta fallo")
    except Exception as exc:
        log.exception("[tools] /agente_inscribir fallo tel=%s", tel)
        return jsonify({"error": str(exc)}), 500
    return jsonify({
        "ok": True,
        "contacto_id": cid,
        "estado_agente": estado,
        "mensaje": (
            f"Agente Petra registrado como '{estado}'. "
            "En las próximas 48h el equipo humano lo contactará."
        ),
    }), 200


# =============================================================================
# Server Tools adicionales (Sprint 19 — Fase 1)
# =============================================================================


@tools_bp.post("/petra_consultar_oportunidad")
@_require_token
@_rate_limit
def tool_consultar_oportunidad():
    """Devuelve resumen de oportunidades abiertas del contacto.
    Payload: {telefono (auto)}"""
    body = request.get_json(silent=True) or {}
    tel = _normalizar_telefono(body.get("telefono"))
    if not tel:
        return jsonify({"error": "telefono invalido"}), 400
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                "SELECT id, nombre FROM petra_contactos WHERE telefono=%s", (tel,),
            ).fetchone()
            if not row:
                return jsonify({"ok": True, "contacto": None, "oportunidades": []}), 200
            cid, nombre = row
            ops = conn.execute(
                """SELECT id, tipo, estado, inmueble_id, valor_cop, updated_at
                   FROM petra_oportunidades
                   WHERE contacto_id=%s
                     AND estado NOT IN ('cerrado_ganado','cerrado_perdido')
                   ORDER BY updated_at DESC LIMIT 5""",
                (cid,),
            ).fetchall()
    except Exception as exc:
        log.exception("[tools] /petra_consultar_oportunidad fallo tel=%s", tel)
        return jsonify({"error": str(exc)}), 500
    return jsonify({
        "ok": True,
        "contacto": {"id": cid, "nombre": nombre},
        "oportunidades": [
            {"id": o[0], "tipo": o[1], "estado": o[2],
             "inmueble_id": o[3], "valor_cop": o[4],
             "actualizado": o[5].isoformat() if o[5] else None}
            for o in ops
        ],
    }), 200


@tools_bp.post("/petra_abrir_caso")
@_require_token
@_rate_limit
def tool_abrir_caso():
    """Abre caso de soporte para contactos con contrato activo.
    Payload: {telefono (auto), categoria, descripcion, prioridad?}"""
    body = request.get_json(silent=True) or {}
    tel = _normalizar_telefono(body.get("telefono"))
    categoria = (body.get("categoria") or "").strip()
    descripcion = (body.get("descripcion") or "").strip()
    prioridad = (body.get("prioridad") or "media").strip()
    if not tel or not categoria or not descripcion:
        return jsonify({"error": "telefono, categoria y descripcion requeridos"}), 400
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                "SELECT id FROM petra_contactos WHERE telefono=%s", (tel,),
            ).fetchone()
            if not row:
                return jsonify({"error": "contacto no encontrado"}), 404
            cid = row[0]
            contrato_row = conn.execute(
                """SELECT id FROM petra_contratos
                   WHERE contacto_id=%s AND estado IN ('activo','firmado')
                   ORDER BY updated_at DESC LIMIT 1""",
                (cid,),
            ).fetchone()
            contrato_id = contrato_row[0] if contrato_row else None
            caso_id = petra_core.crear_caso_soporte(
                conn, contacto_id=cid, categoria=categoria,
                descripcion=descripcion, prioridad=prioridad,
                contrato_id=contrato_id,
            )
            sla_h = petra_core.SLA_HORAS.get(prioridad, 8)
    except Exception as exc:
        log.exception("[tools] /petra_abrir_caso fallo tel=%s", tel)
        return jsonify({"error": str(exc)}), 500
    return jsonify({
        "ok": True,
        "caso_id": caso_id,
        "sla_horas": sla_h,
        "mensaje": (
            f"Caso #{caso_id} ({categoria}, {prioridad}). "
            f"Un humano responderá en máximo {sla_h}h."
        ),
    }), 200


@tools_bp.post("/petra_otorgar_consentimiento")
@_require_token
@_rate_limit
def tool_otorgar_consentimiento():
    """Registra consentimiento del contacto.
    Payload: {telefono (auto), finalidad, canal?, evidencia_texto?}"""
    body = request.get_json(silent=True) or {}
    tel = _normalizar_telefono(body.get("telefono"))
    finalidad = (body.get("finalidad") or "").strip()
    canal = (body.get("canal") or "whatsapp").strip()
    evidencia_texto = (body.get("evidencia_texto") or "").strip() or None
    if not tel or not finalidad:
        return jsonify({"error": "telefono y finalidad requeridos"}), 400
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                "SELECT id FROM petra_contactos WHERE telefono=%s", (tel,),
            ).fetchone()
            if not row:
                # Crear contacto mínimo para registrar el consentimiento
                row = conn.execute(
                    "INSERT INTO petra_contactos (telefono) VALUES (%s) RETURNING id",
                    (tel,),
                ).fetchone()
            cid = row[0]
            consentimiento_id = petra_core.otorgar_consentimiento(
                conn, contacto_id=cid, finalidad=finalidad, canal=canal,
                evidencia={"texto_usuario": evidencia_texto, "canal": canal} if evidencia_texto else {"canal": canal},
            )
    except Exception as exc:
        log.exception("[tools] /petra_otorgar_consentimiento fallo tel=%s", tel)
        return jsonify({"error": str(exc)}), 500
    return jsonify({
        "ok": True,
        "consentimiento_id": consentimiento_id,
        "mensaje": f"Consentimiento '{finalidad}' registrado.",
    }), 200


@tools_bp.post("/petra_publicar_inmueble")
@_require_token
@_rate_limit
def tool_publicar_inmueble():
    """Crea un inmueble en un edificio Petra.
    Payload: {telefono (auto), edificio_slug, tipo, modalidad, torre?, piso?,
              area_m2?, parqueadero?, precio_millones?, precio_arriendo_millones?,
              nombre_anunciante?}"""
    body = request.get_json(silent=True) or {}
    tel = _normalizar_telefono(body.get("telefono"))
    edif_slug = (body.get("edificio_slug") or "").strip().lower()
    tipo = (body.get("tipo") or "").strip().lower()
    modalidad = (body.get("modalidad") or "").strip().lower()
    if not tel or not edif_slug or not tipo or not modalidad:
        return jsonify({"error": "telefono, edificio_slug, tipo y modalidad requeridos"}), 400
    import json as _json
    try:
        with _db._conexion_directa(timeout=10) as conn:
            edif = conn.execute(
                "SELECT id, nombre FROM petra_edificios WHERE qr_slug=%s AND activo=TRUE",
                (edif_slug,),
            ).fetchone()
            if not edif:
                return jsonify({"error": f"edificio '{edif_slug}' no encontrado"}), 404
            edif_id, edif_nombre = edif
            c_row = conn.execute(
                "SELECT id FROM petra_contactos WHERE telefono=%s", (tel,),
            ).fetchone()
            if not c_row:
                c_row = conn.execute(
                    "INSERT INTO petra_contactos (telefono, nombre) VALUES (%s,%s) RETURNING id",
                    (tel, body.get("nombre_anunciante")),
                ).fetchone()
            contacto_id = c_row[0]
            dedup = petra_core.dedup_hash_inmueble(
                edif_id, body.get("torre"), body.get("piso"),
                body.get("apto"), tipo, modalidad,
            )
            existente = conn.execute(
                """SELECT id FROM petra_inmuebles
                   WHERE dedup_hash=%s
                     AND estado IN ('borrador','activo','reservado','pausado')
                   LIMIT 1""",
                (dedup,),
            ).fetchone()
            if existente:
                return jsonify({
                    "ok": False,
                    "duplicado_id": existente[0],
                    "mensaje": "Ya existe un anuncio activo con esos datos.",
                }), 200
            row = conn.execute(
                """INSERT INTO petra_inmuebles
                   (edificio_id, contacto_id, tipo, modalidad, torre, piso,
                    area_m2, parqueadero, precio_millones,
                    precio_arriendo_mensual_millones,
                    nombre_anunciante, telefono_contacto, dedup_hash,
                    estado, publicado_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'activo',NOW())
                   RETURNING id""",
                (edif_id, contacto_id, tipo, modalidad,
                 body.get("torre"), body.get("piso"), body.get("area_m2"),
                 body.get("parqueadero"), body.get("precio_millones"),
                 body.get("precio_arriendo_millones"),
                 body.get("nombre_anunciante"), tel, dedup),
            ).fetchone()
            inm_id = row[0]
            conn.execute(
                """INSERT INTO petra_contacto_roles
                   (contacto_id, rol, estado, edificio_id, metadata)
                   VALUES (%s, 'anunciante', 'activo', %s, %s::jsonb)
                   ON CONFLICT (contacto_id, rol, edificio_id) DO UPDATE SET
                     estado='activo', updated_at=NOW()""",
                (contacto_id, edif_id, _json.dumps({"ultimo_inmueble_id": inm_id})),
            )
    except Exception as exc:
        log.exception("[tools] /petra_publicar_inmueble fallo tel=%s", tel)
        return jsonify({"error": str(exc)}), 500
    return jsonify({
        "ok": True,
        "inmueble_id": inm_id,
        "mensaje": f"Publicado en {edif_nombre}.",
    }), 200


# =============================================================================
# Flujo 2 — Mensajes interactivos (botones / listas) desde Massimiliano
# =============================================================================
@tools_bp.post("/petra_enviar_menu_rama")
@_require_token
@_rate_limit
def tool_enviar_menu_rama():
    """Envía al usuario una lista interactiva con las 4 ramas de Petra.
    Massi la dispara cuando no logra distinguir qué quiere el usuario, o
    como primer prompt tras un saludo neutro.
    Payload: {telefono (auto)}"""
    from app import petra_wa as _wa
    body = request.get_json(silent=True) or {}
    tel = _normalizar_telefono(body.get("telefono"))
    if not tel:
        return jsonify({"error": "telefono requerido"}), 400
    res = _wa.send_list(
        tel,
        "¿Con qué te ayudo? Elige una opción:",
        "Ver opciones",
        [{
            "title": "Petra",
            "rows": [
                {"id": "RAMA_PUBLICAR", "title": "Publicar inmueble",
                 "description": "Vender o arrendar tu propiedad"},
                {"id": "RAMA_BUSCAR", "title": "Buscar inmueble",
                 "description": "Explorar inventario disponible"},
                {"id": "RAMA_CREDITO", "title": "Pedir crédito",
                 "description": "Crédito con garantía inmobiliaria"},
                {"id": "RAMA_AGENTE", "title": "Ser Agente Petra",
                 "description": "Ganar comisión en tu edificio"},
                {"id": "RAMA_SOPORTE", "title": "Soporte",
                 "description": "Hablar con una persona"},
            ],
        }],
    )
    return jsonify({"ok": bool(res.get("ok")), "resultado": res,
                    "mensaje": "Menú de ramas enviado."}), 200


@tools_bp.post("/petra_confirmar_visita_botones")
@_require_token
@_rate_limit
def tool_confirmar_visita_botones():
    """Envía 3 botones para que el usuario confirme/reagende/cancele una
    visita agendada. Payload: {telefono (auto), visita_id}"""
    from app import petra_wa as _wa
    body = request.get_json(silent=True) or {}
    tel = _normalizar_telefono(body.get("telefono"))
    vid = body.get("visita_id")
    if not tel or not vid:
        return jsonify({"error": "telefono y visita_id requeridos"}), 400
    res = _wa.send_buttons(
        tel,
        f"¿Confirmas tu visita? (ref #{vid})",
        [
            {"id": f"VISITA_SI_{vid}", "title": "Sí, confirmo"},
            {"id": f"VISITA_REAGENDAR_{vid}", "title": "Reagendar"},
            {"id": f"VISITA_NO_{vid}", "title": "Cancelar"},
        ],
    )
    return jsonify({"ok": bool(res.get("ok")), "resultado": res,
                    "mensaje": "Botones de confirmación enviados."}), 200


@tools_bp.post("/petra_elegir_modalidad")
@_require_token
@_rate_limit
def tool_elegir_modalidad():
    """Botones para elegir modalidad al publicar/buscar (venta/arriendo).
    Payload: {telefono (auto), intencion: 'publicar'|'buscar'}"""
    from app import petra_wa as _wa
    body = request.get_json(silent=True) or {}
    tel = _normalizar_telefono(body.get("telefono"))
    intencion = (body.get("intencion") or "publicar").strip().lower()
    if not tel:
        return jsonify({"error": "telefono requerido"}), 400
    verbo = "publicar" if intencion == "publicar" else "buscar"
    res = _wa.send_buttons(
        tel,
        f"¿Qué modalidad quieres {verbo}?",
        [
            {"id": f"MOD_VENTA_{intencion}", "title": "Venta"},
            {"id": f"MOD_ARRIENDO_{intencion}", "title": "Arriendo"},
            {"id": f"MOD_AMBAS_{intencion}", "title": "Ambas"},
        ],
    )
    return jsonify({"ok": bool(res.get("ok")), "resultado": res,
                    "mensaje": "Botones de modalidad enviados."}), 200
