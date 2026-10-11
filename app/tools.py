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

log = logging.getLogger("petra")

tools_bp = Blueprint("tools", __name__, url_prefix="/tools")

_TEL_RE = re.compile(r"^\+?\d{10,15}$")
_TEL_STRIP_RE = re.compile(r"[\s\-\.]+")


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
        # Py 3.11+ fromisoformat acepta 'Z'; aceptamos ambos por seguridad.
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


def _require_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = os.environ.get("TOOLS_API_TOKEN", "")
        if not token or request.headers.get("X-Tools-Token") != token:
            return jsonify({"error": "no_autorizado"}), 401
        return fn(*args, **kwargs)
    return wrapper


def _row_lead(row, cols):
    d = dict(zip(cols, row))
    for k in ("created_at", "updated_at", "requiere_humano_at", "enviado_sureti_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    for k in ("avaluo", "monto_solicitado"):
        if d.get(k) is not None:
            d[k] = float(d[k])
    return d


_LEAD_COLS = (
    "id", "telefono", "nombre", "ciudad", "tipo_inmueble", "avaluo",
    "monto_solicitado", "destino_credito", "requiere_humano",
    "requiere_humano_motivo", "requiere_humano_at", "enviado_sureti_at",
    "created_at", "updated_at",
)
_LEAD_SELECT = ", ".join(_LEAD_COLS)


@tools_bp.post("/leads")
@_require_token
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
    try:
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                f"INSERT INTO pilot_leads "
                f"(telefono, nombre, ciudad, tipo_inmueble, avaluo, monto_solicitado, destino_credito) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s) "
                f"ON CONFLICT (telefono) DO UPDATE SET "
                f"  nombre = COALESCE(EXCLUDED.nombre, pilot_leads.nombre), "
                f"  ciudad = COALESCE(EXCLUDED.ciudad, pilot_leads.ciudad), "
                f"  tipo_inmueble = COALESCE(EXCLUDED.tipo_inmueble, pilot_leads.tipo_inmueble), "
                f"  avaluo = COALESCE(EXCLUDED.avaluo, pilot_leads.avaluo), "
                f"  monto_solicitado = COALESCE(EXCLUDED.monto_solicitado, pilot_leads.monto_solicitado), "
                f"  destino_credito = COALESCE(EXCLUDED.destino_credito, pilot_leads.destino_credito), "
                f"  updated_at = NOW() "
                f"RETURNING {_LEAD_SELECT}",
                (tel, nombre, ciudad, tipo, avaluo, monto, destino),
            ).fetchone()
    except Exception as exc:
        log.exception("[tools] /leads fallo tel=%s", tel)
        return jsonify({"error": str(exc)}), 500
    return jsonify(_row_lead(row, _LEAD_COLS)), 200


@tools_bp.get("/leads/<telefono>")
@_require_token
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


@tools_bp.post("/sureti")
@_require_token
def enviar_sureti():
    body = request.get_json(silent=True) or {}
    tel = _normalizar_telefono(body.get("telefono"))
    if not tel:
        return jsonify({"error": "telefono invalido"}), 400
    log.info("[tools] POST /sureti tel=%s", tel)
    try:
        with _db._conexion_directa(timeout=10) as conn:
            fila = conn.execute(
                "SELECT 1 FROM pilot_leads WHERE telefono = %s", (tel,)
            ).fetchone()
            if not fila:
                return jsonify({"error": "no encontrado"}), 404
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
    except Exception as exc:
        log.exception("[tools] /sureti fallo tel=%s", tel)
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True, "enviado_at": row[0].isoformat() if row and row[0] else None}), 200
