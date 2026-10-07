"""Tests para `db.actualizar_estado_entrega` + propagacion de wa_msg_id.

El webhook de Meta envia statuses (sent/delivered/read/failed) para cada
OUT. Correlacionamos con la fila OUT por `wa_msg_id` y actualizamos de
forma monotonica:
  - sent -> delivered -> read: solo "sube".
  - cualquier estado -> failed: siempre pisa.
  - failed NO baja a sent/delivered/read.
"""
import glob
import json
import os
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from app import db


# ---------- Fixtures ---------------------------------------------------------

@pytest.fixture
def spool_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "OUT_SPOOL_DIR", str(tmp_path))
    return tmp_path


def _patch_conexion_directa(monkeypatch, executed):
    """Patchea `db._conexion_directa` para capturar los UPDATE emitidos."""
    def fake(timeout=5):
        conn = MagicMock()

        def _exec(sql, params=None):
            executed.append((sql, params))
            return MagicMock()

        conn.execute.side_effect = _exec
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        return conn
    monkeypatch.setattr(db, "_conexion_directa", fake)


# ---------- actualizar_estado_entrega ---------------------------------------

def test_actualizar_estado_sent_a_delivered_sube(monkeypatch):
    """El UPDATE debe emitirse: la clausula WHERE delega el "sube" al SQL."""
    executed = []
    _patch_conexion_directa(monkeypatch, executed)

    db.actualizar_estado_entrega("wamid.ABC", "delivered", 1760000000)

    assert len(executed) == 1
    sql, params = executed[0]
    assert "UPDATE mensajes" in sql
    assert "estado_entrega" in sql
    assert "wa_msg_id = %s" in sql
    # params = (estado, timestamp, error_text, wa_msg_id, estado_check, rank)
    assert params[0] == "delivered"
    assert params[1] == 1760000000
    assert params[2] is None
    assert params[3] == "wamid.ABC"
    # El rank numerico de 'delivered' es 2 — se compara contra el estado actual.
    assert params[5] == 2


def test_actualizar_estado_delivered_a_sent_no_baja(monkeypatch):
    """Simula que llega 'sent' despues de que ya estaba 'delivered'.
    El UPDATE sale igual, pero la clausula WHERE del SQL se encarga de que
    no afecte la fila (la logica monotonica vive en el SQL). Verificamos
    que el UPDATE lleva el rank correcto (1 para 'sent'), para que el
    CASE del WHERE descarte la actualizacion."""
    executed = []
    _patch_conexion_directa(monkeypatch, executed)

    db.actualizar_estado_entrega("wamid.XYZ", "sent", 1760000000)

    assert len(executed) == 1
    sql, params = executed[0]
    # El rank numerico de 'sent' es 1. El WHERE pide
    # "nuevo_rank > rank_actual_de_la_columna", asi que un rank 1 contra
    # delivered(2) o read(3) no actualiza nada.
    assert params[0] == "sent"
    assert params[5] == 1
    # Y el WHERE debe excluir filas en failed (si estado_entrega='failed'
    # y el nuevo es sent/delivered/read, no toca).
    assert "estado_entrega <> 'failed'" in sql


def test_actualizar_estado_failed_siempre_pisa(monkeypatch):
    """Cualquier estado -> failed. El SQL tiene un OR especifico para failed."""
    executed = []
    _patch_conexion_directa(monkeypatch, executed)

    db.actualizar_estado_entrega("wamid.FAIL", "failed", 1760000000,
                                 error_text="Message undeliverable: fuera de 24h")

    assert len(executed) == 1
    sql, params = executed[0]
    assert "%s = 'failed'" in sql
    assert params[0] == "failed"
    assert params[2] == "Message undeliverable: fuera de 24h"
    assert params[5] == 99


def test_actualizar_estado_wa_msg_id_desconocido_no_falla(monkeypatch):
    """Si el wa_msg_id no esta en la DB, el UPDATE no afecta filas pero
    la funcion no lanza. Simulamos con una conn OK que no retorna error."""
    executed = []
    _patch_conexion_directa(monkeypatch, executed)

    # No lanza aunque el UPDATE no vaya a afectar filas (eso es cosa del
    # driver; aca comprobamos que la logica Python no lanza).
    db.actualizar_estado_entrega("wamid.DESCONOCIDO", "delivered", 1760000000)
    assert len(executed) == 1


