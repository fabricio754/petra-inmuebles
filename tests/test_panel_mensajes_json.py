"""Tests para `panel.panel_mensajes_json`.

Endpoint liviano que el chat del panel pollea cada segundo para
actualizar la conversación en vivo. Verifica:

- Token obligatorio.
- Sin since_id: devuelve los últimos N mensajes.
- Con since_id: solo id > since_id.
- Incluye estado_entrega en el update de chulos.
"""
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from flask import Flask


os.environ.setdefault("PANEL_TOKEN", "test-token")

from app import db, panel as panel_mod  # noqa: E402


@pytest.fixture
def app():
    app = Flask(__name__)
    app.register_blueprint(panel_mod.panel)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _desc(cols):
    class C:
        def __init__(self, n): self.name = n
    return [C(c) for c in cols]


def _make_conn(mensajes_rows, updates_rows, ultimo_in=None):
    """Simula conn directa con dos SELECTs (nuevos + updates) y un
    SELECT MAX(fecha). Soporta since_id y limit en los binds."""
    conn = MagicMock()
    cols_msg = ("id fecha direccion tipo resumen payload wa_msg_id "
                "estado_entrega estado_entrega_at "
                "estado_entrega_error").split()
    cols_upd = ("id estado_entrega estado_entrega_at "
                "estado_entrega_error").split()

    def _exec(sql, params=None):
        cur = MagicMock()
        if "MAX(fecha)" in sql:
            cur.fetchone.return_value = (ultimo_in,)
            cur.description = _desc(["max"])
        elif "SELECT id, fecha, direccion" in sql:
            cur.description = _desc(cols_msg)
            cur.fetchall.return_value = [
                tuple(r.get(c) for c in cols_msg) for r in mensajes_rows
            ]
        elif "SELECT id, estado_entrega" in sql:
            cur.description = _desc(cols_upd)
            cur.fetchall.return_value = [
                tuple(r.get(c) for c in cols_upd) for r in updates_rows
            ]
        else:
            cur.fetchall.return_value = []
            cur.description = None
        return cur

    conn.execute.side_effect = _exec
    conn.close = MagicMock()
    conn.__enter__ = lambda self: self
    conn.__exit__ = lambda self, *a: None
    return conn


def _msg(**over):
    base = dict(
        id=1, fecha=datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc),
        direccion="in", tipo="text", resumen="hola",
        payload=None, wa_msg_id=None,
        estado_entrega=None, estado_entrega_at=None,
        estado_entrega_error=None,
    )
    base.update(over)
    return base


def test_mensajes_json_token_invalido_rechaza(client, monkeypatch):
    monkeypatch.setattr(db, "_conexion_directa",
                        lambda timeout=10: _make_conn([], []))
    r = client.get("/panel/573001234567/mensajes.json?token=mal")
    assert r.status_code == 401


def test_mensajes_json_sin_since_devuelve_recientes(client, monkeypatch):
    rows = [
        _msg(id=10, direccion="in", resumen="hola"),
        _msg(id=11, direccion="out", resumen="hola!",
             estado_entrega="read"),
    ]
    # El endpoint lo pide DESC y lo invierte en Python → la lista real
    # que mockeamos ya está ordenada ASC para que el mock devuelva algo
    # consistente al revés.
    conn = _make_conn(list(reversed(rows)), [])
    monkeypatch.setattr(db, "_conexion_directa", lambda timeout=10: conn)

    r = client.get("/panel/573001234567/mensajes.json?token=test-token")
    assert r.status_code == 200
    data = r.get_json()
    assert "mensajes" in data
    ids = [m["id"] for m in data["mensajes"]]
    # Al revés del mock DESC → ASC: 10, 11.
    assert ids == [10, 11]
    assert data["ultimo_id"] == 11


def test_mensajes_json_con_since_filtra(client, monkeypatch):
    """Con since_id, el SQL pide WHERE id > since_id. Verificamos que el
    bind llega y que el response trae solo lo nuevo."""
    captured = {"params": None, "sql": None}
    cols_msg = ("id fecha direccion tipo resumen payload wa_msg_id "
                "estado_entrega estado_entrega_at "
                "estado_entrega_error").split()
    rows = [_msg(id=42, resumen="nuevo")]
    conn = MagicMock()

    def _exec(sql, params=None):
        cur = MagicMock()
        if "MAX(fecha)" in sql:
            cur.fetchone.return_value = (None,)
            cur.description = _desc(["max"])
        elif "SELECT id, fecha, direccion" in sql:
            captured["sql"] = sql
            captured["params"] = params
            cur.description = _desc(cols_msg)
            cur.fetchall.return_value = [
                tuple(r.get(c) for c in cols_msg) for r in rows
            ]
        else:
            cur.description = _desc(["id", "estado_entrega",
                                     "estado_entrega_at",
                                     "estado_entrega_error"])
            cur.fetchall.return_value = []
        return cur

    conn.execute.side_effect = _exec
    conn.__enter__ = lambda self: self
    conn.__exit__ = lambda self, *a: None
    conn.close = MagicMock()
    monkeypatch.setattr(db, "_conexion_directa", lambda timeout=10: conn)

    r = client.get("/panel/573001234567/mensajes.json?"
                   "token=test-token&since_id=40")
    assert r.status_code == 200
    data = r.get_json()
    assert captured["sql"] is not None
    assert "id > %s" in captured["sql"]
    # params: (telefono, since_id)
    assert captured["params"][1] == 40
    ids = [m["id"] for m in data["mensajes"]]
    assert ids == [42]
    assert data["ultimo_id"] == 42


def test_mensajes_json_incluye_estado_entrega(client, monkeypatch):
    """El bloque `updates` trae estado_entrega para los OUT recientes,
    así el frontend puede refrescar chulos sin recargar."""
    updates = [
        _msg(id=5, direccion="out", estado_entrega="read",
             estado_entrega_at=datetime(2026, 10, 7, 12, 0,
                                        tzinfo=timezone.utc)),
        _msg(id=6, direccion="out", estado_entrega="failed",
             estado_entrega_error="recipient_blocked"),
    ]
    conn = _make_conn([], updates)
    monkeypatch.setattr(db, "_conexion_directa", lambda timeout=10: conn)

    r = client.get("/panel/573001234567/mensajes.json?token=test-token")
    assert r.status_code == 200
    data = r.get_json()
    assert "updates" in data
    por_id = {u["id"]: u for u in data["updates"]}
    assert por_id[5]["estado_entrega"] == "read"
    assert por_id[5]["estado_entrega_at"].startswith("2026-10-07T12:00")
    assert por_id[6]["estado_entrega"] == "failed"
    assert por_id[6]["estado_entrega_error"] == "recipient_blocked"


def test_mensajes_json_db_caida_devuelve_503(client, monkeypatch):
    """Si _conexion_directa revienta (DB saturada), devolvemos 503 con
    error machine-readable para que el frontend haga backoff."""
    def boom(timeout=10):
        raise RuntimeError("pool/pg caido")
    monkeypatch.setattr(db, "_conexion_directa", boom)

    r = client.get("/panel/573001234567/mensajes.json?token=test-token")
    assert r.status_code == 503
    data = r.get_json()
    assert data == {"error": "db_busy"}
