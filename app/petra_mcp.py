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
    {
        "name": "asignar_codigo",
        "description": (
            "Asigna un código de conjunto (ej. '1024') a una agente por su "
            "teléfono. Una agente puede tener varios códigos. Si no se pasa "
            "fecha_envio, usa hoy."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "telefono": {"type": "string", "description": "Teléfono E.164 de la agente."},
                "codigo": {"type": "string", "description": "Código del conjunto (ej. '1024')."},
                "fecha_envio": {
                    "type": "string",
                    "description": "Fecha real de envío del QR (YYYY-MM-DD). Opcional.",
                },
            },
            "required": ["telefono", "codigo"],
        },
    },
    {
        "name": "ajustar_fecha_envio_codigo",
        "description": (
            "Corrige la fecha de envío de un código ya asignado a una agente. "
            "Útil cuando se asignaron ayer varios códigos pero algunos se "
            "habían mandado en otra fecha. Pasá fecha_envio='' (string vacío) "
            "para dejar el campo SIN FECHA (desconocida)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "telefono": {"type": "string"},
                "codigo": {"type": "string"},
                "fecha_envio": {
                    "type": "string",
                    "description": "YYYY-MM-DD o '' para borrar (sin fecha).",
                },
            },
            "required": ["telefono", "codigo", "fecha_envio"],
        },
    },
    {
        "name": "quitar_codigo",
        "description": "Quita un código de conjunto de una agente.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "telefono": {"type": "string"},
                "codigo": {"type": "string"},
            },
            "required": ["telefono", "codigo"],
        },
    },
    {
        "name": "agregar_media",
        "description": (
            "Descarga un archivo (foto/video/PDF) desde una URL y lo guarda "
            "asociado a un conjunto (y opcionalmente a un inmueble). Útil "
            "cuando Del Mar/Valentina adjunta una imagen en el chat: pasá la "
            "URL de la imagen y el código del conjunto. Los archivos quedan "
            "en nuestro repo de media con una URL pública."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "codigo_conjunto": {"type": "string", "description": "Ej: '1024'."},
                "url": {"type": "string", "description": "URL de donde descargar el archivo."},
                "tipo": {
                    "type": "string",
                    "description": (
                        "Categoría. Específicas (preferir cuando aplique): "
                        "qr_instalado | zonas_comunes | frente_conjunto | inventario | "
                        "libro_arriendos | libro_ventas. "
                        "Genéricas: foto_conjunto | foto_inmueble | foto_agente | "
                        "video_conjunto | video_inmueble | video_agente | "
                        "documento | otro. Default: foto_conjunto."
                    ),
                },
                "codigo_inmueble": {
                    "type": "string",
                    "description": "Si la media es de un inmueble específico, ej. 'P0165'.",
                },
                "nota": {"type": "string", "description": "Descripción corta."},
            },
            "required": ["codigo_conjunto", "url"],
        },
    },
    {
        "name": "listar_media_conjunto",
        "description": "Lista todas las fotos/videos/documentos de un conjunto. Cada item trae su `id` que podés usar para cambiar el tipo con actualizar_media_tipo.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "codigo_conjunto": {"type": "string"},
                "tipo": {
                    "type": "string",
                    "description": "Filtra por tipo (opcional).",
                },
            },
            "required": ["codigo_conjunto"],
        },
    },
    {
        "name": "actualizar_media_tipo",
        "description": (
            "Cambia el campo `tipo` de una media ya subida. Útil cuando Del Mar "
            "ve que una foto se categorizó mal (ej. estaba como foto_conjunto y en "
            "realidad es foto del QR instalado)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "media_id": {
                    "type": "integer",
                    "description": "ID de la media (lo devuelve listar_media_conjunto en el campo `id`).",
                },
                "tipo": {
                    "type": "string",
                    "description": (
                        "Nuevo tipo. Específicas: qr_instalado | zonas_comunes | "
                        "frente_conjunto | inventario | libro_arriendos | libro_ventas. "
                        "Genéricas: foto_conjunto | foto_inmueble | foto_agente | "
                        "video_conjunto | video_inmueble | video_agente | documento | otro."
                    ),
                },
                "nota": {
                    "type": "string",
                    "description": "Opcional: reemplaza la nota existente.",
                },
            },
            "required": ["media_id", "tipo"],
        },
    },
    {
        "name": "actualizar_agente",
        "description": (
            "Actualiza datos de la AGENTE (persona). Campos soportados: "
            "nombre, cedula, ciudad, direccion_agente, notas. "
            "Los datos del conjunto (dirección, ubicación, fechas, flags "
            "de envío) se editan con actualizar_conjunto."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "telefono": {"type": "string"},
                "campos": {
                    "type": "object",
                    "description": "Diccionario con los campos a actualizar.",
                    "properties": {
                        "nombre": {"type": "string"},
                        "cedula": {"type": "string"},
                        "ciudad": {"type": "string"},
                        "direccion_agente": {"type": "string"},
                        "notas": {"type": "string"},
                    },
                },
            },
            "required": ["telefono", "campos"],
        },
    },
    {
        "name": "actualizar_conjunto",
        "description": (
            "Actualiza datos del CONJUNTO (lugar). Campos soportados: "
            "nombre, ciudad, barrio, direccion, ubicacion_maps, "
            "fecha_activacion (ISO YYYY-MM-DD), envio_fotos_inventario "
            "(bool), envio_qr_instalado (bool, foto o video del QR ya instalado), "
            "envio_zonas_comunes (bool, foto o video de zonas comunes), "
            "envio_frente_conjunto (bool, foto o video del frente del conjunto), "
            "envio_videos_conjunto (bool, legacy), "
            "envio_inventario_inmueble (bool, legacy), notas."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "codigo": {"type": "string", "description": "Código del conjunto."},
                "campos": {
                    "type": "object",
                    "properties": {
                        "nombre": {"type": "string"},
                        "ciudad": {"type": "string"},
                        "barrio": {"type": "string"},
                        "direccion": {"type": "string"},
                        "ubicacion_maps": {"type": "string"},
                        "fecha_activacion": {"type": "string"},
                        "envio_fotos_inventario": {"type": "boolean"},
                        "envio_qr_instalado": {"type": "boolean"},
                        "envio_zonas_comunes": {"type": "boolean"},
                        "envio_frente_conjunto": {"type": "boolean"},
                        "envio_videos_conjunto": {"type": "boolean"},
                        "envio_inventario_inmueble": {"type": "boolean"},
                        "notas": {"type": "string"},
                    },
                },
            },
            "required": ["codigo", "campos"],
        },
    },
    {
        "name": "ficha_conjunto",
        "description": (
            "Devuelve toda la info de un conjunto por código (nombre, "
            "ciudad, barrio, dirección, maps, fecha activación, flags de "
            "envío, notas) + la lista de agentes que tienen asignado ese "
            "código."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"codigo": {"type": "string"}},
            "required": ["codigo"],
        },
    },
    {
        "name": "cambiar_telefono_agente",
        "description": (
            "Migra el teléfono de una agente existente (ej. de un placeholder "
            "'570000000001' al real '573001234567'). Conserva estado, códigos "
            "asignados y toda la ficha."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "telefono_actual": {"type": "string"},
                "telefono_nuevo": {"type": "string"},
            },
            "required": ["telefono_actual", "telefono_nuevo"],
        },
    },
    {
        "name": "eliminar_agente",
        "description": (
            "Borra la ficha de una agente permanentemente. Requiere confirm=true. "
            "Elimina el contacto, su rol agente, códigos asignados y datos. "
            "Usar sólo para limpiar duplicados o fichas de prueba."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "telefono": {"type": "string"},
                "confirm": {
                    "type": "boolean",
                    "description": "Debe ser true para ejecutar el borrado.",
                },
            },
            "required": ["telefono", "confirm"],
        },
    },
]

