"""Observabilidad por paso (Experiencia 3).

Cada evento interesante del bot (handler atendido, query DB, envío WA)
se mide con `@medir_paso("nombre_etapa")` o manualmente con
`marcar_paso(etapa, resultado, tiempo_ms, error)`.

Diseño:
  - 100% fire-and-forget: si la DB falla al loguear, NUNCA bloquea al bot.
  - Un INSERT por evento, la tabla tiene índices en (etapa, ts) y
    (ambiente, ts) para que el dashboard pueda agregar rápido.
  - Ambiente se detecta por env MODO_STAGING=1 vs cualquier otra cosa.
  - Decorator se extrae `telefono` de kwargs o primer arg (convención
    del flujo visitante).

El dashboard /panel/petra/observabilidad lee esta tabla y muestra:
  - % éxito por etapa en última hora / 24h
  - p50 / p95 latencia
  - último error de cada etapa
  - comparación lado a lado prod vs staging
"""
import logging
import os
import time
from functools import wraps

from app import db as _db

log = logging.getLogger(__name__)


def _ambiente():
    return "staging" if (os.environ.get("MODO_STAGING") or "").strip() == "1" else "prod"


def marcar_paso(etapa, resultado, tiempo_ms=None, error=None, telefono=None):
    """Loguea un evento. Nunca lanza excepción — si falla la DB, hace log
    y sigue. El bot no debe caerse por telemetría.

    Args:
        etapa: string corto identificando qué pasó (ej. "visitante_menu",
               "ofrecer_petra_send", "db_select_inmuebles_conjunto").
        resultado: "ok" | "fallo" | "timeout"
        tiempo_ms: duración en milisegundos (opcional)
        error: tipo + mensaje corto si falló (opcional)
        telefono: usuario asociado (opcional, para debug)
    """
    try:
        with _db._conexion_directa(timeout=2) as conn:
            conn.execute(
                "INSERT INTO petra_paso_eventos "
                "(etapa, resultado, tiempo_ms, error, telefono, ambiente) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (etapa[:100], resultado, tiempo_ms,
                 (error or "")[:500] if error else None,
                 (telefono or "")[:100] if telefono else None,
                 _ambiente()))
    except Exception:
        # Fire-and-forget: no bloquear al bot por falla de telemetría
        log.debug("[metricas] marcar_paso fallo — ignorado", exc_info=True)


def medir_paso(etapa, *, telefono_key="telefono", telefono_arg_idx=0):
    """Decorator que mide tiempo + resultado + captura excepción.

    Uso:
        @medir_paso("visitante_handle_message")
        def handle_message(telefono, texto, ...):
            ...

    El decorator extrae el teléfono de kwargs[telefono_key] o del
    arg[telefono_arg_idx]. Si no lo encuentra, loguea None.

    La excepción original SE RE-LANZA después de medir — el decorator
    es transparente para el flujo normal de errores.
    """
    def _deco(fn):
        @wraps(fn)
        def _wrapper(*args, **kwargs):
            t0 = time.time()
            tel = kwargs.get(telefono_key)
            if tel is None and args and len(args) > telefono_arg_idx:
                tel = args[telefono_arg_idx]
            try:
                result = fn(*args, **kwargs)
                ms = int((time.time() - t0) * 1000)
                marcar_paso(etapa, "ok", ms, telefono=tel)
                return result
            except Exception as e:
                ms = int((time.time() - t0) * 1000)
                err = f"{type(e).__name__}: {str(e)[:200]}"
                marcar_paso(etapa, "fallo", ms, error=err, telefono=tel)
                raise
        return _wrapper
    return _deco
