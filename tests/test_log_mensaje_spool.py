"""Tests para el spool a disco de `db.log_mensaje`.

Durante el piloto 6-oct perdimos 6 filas OUT en la DB porque
`log_mensaje` fallo con `PoolTimeout` y no habia fallback. Estos tests
cubren el nuevo fallback a disco + el drenaje periodico/al arrancar.
"""
import glob
import json
import os
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from psycopg_pool import PoolTimeout

from app import db


@pytest.fixture
def spool_tmp(tmp_path, monkeypatch):
    """Redirige OUT_SPOOL_DIR al tmp del test."""
    monkeypatch.setattr(db, "OUT_SPOOL_DIR", str(tmp_path))
    return tmp_path


@contextmanager
def _conn_ok(executed):
    """Context manager que simula una conn OK: captura las queries."""
    conn = MagicMock()

    def _exec(sql, params=None):
        executed.append((sql, params))
        return MagicMock()

    conn.execute.side_effect = _exec
    yield conn


def _patch_conexion_ok(monkeypatch, executed):
    @contextmanager
    def fake():
        with _conn_ok(executed) as c:
            yield c
    monkeypatch.setattr(db, "_conexion", fake)


def _patch_conexion_pool_timeout(monkeypatch):
    @contextmanager
    def fake():
        raise PoolTimeout("couldn't get a connection after 20.00 sec")
        yield  # pragma: no cover
    monkeypatch.setattr(db, "_conexion", fake)


# ---------- log_mensaje OK vs. fallback ------------------------------------

def test_log_mensaje_ok_no_crea_archivo(spool_tmp, monkeypatch):
    """Si la DB responde OK, log_mensaje NO escribe a disco."""
    executed = []
    _patch_conexion_ok(monkeypatch, executed)

    db.log_mensaje("573001112233", "out", "text", "hola",
                   payload={"status": "sent"})

    # Insert ejecutado.
    assert len(executed) == 1
    assert "INSERT INTO mensajes" in executed[0][0]
    # Nada escrito al spool.
    assert glob.glob(os.path.join(str(spool_tmp), "*.json")) == []


def test_log_mensaje_pool_timeout_cae_a_spool(spool_tmp, monkeypatch):
    """Si _conexion lanza PoolTimeout, log_mensaje serializa a disco."""
    _patch_conexion_pool_timeout(monkeypatch)

    payload = {"status": "sent", "wa_msg_id": "wamid.ABC"}
    db.log_mensaje("573001112233", "out", "text",
                   "mensaje de prueba", payload=payload)

    archivos = glob.glob(os.path.join(str(spool_tmp), "*.json"))
    assert len(archivos) == 1
    with open(archivos[0], "r", encoding="utf-8") as f:
        row = json.load(f)
    assert row["telefono"] == "573001112233"
    assert row["direccion"] == "out"
    assert row["tipo"] == "text"
    assert row["resumen"] == "mensaje de prueba"
    assert row["payload"] == payload
    assert row["motivo"] == "pool_timeout"


def test_log_mensaje_otra_excepcion_tambien_cae_a_spool(spool_tmp, monkeypatch):
    """Cualquier excepcion de la conn debe caer al spool, no solo PoolTimeout."""
    @contextmanager
    def fake():
        raise RuntimeError("SSL error: bad record mac")
        yield  # pragma: no cover
    monkeypatch.setattr(db, "_conexion", fake)

    db.log_mensaje("573001112233", "out", "text", "boom", payload=None)

    archivos = glob.glob(os.path.join(str(spool_tmp), "*.json"))
    assert len(archivos) == 1
    with open(archivos[0], "r", encoding="utf-8") as f:
        row = json.load(f)
    assert row["motivo"].startswith("db_error:")
    assert "RuntimeError" in row["motivo"]


# ---------- drenaje del spool ----------------------------------------------