# Campos de la AGENTE (persona). Los flags de envío de fotos/videos y
# ubicación/dirección/fecha_activacion ahora viven en el CONJUNTO
# (conjuntos_visitante), no en la agente.
_CAMPOS_METADATA = {
    "cedula",
    "ciudad",
    "direccion_agente",
    "notas",
}

# Campos del CONJUNTO (lugar) que puede editar Del Mar.
_CAMPOS_CONJUNTO = {
    "nombre", "ciudad", "barrio", "direccion", "ubicacion_maps",
    "fecha_activacion", "envio_fotos_inventario",
    "envio_videos_conjunto", "envio_inventario_inmueble",
    "envio_qr_instalado", "envio_zonas_comunes", "envio_frente_conjunto",
    "notas",
}

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
    where = ["r.rol = 'agente_petra'"]
    params = []
    if estado:
        where.append("r.metadata->>'estado_agente' = %s")
        params.append(estado)
    if ciudad:
        where.append("LOWER(COALESCE(r.metadata->>'ciudad','')) = LOWER(%s)")
        params.append(ciudad)
    if q:
        where.append("(c.nombre ILIKE %s OR c.telefono ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])
    sql = (
        "SELECT c.id, c.telefono, c.nombre, "
        "       COALESCE(r.metadata->>'estado_agente','pendiente') AS estado, "
        "       COALESCE(r.metadata->>'ciudad','') AS ciudad, "
        "       r.updated_at "
        "  FROM petra_contacto_roles r "
        "  JOIN petra_contactos c ON c.id = r.contacto_id "
        " WHERE " + " AND ".join(where) +
        " ORDER BY r.updated_at DESC LIMIT 100"
    )
    out = []
    with _db._conexion_directa(timeout=10) as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
        ids = [r[0] for r in rows]
        codigos_por_contacto = _codigos_por_contacto(conn, ids) if ids else {}
        for row in rows:
            cid = row[0]
            out.append({
                "telefono": row[1], "nombre": row[2],
                "estado": row[3], "ciudad": row[4],
                "codigos": codigos_por_contacto.get(cid, []),
                "ultima_actualizacion": row[5].isoformat() if row[5] else None,
            })
    return {"count": len(out), "agentes": out}


def _codigos_por_contacto(conn, contacto_ids):
    """Devuelve {contacto_id: [{codigo, nombre, ciudad}]}."""
    if not contacto_ids:
        return {}
    rows = conn.execute(
        "SELECT ac.contacto_id, cv.codigo, cv.nombre, cv.ciudad "
        "  FROM petra_agente_conjuntos ac "
        "  JOIN conjuntos_visitante cv ON cv.id = ac.conjunto_id "
        " WHERE ac.contacto_id = ANY(%s) "
        " ORDER BY cv.codigo ASC", (contacto_ids,)
    ).fetchall()
    out = {}
    for cid, cod, nom, ciu in rows:
        out.setdefault(cid, []).append({
            "codigo": cod, "nombre": nom, "ciudad": ciu or "",
        })
    return out


def _t_ficha_agente(args):
    tel = (args.get("telefono") or "").strip()
    if not tel:
        return {"error": "falta telefono"}
    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT c.id, c.telefono, c.nombre, r.metadata, r.updated_at "
            "  FROM petra_contacto_roles r "
            "  JOIN petra_contactos c ON c.id = r.contacto_id "
            " WHERE r.rol='agente_petra' AND c.telefono = %s", (tel,)
        ).fetchone()
        if not row:
            return {"error": "no_encontrada", "telefono": tel}
        # Trae códigos + todos los campos del conjunto + fecha_envio (de la
        # asignación) para que la ficha muestre todo lo del conjunto.
        conjuntos = conn.execute(
            "SELECT cv.codigo, cv.nombre, cv.ciudad, cv.barrio, "
            "       cv.direccion, cv.ubicacion_maps, cv.fecha_activacion, "
            "       cv.envio_fotos_inventario, cv.envio_videos_conjunto, "
            "       cv.envio_inventario_inmueble, cv.notas, ac.asignado_at, "
            "       cv.envio_qr_instalado, cv.envio_zonas_comunes, "
            "       cv.envio_frente_conjunto "
            "  FROM petra_agente_conjuntos ac "
            "  JOIN conjuntos_visitante cv ON cv.id = ac.conjunto_id "
            " WHERE ac.contacto_id = %s ORDER BY cv.codigo", (row[0],)
        ).fetchall()
    meta = row[3] or {}
    return {
        "telefono": row[1],
        "nombre": row[2],
        "estado": meta.get("estado_agente", "pendiente"),
        "cedula": meta.get("cedula", ""),
        "ciudad": meta.get("ciudad", ""),
        "direccion_agente": meta.get("direccion_agente", ""),
        "notas": meta.get("notas", ""),
        "conjuntos": [
            {
                "codigo": c[0], "nombre": c[1], "ciudad": c[2] or "",
                "barrio": c[3] or "", "direccion": c[4] or "",
                "ubicacion_maps": c[5] or "",
                "fecha_activacion": c[6].isoformat() if c[6] else "",
                "envio_fotos_inventario": bool(c[7]),
                "envio_videos_conjunto": bool(c[8]),
                "envio_inventario_inmueble": bool(c[9]),
                "notas": c[10] or "",
                "fecha_envio_qr": c[11].isoformat() if c[11] else "",
                "envio_qr_instalado": bool(c[12]),
                "envio_zonas_comunes": bool(c[13]),
                "envio_frente_conjunto": bool(c[14]),
            }
            for c in conjuntos
        ],
        "ultima_actualizacion": row[4].isoformat() if row[4] else None,
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
            "UPDATE petra_contacto_roles r "
            "   SET metadata = jsonb_set(COALESCE(r.metadata,'{}'::jsonb), "
            "                            '{estado_agente}', to_jsonb(%s::text)), "
            "       updated_at = NOW() "
            "  FROM petra_contactos c "
            " WHERE r.contacto_id = c.id "
            "   AND r.rol = 'agente_petra' "
            "   AND c.telefono = %s "
            "RETURNING c.telefono",
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
        # 1. upsert contacto
        cur = conn.execute(
            "INSERT INTO petra_contactos (telefono, nombre) VALUES (%s, %s) "
            "ON CONFLICT (telefono) DO UPDATE SET nombre = EXCLUDED.nombre "
            "RETURNING id",
            (tel, nombre),
        )
        contacto_id = cur.fetchone()[0]
        # 2. upsert rol agente_petra
        conn.execute(
            "INSERT INTO petra_contacto_roles (contacto_id, rol, metadata) "
            "VALUES (%s, 'agente_petra', %s::jsonb) "
            "ON CONFLICT (contacto_id, rol, edificio_id) DO UPDATE "
            "   SET metadata = petra_contacto_roles.metadata || EXCLUDED.metadata, "
            "       updated_at = NOW()",
            (contacto_id, json.dumps(metadata)),
        )
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


def _resolver_agente_y_conjunto(conn, telefono, codigo):
    """Devuelve (contacto_id, conjunto_id) o lanza ValueError con mensaje."""
    row_a = conn.execute(
        "SELECT c.id FROM petra_contacto_roles r "
        "  JOIN petra_contactos c ON c.id = r.contacto_id "
        " WHERE r.rol='agente_petra' AND c.telefono = %s", (telefono,)
    ).fetchone()
    if not row_a:
        raise ValueError(f"agente_no_encontrada:{telefono}")
    row_c = conn.execute(
        "SELECT id FROM conjuntos_visitante WHERE codigo = %s", (str(codigo),)
    ).fetchone()
    if not row_c:
        raise ValueError(f"conjunto_no_encontrado:{codigo}")
    return row_a[0], row_c[0]


def _t_asignar_codigo(args):
    tel = (args.get("telefono") or "").strip()
    codigo = (args.get("codigo") or "").strip()
    fecha = (args.get("fecha_envio") or "").strip()
    if not tel or not codigo:
        return {"error": "falta telefono o codigo"}
    try:
        with _db._conexion_directa(timeout=10) as conn:
            contacto_id, conjunto_id = _resolver_agente_y_conjunto(conn, tel, codigo)
            if fecha:
                # INSERT con fecha custom; si ya existía, UPDATE la fecha.
                conn.execute(
                    "INSERT INTO petra_agente_conjuntos "
                    "  (contacto_id, conjunto_id, asignado_at) "
                    "VALUES (%s, %s, %s::date) "
                    "ON CONFLICT (contacto_id, conjunto_id) "
                    "DO UPDATE SET asignado_at = EXCLUDED.asignado_at",
                    (contacto_id, conjunto_id, fecha),
                )
            else:
                conn.execute(
                    "INSERT INTO petra_agente_conjuntos (contacto_id, conjunto_id) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (contacto_id, conjunto_id),
                )
            conn.commit()
    except ValueError as e:
        return {"error": str(e)}
    return {"ok": True, "telefono": tel, "codigo": codigo,
            "fecha_envio": fecha or "hoy"}


def _t_ajustar_fecha_envio_codigo(args):
    tel = (args.get("telefono") or "").strip()
    codigo = (args.get("codigo") or "").strip()
    # Nota: NO usamos .strip() con default — un string vacío significa "borrar".
    fecha_raw = args.get("fecha_envio")
    if fecha_raw is None:
        return {"error": "falta fecha_envio (usá '' para borrar)"}
    if not tel or not codigo:
        return {"error": "falta telefono o codigo"}
    fecha = str(fecha_raw).strip()
    try:
        with _db._conexion_directa(timeout=10) as conn:
            contacto_id, conjunto_id = _resolver_agente_y_conjunto(conn, tel, codigo)
            if fecha == "":
                cur = conn.execute(
                    "UPDATE petra_agente_conjuntos "
                    "   SET asignado_at = NULL "
                    " WHERE contacto_id = %s AND conjunto_id = %s "
                    "RETURNING contacto_id",
                    (contacto_id, conjunto_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE petra_agente_conjuntos "
                    "   SET asignado_at = %s::date "
                    " WHERE contacto_id = %s AND conjunto_id = %s "
                    "RETURNING contacto_id",
                    (fecha, contacto_id, conjunto_id),
                )
            found = cur.fetchone()
            conn.commit()
    except ValueError as e:
        return {"error": str(e)}
    if not found:
        return {"error": "no_estaba_asignado",
                "mensaje": f"{tel} no tiene asignado el código {codigo}. "
                            "Usá asignar_codigo con fecha_envio directamente."}
    return {"ok": True, "telefono": tel, "codigo": codigo,
            "fecha_envio": fecha or "(sin fecha)"}


def _t_quitar_codigo(args):
    tel = (args.get("telefono") or "").strip()
    codigo = (args.get("codigo") or "").strip()
    if not tel or not codigo:
        return {"error": "falta telefono o codigo"}
    try:
        with _db._conexion_directa(timeout=10) as conn:
            contacto_id, conjunto_id = _resolver_agente_y_conjunto(conn, tel, codigo)
            cur = conn.execute(
                "DELETE FROM petra_agente_conjuntos "
                " WHERE contacto_id = %s AND conjunto_id = %s RETURNING contacto_id",
                (contacto_id, conjunto_id),
            )
            removed = cur.fetchone()
            conn.commit()
    except ValueError as e:
        return {"error": str(e)}
    return {"ok": True, "telefono": tel, "codigo": codigo,
            "estaba_asignado": removed is not None}


def _t_actualizar_agente(args):
    tel = (args.get("telefono") or "").strip()
    campos = args.get("campos") or {}
    if not tel or not isinstance(campos, dict) or not campos:
        return {"error": "falta telefono o campos"}
    nombre = campos.get("nombre")
    meta_patch = {}
    for k, v in campos.items():
        if k == "nombre":
            continue
        if k not in _CAMPOS_METADATA:
            return {"error": f"campo_invalido:{k}",
                    "validos": sorted(_CAMPOS_METADATA | {"nombre"})}
        # Normalización mínima: strings vacíos los removemos para no ensuciar
        if isinstance(v, str) and not v.strip():
            v = ""
        meta_patch[k] = v
    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT c.id FROM petra_contacto_roles r "
            "  JOIN petra_contactos c ON c.id = r.contacto_id "
            " WHERE r.rol='agente_petra' AND c.telefono = %s", (tel,)
        ).fetchone()
        if not row:
            return {"error": "no_encontrada", "telefono": tel}
        contacto_id = row[0]
        if nombre is not None and nombre.strip():
            conn.execute(
                "UPDATE petra_contactos SET nombre = %s, updated_at = NOW() "
                "WHERE id = %s", (nombre.strip(), contacto_id))
        if meta_patch:
            conn.execute(
                "UPDATE petra_contacto_roles "
                "   SET metadata = COALESCE(metadata,'{}'::jsonb) || %s::jsonb, "
                "       updated_at = NOW() "
                " WHERE rol='agente_petra' AND contacto_id = %s",
                (json.dumps(meta_patch), contacto_id))
        conn.commit()
    return {"ok": True, "telefono": tel, "actualizado": campos}


_TIPOS_MEDIA_VALIDOS = {
    "foto_conjunto", "foto_inmueble", "foto_agente",
    "video_conjunto", "video_inmueble", "video_agente",
    "libro_arriendos", "libro_ventas", "documento", "otro",
}


def _inferir_extension(mime, source_url):
    mime = (mime or "").lower()
    mapa = {
        "image/jpeg": "jpg", "image/jpg": "jpg",
        "image/png": "png", "image/webp": "webp", "image/gif": "gif",
        "video/mp4": "mp4", "video/quicktime": "mov", "video/3gpp": "3gp",
        "application/pdf": "pdf",
    }
    if mime in mapa:
        return mapa[mime]
    # Fallback por URL
    try:
        tail = (source_url or "").split("?")[0].rsplit(".", 1)[-1].lower()
        if 1 <= len(tail) <= 5 and tail.isalnum():
            return tail
    except Exception:
        pass
    return "bin"


def _t_agregar_media(args):
    import requests as _rq
    import uuid as _uuid
    from app.petra_panel import _subir_media_a_github

    cod_conj = (args.get("codigo_conjunto") or "").strip()
    url = (args.get("url") or "").strip()
    tipo = (args.get("tipo") or "foto_conjunto").strip()
    cod_inm = (args.get("codigo_inmueble") or "").strip()
    nota = (args.get("nota") or "").strip()
    if not cod_conj or not url:
        return {"error": "falta codigo_conjunto o url"}
    if tipo not in _TIPOS_MEDIA_VALIDOS:
        return {"error": f"tipo_invalido:{tipo}",
                "validos": sorted(_TIPOS_MEDIA_VALIDOS)}

    # 1) Resolver conjunto e inmueble en DB.
    with _db._conexion_directa(timeout=10) as conn:
        row_c = conn.execute(
            "SELECT id FROM conjuntos_visitante WHERE codigo = %s",
            (cod_conj,)).fetchone()
        if not row_c:
            return {"error": f"conjunto_no_encontrado:{cod_conj}"}
        conjunto_id = row_c[0]
        inmueble_id = None
        if cod_inm:
            cod_inm_num = cod_inm.lstrip("Pp")
            try:
                iid = int(cod_inm_num)
                row_i = conn.execute(
                    "SELECT id FROM inmuebles_visitante WHERE id = %s", (iid,)
                ).fetchone()
                if row_i:
                    inmueble_id = row_i[0]
            except Exception:
                pass

    # 2) Descargar.
    try:
        r = _rq.get(url, timeout=30)
    except Exception as e:
        return {"error": f"download_error:{type(e).__name__}"}
    if not r.ok:
        return {"error": f"download_http_{r.status_code}"}
    content = r.content
    mime = (r.headers.get("Content-Type") or "").split(";")[0].strip()
    ext = _inferir_extension(mime, url)

    # 3) Nombre único y carpeta por conjunto.
    uid = _uuid.uuid4().hex
    filename = f"{uid}.{ext}"
    folder = f"conjuntos/{cod_conj}"
    raw_url = _subir_media_a_github(content, filename, folder=folder)
    if not raw_url:
        return {"error": "upload_github_fallo"}

    # 4) Guardar en DB.
    with _db._conexion_directa(timeout=10) as conn:
        cur = conn.execute(
            "INSERT INTO petra_conjunto_media "
            "  (conjunto_id, inmueble_id, tipo, url, mime, tamano_bytes, "
            "   nota, source_url, subido_por) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (conjunto_id, inmueble_id, tipo, raw_url, mime, len(content),
             nota or None, url[:1024], "delmar_via_mcp")
        )
        media_id = cur.fetchone()[0]
        conn.commit()

    return {
        "ok": True, "id": media_id,
        "url_guardada": raw_url,
        "tipo": tipo,
        "codigo_conjunto": cod_conj,
        "codigo_inmueble": cod_inm or None,
        "tamano_bytes": len(content),
        "mime": mime,
    }


