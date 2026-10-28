"""Scheduler Petra — jobs periódicos del multi-agent y de la operación
de agentes.

APScheduler se usa en modo Background (no bloquea Gunicorn).
"""
import logging
import os
import threading

log = logging.getLogger("petra")

_scheduler = None


def iniciar():
    global _scheduler
    db_url   = os.environ.get("DATABASE_URL", "").strip()

    if not db_url:
        log.info("[Scheduler] Sin DATABASE_URL — no se inicia.")
        return

    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler(timezone="America/Bogota")
    _scheduler.add_job(_webhook_queue_health, "interval", minutes=10,
                       id="webhook_queue_health",
                       max_instances=1, coalesce=True)
    _scheduler.add_job(_drenar_log_mensaje_spool, "interval", minutes=10,
                       id="log_mensaje_spool_drain",
                       max_instances=1, coalesce=True)
    # Iter 5 (oct-2026): reminders y scheduled messages c/5 min.
    _scheduler.add_job(_disparar_reminders, "interval", minutes=5,
                       id="disparar_reminders",
                       max_instances=1, coalesce=True)
    _scheduler.add_job(_enviar_mensajes_programados, "interval", minutes=5,
                       id="enviar_mensajes_programados",
                       max_instances=1, coalesce=True)
    # Sprint 8 — control de calidad del inventario (Flujo 9) cada 6h
    _scheduler.add_job(_petra_control_calidad, "interval", hours=6,
                       id="petra_control_calidad",
                       max_instances=1, coalesce=True)
    # Sprint 9 — escalado de casos de soporte con SLA vencido cada 15 min
    _scheduler.add_job(_petra_escalar_casos, "interval", minutes=15,
                       id="petra_escalar_casos",
                       max_instances=1, coalesce=True)
    # Sprint 10 — recordatorios de visitas próximas cada 30 min
    _scheduler.add_job(_petra_recordatorios_visitas, "interval", minutes=30,
                       id="petra_recordatorios_visitas",
                       max_instances=1, coalesce=True)
    # Sprint 13 — reintentos de integraciones externas cada 5 min
    _scheduler.add_job(_petra_retry_integraciones, "interval", minutes=5,
                       id="petra_retry_integraciones",
                       max_instances=1, coalesce=True)
    # Multi-agent: cerrar sesiones conversacionales inactivas (default
    # 5 min sin actividad) con un mensaje de despedida.
    _scheduler.add_job(_petra_cerrar_sesiones_inactivas, "interval",
                       minutes=1, id="petra_cerrar_sesiones_inactivas",
                       max_instances=1, coalesce=True)
    # Fase D — generar filas de pago de arriendo el 1er día del mes 06:00 Bogotá
    _scheduler.add_job(_petra_generar_pagos_arriendo, "cron",
                       day=1, hour=6, minute=0,
                       id="petra_generar_pagos_arriendo",
                       max_instances=1, coalesce=True)
    # Fase A — nudge diario a autorizaciones admin estancadas (10am Bogotá)
    _scheduler.add_job(_petra_nudge_autorizaciones, "cron",
                       hour=10, minute=0,
                       id="petra_nudge_autorizaciones",
                       max_instances=1, coalesce=True)
    # Briefing diario a Valentina (8am Bogotá) — listado de agentes en flow
    # agrupadas por estado para que sepa a quién ayudar hoy.
    _scheduler.add_job(_petra_briefing_valentina, "cron",
                       hour=8, minute=0,
                       id="petra_briefing_valentina",
                       max_instances=1, coalesce=True)
    # Capacitación de agentes: 2 videos por día x 6 días, 11am Bogotá.
    # Solo avanza al día siguiente si la agente confirmó los del anterior.
    _scheduler.add_job(_petra_enviar_capacitacion_diaria, "cron",
                       hour=11, minute=0,
                       id="petra_capacitacion_diaria",
                       max_instances=1, coalesce=True)
    # FASE F — Nudges de remarketing: evalúa reglas y manda recordatorios
    # a agentes atascadas en una fase. Hora en punto (hora Bogotá no importa:
    # las ventanas son en horas, no horarios específicos).
    _scheduler.add_job(_petra_evaluar_nudges, "cron",
                       minute=15,
                       id="petra_nudges_horario",
                       max_instances=1, coalesce=True)
    # M5.A — Nudges de "cita nueva sin responder" (cadencia agresiva:
    # 5min x3 → 1h x6 → 24h). Corre cada 5 min; el helper filtra por
    # horario laboral y feriados Colombia.
    _scheduler.add_job(_petra_evaluar_citas_pendientes, "interval",
                       minutes=5,
                       id="petra_nudges_citas",
                       max_instances=1, coalesce=True)
    # Autonomía kit: pasadas ~KIT_AUTOENVIO_HORAS (24 default) desde que la
    # agente completó la dirección, el bot automáticamente la pasa a
    # kit_enviado y le manda "tu kit salió en camino" + video + botón QR.
    # Fuente: variable env PETRA_KIT_AUTOENVIO_HORAS (int, default 24).
    _scheduler.add_job(_petra_auto_marcar_kits_enviados, "interval",
                       minutes=30,
                       id="petra_kit_autoenvio",
                       max_instances=1, coalesce=True)
    # Tool-calling queue: cada 2 min reintenta los mensajes que fallaron
    # (LLM caído o lento). Si LLM responde → avanza la conversación donde
    # quedó. Si falla 10x seguidas → marca abandonado + alerta humano.
    _scheduler.add_job(_petra_tc_queue_procesar, "interval",
                       minutes=2,
                       id="petra_tc_queue",
                       max_instances=1, coalesce=True)
    _scheduler.start()
    log.info("[Scheduler] Iniciado — healthcheck webhook_queue c/10min, drain log_mensaje spool c/10min, "
             "nudges/citas/kit/tc_queue Petra.")

    # Drenaje inicial del spool de OUTs: en background para no bloquear el
    # arranque si hay cientos de archivos acumulados (bloquearia health
    # checks → host reinicia → loop). El job periodico igual completara
    # lo que falte.
    from app import db as _db
    if _db.OUT_SPOOL_DRAIN_ON_START:
        threading.Thread(target=_drenar_log_mensaje_spool_background,
                         name="log-mensaje-spool-drain-start",
                         daemon=True).start()
    else:
        log.warning("[LogMensajeSpool] OUT_SPOOL_DRAIN_ON_START=false — skip drenaje inicial.")


