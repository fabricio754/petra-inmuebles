"""Mensajes extra: textos adicionales que el bot envía DESPUÉS de un
mensaje principal. Editables desde el panel sin tocar código.

Modelo: tabla `petra_mensajes_extra` con parent_clave + contenido + orden.
Cuando el flow envía un mensaje con una clave X, luego envía todos los
extras con parent_clave=X en orden.
"""
import logging

log = logging.getLogger("petra")


def _conn():
    from app import db as _db
    return _db._conexion_directa(timeout=5)


def listar_por_padre(parent_clave):
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT id, parent_clave, orden, contenido, activo "
                "FROM petra_mensajes_extra "
                "WHERE parent_clave=%s "
                "ORDER BY orden, id",
                (parent_clave,),
            ).fetchall()
        return [
            {"id": r[0], "parent_clave": r[1], "orden": r[2],
             "contenido": r[3], "activo": r[4]}
            for r in rows
        ]
    except Exception:
        log.exception("[extra] listar fallo parent=%s", parent_clave)
        return []


def crear(parent_clave, contenido, orden=0):
    parent_clave = (parent_clave or "").strip()
    contenido = (contenido or "").strip()
    if not parent_clave or not contenido:
        raise ValueError("parent_clave y contenido requeridos")
    with _conn() as conn:
        row = conn.execute(
            "INSERT INTO petra_mensajes_extra "
            "(parent_clave, orden, contenido, activo) "
            "VALUES (%s, %s, %s, TRUE) RETURNING id",
            (parent_clave, int(orden or 0), contenido),
        ).fetchone()
    return row[0]


def eliminar(mid):
    with _conn() as conn:
        conn.execute("DELETE FROM petra_mensajes_extra WHERE id=%s", (int(mid),))


def enviar_extras(telefono, parent_clave, variables=None):
    """Para cada extra activo del parent, lo envía por WhatsApp.
    Usa format() con `variables` para reemplazar {nombre_corto} etc.
    Silencio si no hay extras."""
    from app import petra_wa
    import time as _tm
    rows = listar_por_padre(parent_clave)
    for r in rows:
        if not r["activo"]:
            continue
        contenido = r["contenido"] or ""
        try:
            contenido = contenido.format(**(variables or {}))
        except (KeyError, IndexError):
            pass
        if not contenido.strip():
            continue
        try:
            petra_wa.send_text(telefono, contenido)
            _tm.sleep(0.8)
        except Exception:
            log.exception("[extra] send fallo id=%s", r["id"])
