"""Deduplicación de mensajes entrantes de WhatsApp.

Meta puede reenviar el mismo webhook si no responde 200 lo suficientemente
rápido, o si hay reintentos. Para evitar que Petra conteste dos veces,
guardamos cada wa_msg_id visto y rechazamos duplicados.

Tabla:
    CREATE TABLE petra_mensajes_vistos (
        wa_msg_id TEXT PRIMARY KEY,
        telefono TEXT,
        visto_at TIMESTAMPTZ DEFAULT NOW()
    );

Se limpia automáticamente los más viejos de 24h con un cron (TODO).
"""
import logging
log = logging.getLogger("petra")


def ya_visto(wa_msg_id, telefono=None):
    """Devuelve True si ya se procesó este wamid. Si es nuevo, lo marca
    como visto y devuelve False. Idempotencia a nivel DB (PK ON CONFLICT)."""
    if not wa_msg_id:
        return False  # sin id, no deduplicamos (fallback compatible)
    from app import db as _db
    try:
        with _db._conexion_directa(timeout=3) as conn:
            r = conn.execute(
                "INSERT INTO petra_mensajes_vistos (wa_msg_id, telefono) "
                "VALUES (%s, %s) ON CONFLICT (wa_msg_id) DO NOTHING",
                (wa_msg_id, telefono),
            )
            # rowcount=0 significa que ya existía → es duplicado
            return r.rowcount == 0
    except Exception:
        log.exception("[dedup] fallo tel=%s wamid=%s", telefono, wa_msg_id)
        return False