# ---------------------------------------------------------------------------

def _drenar_log_mensaje_spool():
    """Job periodico: drena una tanda del spool de OUTs. Si quedan mas
    archivos luego de la tanda, el proximo tick los procesa."""
    from app import db
    try:
        db._drenar_spool_log_mensaje()
    except Exception:
        log.exception("[LogMensajeSpool] Error drenando spool.")


def _drenar_log_mensaje_spool_background():
    """Drenaje inicial completo (en tandas). Corre en un thread aparte para
    no bloquear el arranque del server."""
    from app import db
    try:
        total = 0
        # Tope de seguridad: evitar loop infinito si siempre falla. Al
        # alcanzarlo cedemos al job periodico.
        for _ in range(500):
            n = db._drenar_spool_log_mensaje()
            if not n:
                break
            total += n
        if total:
            log.info("[LogMensajeSpool] Drenaje inicial: %d fila(s) recuperadas.", total)
    except Exception:
        log.exception("[LogMensajeSpool] Error en drenaje inicial.")


def _webhook_queue_health():
    """Alerta si hay webhooks pendientes atascados (>1 min sin procesar).
    En operación normal la cola se drena en milisegundos; una acumulación
    sostenida indica que el worker está colgado o la DB saturada."""
    from app import db
    try:
        with db._conexion() as conn:
            fila = conn.execute(
                "SELECT COUNT(*) FROM webhook_queue "
                "WHERE estado='pending' AND recibido_at < NOW() - INTERVAL '1 minute'"
            ).fetchone()
        pendientes = fila[0] if fila else 0
        if pendientes > 0:
            log.warning("[WebhookQueue] %d webhooks pendientes atascados >1 min.",
                        pendientes)
    except Exception:
        log.exception("[WebhookQueue] Error en healthcheck.")


# ===========================================================================
# Iter 5 (oct-2026) — Jobs
# ===========================================================================
#
# Decisión sobre pausas de bot en scheduled messages:
# los mensajes programados SIEMPRE se envían al disparar, incluso si el bot
# está pausado para ese lead. Razonamiento: programar un mensaje es una
# decisión explícita del asesor con fecha futura; que luego alguien pause el
# bot per lead (típicamente para hacerse cargo de la conversación) no debe
# cancelar silenciosamente el envío programado. Se loguea warning si el bot
# está pausado al momento del envío. Los contactos con `no_contactar = TRUE`
# SÍ se saltean (el opt-out es una señal fuerte).


