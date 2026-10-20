"""Botones editables por estación (crear nuevos / ocultar defaults).

Tabla `petra_botones_estacion`:
    - tipo='custom':   botón que no existe en el código. Al pulsarlo el bot
                       responde con `texto_respuesta` y se queda. ID WA = CUSTOM_<id>.
    - tipo='override': regla sobre un botón default (ONBAG_*). Si activo=FALSE
                       se oculta cuando el flow lo intente enviar.

Entrada principal:
    aplicar_reglas(estado_key, defaults) → lista final de botones a enviar
        (filtra defaults ocultados y agrega customs, cap 3 por mensaje WA).

    resolver_custom(button_id) → (estado_key, texto_respuesta) o None
        para que el flow responda cuando entra un click CUSTOM_<id>.
"""
import logging

log = logging.getLogger("petra")

MAX_BOTONES_WA = 3  # WhatsApp cap


def _conn():
    from app import db as _db
    return _db._conexion_directa(timeout=5)


def _row_to_dict(r):
    return {
        "id": r[0], "estado_key": r[1], "tipo": r[2],
        "boton_id": r[3], "titulo": r[4],
        "texto_respuesta": r[5], "orden": r[6], "activo": r[7],
        "clave_texto": r[8] if len(r) > 8 else None,
        "accion": r[9] if len(r) > 9 else "enviar_texto",
        "payload": r[10] if len(r) > 10 else None,
    }


