"""APScheduler: tareas periódicas de Massi.

Jobs:
  - Scraper:       cada 2h → correr_todos()
  - Sureti check:  cada 2h → revisar leads en_estudio/registrado
  - Remarketing:   9am hora Colombia (L-V, no festivos)

Se inicia desde server.py al arrancar la app (iniciar()).
Usa BackgroundScheduler (hilo separado en el mismo proceso).
"""
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import db, whatsapp

log = logging.getLogger("petra")

ZONA = ZoneInfo("America/Bogota")
_scheduler = None


# ─────────────────────────────────────────────────────────────────────────────
# Jobs
# ─────────────────────────────────────────────────────────────────────────────

def _job_scraper():
    """Corre todos los scrapers."""
    try:
        from app.scraper import correr_todos
        correr_todos()
    except Exception:
        log.exception("[Scheduler] Error en scraper.")


def _job_remarketing():
    """Ronda de remarketing."""
    try:
        from app import remarketing
        remarketing.ejecutar()
    except Exception:
        log.exception("[Scheduler] Error en remarketing.")


def _job_revisar_leads():
    """Consulta estado de leads en Sureti y notifica al vendedor."""
    try:
        from app import sureti
        leads = db.leads_en_seguimiento()
        for lead in leads:
            if not lead.get("sureti_lead_id"):
                continue
            resultado = sureti.consultar_estado(lead["sureti_lead_id"])
            estado = resultado.get("estado")
            if estado == lead.get("estado"):
                continue  # Sin cambio

            telefono = lead["telefono"]
            db.actualizar_estado_lead(telefono, estado, resultado)

            if estado == "desembolsado":
                monto = resultado.get("monto_aprobado", 0) or 0
                comision = db.calcular_comision(monto)
                db.registrar_desembolso(telefono, monto, comision)
                monto_m = round(monto / 1_000_000)
                whatsapp.send_credito_desembolsado(telefono, monto_m)
                log.info(
                    "[Scheduler] Desembolso: %s — $%dM — comisión $%s COP",
                    telefono, monto_m, f"{comision:,}",
                )

            elif estado == "aprobado":
                monto = resultado.get("monto_aprobado", 0) or 0
                monto_m = round(monto / 1_000_000)
                whatsapp.send_credito_aprobado(telefono, monto_m)
                log.info("[Scheduler] Lead aprobado: %s — $%dM", telefono, monto_m)

            elif estado == "no_aprobado":
                razon = resultado.get("razon", "")
                whatsapp.send_credito_rechazado(telefono, razon)
                log.info("[Scheduler] Lead rechazado: %s — %s", telefono, razon)

    except Exception:
        log.exception("[Scheduler] Error revisando leads.")


def _job_registrar_nuevos():
    """Registra leads nuevos (estado=NUEVO) en Sureti."""
    try:
        from app import sureti
        leads = db.leads_nuevos()
        for lead in leads:
            docs = {d["tipo"]: d["url_storage"] for d in db.documentos_de(lead["telefono"])}
            lead_id = sureti.registrar_lead(lead, docs)
            if lead_id:
                db.marcar_registrado_sureti(lead["telefono"], lead_id)
                whatsapp.send_solicitud_enviada(lead["telefono"])
                log.info("[Scheduler] Lead registrado en Sureti: %s → %s", lead["telefono"], lead_id)
    except Exception:
        log.exception("[Scheduler] Error registrando nuevos leads.")


# ─────────────────────────────────────────────────────────────────────────────
# Inicialización
# ─────────────────────────────────────────────────────────────────────────────

def iniciar():
    global _scheduler
    if _scheduler is not None:
        return

    _scheduler = BackgroundScheduler(timezone="America/Bogota")

    # Scraper cada 2h
    if os.environ.get("SCRAPER_ENABLED", "false").lower() == "true":
        _scheduler.add_job(
            _job_scraper,
            trigger=IntervalTrigger(hours=2),
            id="scraper",
            replace_existing=True,
        )
        log.info("[Scheduler] Scraper registrado (cada 2h).")

    # Revisar leads en Sureti cada 2h
    _scheduler.add_job(
        _job_revisar_leads,
        trigger=IntervalTrigger(hours=2),
        id="revisar_leads",
        replace_existing=True,
    )

    # Registrar leads nuevos cada 30 min
    _scheduler.add_job(
        _job_registrar_nuevos,
        trigger=IntervalTrigger(minutes=30),
        id="registrar_nuevos",
        replace_existing=True,
    )

    # Remarketing: 9am Colombia L-V
    _scheduler.add_job(
        _job_remarketing,
        trigger=CronTrigger(
            hour=9, minute=0, day_of_week="mon-fri",
            timezone="America/Bogota",
        ),
        id="remarketing",
        replace_existing=True,
    )

    _scheduler.start()
    log.info("[Scheduler] APScheduler iniciado.")