def _disparar_reminders():
    """Dispara los reminders cuyo disparar_at ya venció.

    Por cada uno: (a) marca al contacto como no-leído (visto_at = NULL),
    (b) registra evento timeline `reminder_disparado`, (c) marca reminder
    como `disparado`.
    """
    from app import db
    try:
        pendientes = db.listar_reminders_para_disparar()
    except Exception:
        log.exception("[Scheduler] reminders listar fallo")
        return
    if not pendientes:
        return
    log.info("[Scheduler] %d reminder(s) por disparar.", len(pendientes))
    for r in pendientes:
        rid = r.get("id")
        cid = r.get("contacto_id")
        try:
            db.marcar_no_visto(cid)
        except Exception:
            log.exception("[Scheduler] reminder marcar_no_visto fallo cid=%s", cid)
        try:
            db.registrar_evento(
                cid, "reminder_disparado",
                {"reminder_id": rid,
                 "texto": (r.get("texto") or "")[:200]},
                autor=r.get("autor") or "sistema",
            )
        except Exception:
            log.exception("[Scheduler] reminder evento fallo rid=%s", rid)
        try:
            db.marcar_reminder_disparado(rid)
        except Exception:
            log.exception("[Scheduler] reminder marcar fallo rid=%s", rid)


def _enviar_mensajes_programados():
    """Envía los mensajes programados cuyo enviar_at ya venció.

    SKIP sólo si `no_contactar = TRUE`. Si bot pausado, loguea warning pero
    envía (ver nota arriba).
    """
    from app import db
    try:
        pendientes = db.listar_mensajes_programados_para_enviar()
    except Exception:
        log.exception("[Scheduler] mp listar fallo")
        return
    if not pendientes:
        return
    log.info("[Scheduler] %d mensaje(s) programado(s) por enviar.",
             len(pendientes))
    for mp in pendientes:
        mp_id = mp.get("id")
        cid = mp.get("contacto_id")
        tel = mp.get("telefono")
        template_name = mp.get("template_name")
        params = mp.get("template_params") or {}
        if isinstance(params, str):
            try:
                import json as _json
                params = _json.loads(params)
            except Exception:
                params = {}
        if mp.get("no_contactar"):
            db.marcar_mensaje_programado_fallido(
                mp_id, "contacto_opt_out")
            try:
                db.registrar_evento(
                    cid, "mensaje_programado_cancelado",
                    {"mensaje_programado_id": mp_id, "razon": "no_contactar"},
                    autor="sistema",
                )
            except Exception:
                pass
            continue
        try:
            if db.bot_esta_pausado(tel):
                log.warning("[Scheduler] mp tel=%s bot pausado — se envía igual",
                            tel)
        except Exception:
            pass
        try:
            from app import broadcasts as _bc
            res = _bc._enviar_template(tel, template_name, params)
            status = res.get("status") if isinstance(res, dict) else None
            ok = (status in (200, 201, "dry-run", "paused")
                  or (isinstance(status, int) and status < 300))
            if ok:
                db.marcar_mensaje_programado_enviado(mp_id)
                try:
                    db.registrar_evento(
                        cid, "mensaje_programado_enviado",
                        {"mensaje_programado_id": mp_id,
                         "template_name": template_name},
                        autor=mp.get("autor") or "sistema",
                    )
                except Exception:
                    pass
            else:
                err = f"status={status}"
                db.marcar_mensaje_programado_fallido(mp_id, err)
        except Exception as exc:  # noqa: BLE001
            log.exception("[Scheduler] mp send fallo mp_id=%s", mp_id)
            db.marcar_mensaje_programado_fallido(mp_id, str(exc)[:200])


