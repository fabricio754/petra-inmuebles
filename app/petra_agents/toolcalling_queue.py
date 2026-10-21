"""Queue de mensajes LLM que fallaron para re-procesar en background.

Cuando el LLM primario falla todos sus retries, encolamos el mensaje acá
y le mandamos a la agente un "disculpame, se me enredó". Un cron corre
cada 2 min y re-intenta hasta que el LLM responda (o se abandona a los
10 intentos ~ 20 min de fallas seguidas).

API:
    encolar(telefono, texto, cid, fase, error)
    procesar_pendientes() → cantidad resueltos
"""
import datetime as _dt
import logging

log = logging.getLogger("petra")

MAX_INTENTOS = 10
BACKOFF_MINUTOS = 2  # tiempo entre reintentos


def _conn():
    from app import db as _db
    return _db._conexion_directa(timeout=5)


def encolar(telefono, texto, cid, fase, error):
    """Encola un mensaje que no pudo ser procesado por el LLM."""
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO petra_mensajes_pendientes_llm "
                "(telefono, texto, cid, fase, ultimo_error, siguiente_at) "
                "VALUES (%s, %s, %s, %s, %s, NOW() + INTERVAL '%s minutes')",
                (telefono, (texto or "")[:4000], cid, fase or "",
                 (error or "")[:500], BACKOFF_MINUTOS),
            )
        log.info("[tc-queue] encolado tel=%s fase=%s err=%s",
                 telefono, fase, (error or "")[:80])
        return True
    except Exception:
        log.exception("[tc-queue] encolar fallo tel=%s", telefono)
        return False


def enviar_disculpame(telefono):
    """Manda el mensaje humano cuando el LLM falló. Idempotente a nivel
    'ya fue avisado hace poco' no — si llamás 2x en 10s, manda 2x. El
    caller debe asegurar que solo se llama 1x por mensaje encolado."""
    from app.petra_agents.toolcalling import MENSAJE_FALLO_HUMANO
    try:
        from app import petra_wa
        petra_wa.send_text(telefono, MENSAJE_FALLO_HUMANO)
        return True
    except Exception:
        log.exception("[tc-queue] send disculpame fallo tel=%s", telefono)
        return False


def _reintentar_fase(telefono, texto, cid, fase):
    """Re-ejecuta el pipeline de tool-calling para la fase indicada.
    Devuelve True si el LLM respondió OK; False si volvió a fallar."""
    from app import db as _db
    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT metadata FROM petra_contacto_roles "
            "WHERE contacto_id=%s AND rol='agente_petra'",
            (cid,),
        ).fetchone()
        if not row:
            log.warning("[tc-queue] re-intento sin rol tel=%s cid=%s",
                        telefono, cid)
            return False
        meta = dict(row[0] or {})
        if fase == "inscripcion":
            from app.petra_agents import tools_inscripcion as _tins
            resultado = _tins.procesar(telefono, texto, cid, meta, conn)
            return bool(resultado.get("ok"))
        if fase == "autorizacion":
            from app.petra_agents import tools_autorizacion as _tau
            resultado = _tau.procesar(telefono, texto, cid, meta, conn)
            return bool(resultado.get("ok"))
        # Otras fases: a medida que se migren, agregarlas acá
        log.warning("[tc-queue] fase desconocida=%s tel=%s", fase, telefono)
        return False


def procesar_pendientes():
    """Cron cada 2 min. Toma mensajes con siguiente_at <= NOW() y los
    re-procesa. Si funciona → marca resuelto. Si falla y intentos<MAX →
    reschedule con backoff. Si pasa MAX → abandona."""
    try:
        with _conn() as conn:
            pendientes = conn.execute(
                "SELECT id, telefono, texto, cid, fase, intentos "
                "FROM petra_mensajes_pendientes_llm "
                "WHERE estado='pendiente' AND siguiente_at <= NOW() "
                "ORDER BY created_at LIMIT 50"
            ).fetchall()
    except Exception:
        log.exception("[tc-queue] query pendientes fallo")
        return 0
    if not pendientes:
        return 0
    resueltos = 0
    for pid, telefono, texto, cid, fase, intentos in pendientes:
        try:
            ok = _reintentar_fase(telefono, texto, cid, fase)
        except Exception:
            log.exception("[tc-queue] reintento excepción tel=%s", telefono)
            ok = False
        if ok:
            with _conn() as conn2:
                conn2.execute(
                    "UPDATE petra_mensajes_pendientes_llm "
                    "SET estado='resuelto', resuelto_at=NOW() WHERE id=%s",
                    (pid,),
                )
            resueltos += 1
            log.info("[tc-queue] resuelto tel=%s fase=%s intentos=%d",
                     telefono, fase, intentos + 1)
        else:
            nuevo_intentos = intentos + 1
            if nuevo_intentos >= MAX_INTENTOS:
                with _conn() as conn2:
                    conn2.execute(
                        "UPDATE petra_mensajes_pendientes_llm "
                        "SET estado='abandonado', intentos=%s "
                        "WHERE id=%s",
                        (nuevo_intentos, pid),
                    )
                log.error("[tc-queue] abandonado tel=%s fase=%s tras %d intentos",
                          telefono, fase, nuevo_intentos)
                # Avisar al agente humano
                try:
                    from app import db as _db
                    _db.marcar_requiere_humano(
                        telefono,
                        f"[tc-queue] LLM falló {nuevo_intentos}x — msg encolado abandonado",
                    )
                except Exception:
                    pass
            else:
                backoff_mins = BACKOFF_MINUTOS * (2 ** min(nuevo_intentos - 1, 5))
                with _conn() as conn2:
                    conn2.execute(
                        "UPDATE petra_mensajes_pendientes_llm "
                        "SET intentos=%s, "
                        "    siguiente_at = NOW() + INTERVAL '%s minutes' "
                        "WHERE id=%s",
                        (nuevo_intentos, backoff_mins, pid),
                    )
    return resueltos
