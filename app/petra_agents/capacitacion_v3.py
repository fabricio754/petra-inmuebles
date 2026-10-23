"""Motor DETERMINÍSTICO (sin LLM) para la fase de CAPACITACIÓN.

M3.A: reemplaza a `tools_capacitacion.py` (tool-calling LLM) y a la lógica
dentro de `_fase_capacitacion` del flow legacy. Patrón idéntico a
`inscripcion_v3.py`:

    handle(telefono, texto, cid, meta, conn, es_handoff=False) -> bool

Activada con `PETRA_V3=1`. Rutas viejas (sin flag) quedan intactas.

Estados que maneja:
  - `esperando_capacitacion_dia1`  → `ONBAG_EMPEZAR_AHORA` dispara el día 1.
  - `en_capacitacion`              → ONBAG_LISTO avanza, SIGUIENTE_DIA
                                     dispara día siguiente, reenvío por
                                     palabras clave.

Al completar los 14 videos (7 días × 2) → estado `capacitacion_completa`
+ mensaje de felicitación + botón `ONBAG_INICIAR` que después transita
a autorización (manejado por `autorizacion_v3`).
"""
import datetime as _dt
import json as _json
import logging
import time as _time

log = logging.getLogger("petra")


# ─── Palabras clave ─────────────────────────────────────────────────────

_PALABRAS_LISTO = (
    "vi", "visto", "listo", "ya", "ok", "terminé", "termine", "ya los vi",
    "ya los vi todos", "ya vi", "los vi",
)
_PALABRAS_SIGUIENTE = ("siguiente", "proximo", "próximo", "mas", "más")
_PALABRAS_REENVIO = (
    "reenviar", "reenvia", "re-enviar", "perdi", "perdí",
    "mandame", "mándame", "manda los videos",
    "volver a enviar", "volver ah enviar",
    "no vi", "no me llego", "no me llegó",
)


# ─── Senders / helpers compartidos con el flow legacy ───────────────────

def _t(clave, fallback, **vars):
    from app.petra_agents.textos import t as _tfn
    return _tfn(clave, fallback=fallback, **vars)


def _b(id_boton, titulo):
    from app.petra_agents.textos import boton as _bfn
    return _bfn(id_boton, titulo)


def _send_text(telefono, texto):
    from app import petra_wa
    petra_wa.send_text(telefono, texto)


def _send_buttons(telefono, texto, botones, estado_key=None):
    from app import petra_wa
    try:
        from app.petra_agents import botones_estacion as _bots
        final = _bots.aplicar_reglas(estado_key, botones) if estado_key else botones
    except Exception:
        final = botones
    if not final:
        petra_wa.send_text(telefono, texto)
        return
    petra_wa.send_buttons(telefono, texto, final)


def _save_meta(conn, cid, meta):
    conn.execute(
        "UPDATE petra_contacto_roles SET metadata=%s::jsonb, "
        "updated_at=NOW() WHERE contacto_id=%s AND rol='agente_petra'",
        (_json.dumps(meta), cid),
    )


def _log(telefono, texto_usuario, texto_asistente, nota=""):
    try:
        from app.petra_agents.flow_onboarding_agente import _log as _flog
        _flog(telefono, texto_usuario, texto_asistente, nota=nota)
    except Exception:
        pass


def _set_estado(meta, nuevo):
    from app.petra_agents.flow_onboarding_agente import _set_estado as _fse
    _fse(meta, nuevo)


# ─── Lógica puntual ─────────────────────────────────────────────────────

def _disparar_dia(conn, cid, telefono, meta):
    """Dispara el envío inmediato de los 2 videos del día actual
    reusando la función del cron. True si envió algo."""
    try:
        from zoneinfo import ZoneInfo
        hoy_bogota = _dt.datetime.now(ZoneInfo("America/Bogota")).date().isoformat()
    except Exception:
        hoy_bogota = _dt.datetime.utcnow().date().isoformat()
    try:
        from app.scheduler import _cap_enviar_para_agente
        from app import petra_wa as _pwa
        return bool(_cap_enviar_para_agente(
            conn, cid, telefono, meta, hoy_bogota,
            _pwa, _time, _json, ignorar_dedup_fecha=True,
        ))
    except Exception:
        log.exception("[v3:cap] fallo disparar día tel=%s", telefono)
        return False