# Sprint 8 — control de calidad del inventario Petra (Flujo 9)
def _petra_briefing_valentina():
    """Resumen diario a Valentina (8am Bogota): todas las agentes que
    están en algún estado activo del flow, agrupadas por estado."""
    try:
        import os as _os
        vale_tel = (_os.environ.get("VALENTINA_PHONE") or "").strip()
        if not vale_tel:
            log.info("[Scheduler] briefing_valentina: sin VALENTINA_PHONE, skip")
            return
        from app import db as _db, petra_wa
        with _db._conexion_directa(timeout=15) as conn:
            rows = conn.execute(
                "SELECT c.nombre, c.telefono, "
                "       r.metadata->>'estado_agente' AS estado, "
                "       r.metadata->>'conjunto' AS conjunto, "
                "       r.metadata->>'ciudad' AS ciudad, "
                "       (r.metadata->>'videos_vistos')::int AS vistos "
                "FROM petra_contactos c "
                "JOIN petra_contacto_roles r ON r.contacto_id=c.id "
                "WHERE r.rol='agente_petra' "
                "  AND COALESCE(r.metadata->>'estado_agente','desconocido') "
                "      NOT IN ('desconocido','activa','pausada') "
                "ORDER BY r.metadata->>'estado_agente', c.nombre NULLS LAST"
            ).fetchall()
        if not rows:
            msg = (
                "¡Hola Valentina! ☀️💜\n\n"
                "Hoy no hay agentes esperando ayuda. "
                "¡Qué buen día! 🚀"
            )
            petra_wa.send_text(vale_tel, msg)
            log.info("[Scheduler] briefing_valentina: 0 agentes")
            return

        _ETIQUETAS = {
            "recolectando": "📝 Recolectando datos",
            "inscrito": "✅ Inscritas (falta arrancar capacitación)",
            "bienvenida_enviada": "👋 Bienvenida enviada",
            "en_capacitacion": "🎓 En capacitación",
            "autorizacion_pendiente": "🏢 Esperando autorización",
            "autorizacion_rechazada": "⚠️ Autorización rechazada",
            "autorizada": "✨ Autorizadas (falta kit)",
            "kit_pendiente": "📦 Kit pendiente",
            "kit_enviado": "📬 Kit enviado (falta activar)",
        }
        por_estado = {}
        for r in rows:
            nombre, tel, estado, conjunto, ciudad, vistos = r
            por_estado.setdefault(estado or "otro", []).append(
                {"nombre": nombre, "tel": tel, "conjunto": conjunto,
                 "ciudad": ciudad, "vistos": vistos}
            )
        total = len(rows)
        partes = [
            f"¡Hola Valentina! ☀️💜",
            "",
            f"Hoy tenemos que ayudar a *{total} agente"
            f"{'s' if total != 1 else ''}* 🙌",
            "",
        ]
        for est, lista in por_estado.items():
            etiqueta = _ETIQUETAS.get(est, f"• {est}")
            partes.append(f"*{etiqueta}* ({len(lista)})")
            for a in lista[:10]:
                linea = f"• {a['nombre'] or '(sin nombre)'}"
                if a["tel"]:
                    linea += f" · {a['tel']}"
                if est == "en_capacitacion" and a["vistos"] is not None:
                    dia = a["vistos"] // 2
                    linea += f" · día {dia}/6"
                if a["conjunto"] and est in (
                    "autorizacion_pendiente", "autorizada",
                    "kit_pendiente", "kit_enviado",
                ):
                    linea += f" · {a['conjunto']}"
                partes.append(linea)
            if len(lista) > 10:
                partes.append(f"  … y {len(lista) - 10} más")
            partes.append("")
        partes.append("¡Vamos a por eso! 💜🚀")
        msg = "\n".join(partes)
        petra_wa.send_text(vale_tel, msg)
        log.info("[Scheduler] briefing_valentina: enviado (%d agentes)", total)
    except Exception:
        log.exception("[Scheduler] briefing_valentina fallo")


def _petra_control_calidad():
    try:
        from app import db as _db, petra_core
        with _db._conexion_directa(timeout=30) as conn:
            out = petra_core.control_calidad_inventario(conn)
        log.info("[Scheduler] petra_control_calidad: %s", out)
    except Exception:
        log.exception("[Scheduler] petra_control_calidad fallo")


# Sprint 9 — escalado automático de casos de soporte (Flujo 7)
def _petra_escalar_casos():
    try:
        from app import db as _db, petra_core
        with _db._conexion_directa(timeout=15) as conn:
            escalados = petra_core.escalar_casos_vencidos(conn)
        if escalados:
            log.warning("[Scheduler] petra_escalar_casos: %d escalados: %s",
                        len(escalados), escalados)
    except Exception:
        log.exception("[Scheduler] petra_escalar_casos fallo")


# Sprint 10 — recordatorios de visitas próximas (Flujo 5)
def _petra_recordatorios_visitas():
    try:
        from app import db as _db, petra_core
        with _db._conexion_directa(timeout=15) as conn:
            n = petra_core.visitas_proximas_a_recordar(conn, horas_antes=24)
        if n:
            log.info("[Scheduler] petra_recordatorios_visitas: %d encoladas", n)
    except Exception:
        log.exception("[Scheduler] petra_recordatorios_visitas fallo")


