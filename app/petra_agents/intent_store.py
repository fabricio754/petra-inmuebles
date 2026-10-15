"""Intent activo del contacto: a qué sub-agente estamos ruteando ahora.

Cuando el router hace handoff a un sub-agente, lo guarda acá para que
los próximos mensajes vayan directo al sub-agente sin volver a clasificar
(ahorra latencia + evita reclasificaciones inconsistentes).

El intent expira tras VENTANA_INTENT_MIN minutos de inactividad.
Un sub-agente puede llamar `cerrar()` cuando termina el flujo (ej: la
agente ya completó capacitación), y entonces el próximo mensaje vuelve
al router para re-clasificar.

Schema:
    CREATE TABLE petra_intent_activo (
        telefono TEXT PRIMARY KEY,
        agente   TEXT NOT NULL,
        razon    TEXT,
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
"""
import logging
import os

log = logging.getLogger("petra")

VENTANA_INTENT_MIN = int(os.environ.get("PETRA_INTENT_VENTANA_MIN", "30"))


def leer(telefono):
    """Devuelve dict con {agente, razon} si hay intent activo no expirado,
    o None. Usa updated_at + VENTANA_INTENT_MIN para expiración."""
    from app import db as _db
    try:
        with _db._conexion_directa(timeout=5) as conn:
            row = conn.execute(
                "SELECT agente, razon FROM petra_intent_activo "
                "WHERE telefono=%s "
                "AND updated_at > NOW() - make_interval(mins => %s)",
                (telefono, VENTANA_INTENT_MIN),
            ).fetchone()
            if row:
                return {"agente": row[0], "razon": row[1]}
    except Exception:
        log.exception("[intent_store] leer fallo tel=%s", telefono)
    return None


def set_intent(telefono, agente, razon=None):
    from app import db as _db
    try:
        with _db._conexion_directa(timeout=5) as conn:
            conn.execute(
                "INSERT INTO petra_intent_activo (telefono, agente, razon) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (telefono) DO UPDATE SET "
                "agente=EXCLUDED.agente, razon=EXCLUDED.razon, "
                "updated_at=NOW()",
                (telefono, agente, razon),
            )
    except Exception:
        log.exception("[intent_store] set fallo tel=%s agente=%s",
                      telefono, agente)


def tocar(telefono):
    """Refresca updated_at. Llamar en cada mensaje para extender la ventana."""
    from app import db as _db
    try:
        with _db._conexion_directa(timeout=5) as conn:
            conn.execute(
                "UPDATE petra_intent_activo SET updated_at=NOW() "
                "WHERE telefono=%s",
                (telefono,),
            )
    except Exception:
        pass


def cerrar(telefono):
    """Elimina el intent activo (próximo mensaje va al router)."""
    from app import db as _db
    try:
        with _db._conexion_directa(timeout=5) as conn:
            conn.execute(
                "DELETE FROM petra_intent_activo WHERE telefono=%s",
                (telefono,),
            )
    except Exception:
        log.exception("[intent_store] cerrar fallo tel=%s", telefono)
