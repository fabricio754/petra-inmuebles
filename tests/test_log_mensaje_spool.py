"""Tests para `db.log_mensaje`: fast path + spool a disco.

Historia:
- Piloto 6-oct: `log_mensaje` fallo con `PoolTimeout` → perdimos 6 filas OUT.
  Se agrego spool a disco (PR #64).
- Oct 2026: con el pool saturado, el pool esperaba 20s antes de caer al
  spool, y el spool se drenaba cada 10 min → los mensajes IN/OUT aparecian
  tarde en el chat del panel. Se agrego fast path con 3 niveles:
    1. pool con timeout 3s
    2. _conexion_directa (bypass del pool)
    3. spool a disco (fallback final)
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
    """Patchea `_conexion` (usado por el drain) para devolver una conn OK."""
    @contextmanager
    def fake():
        with _conn_ok(executed) as c:
            yield c
    monkeypatch.setattr(db, "_conexion", fake)


def _patch_conexion_pool_timeout(monkeypatch):
    """Patchea `_conexion` (drain) para que simule pool saturado."""
    @contextmanager
    def fake():
        raise PoolTimeout("couldn't get a connection after 20.00 sec")
        yield  # pragma: no cover
    monkeypatch.setattr(db, "_conexion", fake)


def _patch_pool_conexion_ok(monkeypatch, executed):
    """Nivel 1 OK: `_pool_conexion` devuelve conn que captura queries."""
    @contextmanager
    def fake(timeout=3.0):
        with _conn_ok(executed) as c:
            yield c
    monkeypatch.setattr(db, "_pool_conexion", fake)


def _patch_pool_conexion_timeout(monkeypatch):
    """Nivel 1 roto: `_pool_conexion` lanza PoolTimeout."""
    @contextmanager
    def fake(timeout=3.0):
        raise PoolTimeout("couldn't get a connection after 3.00 sec")
        yield  # pragma: no cover
    monkeypatch.setattr(db, "_pool_conexion", fake)


def _patch_pool_conexion_error(monkeypatch, exc):
    """Nivel 1 roto con excepcion arbitraria (ej. SSL)."""
    @contextmanager
    def fake(timeout=3.0):
        raise exc
        yield  # pragma: no cover
    monkeypatch.setattr(db, "_pool_conexion", fake)


def _patch_conexion_directa_ok(monkeypatch, executed):
    """Nivel 2 OK: `_conexion_directa` devuelve conn que captura queries."""
    def fake(timeout=5):
        conn = MagicMock()

        def _exec(sql, params=None):
            executed.append((sql, params))
            return MagicMock()

        conn.execute.side_effect = _exec
        # psycopg.connect devuelve una conn con context-manager de
        # transaccion; emulamos __enter__/__exit__.
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        return conn
    monkeypatch.setattr(db, "_conexion_directa", fake)


def _patch_conexion_directa_error(monkeypatch, exc):
    """Nivel 2 roto: `_conexion_directa` lanza excepcion al abrir."""
    def fake(timeout=5):
        raise exc
    monkeypatch.setattr(db, "_conexion_directa", fake)


# ---------- log_mensaje: fast path (3 niveles) -----------------------------

def test_log_mensaje_usa_pool_cuando_disponible(spool_tmp, monkeypatch):
    """Nivel 1 OK: escribe por el pool, NO toca `_conexion_directa` ni spool."""
    pool_executed = []
    _patch_pool_conexion_ok(monkeypatch, pool_executed)

    # Si el fast path toca `_conexion_directa` el test falla.
    directa_llamada = []

    def directa_boom(timeout=5):
        directa_llamada.append(True)
        raise AssertionError("no deberia llegar a _conexion_directa")
    monkeypatch.setattr(db, "_conexion_directa", directa_boom)

    db.log_mensaje("573001112233", "out", "text", "hola",
                   payload={"status": "sent"})

    assert len(pool_executed) == 1
    assert "INSERT INTO mensajes" in pool_executed[0][0]
    assert directa_llamada == []
    assert glob.glob(os.path.join(str(spool_tmp), "*.json")) == []


def test_log_mensaje_cae_a_directa_si_pool_timeout(spool_tmp, monkeypatch):
    """Nivel 1 PoolTimeout → nivel 2 OK: escribe por directa, NO va al spool."""
    _patch_pool_conexion_timeout(monkeypatch)
    directa_executed = []
    _patch_conexion_directa_ok(monkeypatch, directa_executed)

    db.log_mensaje("573001112233", "in", "text", "hola panel",
                   payload={"from": "573001112233"})

    assert len(directa_executed) == 1
    assert "INSERT INTO mensajes" in directa_executed[0][0]
    # Spool vacio: la directa persistio al instante.
    assert glob.glob(os.path.join(str(spool_tmp), "*.json")) == []


def test_log_mensaje_cae_a_directa_si_pool_otra_excepcion(spool_tmp, monkeypatch):
    """Nivel 1 excepcion arbitraria (SSL) → nivel 2 OK: directa escribe,
    no cae al spool. Cualquier error del pool, no solo PoolTimeout."""
    _patch_pool_conexion_error(monkeypatch, RuntimeError("SSL: bad record mac"))
    directa_executed = []
    _patch_conexion_directa_ok(monkeypatch, directa_executed)

    db.log_mensaje("573001112233", "out", "text", "boom", payload=None)

    assert len(directa_executed) == 1
    assert glob.glob(os.path.join(str(spool_tmp), "*.json")) == []


def test_log_mensaje_cae_a_spool_si_pool_y_directa_fallan(spool_tmp, monkeypatch):
    """Nivel 1 PoolTimeout + nivel 2 falla → nivel 3: spool a disco."""
    _patch_pool_conexion_timeout(monkeypatch)
    _patch_conexion_directa_error(monkeypatch, RuntimeError("conn refused"))

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
    assert row["motivo"] == "pool_y_directa_fallaron"


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