# Sprint 13 — reintentos de integraciones externas (Flujo 13)
def _petra_retry_integraciones():
    """Toma errores con proximo_intento_at vencido y los re-ejecuta a través
    de petra_core.reintentar_integracion. Resuelve al éxito, re-encola con
    backoff al fallo (dead_letter cuando se agota max_intentos)."""
    try:
        from app import db as _db, petra_core
        with _db._conexion_directa(timeout=15) as conn:
            rows = petra_core.errores_pendientes_de_retry(conn, limit=50)
            # Marcar reintentando primero (lock lógico) para no duplicar
            # el envío si una segunda corrida del scheduler se superpone.
            ids = [r[0] for r in rows]
            if ids:
                conn.execute(
                    "UPDATE petra_integracion_errores SET estado='reintentando', updated_at=NOW() WHERE id = ANY(%s)",
                    (ids,),
                )
        for (eid, prov, op, _ent_t, _ent_id, payload, intentos) in rows:
            log.info("[Scheduler] retry integracion id=%s %s/%s intento=%s",
                     eid, prov, op, intentos + 1)
            ok, nota = petra_core.reintentar_integracion(
                eid, prov, op, payload, intentos
            )
            with _db._conexion_directa(timeout=10) as conn:
                if ok:
                    petra_core.resolver_error_integracion(conn, eid, nota=nota)
                else:
                    petra_core.registrar_error_integracion(
                        conn,
                        proveedor=prov,
                        operacion=op,
                        error_mensaje=nota,
                        entidad_tipo=_ent_t,
                        entidad_id=_ent_id,
                        payload=payload,
                    )
    except Exception:
        log.exception("[Scheduler] petra_retry_integraciones fallo")


# Fase D — pagos de arriendo mensuales
def _petra_generar_pagos_arriendo():
    try:
        from app import db as _db, petra_core
        with _db._conexion_directa(timeout=20) as conn:
            res = petra_core.generar_pagos_mes(conn)
        log.info("[Scheduler] petra_generar_pagos_arriendo: %s", res)
    except Exception:
        log.exception("[Scheduler] petra_generar_pagos_arriendo fallo")


# Fase A — nudge a autorizaciones admin estancadas (3/7/14 días)
def _petra_nudge_autorizaciones():
    """Encola un recordatorio al agente para autorizaciones en estado
    'kit_enviado' o 'en_gestion' que lleven 3/7/14 días sin actualización."""
    try:
        from app import db as _db
        import json as _json
        with _db._conexion_directa(timeout=20) as conn:
            rows = conn.execute(
                """SELECT a.id, a.agente_id, c.telefono, a.conjunto_nombre,
                          EXTRACT(DAY FROM NOW() - a.updated_at)::int AS dias
                   FROM petra_autorizaciones_admin a
                   JOIN petra_contactos c ON c.id = a.agente_id
                   WHERE a.estado IN ('kit_enviado','en_gestion')
                     AND EXTRACT(DAY FROM NOW() - a.updated_at)::int IN (3,7,14)
                   ORDER BY a.updated_at ASC"""
            ).fetchall()
            for (aid, agente_id, tel, conjunto, dias) in rows:
                texto = (
                    f"Hola 👋 Han pasado {dias} días desde que te mandamos el "
                    f"material para el administrador de {conjunto}. "
                    f"¿Cómo va? Cuéntame si pudiste hablar con él."
                )
                payload = {"tipo_mensaje": "buttons", "texto": texto,
                           "buttons": [
                               {"id": f"AUTH_{aid}_APROBADA", "title": "Ya aprobaron"},
                               {"id": f"AUTH_{aid}_EN_GESTION", "title": "Sigo en eso"},
                               {"id": f"AUTH_{aid}_RECHAZADA", "title": "No se pudo"},
                           ]}
                conn.execute(
                    """INSERT INTO petra_outbound_queue
                       (contacto_id, telefono, tipo, payload, origen, estado)
                       VALUES (%s,%s,'buttons',%s::jsonb,'nudge_autorizacion','pendiente')""",
                    (agente_id, tel, _json.dumps(payload, ensure_ascii=False)),
                )
                conn.execute(
                    "UPDATE petra_autorizaciones_admin SET updated_at=NOW() WHERE id=%s",
                    (aid,),
                )
        if rows:
            log.info("[Scheduler] petra_nudge_autorizaciones: %d enviados", len(rows))
    except Exception:
        log.exception("[Scheduler] petra_nudge_autorizaciones fallo")


def _petra_evaluar_nudges():
    """FASE F — Pasada horaria del motor de nudges. Encuentra agentes
    atascadas por X horas en una fase y les manda el nudge configurado."""
    try:
        from app.petra_agents import nudges
        reglas, enviados = nudges.evaluar_y_enviar()
        if enviados:
            log.info("[Scheduler] nudges: %d reglas → %d mensajes enviados",
                     reglas, enviados)
    except Exception:
        log.exception("[Scheduler] evaluar_nudges fallo")


def _petra_evaluar_citas_pendientes():
    """M5.A — Cadencia agresiva para 'activa + cita_pendiente'.
    5min x3 → 1h x6 → 24h, pausado fin de semana/feriados CO y fuera 8-20."""
    try:
        from app.petra_agents import nudges
        enviados = nudges.evaluar_citas_pendientes()
        if enviados:
            log.info("[Scheduler] nudges:cita: %d enviados", enviados)
    except Exception:
        log.exception("[Scheduler] evaluar_citas_pendientes fallo")