def _t_listar_media_conjunto(args):
    cod = (args.get("codigo_conjunto") or "").strip()
    tipo_filter = (args.get("tipo") or "").strip()
    if not cod:
        return {"error": "falta codigo_conjunto"}
    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT id FROM conjuntos_visitante WHERE codigo = %s", (cod,)
        ).fetchone()
        if not row:
            return {"error": f"conjunto_no_encontrado:{cod}"}
        where = ["conjunto_id = %s"]
        params = [row[0]]
        if tipo_filter:
            where.append("tipo = %s")
            params.append(tipo_filter)
        rows = conn.execute(
            "SELECT id, tipo, url, mime, tamano_bytes, nota, inmueble_id, created_at "
            "  FROM petra_conjunto_media "
            " WHERE " + " AND ".join(where) +
            " ORDER BY created_at DESC LIMIT 500",
            tuple(params)).fetchall()
    out = [{
        "id": r[0], "tipo": r[1], "url": r[2], "mime": r[3],
        "tamano_bytes": r[4], "nota": r[5] or "",
        "inmueble_id": r[6],
        "created_at": r[7].isoformat() if r[7] else None,
    } for r in rows]
    return {"codigo_conjunto": cod, "count": len(out), "media": out}


_MEDIA_TIPOS_VALIDOS = {
    "foto_conjunto", "foto_inmueble", "foto_agente",
    "video_conjunto", "video_inmueble", "video_agente",
    "libro_arriendos", "libro_ventas",
    "qr_instalado", "zonas_comunes", "frente_conjunto", "inventario",
    "documento", "otro",
}


