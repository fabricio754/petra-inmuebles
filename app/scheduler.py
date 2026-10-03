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

log = logging.getLogger("petra")

_scheduler = None


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
                       max_instances=1, coalesce=True, minutes=5)  # 5 min desfase
    _scheduler.start()
    log.info("[Scheduler] Iniciado — revisión Sureti cada 2 horas.")


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
            db.actualizar_estado_lead(lead["id"], nuevo_estado, monto=monto, razon=razon)
            log.info("[Scheduler] Lead %s: %s → %s", lead["id"], lead["estado"], nuevo_estado)

            telefono = lead["telefono"]
            nombre   = lead.get("nombre") or ""
            nombre_corto = nombre.split()[0] if nombre else ""

            if nuevo_estado == "APROBADO":
                whatsapp.send_credito_aprobado(telefono, nombre_corto, monto)
            elif nuevo_estado == "RECHAZADO":
                whatsapp.send_credito_rechazado(telefono, razon)
        except Exception:
            log.exception("[Scheduler] Error revisando lead %s", lead.get("id"))