def _petra_tc_queue_procesar():
    """Reintenta cada 2 min los mensajes de la queue toolcalling (LLM caído).
    Cuando el LLM responde, continúa la conversación donde quedó."""
    try:
        from app.petra_agents import toolcalling_queue as _tcq
        n = _tcq.procesar_pendientes()
        if n:
            log.info("[Scheduler] tc-queue: %d mensajes resueltos", n)
    except Exception:
        log.exception("[Scheduler] tc-queue fallo")


def _petra_auto_marcar_kits_enviados():
    """Autonomía kit: pasadas N horas (PETRA_KIT_AUTOENVIO_HORAS, default 24)
    desde que la agente completó su dirección, la pasamos auto a estado
    kit_enviado y disparamos el mismo mensaje+video+botón QR que haría un
    humano desde el panel. Esto reemplaza el "humano marca enviado"."""
    import os as _os
    import datetime as _dt
    import json as _json
    import time as _tm
    from app import db as _db
    from app import petra_wa as _pwa
    from app.petra_agents.textos import t as _t
    try:
        horas = int(_os.environ.get("PETRA_KIT_AUTOENVIO_HORAS", "24"))
    except (ValueError, TypeError):
        horas = 24
    corte = _dt.datetime.utcnow() - _dt.timedelta(hours=horas)
    try:
        with _db._conexion_directa(timeout=15) as conn:
            rows = conn.execute(
                "SELECT c.id, c.telefono, r.metadata "
                "FROM petra_contactos c "
                "JOIN petra_contacto_roles r "
                "  ON r.contacto_id=c.id AND r.rol='agente_petra' "
                "WHERE r.metadata->>'estado_agente' = 'kit_pendiente' "
                "  AND r.metadata->>'direccion_capturada_at' IS NOT NULL "
                "  AND (r.metadata->>'direccion_capturada_at')::timestamp <= %s",
                (corte,),
            ).fetchall()
    except Exception:
        log.exception("[Scheduler] auto_marcar_kits query fallo")
        return
    if not rows:
        return
    enviados = 0
    for cid, telefono, meta in rows:
        meta = dict(meta or {})
        # Doble-check para no pasar sin dirección capturada
        direccion = meta.get("direccion") or {}
        direccion_txt = meta.get("direccion_texto") or direccion.get("completa")
        tiene_direccion_vieja = all(direccion.get(c) for c in ("torre", "apto", "barrio", "ciudad"))
        if not direccion_txt and not tiene_direccion_vieja:
            continue
        try:
            # 1) estado → kit_enviado
            meta["estado_agente"] = "kit_enviado"
            meta["kit_enviado_at"] = _dt.datetime.utcnow().isoformat()
            meta["kit_enviado_auto"] = True
            with _db._conexion_directa(timeout=10) as conn:
                conn.execute(
                    "UPDATE petra_contacto_roles SET metadata=%s::jsonb, "
                    "updated_at=NOW() WHERE contacto_id=%s AND rol='agente_petra'",
                    (_json.dumps(meta), cid),
                )
                # Cargar QR + video (idéntico a kit_marcar_enviado del panel)
                qr_url = None
                try:
                    qr_row = conn.execute(
                        "SELECT e.qr_slug FROM petra_edificios e "
                        "JOIN petra_contacto_roles r "
                        "  ON r.edificio_id = e.id AND r.contacto_id = %s "
                        "  AND r.rol = 'agente_petra' "
                        "WHERE e.qr_slug IS NOT NULL LIMIT 1",
                        (cid,),
                    ).fetchone()
                    if qr_row and qr_row[0]:
                        qr_url = f"https://petra-inmuebles.onrender.com/qr/{qr_row[0]}"
                except Exception:
                    pass
                vid_row = conn.execute(
                    "SELECT titulo, url FROM petra_contenidos "
                    "WHERE tipo='video_como_usar_qr' AND activo=TRUE "
                    "ORDER BY orden LIMIT 1"
                ).fetchone()
            # 2) Envío de mensajes. Usamos _sm para que respete media/botones/
            #    extras adjuntos al mensaje desde el panel.
            from app.petra_agents.flow_onboarding_agente import _sm as _sm_flow
            _sm_flow(telefono,
                "activacion.msg_kit_enviado",
                fallback=(
                    "📦 *¡Tu kit ya está en camino!*\n\n"
                    "Dentro vas a encontrar tu QR + un regalito 💜. "
                    "Cuando lo recibas, instalá el QR en la cartelera del "
                    "conjunto o donde lo autorizó el administrador.\n\n"
                    "Te paso un video rápido con el paso a paso 👇"
                ),
            )
            _tm.sleep(1)
            if vid_row:
                _pwa.send_media_o_texto(telefono, vid_row[0], vid_row[1])
                _tm.sleep(2)
            if qr_url:
                _pwa.send_text(telefono, f"🔗 Tu QR único: {qr_url}")
                _tm.sleep(1)
            _pwa.send_buttons(telefono, _t(
                "activacion.msg_qr_boton",
                fallback="Cuando lo tengas instalado, pulsá el botón 👇",
            ), [{"id": "ONBAG_QR_INSTALADO", "title": "✅ Ya instalé el QR"}])
            enviados += 1
            log.info("[Scheduler] auto_kit_enviado tel=%s cid=%s horas=%s",
                     telefono, cid, horas)
        except Exception:
            log.exception("[Scheduler] auto_marcar_kits fallo cid=%s", cid)
    if enviados:
        log.info("[Scheduler] auto_marcar_kits: %d agentes avanzadas a kit_enviado",
                 enviados)