def _t_actualizar_media_tipo(args):
    try:
        mid = int(args.get("media_id") or 0)
    except (TypeError, ValueError):
        return {"error": "media_id invalido"}
    tipo = (args.get("tipo") or "").strip()
    nota = args.get("nota")
    if not mid:
        return {"error": "falta media_id"}
    if tipo not in _MEDIA_TIPOS_VALIDOS:
        return {"error": f"tipo_invalido:{tipo}",
                "validos": sorted(_MEDIA_TIPOS_VALIDOS)}
    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT id FROM petra_conjunto_media WHERE id = %s", (mid,)
        ).fetchone()
        if not row:
            return {"error": f"media_no_encontrada:{mid}"}
        if nota is not None:
            conn.execute(
                "UPDATE petra_conjunto_media SET tipo = %s, nota = %s WHERE id = %s",
                (tipo, nota, mid))
        else:
            conn.execute(
                "UPDATE petra_conjunto_media SET tipo = %s WHERE id = %s",
                (tipo, mid))
        conn.commit()
    return {"ok": True, "media_id": mid, "tipo": tipo}


def _t_cambiar_telefono_agente(args):
    actual = (args.get("telefono_actual") or "").strip()
    nuevo = (args.get("telefono_nuevo") or "").strip()
    if not actual or not nuevo:
        return {"error": "falta telefono_actual o telefono_nuevo"}
    if actual == nuevo:
        return {"error": "son iguales"}
    with _db._conexion_directa(timeout=10) as conn:
        # Verificar que el actual exista y sea agente.
        row = conn.execute(
            "SELECT c.id FROM petra_contacto_roles r "
            "  JOIN petra_contactos c ON c.id = r.contacto_id "
            " WHERE r.rol='agente_petra' AND c.telefono = %s", (actual,)
        ).fetchone()
        if not row:
            return {"error": f"agente_actual_no_encontrada:{actual}"}
        # Verificar que el nuevo no esté en uso.
        ya = conn.execute(
            "SELECT id FROM petra_contactos WHERE telefono = %s", (nuevo,)
        ).fetchone()
        if ya:
            return {"error": f"telefono_nuevo_ya_existe:{nuevo}",
                    "hint": "Si querés fusionar fichas, primero eliminá o "
                             "transferí los datos manualmente."}
        conn.execute(
            "UPDATE petra_contactos SET telefono = %s, updated_at = NOW() "
            "WHERE id = %s", (nuevo, row[0]))
        conn.commit()
    return {"ok": True, "telefono_actual": actual, "telefono_nuevo": nuevo}


