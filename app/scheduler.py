"""Verificación periódica del estado de leads en Sureti.

Cada 2 horas:
  1. registrar_nuevos() — toma leads en estado 'NUEVO' sin sureti_lead_id y
     los registra en el portal (si DATABASE_URL y SURETI_EMAIL están activos).
  2. revisar_leads()    — consulta el estado de leads en 'REGISTRADO' o
     'EN_ESTUDIO' y, si cambió, actualiza la BD y avisa al cliente por WA.

APScheduler se usa en modo Background (no bloquea Gunicorn).
"""
import logging
import os
import threading

log = logging.getLogger("petra")

_scheduler = None

# Retención de `webhook_queue` en días. Default 90 — alineado con la
# necesidad de trazabilidad del piloto (6-oct perdimos data raw anterior
# a las 15:30 UTC). Configurable via env var por si en algún deploy hace
# falta ajustarlo (ej. presión de disco en Render o auditoría más larga).
# El valor se lee una sola vez al arrancar el scheduler.
def _retencion_dias_default():
    try:
        valor = int(os.environ.get("WEBHOOK_QUEUE_RETENCION_DIAS", "90"))
    except ValueError:
        valor = 90
    # Clamp defensivo: nunca menos de 1 día (evita wipe accidental si
    # alguien exporta "0" por error).
    return max(1, valor)


WEBHOOK_QUEUE_RETENCION_DIAS = _retencion_dias_default()


def iniciar():
    global _scheduler
    db_url   = os.environ.get("DATABASE_URL", "").strip()
    email    = os.environ.get("SURETI_EMAIL", "").strip()

    if not db_url:
        log.info("[Scheduler] Sin DATABASE_URL — no se inicia.")
        return
    if not email:
        log.info("[Scheduler] Sin SURETI_EMAIL — scheduler en modo dry-run.")

    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler(timezone="America/Bogota")
    _scheduler.add_job(revisar_leads,    "interval", hours=2, id="revisar_leads",
                       max_instances=1, coalesce=True)
    _scheduler.add_job(registrar_nuevos, "interval", hours=2, id="registrar_nuevos",
                       max_instances=1, coalesce=True,
                       start_date="2000-01-01 00:05:00")  # 5 min desfase inicial
    _scheduler.add_job(_remarketing_job, "cron", hour=9, minute=0,
                       id="remarketing", max_instances=1, coalesce=True)
    _scheduler.add_job(_export_job, "cron", hour=6, minute=0,
                       id="export_sheets", max_instances=1, coalesce=True)
    _scheduler.add_job(_webhook_queue_health, "interval", minutes=10,
                       id="webhook_queue_health",
                       max_instances=1, coalesce=True)
    _scheduler.add_job(_drenar_log_mensaje_spool, "interval", minutes=10,
                       id="log_mensaje_spool_drain",
                       max_instances=1, coalesce=True)
    _scheduler.add_job(_purgar_webhook_queue, "cron", hour=4, minute=17,
                       id="webhook_queue_purge",
                       max_instances=1, coalesce=True)
    _scheduler.start()
    log.info("[Scheduler] Iniciado — Sureti c/2h, remarketing 9am, export Sheet 6am Bogotá, "
             "healthcheck webhook_queue c/10min, drain log_mensaje spool c/10min, "
             "purga webhook_queue diaria 04:17 Bogotá (retención %dd).",
             WEBHOOK_QUEUE_RETENCION_DIAS)

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

def registrar_nuevos():
    """Registra en Sureti los leads que aún no tienen sureti_lead_id."""
    from app import db, sureti
    leads = db.leads_nuevos()
    if not leads:
        return
    log.info("[Scheduler] %d leads nuevos por registrar.", len(leads))
    for lead in leads:
        try:
            docs = db.documentos_de(lead["telefono"])
            lead_id = sureti.registrar_lead(lead, docs)
            db.marcar_registrado_sureti(lead["id"], lead_id)
            log.info("[Scheduler] Lead %s registrado en Sureti → %s", lead["id"], lead_id)
        except Exception:
            log.exception("[Scheduler] Error registrando lead %s", lead.get("id"))


def _remarketing_job():
    from app import remarketing
    try:
        remarketing.ejecutar()
    except Exception:
        log.exception("[Scheduler] Error en remarketing.")


def _export_job():
    from app import export
    try:
        export.ejecutar()
    except Exception:
        log.exception("[Scheduler] Error en export sheets.")


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


def _purgar_webhook_queue():
    """Purga filas de `webhook_queue` más viejas que WEBHOOK_QUEUE_RETENCION_DIAS.

    Antes del PR #? no había purga explícita a nivel aplicación; el piloto
    6-oct perdimos la data raw anterior a las 15:30 UTC (causa aún no
    confirmada, posiblemente auto-vacuum / mantenimiento de Render). Este
    job pone la retención bajo control explícito.

    Solo borra filas ya terminadas (`estado IN ('ok','error')`) para no
    tocar jamás un pendiente/procesando huérfano que todavía deba drenarse.
    """
    from app import db
    dias = WEBHOOK_QUEUE_RETENCION_DIAS
    try:
        with db._conexion() as conn:
            n = conn.execute(
                "DELETE FROM webhook_queue "
                "WHERE recibido_at < NOW() - make_interval(days => %s) "
                "AND estado IN ('ok', 'error')",
                (dias,),
            ).rowcount
        if n:
            log.info("[WebhookQueue] Purga: %d filas eliminadas (>%dd).", n, dias)
        else:
            log.debug("[WebhookQueue] Purga: nada que borrar (>%dd).", dias)
    except Exception:
        log.exception("[WebhookQueue] Error en purga (>%dd).", dias)


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


def revisar_leads():
    """Consulta el estado de leads en seguimiento y avisa al cliente si cambió."""
    from app import db, sureti, whatsapp
    leads = db.leads_en_seguimiento()
    if not leads:
        return
    log.info("[Scheduler] Revisando %d leads en Sureti.", len(leads))
    for lead in leads:
        try:
            resultado = sureti.consultar_estado(lead["sureti_lead_id"])
            nuevo_estado = resultado.get("estado", "").upper()
            if not nuevo_estado or nuevo_estado == lead["estado"].upper():
                continue  # sin cambio

            monto = resultado.get("monto_aprobado")
            razon = resultado.get("razon")
            fecha_desembolso = resultado.get("fecha_desembolso")
            telefono = lead["telefono"]
            nombre   = lead.get("nombre") or ""
            nombre_corto = nombre.split()[0] if nombre else ""

            if nuevo_estado == "DESEMBOLSADO":
                comision = db.registrar_desembolso(lead["id"], monto, fecha_desembolso)
                log.info("[Scheduler] Lead %s desembolsado — comisión: %s", lead["id"], comision)
                whatsapp.send_credito_desembolsado(telefono, nombre_corto, monto, comision)
            else:
                db.actualizar_estado_lead(lead["id"], nuevo_estado, monto=monto, razon=razon)
                log.info("[Scheduler] Lead %s: %s → %s", lead["id"], lead["estado"], nuevo_estado)
                if nuevo_estado == "APROBADO":
                    whatsapp.send_credito_aprobado(telefono, nombre_corto, monto)
                elif nuevo_estado == "RECHAZADO":
                    whatsapp.send_credito_rechazado(telefono, razon)
        except Exception:
            log.exception("[Scheduler] Error revisando lead %s", lead.get("id"))