def _crear_fila_fake(spool_dir, telefono, resumen, ts_ms):
    nombre = f"{ts_ms}-abc12345.json"
    ruta = os.path.join(str(spool_dir), nombre)
    row = {
        "telefono": telefono,
        "direccion": "out",
        "tipo": "text",
        "resumen": resumen,
        "payload": {"status": "sent"},
        "spooled_at_ms": ts_ms,
        "motivo": "pool_timeout",
    }
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(row, f)
    return ruta


def test_drenar_spool_procesa_y_borra(spool_tmp, monkeypatch):
    """Crea 3 archivos fake, mock de conn OK, verifica que se procesaron
    (3 INSERTS) y los archivos ya no estan."""
    r1 = _crear_fila_fake(spool_tmp, "573000000001", "msg1", 1_000_000_000_000)
    r2 = _crear_fila_fake(spool_tmp, "573000000002", "msg2", 1_000_000_000_001)
    r3 = _crear_fila_fake(spool_tmp, "573000000003", "msg3", 1_000_000_000_002)

    executed = []
    _patch_conexion_ok(monkeypatch, executed)

    ok = db._drenar_spool_log_mensaje()

    assert ok == 3
    for r in (r1, r2, r3):
        assert not os.path.exists(r)
    assert len(executed) == 3
    # Teléfonos en el orden FIFO (nombre = timestamp).
    telefonos = [params[0] for _, params in executed]
    assert telefonos == ["573000000001", "573000000002", "573000000003"]


def test_drenar_spool_falla_mantiene_archivo(spool_tmp, monkeypatch):
    """Si la DB sigue cayendo con PoolTimeout, los archivos quedan en el spool
    para reintentos posteriores."""
    r1 = _crear_fila_fake(spool_tmp, "573000000001", "msg1", 1_000_000_000_000)

    _patch_conexion_pool_timeout(monkeypatch)

    ok = db._drenar_spool_log_mensaje()

    assert ok == 0
    assert os.path.exists(r1)


def test_drenar_spool_respeta_batch(spool_tmp, monkeypatch):
    """Con 50 archivos y batch=10, el drenaje procesa 10 y deja 40."""
    monkeypatch.setattr(db, "OUT_SPOOL_DRAIN_BATCH", 10)

    archivos = []
    for i in range(50):
        archivos.append(_crear_fila_fake(spool_tmp, f"5730000{i:05d}",
                                         f"msg{i}", 1_000_000_000_000 + i))

    executed = []
    _patch_conexion_ok(monkeypatch, executed)

    ok = db._drenar_spool_log_mensaje()

    assert ok == 10
    assert len(executed) == 10
    restantes = glob.glob(os.path.join(str(spool_tmp), "*.json"))
    assert len(restantes) == 40
    # Los primeros 10 (los mas viejos) deberian haberse ido primero (FIFO).
    assert not os.path.exists(archivos[0])
    assert not os.path.exists(archivos[9])
    assert os.path.exists(archivos[10])
    assert os.path.exists(archivos[49])


def test_drenar_spool_vacio_devuelve_cero(spool_tmp, monkeypatch):
    """Sin archivos, no toca la DB ni rompe."""
    toco_db = []

    @contextmanager
    def fake():
        toco_db.append(True)
        yield MagicMock()
    monkeypatch.setattr(db, "_conexion", fake)

    assert db._drenar_spool_log_mensaje() == 0
    assert toco_db == []


def test_drenar_spool_archivo_corrupto_se_remueve(spool_tmp, monkeypatch):
    """Un JSON invalido se borra (no bloquea los siguientes)."""
    corrupto = os.path.join(str(spool_tmp), "1234567890000-badbadbd.json")
    with open(corrupto, "w", encoding="utf-8") as f:
        f.write("{not valid json")

    ok_file = _crear_fila_fake(spool_tmp, "573000000001", "msg1", 1_234_567_890_001)

    executed = []
    _patch_conexion_ok(monkeypatch, executed)

    ok = db._drenar_spool_log_mensaje()

    assert ok == 1
    assert not os.path.exists(corrupto)
    assert not os.path.exists(ok_file)
    assert len(executed) == 1
