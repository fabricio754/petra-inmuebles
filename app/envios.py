"""Envío automático de la plantilla de apertura a los contactos captados.

Corre en un hilo dentro del mismo proceso del bot (gunicorn -w 1) y solo si
ENVIO_AUTOMATICO=true. Respeta la Ley 2300 de 2023: lunes a viernes 7:00–19:00
y sábados 8:00–15:00 (hora Colombia), nunca domingos ni festivos. Manda como
máximo ENVIO_LIMITE_DIARIO plantillas por día (subirlo poco a poco mientras la
calidad del número en WhatsApp Manager se mantenga en verde)."""
import logging
import os
import threading
import time
from datetime import datetime, time as hora
from zoneinfo import ZoneInfo

import holidays

from app import db, whatsapp

log = logging.getLogger("petra")

ZONA = ZoneInfo("America/Bogota")
FESTIVOS = holidays.country_holidays("CO")
ACTIVO = os.environ.get("ENVIO_AUTOMATICO", "false").strip().lower() == "true"
LIMITE_DIARIO = int(os.environ.get("ENVIO_LIMITE_DIARIO", "20"))
# Fecha (AAAA-MM-DD, hora Colombia): solo se escribe a contactos capturados
# desde ese día. Sirve para dejar fuera las capturas de prueba.
_desde = os.environ.get("ENVIO_DESDE", "").strip()
DESDE = datetime.fromisoformat(_desde).replace(tzinfo=ZONA) if _desde else None
INTERVALO_SEG = 300   # revisa la cola cada 5 minutos
POR_RONDA = int(os.environ.get("ENVIOS_POR_RONDA", "5"))  # máximo plantillas por ronda (cada 5 min)

_iniciado = False


def horario_permitido(ahora=None):
    ahora = ahora or datetime.now(ZONA)
    if ahora.date() in FESTIVOS:
        return False
    dia, h = ahora.weekday(), ahora.time()
    if dia < 5:
        return hora(7) <= h < hora(19)
    if dia == 5:
        return hora(8) <= h < hora(15)
    return False


def ronda():
    """Una pasada: manda lo que se pueda ahora. Devuelve cuántos envió."""
    if not horario_permitido():
        return 0
    ahora = datetime.now(ZONA)
    inicio_dia = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    disponibles = LIMITE_DIARIO - db.enviados_desde(inicio_dia)
    if disponibles <= 0:
        return 0
    enviados = 0
    for c in db.contactos_por_enviar(min(disponibles, POR_RONDA), DESDE):
        r = whatsapp.send_plantilla_apertura(c["telefono"], c["tipo"], c["portal"], c["monto_hasta"])
        ok = r.get("status") == "dry-run" or (isinstance(r.get("status"), int) and r["status"] < 300)
        db.marcar_contactado(c["telefono"], "plantilla_enviada" if ok else "error_envio")
        enviados += ok
    if enviados:
        log.info("[Envíos] %d plantillas enviadas.", enviados)
    return enviados


def _bucle():
    while True:
        try:
            ronda()
        except Exception:
            log.exception("[Envíos] Error en la ronda de envío.")
        time.sleep(INTERVALO_SEG)


def iniciar():
    """Arranca el hilo de envíos (una sola vez por proceso)."""
    global _iniciado
    if _iniciado or not ACTIVO:
        return
    if not (whatsapp.PLANTILLA_APERTURA and db.DATABASE_URL):
        log.warning("[Envíos] ENVIO_AUTOMATICO=true pero falta META_PLANTILLA_APERTURA o DATABASE_URL.")
        return
    _iniciado = True
    threading.Thread(target=_bucle, name="envios", daemon=True).start()
    log.info("[Envíos] Activos: hasta %d por día.", LIMITE_DIARIO)