def _reenviar_dia_actual(conn, telefono, meta):
    """Re-envía los 2 videos del día actualmente enviado."""
    cap_v = int(meta.get("cap_version", 2) or 2)
    dia_env = int(meta.get("cap_dia_enviado", 0) or 0)
    if dia_env < 1:
        return False
    if cap_v >= 2 and dia_env == 1:
        row_b = conn.execute(
            "SELECT titulo, url FROM petra_contenidos "
            "WHERE tipo='bienvenida' AND activo=TRUE ORDER BY orden LIMIT 1"
        ).fetchone()
        row_p = conn.execute(
            "SELECT titulo, url FROM petra_contenidos "
            "WHERE tipo='proximos_pasos' AND activo=TRUE ORDER BY orden LIMIT 1"
        ).fetchone()
        videos = [r for r in (row_b, row_p) if r]
    else:
        offset_dia = (dia_env - 2) if cap_v >= 2 else (dia_env - 1)
        offset = offset_dia * 2
        videos = conn.execute(
            "SELECT titulo, url FROM petra_contenidos "
            "WHERE tipo='video_capacitacion' AND activo=TRUE "
            "ORDER BY orden LIMIT 2 OFFSET %s", (offset,),
        ).fetchall()
    if len(videos) < 2:
        return False
    from app import petra_wa as _pwa
    dias_total = 7 if cap_v >= 2 else 6
    _pwa.send_text(telefono, _t(
        "capacitacion.msg_reenvio_videos",
        fallback="¡Claro! Acá los tenés de nuevo 💜 — videos del día *{d}/{t}*:",
        d=dia_env, t=dias_total,
    ))
    _time.sleep(1)
    _pwa.send_media_o_texto(telefono, videos[0][0], videos[0][1])
    _time.sleep(2)
    _pwa.send_media_o_texto(telefono, videos[1][0], videos[1][1])
    _time.sleep(2)
    _send_buttons(telefono,
        "Cuando los termines, pulsá el botón 👇",
        [{"id": "ONBAG_LISTO", "title": _b("ONBAG_LISTO", "✅ Listo")}],
        estado_key="en_capacitacion",
    )
    return True


def _responder_con_llm_fallback(telefono, texto, sesion_id=None):
    """Delegamos preguntas abiertas al agente_petra LLM (igual que el flow
    legacy). En M4 esto va a ir al FAQ."""
    try:
        from app.petra_agents.flow_onboarding_agente import _responder_con_llm
        return _responder_con_llm(telefono, texto, sesion_id=sesion_id)
    except Exception:
        log.exception("[v3:cap] fallback LLM falló tel=%s", telefono)
        _send_text(telefono,
            "Perdón, no te pude responder bien ahora. Seguimos con tu "
            "capacitación 💜")
        return True


# ─── Entry-point ────────────────────────────────────────────────────────

