"""Remarketing diario (9am hora Colombia).

1. No-respondedores de captación (días 3, 7, 15): contactos con
   resultado_contacto = 'no_responde'. Día 15 cierra el contacto.

2. Paz y salvos (días 15, 30): leads en pipeline con estado
   'pausado_paz_salvo'. Recordatorio a día 15 y último aviso a día 30.

Cumple Ley 2300: no envía domingos ni festivos colombianos.
"""
import logging
from datetime import date

import holidays

from app import db, whatsapp

log = logging.getLogger("petra")

_CO_HOLIDAYS = holidays.Colombia()

# Monto estimado: 25 % del precio publicado como crédito disponible.
def _monto(precio_publicado):
    if not precio_publicado:
        return None
    return round(precio_publicado * 0.25 / 1_000_000)


SECUENCIA = [
    {
        "tipo": "dia_3",
        "dias": 3,
        "plantilla": (
            "Hola {nombre}, ¿pudiste ver mi mensaje?\n\n"
            "El propietario de {direccion} podría acceder a ~${monto}M "
            "sin vender el inmueble. ¿Le interesaría?"
        ),
        "plantilla_sin_monto": (
            "Hola {nombre}, ¿pudiste ver mi mensaje?\n\n"
            "El propietario de {direccion} podría acceder a liquidez rápida "
            "usando el inmueble como respaldo, sin venderlo. ¿Le interesaría?"
        ),
        "cerrar": False,
    },
    {
        "tipo": "dia_7",
        "dias": 7,
        "plantilla": (
            "¿El propietario ha pensado en usar el inmueble como respaldo "
            "para conseguir liquidez rápida? Podemos tenerlo listo "
            "en menos de 2 semanas."
        ),
        "cerrar": False,
    },
    {
        "tipo": "dia_15",
        "dias": 15,
        "plantilla": (
            "Voy a cerrar el caso por ahora. Si en algún momento "
            "le interesa, aquí estaremos."
        ),
        "cerrar": True,
    },
]


def ejecutar():
    """Punto de entrada llamado por el scheduler."""
    hoy = date.today()

    # Ley 2300: no enviar domingos ni festivos colombianos.
    if hoy.weekday() == 6:  # domingo
        log.info("[Remarketing] Domingo — se omite.")
        return
    if hoy in _CO_HOLIDAYS:
        log.info("[Remarketing] Festivo (%s) — se omite.", hoy)
        return

    for etapa in SECUENCIA:
        _procesar_etapa(etapa)

    _procesar_paz_salvos()


# ---------------------------------------------------------------------------

def _procesar_etapa(etapa: dict):
    tipo = etapa["tipo"]
    dias = etapa["dias"]
    contactos = db.contactos_sin_respuesta(dias, tipo)
    if not contactos:
        return

    log.info("[Remarketing] %s: %d contactos elegibles.", tipo, len(contactos))
    for c in contactos:
        try:
            _enviar(c, etapa)
        except Exception:
            log.exception("[Remarketing] Error enviando %s a %s", tipo, c.get("telefono"))


def _enviar(c: dict, etapa: dict):
    telefono = c["telefono"]
    nombre_raw = (c.get("nombre") or "").strip()
    nombre = nombre_raw.split()[0] if nombre_raw else "propietario"
    direccion = (c.get("direccion") or "").strip() or "tu inmueble"
    precio = c.get("precio_publicado")
    monto = _monto(precio)

    plantilla = etapa.get("plantilla", "")
    if "{monto}" in plantilla and not monto:
        # Usar plantilla alternativa sin monto si no hay precio disponible
        plantilla = etapa.get("plantilla_sin_monto", plantilla)

    if monto:
        mensaje = plantilla.format(nombre=nombre, direccion=direccion, monto=monto)
    else:
        mensaje = plantilla.format(nombre=nombre, direccion=direccion)

    whatsapp.send_text(telefono, mensaje)
    db.registrar_remarketing_envio(telefono, etapa["tipo"], mensaje)
    log.info("[Remarketing] %s enviado a %s.", etapa["tipo"], telefono)

    if etapa.get("cerrar"):
        db.cerrar_contacto(telefono)
        log.info("[Remarketing] Contacto %s cerrado (día 15).", telefono)


# ---------------------------------------------------------------------------
# Paz y salvos

_PAZ_SALVO_DIAS = [15, 30]


def _procesar_paz_salvos():
    for dias in _PAZ_SALVO_DIAS:
        leads = db.leads_paz_salvo_por_contactar(dias)
        if not leads:
            continue
        log.info("[Remarketing] paz_salvo_dia_%d: %d leads elegibles.", dias, len(leads))
        for lead in leads:
            try:
                telefono = lead["telefono"]
                nombre_raw = (lead.get("nombre") or "").strip()
                nombre_corto = nombre_raw.split()[0] if nombre_raw else ""
                whatsapp.send_paz_salvo_recordatorio(telefono, nombre_corto, dias)
                tipo = f"paz_salvo_dia_{dias}"
                db.registrar_remarketing_envio(telefono, tipo, f"paz_salvo_dia_{dias}")
                log.info("[Remarketing] %s enviado a %s.", tipo, telefono)
            except Exception:
                log.exception("[Remarketing] Error paz_salvo_dia_%d a %s", dias, lead.get("telefono"))
