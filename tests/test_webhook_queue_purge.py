"""Tests para `scheduler._purgar_webhook_queue` y su env var de retención.

Historia:
- Piloto 6-oct: se perdió la data raw de `webhook_queue` anterior a las
  15:30 UTC (causa no confirmada; posiblemente mantenimiento Render o
  auto-vacuum). No había purga explícita a nivel aplicación, así que se
  agregó un job que corre 04:17 Bogotá con retención configurable via
  WEBHOOK_QUEUE_RETENCION_DIAS (default 90).
"""
import importlib
from contextlib import contextmanager
from unittest.mock import MagicMock


def _reload_scheduler_with_env(monkeypatch, env_value):
    """Reimporta `app.scheduler` con un valor fijo de la env var para que
    `WEBHOOK_QUEUE_RETENCION_DIAS` se recalcule desde cero."""
    if env_value is None:
        monkeypatch.delenv("WEBHOOK_QUEUE_RETENCION_DIAS", raising=False)
    else:
        monkeypatch.setenv("WEBHOOK_QUEUE_RETENCION_DIAS", env_value)
    import app.scheduler as sched
    importlib.reload(sched)
    return sched


def _patch_conexion(monkeypatch, sched, executed, rowcount=0):
    """Patchea `db._conexion` para capturar la query ejecutada por la purga."""
    from app import db

    conn = MagicMock()

    def _exec(sql, params=None):
        executed.append((sql, params))
        result = MagicMock()
        result.rowcount = rowcount
        return result

    conn.execute.side_effect = _exec

    @contextmanager
    def fake():
        yield conn

    monkeypatch.setattr(db, "_conexion", fake)


def test_retencion_default_es_90_dias(monkeypatch):
    sched = _reload_scheduler_with_env(monkeypatch, None)
    assert sched.WEBHOOK_QUEUE_RETENCION_DIAS == 90


def test_retencion_respeta_env_var(monkeypatch):
    sched = _reload_scheduler_with_env(monkeypatch, "30")
    assert sched.WEBHOOK_QUEUE_RETENCION_DIAS == 30


def test_retencion_env_var_invalida_cae_a_90(monkeypatch):
    sched = _reload_scheduler_with_env(monkeypatch, "no-es-numero")
    assert sched.WEBHOOK_QUEUE_RETENCION_DIAS == 90


def test_retencion_env_var_cero_sube_a_minimo_1(monkeypatch):
    """Clamp defensivo: evitar que un '0' por error borre todo."""
    sched = _reload_scheduler_with_env(monkeypatch, "0")
    assert sched.WEBHOOK_QUEUE_RETENCION_DIAS == 1


def test_purga_usa_valor_de_env_var(monkeypatch):
    sched = _reload_scheduler_with_env(monkeypatch, "45")
    executed = []
    _patch_conexion(monkeypatch, sched, executed, rowcount=3)

    sched._purgar_webhook_queue()

    assert len(executed) == 1
    sql, params = executed[0]
    assert "DELETE FROM webhook_queue" in sql
    # La query debe pasar el valor de retención como parámetro, no como literal.
    assert params == (45,)


def test_purga_solo_borra_filas_terminadas(monkeypatch):
    """Pendientes/procesando NUNCA se tocan — pueden ser webhooks huérfanos
    que todavía deban drenarse al reiniciar el servicio."""
    sched = _reload_scheduler_with_env(monkeypatch, "90")
    executed = []
    _patch_conexion(monkeypatch, sched, executed, rowcount=0)

    sched._purgar_webhook_queue()

    sql, _ = executed[0]
    assert "estado IN ('ok', 'error')" in sql
    assert "pending" not in sql
    assert "procesando" not in sql


def test_purga_usa_intervalo_parametrico_no_string_interpolation(monkeypatch):
    """Protección contra SQL injection: el intervalo se arma con
    `make_interval(days => %s)`, nunca por f-string."""
    sched = _reload_scheduler_with_env(monkeypatch, "90")
    executed = []
    _patch_conexion(monkeypatch, sched, executed, rowcount=0)

    sched._purgar_webhook_queue()

    sql, _ = executed[0]
    assert "make_interval(days => %s)" in sql
    # Ningún día debe quedar interpolado en el SQL literal.
    assert "90 days" not in sql
    assert "'90'" not in sql


def test_purga_absorbe_excepciones(monkeypatch, caplog):
    """Si la DB falla, el job no debe propagar — rompería el scheduler."""
    sched = _reload_scheduler_with_env(monkeypatch, "90")
    from app import db

    @contextmanager
    def fake_broken():
        raise RuntimeError("DB caida")
        yield  # pragma: no cover

    monkeypatch.setattr(db, "_conexion", fake_broken)

    # No debe levantar.
    sched._purgar_webhook_queue()
