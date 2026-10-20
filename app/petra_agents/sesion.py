"""Sesión conversacional del contacto.

Una sesión = ventana de actividad continua. Mientras haya mensajes
dentro de los últimos N minutos (default 5), es la misma sesión. Si
pasa el umbral sin mensajes, el cron la cierra con un aviso ("Vamos a
cerrar esta conversación..."). El próximo mensaje tras el cierre abre
una sesión nueva (fresh — Claude no ve nada previo).

El historial que ve Claude = mensajes DE la sesión actual (no ventana
temporal). Esto da continuidad conversacional perfecta sin arrastrar
flujos cerrados viejos.
"""
import logging
import os

log = logging.getLogger("petra")

INACTIVIDAD_MIN = int(os.environ.get("PETRA_SESION_INACTIVIDAD_MIN", "5"))


def obtener_o_abrir(telefono):
    """Devuelve dict {id, abierta_at, ultimo_msg_at} de la sesión activa.
    Si no hay o la última cerró, abre una nueva. Actualiza ultimo_msg_at.

    Idempotente dentro de la misma transacción."""
    from app import db as _db
    try:
        with _db._conexion_directa(timeout=5) as conn:
            # Buscar sesión abierta reciente
            row = conn.execute(
                "SELECT id, abierta_at, ultimo_msg_at FROM petra_sesion_chat "
                "WHERE telefono=%s AND cerrada_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (telefono,),
            ).fetchone()
            if row:
                # Actualizar ultimo_msg_at al momento actual
                conn.execute(
                    "UPDATE petra_sesion_chat SET ultimo_msg_at=NOW() "
                    "WHERE id=%s",
                    (row[0],),
                )
                return {"id": row[0], "abierta_at": row[1],
                        "ultimo_msg_at": row[2], "nueva": False}
            # Abrir nueva
            new_row = conn.execute(
                "INSERT INTO petra_sesion_chat (telefono) VALUES (%s) "
                "RETURNING id, abierta_at, ultimo_msg_at",
                (telefono,),
            ).fetchone()
            log.info("[sesion] nueva tel=%s id=%s", telefono, new_row[0])
            return {"id": new_row[0], "abierta_at": new_row[1],
                    "ultimo_msg_at": new_row[2], "nueva": True}
    except Exception:
        log.exception("[sesion] obtener_o_abrir fallo tel=%s", telefono)
        return None


def leer_activa(telefono):
    """Lee sesión activa (sin modificar). Devuelve None si no hay."""
    from app import db as _db
    try:
        with _db._conexion_directa(timeout=5) as conn:
            row = conn.execute(
                "SELECT id, abierta_at, ultimo_msg_at FROM petra_sesion_chat "
                "WHERE telefono=%s AND cerrada_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (telefono,),
            ).fetchone()
            if row:
                return {"id": row[0], "abierta_at": row[1],
                        "ultimo_msg_at": row[2]}
    except Exception:
        log.exception("[sesion] leer_activa fallo tel=%s", telefono)
    return None


def cerrar(telefono, razon="completada"):
    """Cierra la sesión activa (si existe). No manda mensaje al user."""
    from app import db as _db
    try:
        with _db._conexion_directa(timeout=5) as conn:
            conn.execute(
                "UPDATE petra_sesion_chat SET cerrada_at=NOW(), razon_cierre=%s "
                "WHERE telefono=%s AND cerrada_at IS NULL",
                (razon, telefono),
            )
    except Exception:
        log.exception("[sesion] cerrar fallo tel=%s", telefono)


def cargar_mensajes_sesion(telefono, sesion_id, limite=50):
    """Devuelve mensajes de la sesión en formato [{role, content}] para
    pasárselos a Claude. Cualquier mensaje con created_at >= abierta_at
    de la sesión actual."""
    from app import db as _db
    try:
        with _db._conexion_directa(timeout=5) as conn:
            abierta = conn.execute(
                "SELECT abierta_at FROM petra_sesion_chat WHERE id=%s",
                (sesion_id,),
            ).fetchone()
            if not abierta:
                return []
            rows = conn.execute(
                "SELECT texto_usuario, texto_asistente FROM petra_llm_logs "
                "WHERE telefono=%s AND created_at >= %s "
                "AND (rama LIKE 'agent:%%' OR rama IN ('claude', 'orchestrator')) "
                "ORDER BY created_at ASC LIMIT %s",
                (telefono, abierta[0], limite),
            ).fetchall()
            msgs = []
            for user_t, asst_t in rows:
                if user_t:
                    msgs.append({"role": "user", "content": user_t[:4000]})
                if asst_t:
                    msgs.append({"role": "assistant", "content": asst_t[:4000]})
            return msgs
    except Exception:
        log.exception("[sesion] cargar_mensajes fallo tel=%s sesion=%s",
                      telefono, sesion_id)
        return []


def cerrar_inactivas_y_avisar():
    """Cron job: cierra sesiones con última actividad > INACTIVIDAD_MIN
    minutos atrás, y manda un mensaje de despedida al usuario. Idempotente.

    Debe llamarse con frecuencia (ej cada 1 min) desde scheduler.py."""
    from app import db as _db, petra_wa
    cerradas = []
    try:
        with _db._conexion_directa(timeout=5) as conn:
            rows = conn.execute(
                "SELECT id, telefono FROM petra_sesion_chat "
                "WHERE cerrada_at IS NULL "
                "AND ultimo_msg_at < NOW() - make_interval(mins => %s) "
                "ORDER BY id ASC LIMIT 50",
                (INACTIVIDAD_MIN,),
            ).fetchall()
            for sid, tel in rows:
                try:
                    conn.execute(
                        "UPDATE petra_sesion_chat SET cerrada_at=NOW(), "
                        "razon_cierre='inactividad' WHERE id=%s",
                        (sid,),
                    )
                    cerradas.append((sid, tel))
                except Exception:
                    log.exception("[sesion] cerrar id=%s fallo", sid)
    except Exception:
        log.exception("[sesion] cerrar_inactivas query fallo")
    # Enviar aviso de despedida fuera de la transacción
    for sid, tel in cerradas:
        try:
            petra_wa.send_text(tel,
                "Voy a cerrar esta conversación por ahora 👋 "
                "Vuelvenos a escribir cuando nos necesites — "
                "siempre acá para ayudarte. ❤️")
            log.info("[sesion] cerrada por inactividad sid=%s tel=%s",
                     sid, tel)
        except Exception:
            log.exception("[sesion] aviso cierre fallo tel=%s", tel)
    return len(cerradas)