def _t_eliminar_agente(args):
    tel = (args.get("telefono") or "").strip()
    confirm = bool(args.get("confirm"))
    if not tel:
        return {"error": "falta telefono"}
    if not confirm:
        return {"error": "requiere_confirm_true",
                "mensaje": "Esta acción es irreversible. Pasá confirm=true "
                            "para ejecutar."}
    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT c.id, c.nombre FROM petra_contacto_roles r "
            "  JOIN petra_contactos c ON c.id = r.contacto_id "
            " WHERE r.rol='agente_petra' AND c.telefono = %s", (tel,)
        ).fetchone()
        if not row:
            return {"error": f"no_encontrada:{tel}"}
        contacto_id, nombre = row[0], row[1]
        # Borrar el contacto cascada todo (roles, agente_conjuntos).
        conn.execute("DELETE FROM petra_contactos WHERE id = %s", (contacto_id,))
        conn.commit()
    return {"ok": True, "telefono": tel, "nombre": nombre,
            "borrado": True}


_CAMPOS_CONJUNTO_SQL_TIPO = {
    "nombre": "text", "ciudad": "text", "barrio": "text",
    "direccion": "text", "ubicacion_maps": "text",
    "fecha_activacion": "date",
    "envio_fotos_inventario": "bool",
    "envio_videos_conjunto": "bool",
    "envio_inventario_inmueble": "bool",
    "envio_qr_instalado": "bool",
    "envio_zonas_comunes": "bool",
    "envio_frente_conjunto": "bool",
    "notas": "text",
}