def test_actualizar_estado_sin_wa_msg_id_no_hace_nada(monkeypatch):
    """wa_msg_id vacio/None: no debe tocar la DB."""
    directa_llamada = []

    def fake(timeout=5):
        directa_llamada.append(True)
        raise AssertionError("no deberia abrir conn directa")
    monkeypatch.setattr(db, "_conexion_directa", fake)

    db.actualizar_estado_entrega(None, "delivered", 1760000000)
    db.actualizar_estado_entrega("", "delivered", 1760000000)
    db.actualizar_estado_entrega("wamid.X", None, 1760000000)

    assert directa_llamada == []


def test_actualizar_estado_db_error_no_propaga(monkeypatch):
    """Si la DB falla al abrir la conn, la funcion traga el error."""
    def fake(timeout=5):
        raise RuntimeError("db caida")
    monkeypatch.setattr(db, "_conexion_directa", fake)

    # No debe lanzar.
    db.actualizar_estado_entrega("wamid.ABC", "delivered", 1760000000)


# ---------- log_mensaje guarda wa_msg_id ------------------------------------

@contextmanager
def _conn_ok(executed):
    conn = MagicMock()

    def _exec(sql, params=None):
        executed.append((sql, params))
        return MagicMock()

    conn.execute.side_effect = _exec
    yield conn


def test_log_mensaje_guarda_wa_msg_id(spool_tmp, monkeypatch):
    """El INSERT del fast path nivel 1 incluye la columna wa_msg_id como
    sexto parametro."""
    pool_executed = []

    @contextmanager
    def fake(timeout=3.0):
        with _conn_ok(pool_executed) as c:
            yield c
    monkeypatch.setattr(db, "_pool_conexion", fake)

    db.log_mensaje("573001112233", "out", "text", "hola",
                   payload={"status": 200},
                   wa_msg_id="wamid.ABCDEF")

    assert len(pool_executed) == 1
    sql, params = pool_executed[0]
    assert "INSERT INTO mensajes" in sql
    assert "wa_msg_id" in sql
    # params = (telefono, direccion, tipo, resumen, payload_json, wa_msg_id)
    assert params[0] == "573001112233"
    assert params[1] == "out"
    assert params[5] == "wamid.ABCDEF"


def test_log_mensaje_spool_guarda_wa_msg_id(spool_tmp, monkeypatch):
    """Si cae al spool, el JSON de disco incluye wa_msg_id para que el
    drain lo use en el INSERT eventual."""
    from psycopg_pool import PoolTimeout

    @contextmanager
    def pool_timeout(timeout=3.0):
        raise PoolTimeout("x")
        yield  # pragma: no cover
    monkeypatch.setattr(db, "_pool_conexion", pool_timeout)

    def directa_boom(timeout=5):
        raise RuntimeError("conn refused")
    monkeypatch.setattr(db, "_conexion_directa", directa_boom)

    db.log_mensaje("573001112233", "out", "text", "al spool",
                   payload={"x": 1}, wa_msg_id="wamid.SPOOLED")

    archivos = glob.glob(os.path.join(str(spool_tmp), "*.json"))
    assert len(archivos) == 1
    with open(archivos[0], "r", encoding="utf-8") as f:
        row = json.load(f)
    assert row["wa_msg_id"] == "wamid.SPOOLED"


def test_log_mensaje_drain_spool_insert_lleva_wa_msg_id(spool_tmp, monkeypatch):
    """El drain lee el JSON del spool y debe INSERTar wa_msg_id tambien."""
    # Fila fake con wa_msg_id guardado.
    ruta = os.path.join(str(spool_tmp), "1700000000000-abcd1234.json")
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump({
            "telefono": "573001112233",
            "direccion": "out",
            "tipo": "text",
            "resumen": "hola",
            "payload": {"x": 1},
            "wa_msg_id": "wamid.DRAINED",
            "spooled_at_ms": 1700000000000,
            "motivo": "pool_y_directa_fallaron",
        }, f)

    executed = []

    @contextmanager
    def fake():
        with _conn_ok(executed) as c:
            yield c
    monkeypatch.setattr(db, "_conexion", fake)

    ok = db._drenar_spool_log_mensaje()
    assert ok == 1
    assert len(executed) == 1
    sql, params = executed[0]
    assert "wa_msg_id" in sql
    assert params[-1] == "wamid.DRAINED"
    assert not os.path.exists(ruta)