def _petra_cerrar_sesiones_inactivas():
    """Cierra sesiones conversacionales con inactividad > PETRA_SESION_INACTIVIDAD_MIN
    (default 5). Envía despedida al user y marca cerrada_at."""
    try:
        from app.petra_agents import sesion
        n = sesion.cerrar_inactivas_y_avisar()
        if n:
            log.info("[Scheduler] cerradas %d sesiones por inactividad", n)
    except Exception:
        log.exception("[Scheduler] cerrar_sesiones_inactivas fallo")


# =============================================================================
# Capacitación diaria de agentes Petra (cron 8am Bogotá)
# =============================================================================


def _petra_enviar_capacitacion_diaria(ignorar_dedup_fecha=False):
    """Para cada agente en capacitación activa, envía los 2 videos del
    día siguiente SOLO si ya confirmó los del día anterior.

    ignorar_dedup_fecha: si True, bypasea la regla "no mandar 2 veces el
    mismo día". Útil para pruebas y re-disparos admin desde el panel.

    Kill switch: env var PETRA_CRON_CAPACITACION_OFF=true salta el job sin
    hacer nada. Útil para pausar envíos durante pruebas o incidentes sin
    tocar código. Para reactivar: borrar la env var (o setearla vacía).

    Reglas:
      - Solo rol=agente_petra con estado_agente IN ('esperando_capacitacion_dia1',
        'en_capacitacion').
      - Respeta metadata.bot_pausado=true → skip.
      - Respeta opt_out → skip.
      - Avance: envía día N+1 solo si videos_vistos >= N*2 (confirmó
        'LISTO' por cada video del día anterior).
      - Dedup por fecha: si cap_ultimo_envio_fecha == hoy_bogota → skip
        (idempotente ante reinicios del scheduler).
      - Silencioso si no hay 2 videos cargados en petra_contenidos para
        el día que corresponde: log y skip.
      - Al enviar día 1 por primera vez, cambia estado_agente a
        'en_capacitacion' (desde 'esperando_capacitacion_dia1').
    """
    if (os.environ.get("PETRA_CRON_CAPACITACION_OFF") or "").strip().lower() in ("1", "true", "yes"):
        log.info("[cap-cron] deshabilitado por PETRA_CRON_CAPACITACION_OFF — skip")
        return
    import datetime as _dt
    import json as _json
    import time as _time
    try:
        from zoneinfo import ZoneInfo
    except Exception:
        log.exception("[cap-cron] zoneinfo no disponible")
        return
    try:
        from app import db as _db, petra_wa
    except Exception:
        log.exception("[cap-cron] imports fallaron")
        return

    hoy_bogota = _dt.datetime.now(ZoneInfo("America/Bogota")).date().isoformat()
    try:
        with _db._conexion_directa(timeout=15) as conn:
            cur = conn.execute(
                "SELECT c.id, c.telefono, r.metadata "
                "FROM petra_contactos c "
                "JOIN petra_contacto_roles r ON r.contacto_id = c.id "
                "WHERE r.rol = 'agente_petra' "
                "  AND r.metadata->>'estado_agente' IN "
                "      ('esperando_capacitacion_dia1','en_capacitacion') "
                "  AND COALESCE(r.metadata->>'bot_pausado','false') <> 'true' "
                "  AND COALESCE(c.opt_out, FALSE) = FALSE"
            )
            agentes = cur.fetchall()
    except Exception:
        log.exception("[cap-cron] consulta de agentes fallo")
        return

    log.info("[cap-cron] evaluando %s agentes", len(agentes))
    enviados = 0
    for row in agentes:
        cid, tel, meta = row[0], row[1], row[2] or {}
        try:
            with _db._conexion_directa(timeout=15) as conn:
                ok = _cap_enviar_para_agente(conn, cid, tel, meta, hoy_bogota,
                                             petra_wa, _time, _json,
                                             ignorar_dedup_fecha=ignorar_dedup_fecha)
                if ok:
                    enviados += 1
        except Exception:
            log.exception("[cap-cron] fallo para tel=%s", tel)
    log.info("[cap-cron] enviados: %s/%s", enviados, len(agentes))