def handle(telefono, texto, cid, meta, conn, es_handoff=False):
    """Procesa UN turno de capacitación. Devuelve True si manejó."""
    texto = (texto or "").strip()
    t_lc = texto.lower()
    estado = meta.get("estado_agente", "en_capacitacion")

    cap_v = int(meta.get("cap_version", 2) or 2)
    total = 14 if cap_v >= 2 else 12
    dias_total = 7 if cap_v >= 2 else 6
    vistos = int(meta.get("videos_vistos", 0) or 0)

    # ── 1) Empezar ahora: solo en esperando_capacitacion_dia1 ───────────
    if estado == "esperando_capacitacion_dia1" and (
        texto == "ONBAG_EMPEZAR_AHORA"
        or t_lc in ("empezar", "empezar ahora", "arrancar", "listo", "ya", "ok")
    ):
        enviado = _disparar_dia(conn, cid, telefono, meta)
        if enviado:
            _log(telefono, texto, "[v3:cap día 1 enviado]",
                 nota="PETRA_V3 empezar_ahora")
        else:
            _send_buttons(telefono, _t(
                "capacitacion.msg_listo_para_arrancar",
                fallback=(
                    "🎉✨ *¡Todo listo!*\n\n"
                    "Tu capacitación arranca cuando vos quieras 💜\n\n"
                    "¿Querés ver los videos de hoy? Tocá el botón 👇"
                ),
            ), [{"id": "ONBAG_VER_VIDEOS_HOY",
                 "title": _b("ONBAG_VER_VIDEOS_HOY", "▶️ Ver mis videos")}],
               estado_key="bienvenida_enviada")
        return True

    # ── 2) ONBAG_LISTO (o sinónimos) → avanza un día ────────────────────
    if texto == "ONBAG_LISTO" or t_lc in _PALABRAS_LISTO:
        # Un "Listo" cuenta por los 2 videos del día.
        vistos_nuevos = min(((vistos // 2) + 1) * 2, total)
        meta["videos_vistos"] = vistos_nuevos
        meta[f"dia_{vistos_nuevos // 2}_visto_at"] = _dt.datetime.utcnow().isoformat()

        if vistos_nuevos >= total:
            # Capacitación completa → estado `capacitacion_completa`
            # (NO autorizacion_pendiente — ese salto lo hace autorizacion_v3
            # al pulsar ONBAG_INICIAR). Mensaje de felicitación + botón.
            _set_estado(meta, "capacitacion_completa")
            _save_meta(conn, cid, meta)
            _send_buttons(telefono, _t(
                "capacitacion.msg_completada_header",
                fallback=(
                    "🎓🎉 *¡Terminaste tu capacitación completa!*\n\n"
                    "Ya tenés las herramientas para arrancar como Agente Petra 💜\n\n"
                    "📍 *Siguiente paso: Autorización del conjunto* 🏢\n\n"
                    "Ahora toca hablar con el administrador de tu edificio para "
                    "que te autorice a representar a Petra ahí.\n\n"
                    "Pulsá el botón para arrancar 👇"
                ),
            ), [
                {"id": "ONBAG_INICIAR",
                 "title": _b("ONBAG_INICIAR", "🚀 Iniciar")},
            ], estado_key="capacitacion_completa")
            _log(telefono, texto, "[v3:cap completa → capacitacion_completa]",
                 nota=f"vistos={vistos_nuevos}/{total}")
            return True

        # Día intermedio: felicitamos y ofrecemos pasar al siguiente.
        # También aseguramos que estado=en_capacitacion.
        if estado != "en_capacitacion":
            _set_estado(meta, "en_capacitacion")
        _save_meta(conn, cid, meta)
        dia_actual = vistos_nuevos // 2
        quedan = total - vistos_nuevos
        dias_rest = quedan // 2
        resp = _t(
            "capacitacion.msg_dia_completo",
            fallback=(
                "🎉 *¡Felicitaciones, cerraste el día {dia_actual}/{dias_total}!* 🌟\n\n"
                "Ya viste *{vistos}/{total} videos* ✅ Vas re bien 💪\n\n"
                "Te quedan solo *{quedan} videos ({dias_restantes} días)* para completar.\n\n"
                "Mañana a las *11am* te mando los próximos 2 videos. "
                "O si querés seguir ahora, apretá el botón 👇"
            ),
            dia_actual=dia_actual, dias_total=dias_total,
            vistos=vistos_nuevos, total=total,
            quedan=quedan, dias_restantes=dias_rest,
            # Compat alias cortos (templates viejos).
            d=dia_actual, dt=dias_total,
            v=vistos_nuevos, t=total,
            q=quedan, dr=dias_rest,
        )
        _send_buttons(telefono, resp, [
            {"id": "ONBAG_SIGUIENTE_DIA",
             "title": _b("ONBAG_SIGUIENTE_DIA", "⏭ Ver día siguiente")},
        ], estado_key="en_capacitacion")
        _log(telefono, texto, "[v3:cap día marcado]",
             nota=f"vistos={vistos_nuevos}/{total}")
        return True

    # ── 3) Botón "ver día siguiente" ────────────────────────────────────
    if texto == "ONBAG_SIGUIENTE_DIA" or t_lc in _PALABRAS_SIGUIENTE:
        enviado = _disparar_dia(conn, cid, telefono, meta)
        if not enviado:
            _send_text(telefono, _t(
                "capacitacion.msg_siguiente_dia_fallo",
                fallback=(
                    "Hmm, no pude mandarte el siguiente par ahora. "
                    "Mañana a las 11am te llega sin falta. 🙏"
                ),
            ))
        _log(telefono, texto, f"[v3:cap siguiente_dia enviado={enviado}]")
        return True

    # ── 4) Palabras de reenvío ──────────────────────────────────────────
    if any(kw in t_lc for kw in _PALABRAS_REENVIO):
        if _reenviar_dia_actual(conn, telefono, meta):
            _log(telefono, texto, "[v3:cap reenvío día actual]")
            return True

    # ── 5) Pregunta libre: M4.B escape hatch → FAQ ──────────────────────
    # Anti-spam: 30s entre delegaciones.
    now = _dt.datetime.utcnow()
    ultimo = meta.get("ultimo_llm_at")
    if ultimo:
        try:
            dt_last = _dt.datetime.fromisoformat(ultimo)
            if (now - dt_last).total_seconds() < 30:
                _log(telefono, texto, "[v3:cap silencio LLM <30s]")
                return True
        except Exception:
            pass
    meta["ultimo_llm_at"] = now.isoformat()
    _save_meta(conn, cid, meta)
    # Primero intento FAQ (M4.B): si logra responder la duda, listo.
    try:
        from app.petra_agents._faq_hook import intentar_faq
        dia_actual = (vistos // 2) + 1
        if intentar_faq(telefono, texto, meta, conn, cid,
                        ultimo_paso_descripcion=(
                            f"ver los videos del día {dia_actual}"),
                        ultimo_boton={"id": "ONBAG_LISTO",
                                      "title": _b("ONBAG_LISTO", "✅ Listo")}):
            _log(telefono, texto, "[v3:cap FAQ respondió]",
                 nota=f"vistos={vistos}/{total}")
            return True
    except Exception:
        log.exception("[v3:cap] FAQ hook fallo tel=%s", telefono)
    _log(telefono, texto, "[v3:cap delegado a LLM/FAQ]",
         nota=f"vistos={vistos}/{total}")
    return _responder_con_llm_fallback(telefono, texto)
