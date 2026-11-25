"""MCP server para Petra — expone tools de agentes vía JSON-RPC HTTP.

Sofía lo agrega en claude.ai → Settings → Custom MCP servers:
  URL: https://petra-staging.onrender.com/mcp (o prod)
  Auth: Authorization: Bearer <PETRA_MCP_KEY>

Transport: Streamable HTTP (POST /mcp con JSON-RPC 2.0).
"""

from __future__ import annotations

import json
import logging
import os

from flask import Blueprint, Response, jsonify, request

from app import db as _db

log = logging.getLogger("petra.mcp")

petra_mcp_bp = Blueprint("petra_mcp", __name__)


# ---------------------------------------------------------------------------
# Tools definidos
# ---------------------------------------------------------------------------

_TOOLS = [
    {
        "name": "listar_agentes",
        "description": (
            "Lista agentes Petra, opcionalmente filtrando por estado, "
            "ciudad o búsqueda de texto (nombre o teléfono). Devuelve hasta "
            "100 agentes con sus datos básicos."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "estado": {
                    "type": "string",
                    "description": (
                        "Filtra por estado. Valores: pendiente, recolectando, "
                        "inscrito, bienvenida_enviada, en_capacitacion, "
                        "capacitacion_completa, autorizacion_pendiente, "
                        "autorizacion_rechazada, kit_pendiente, kit_enviado, "
                        "activa, pausada, certificado."
                    ),
                },
                "ciudad": {"type": "string", "description": "Ej: 'Bogotá'."},
                "q": {"type": "string", "description": "Texto libre en nombre o teléfono."},
            },
        },
    },
    {
        "name": "ficha_agente",
        "description": "Devuelve toda la info conocida de un agente por teléfono E.164.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "telefono": {"type": "string", "description": "Ej: '573196287568'."},
            },
            "required": ["telefono"],
        },
    },
    {
        "name": "cambiar_estado_agente",
        "description": (
            "Cambia el estado de un agente. Útil para 'pasar a inscrito', "
            "'marcar como activa', etc."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "telefono": {"type": "string"},
                "estado": {
                    "type": "string",
                    "description": "Nuevo estado (ver listar_agentes para valores).",
                },
            },
            "required": ["telefono", "estado"],
        },
    },
    {
        "name": "crear_agente",
        "description": "Da de alta una nueva agente con estado inicial 'pendiente'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string"},
                "telefono": {"type": "string", "description": "E.164, ej '573001234567'."},
                "ciudad": {"type": "string"},
            },
            "required": ["nombre", "telefono"],
        },
    },
    {
        "name": "listar_conjuntos",
        "description": "Lista conjuntos con su código, nombre y ciudad.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_ESTADOS_VALIDOS = {
    "pendiente", "recolectando", "inscrito", "bienvenida_enviada",
    "esperando_capacitacion_dia1", "en_capacitacion",
    "capacitacion_completa", "autorizacion_pendiente",
    "autorizacion_rechazada", "kit_pendiente", "kit_enviado",
    "activa", "pausada", "certificado",
}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _check_api_key():
    expected = (os.environ.get("PETRA_MCP_KEY") or "").strip()
    if not expected:
        return False  # sin key → bloqueado (no se expone sin password)
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.startswith("Bearer "):
        got = auth[7:].strip()
    else:
        got = (request.args.get("key") or "").strip()
    return got == expected


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _t_listar_agentes(args):
    estado = (args.get("estado") or "").strip()
    ciudad = (args.get("ciudad") or "").strip()
    q = (args.get("q") or "").strip()
    where = ["rol = 'agente_petra'"]
    params = []
    if estado:
        where.append("metadata->>'estado_agente' = %s")
        params.append(estado)
    if ciudad:
        where.append("LOWER(COALESCE(metadata->>'ciudad','')) = LOWER(%s)")
        params.append(ciudad)
    if q:
        where.append("(nombre ILIKE %s OR telefono ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])
    sql = (
        "SELECT telefono, nombre, "
        "       COALESCE(metadata->>'estado_agente','pendiente') AS estado, "
        "       COALESCE(metadata->>'ciudad','') AS ciudad, "
        "       actualizado_en "
        "  FROM petra_contacto_roles "
        " WHERE " + " AND ".join(where) +
        " ORDER BY actualizado_en DESC LIMIT 100"
    )
    out = []
    with _db._conexion_directa(timeout=10) as conn:
        for row in conn.execute(sql, tuple(params)).fetchall():
            out.append({
                "telefono": row[0], "nombre": row[1],
                "estado": row[2], "ciudad": row[3],
                "ultima_actualizacion": row[4].isoformat() if row[4] else None,
            })
    return {"count": len(out), "agentes": out}


def _t_ficha_agente(args):
    tel = (args.get("telefono") or "").strip()
    if not tel:
        return {"error": "falta telefono"}
    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT telefono, nombre, metadata, actualizado_en "
            "  FROM petra_contacto_roles "
            " WHERE rol='agente_petra' AND telefono = %s", (tel,)
        ).fetchone()
    if not row:
        return {"error": "no_encontrada", "telefono": tel}
    return {
        "telefono": row[0], "nombre": row[1],
        "metadata": row[2] or {},
        "ultima_actualizacion": row[3].isoformat() if row[3] else None,
    }