def _cap_enviar_para_agente(conn, cid, telefono, meta, hoy_iso,
                            petra_wa, _time, _json,
                            ignorar_dedup_fecha=False):
    """Procesa una agente. Devuelve True si envió videos hoy.

    Flow version (meta.cap_version):
      v1 (default): 12 videos / 6 días. Todos los días son video_capacitacion.
      v2: 14 videos / 7 días. Día 1 = bienvenida+proximos_pasos;
          días 2-7 = video_capacitacion offset (dia-2)*2.
    """
    cap_v = int(meta.get("cap_version", 1) or 1)
    dia_enviado = int(meta.get("cap_dia_enviado", 0) or 0)
    videos_vistos = int(meta.get("videos_vistos", 0) or 0)
    max_dia = 7 if cap_v >= 2 else 6

    # Terminó capacitación
    if dia_enviado >= max_dia:
        return False

    # Regla "solo avanza si confirmó": esperamos LISTO por los anteriores
    if videos_vistos < dia_enviado * 2:
        return False

    # Dedup por fecha (saltable solo desde el endpoint admin para pruebas)
    if not ignorar_dedup_fecha and meta.get("cap_ultimo_envio_fecha") == hoy_iso:
        return False

    dia_a_enviar = dia_enviado + 1
    # Resolver qué videos corresponden al día según cap_version
    if cap_v >= 2 and dia_a_enviar == 1:
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
        offset_dia = (dia_a_enviar - 2) if cap_v >= 2 else (dia_a_enviar - 1)
        offset = offset_dia * 2
        cur = conn.execute(
            "SELECT titulo, url FROM petra_contenidos "
            "WHERE tipo='video_capacitacion' AND activo=TRUE "
            "ORDER BY orden LIMIT 2 OFFSET %s", (offset,),
        )
        videos = cur.fetchall()
    if len(videos) < 2:
        log.info("[cap-cron] día %s (v%s) sin 2 videos cargados — skip tel=%s",
                 dia_a_enviar, cap_v, telefono)
        return False

    # Enviar los 2 videos del día + CTA. Si el primer envío cae en
    # whitelist_skip (dry-run), abortamos sin actualizar metadata — la
    # agente no vio nada, no hay avance real.
    primera = petra_wa.send_media_o_texto(telefono, videos[0][0], videos[0][1])
    if primera.get("whitelist_skip"):
        log.info("[cap-cron] whitelist bloquea tel=%s — no se actualiza metadata",
                 telefono)
        return False
    _time.sleep(2)
    petra_wa.send_media_o_texto(telefono, videos[1][0], videos[1][1])
    _time.sleep(2)
    from app.petra_agents.textos import t as _t_textos, boton as _t_textos_boton
    petra_wa.send_buttons(telefono, _t_textos(
        "capacitacion.msg_cron_diario",
        fallback=(
            "🎬✨ *¡Nuevo día de capacitación!*\n\n"
            "Acá te dejo los 2 videos del día *{dia_a_enviar} de {max_dia}* 💜\n\n"
            "Mirálos con calma, cuando puedas — ni muy rápido ni muy lento. "
            "Aprendé a tu ritmo, pausá si lo necesitás, y volvé cuando estés lista.\n\n"
            "Cada video que ves te acerca un paso más a ser una Agente Petra "
            "experta en tu edificio 🏢💪\n\n"
            "Cuando los termines, apretá el botón 👇"
        ),
        dia_a_enviar=dia_a_enviar, max_dia=max_dia,
    ), [
        {"id": "ONBAG_LISTO",
         "title": _t_textos_boton("ONBAG_LISTO", "✅ Listo")},
    ])

    # Actualizar metadata
    meta["cap_dia_enviado"] = dia_a_enviar
    meta["cap_ultimo_envio_fecha"] = hoy_iso
    if meta.get("estado_agente") == "esperando_capacitacion_dia1":
        meta["estado_agente"] = "en_capacitacion"
    conn.execute(
        "UPDATE petra_contacto_roles SET metadata = %s::jsonb "
        "WHERE contacto_id = %s AND rol = 'agente_petra'",
        (_json.dumps(meta), cid),
    )
    log.info("[cap-cron] enviado día %s (v%s) a tel=%s", dia_a_enviar, cap_v, telefono)
    return True