def _t_actualizar_conjunto(args):
    cod = (args.get("codigo") or "").strip()
    campos = args.get("campos") or {}
    if not cod or not isinstance(campos, dict) or not campos:
        return {"error": "falta codigo o campos"}
    set_parts = []
    params = []
    for k, v in campos.items():
        if k not in _CAMPOS_CONJUNTO:
            return {"error": f"campo_invalido:{k}",
                    "validos": sorted(_CAMPOS_CONJUNTO)}
        set_parts.append(f"{k} = %s")
        params.append(v)
    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT id FROM conjuntos_visitante WHERE codigo = %s", (cod,)
        ).fetchone()
        if not row:
            return {"error": f"conjunto_no_encontrado:{cod}"}
        params.append(row[0])
        conn.execute(
            f"UPDATE conjuntos_visitante SET {', '.join(set_parts)} "
            f"WHERE id = %s", tuple(params))
        conn.commit()
    return {"ok": True, "codigo": cod, "actualizado": campos}


def _t_ficha_conjunto(args):
    cod = (args.get("codigo") or "").strip()
    if not cod:
        return {"error": "falta codigo"}
    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT id, codigo, nombre, ciudad, barrio, direccion, "
            "       ubicacion_maps, fecha_activacion, envio_fotos_inventario, "
            "       envio_videos_conjunto, envio_inventario_inmueble, notas, "
            "       envio_qr_instalado, envio_zonas_comunes, envio_frente_conjunto "
            "  FROM conjuntos_visitante WHERE codigo = %s", (cod,)
        ).fetchone()
        if not row:
            return {"error": f"conjunto_no_encontrado:{cod}"}
        # Agentes asignadas a este conjunto.
        agts = conn.execute(
            "SELECT c.telefono, c.nombre, ac.asignado_at "
            "  FROM petra_agente_conjuntos ac "
            "  JOIN petra_contactos c ON c.id = ac.contacto_id "
            " WHERE ac.conjunto_id = %s ORDER BY ac.asignado_at ASC",
            (row[0],)
        ).fetchall()
    return {
        "codigo": row[1], "nombre": row[2],
        "ciudad": row[3] or "", "barrio": row[4] or "",
        "direccion": row[5] or "", "ubicacion_maps": row[6] or "",
        "fecha_activacion": row[7].isoformat() if row[7] else "",
        "envio_fotos_inventario": bool(row[8]),
        "envio_videos_conjunto": bool(row[9]),
        "envio_inventario_inmueble": bool(row[10]),
        "notas": row[11] or "",
        "envio_qr_instalado": bool(row[12]),
        "envio_zonas_comunes": bool(row[13]),
        "envio_frente_conjunto": bool(row[14]),
        "agentes": [
            {"telefono": a[0], "nombre": a[1],
             "fecha_envio_qr": a[2].isoformat() if a[2] else ""}
            for a in agts
        ],
    }


_TOOL_FNS = {
    "listar_agentes": _t_listar_agentes,
    "ficha_agente": _t_ficha_agente,
    "cambiar_estado_agente": _t_cambiar_estado_agente,
    "crear_agente": _t_crear_agente,
    "listar_conjuntos": _t_listar_conjuntos,
    "asignar_codigo": _t_asignar_codigo,
    "ajustar_fecha_envio_codigo": _t_ajustar_fecha_envio_codigo,
    "quitar_codigo": _t_quitar_codigo,
    "actualizar_agente": _t_actualizar_agente,
    "actualizar_conjunto": _t_actualizar_conjunto,
    "ficha_conjunto": _t_ficha_conjunto,
    "agregar_media": _t_agregar_media,
    "listar_media_conjunto": _t_listar_media_conjunto,
    "actualizar_media_tipo": _t_actualizar_media_tipo,
    "cambiar_telefono_agente": _t_cambiar_telefono_agente,
    "eliminar_agente": _t_eliminar_agente,
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