def _t_cambiar_estado_agente(args):
    tel = (args.get("telefono") or "").strip()
    estado = (args.get("estado") or "").strip()
    if not tel or not estado:
        return {"error": "falta telefono o estado"}
    if estado not in _ESTADOS_VALIDOS:
        return {"error": "estado_invalido", "validos": sorted(_ESTADOS_VALIDOS)}
    with _db._conexion_directa(timeout=10) as conn:
        cur = conn.execute(
            "UPDATE petra_contacto_roles "
            "   SET metadata = jsonb_set(COALESCE(metadata,'{}'::jsonb), "
            "                            '{estado_agente}', to_jsonb(%s::text)), "
            "       actualizado_en = NOW() "
            " WHERE rol='agente_petra' AND telefono = %s "
            "RETURNING telefono",
            (estado, tel),
        )
        found = cur.fetchone()
        conn.commit()
    if not found:
        return {"error": "no_encontrada", "telefono": tel}
    return {"ok": True, "telefono": tel, "estado": estado}


def _t_crear_agente(args):
    nombre = (args.get("nombre") or "").strip()
    tel = (args.get("telefono") or "").strip()
    ciudad = (args.get("ciudad") or "").strip()
    if not nombre or not tel:
        return {"error": "falta nombre o telefono"}
    metadata = {"estado_agente": "pendiente"}
    if ciudad:
        metadata["ciudad"] = ciudad
    with _db._conexion_directa(timeout=10) as conn:
        cur = conn.execute(
            "INSERT INTO petra_contacto_roles (telefono, rol, nombre, metadata) "
            "VALUES (%s, 'agente_petra', %s, %s::jsonb) "
            "ON CONFLICT (telefono, rol) DO UPDATE "
            "   SET nombre = EXCLUDED.nombre, "
            "       metadata = petra_contacto_roles.metadata || EXCLUDED.metadata, "
            "       actualizado_en = NOW() "
            "RETURNING telefono",
            (tel, nombre, json.dumps(metadata)),
        )
        cur.fetchone()
        conn.commit()
    return {"ok": True, "telefono": tel, "nombre": nombre, "estado": "pendiente"}


def _t_listar_conjuntos(args):
    with _db._conexion_directa(timeout=10) as conn:
        rows = conn.execute(
            "SELECT codigo, nombre, ciudad "
            "  FROM conjuntos_visitante "
            " ORDER BY nombre ASC LIMIT 500"
        ).fetchall()
    return {
        "count": len(rows),
        "conjuntos": [
            {"codigo": r[0], "nombre": r[1], "ciudad": r[2] or ""}
            for r in rows
        ],
    }


_TOOL_FNS = {
    "listar_agentes": _t_listar_agentes,
    "ficha_agente": _t_ficha_agente,
    "cambiar_estado_agente": _t_cambiar_estado_agente,
    "crear_agente": _t_crear_agente,
    "listar_conjuntos": _t_listar_conjuntos,
}


# ---------------------------------------------------------------------------
# JSON-RPC handler (MCP Streamable HTTP transport)
# ---------------------------------------------------------------------------

def _rpc_result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _dispatch(body):
    """Procesa un solo JSON-RPC request. Devuelve dict de respuesta."""
    req_id = body.get("id")
    method = body.get("method") or ""
    params = body.get("params") or {}

    if method == "initialize":
        return _rpc_result(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "petra-mcp", "version": "0.1.0"},
        })

    if method == "notifications/initialized":
        return None  # notification — no response

    if method == "tools/list":
        return _rpc_result(req_id, {"tools": _TOOLS})

    if method == "tools/call":
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        fn = _TOOL_FNS.get(name)
        if not fn:
            return _rpc_error(req_id, -32601, f"Tool no encontrada: {name}")
        try:
            result = fn(args)
        except Exception as e:
            log.exception("[mcp] tool %s fallo", name)
            return _rpc_error(req_id, -32000, f"Error ejecutando tool: {type(e).__name__}: {e}")
        return _rpc_result(req_id, {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}],
            "isError": False,
        })

    if method == "ping":
        return _rpc_result(req_id, {})

    return _rpc_error(req_id, -32601, f"Método no soportado: {method}")


@petra_mcp_bp.route("/mcp", methods=["POST", "GET", "DELETE"])
def mcp_endpoint():
    if not _check_api_key():
        return jsonify({"error": "unauthorized"}), 401

    if request.method == "GET":
        # Transport GET para SSE; respondemos vacío (no mantenemos session server-side).
        return Response("", status=200, mimetype="text/event-stream")

    if request.method == "DELETE":
        # Termina sesión; no mantenemos state.
        return "", 204

    # POST: puede ser un request o batch.
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "Parse error"}}), 400

    if isinstance(body, list):
        responses = [r for r in (_dispatch(b) for b in body) if r is not None]
        return jsonify(responses) if responses else ("", 202)

    resp = _dispatch(body)
    if resp is None:
        return "", 202
    return jsonify(resp)
