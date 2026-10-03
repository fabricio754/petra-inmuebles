"""Sistema de remarketing automático.

Se ejecuta diariamente a las 9am hora Colombia (APScheduler en scheduler.py).
Antes de correr verifica que no sea festivo colombiano ni fin de semana.

Segmentos:
  1. No contestaron el primer mensaje:
     Día 3  → recordatorio con monto estimado
     Día 7  → propuesta de valor
     Día 15 → cierre definitivo
  2. Paz y salvos pendiente (dijeron sí pero necesitan ponerse al día):
     Día 15 → retoma
     Día 30 → cierre definitivo
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import holidays

from app import db, whatsapp

log = logging.getLogger("petra")

ZONA = ZoneInfo("America/Bogota")
FESTIVOS = holidays.country_holidays("CO")

# Mensajes por día (se formatea con los datos del contacto)
_MSG = {
    "no_responde_d3": (
        "Hola {nombre} 👋 ¿Pudiste ver mi mensaje anterior?\n\n"
        "El propietario de {direccion} podría acceder a ~${monto}M en liquidez "
        "sin necesidad de vender el inmueble. Tenemos un aliado financiero que "
        "puede ayudar. ¿Le interesa saber cómo funciona?"
    ),
    "no_responde_d7": (
        "Hola {nombre}, ¿el propietario ha pensado en usar el inmueble como "
        "respaldo para conseguir liquidez rápida?\n\n"
        "No necesita venderlo. Se puede conseguir hasta ${monto}M manteniendo "
        "la propiedad. Escribenos para contarles más 🏠"
    ),
    "no_responde_d15": (
        "Hola {nombre}, voy a cerrar este caso por ahora.\n\n"
        "Si en algún momento el propietario necesita liquidez con su inmueble "
        "como garantía, estamos disponibles. ¡Hasta pronto! 👋"
    ),
    "paz_salvo_d15": (
        "Hola {nombre} 👋 ¿Lograste ponerte al día con predial y servicios?\n\n"
        "Podemos retomar el proceso de crédito cuando quieras. Solo avísanos."
    ),
    "paz_salvo_d30": (
        "Hola {nombre}, hacemos un último seguimiento. Si en algún momento "
        "lograste ponerte al día y quieres explorar el crédito, escríbenos.\n\n"
        "Estamos disponibles 🤝"
    ),
}


def _puede_correr() -> bool:
    ahora = datetime.now(ZONA)
    if ahora.date() in FESTIVOS:
        log.info("[Remarketing] Hoy es festivo — omitiendo.")
        return False
    if ahora.weekday() >= 5:  # sábado o domingo
        log.info("[Remarketing] Fin de semana — omitiendo.")
        return False
    return True


def _monto_estimado(precio_publicado: int | None) -> int:
    """25% del precio publicado, en millones (Sureti usa ~25% de tasación)."""
    if not precio_publicado:
        return 50  # fallback razonable
    return max(20, round(precio_publicado * 0.25 / 1_000_000))


def _enviar(telefono: str, tipo: str, contacto: dict):
    plantilla = _MSG.get(tipo, "")
    if not plantilla:
        return

    nombre = (contacto.get("nombre") or "").split()[0] or "buenas"
    direccion = contacto.get("direccion") or "su inmueble"
    monto = _monto_estimado(contacto.get("precio_publicado"))

    mensaje = plantilla.format(nombre=nombre, direccion=direccion, monto=monto)
    whatsapp.send_text(telefono, mensaje)
    db.registrar_remarketing_envio(telefono, tipo, mensaje)
    log.info("[Remarketing] %s → %s", tipo, telefono)


def ejecutar():
    """Corre una ronda de remarketing. Llamado por APScheduler a las 9am."""
    if not _puede_correr():
        return

    # ── Segmento 1: no contestaron ──────────────────────────────────────────
    for tipo, dias in [
        ("no_responde_d3",  3),
        ("no_responde_d7",  7),
        ("no_responde_d15", 15),
    ]:
        contactos = db.contactos_sin_respuesta(dias=dias, tipo_remarketing=tipo)
        for c in contactos:
            _enviar(c["telefono"], tipo, c)
            if tipo == "no_responde_d15":
                db.cerrar_contacto(c["telefono"], "remarketing_agotado")

    # ── Segmento 2: paz y salvos pendiente ──────────────────────────────────
    for tipo, dias in [
        ("paz_salvo_d15", 15),
        ("paz_salvo_d30", 30),
    ]:
        contactos = db.contactos_paz_salvo_pendiente(dias=dias, tipo_remarketing=tipo)
        for c in contactos:
            _enviar(c["telefono"], tipo, c)
            if tipo == "paz_salvo_d30":
                db.cerrar_contacto(c["telefono"], "paz_salvo_sin_respuesta")