def listar_por_estado(estado_key):
    """Lista TODAS las reglas (custom + override) para un estado."""
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT id, estado_key, tipo, boton_id, titulo, "
                "texto_respuesta, orden, activo, clave_texto, accion, payload "
                "FROM petra_botones_estacion "
                "WHERE estado_key=%s "
                "ORDER BY tipo, orden, id",
                (estado_key,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception:
        log.exception("[botones] listar fallo estado=%s", estado_key)
        return []


def listar_por_clave(clave_texto):
    """Lista botones adjuntos a un mensaje específico (clave de petra_textos)."""
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT id, estado_key, tipo, boton_id, titulo, "
                "texto_respuesta, orden, activo, clave_texto, accion, payload "
                "FROM petra_botones_estacion "
                "WHERE clave_texto=%s "
                "ORDER BY orden, id",
                (clave_texto,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception:
        log.exception("[botones] listar fallo clave=%s", clave_texto)
        return []


def botones_de_clave(clave_texto):
    """Devuelve los botones (como lista {id, title}) para adjuntar al mensaje
    con esa clave de texto. Hasta 3, respetando activo. Cada custom se envía
    como CUSTOM_<id>, cada override con su boton_id tal cual."""
    rows = listar_por_clave(clave_texto)
    out = []
    for r in rows:
        if not r["activo"]:
            continue
        titulo = (r["titulo"] or "")[:20].strip()
        if not titulo:
            continue
        if r["tipo"] == "custom":
            out.append({"id": f"CUSTOM_{r['id']}", "title": titulo})
        else:
            out.append({"id": r["boton_id"], "title": titulo})
    return out[:MAX_BOTONES_WA]


def aplicar_reglas(estado_key, defaults):
    """Dada la lista de defaults que el flow quiere mandar, devuelve la
    lista final a enviar: filtra defaults ocultados por override activo=FALSE,
    y agrega botones custom (ordenados por `orden`), cap a 3.

    `defaults` es list[{"id": ..., "title": ...}].
    """
    if not estado_key:
        return defaults[:MAX_BOTONES_WA]
    try:
        reglas = listar_por_estado(estado_key)
    except Exception:
        return defaults[:MAX_BOTONES_WA]

    ocultos = {
        r["boton_id"] for r in reglas
        if r["tipo"] == "override" and not r["activo"]
    }
    final = [b for b in defaults if b.get("id") not in ocultos]

    customs = [
        {"id": f"CUSTOM_{r['id']}", "title": (r["titulo"] or "")[:20]}
        for r in reglas
        if r["tipo"] == "custom" and r["activo"] and (r["titulo"] or "").strip()
    ]
    final.extend(customs)
    return final[:MAX_BOTONES_WA]


def resolver_custom(button_id):
    """Si `button_id` tiene formato CUSTOM_<n>, devuelve (estado_key,
    texto_respuesta). Si no es custom o no existe, devuelve None."""
    if not button_id or not button_id.startswith("CUSTOM_"):
        return None
    try:
        bid = int(button_id.replace("CUSTOM_", ""))
    except (ValueError, TypeError):
        return None
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT estado_key, texto_respuesta, accion, payload "
                "FROM petra_botones_estacion "
                "WHERE id=%s AND tipo='custom' AND activo=TRUE",
                (bid,),
            ).fetchone()
        if not row:
            return None
        return {
            "estado_key": row[0],
            "texto_respuesta": row[1] or "",
            "accion": row[2] or "enviar_texto",
            "payload": row[3],
        }
    except Exception:
        log.exception("[botones] resolver fallo id=%s", button_id)
        return None


def crear_custom(estado_key, titulo, texto_respuesta, orden=0,
                 clave_texto=None, accion="enviar_texto", payload=None):
    """Crea un botón custom nuevo. Devuelve id.
    Si `clave_texto` se pasa, el botón se adjunta al mensaje puntual
    con esa clave en vez de a toda la estación.
    `accion` = 'enviar_texto' (responde con texto_respuesta y queda)
              | 'disparar_boton' (procesa como si pulsaran el botón del
                                  flow con id en `payload`, ej. ONBAG_LISTO)."""
    estado_key = (estado_key or "").strip()
    titulo = (titulo or "").strip()[:20]
    texto_respuesta = (texto_respuesta or "").strip()
    clave_texto = (clave_texto or "").strip() or None
    accion = (accion or "enviar_texto").strip()
    payload = (payload or "").strip() or None
    if not estado_key or not titulo:
        raise ValueError("estado_key y titulo son requeridos")
    if accion == "enviar_texto" and not texto_respuesta:
        raise ValueError("texto_respuesta requerido para accion=enviar_texto")
    if accion == "disparar_boton" and not payload:
        raise ValueError("payload requerido para accion=disparar_boton")
    with _conn() as conn:
        row = conn.execute(
            "INSERT INTO petra_botones_estacion "
            "(estado_key, tipo, boton_id, titulo, texto_respuesta, orden, "
            "activo, clave_texto, accion, payload) "
            "VALUES (%s, 'custom', '', %s, %s, %s, TRUE, %s, %s, %s) RETURNING id",
            (estado_key, titulo, texto_respuesta, int(orden or 0),
             clave_texto, accion, payload),
        ).fetchone()
        new_id = row[0]
        conn.execute(
            "UPDATE petra_botones_estacion SET boton_id=%s WHERE id=%s",
            (f"CUSTOM_{new_id}", new_id),
        )
    return new_id


def eliminar(regla_id):
    with _conn() as conn:
        conn.execute("DELETE FROM petra_botones_estacion WHERE id=%s", (int(regla_id),))


def toggle_override(estado_key, boton_id, oculto):
    """Oculta (o desoculta) un botón default. Crea row si no existe."""
    estado_key = (estado_key or "").strip()
    boton_id = (boton_id or "").strip()
    if not estado_key or not boton_id:
        raise ValueError("estado_key y boton_id son requeridos")
    activo = not bool(oculto)
    with _conn() as conn:
        existe = conn.execute(
            "SELECT id FROM petra_botones_estacion "
            "WHERE estado_key=%s AND tipo='override' AND boton_id=%s",
            (estado_key, boton_id),
        ).fetchone()
        if existe:
            conn.execute(
                "UPDATE petra_botones_estacion SET activo=%s WHERE id=%s",
                (activo, existe[0]),
            )
        else:
            conn.execute(
                "INSERT INTO petra_botones_estacion "
                "(estado_key, tipo, boton_id, titulo, orden, activo) "
                "VALUES (%s, 'override', %s, NULL, 0, %s)",
                (estado_key, boton_id, activo),
            )
