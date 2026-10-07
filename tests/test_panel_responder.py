"""Tests para `panel.panel_responder`.

Garantiza que, tras enviar un texto manual desde el panel:

- `wa.send_text` se invoca con `log=False` (para que su propio `_log_out`
  no caiga al spool por `pool_timeout` cuando el pool esta saturado).
- El OUT se persiste al instante con `db._conexion_directa` (bypass del
  pool), con el payload marcado como `panel_responder`.
- Fuera de la ventana de 24h de WhatsApp el envio se bloquea.
- Si el log directo falla, no rompemos el envio: el mensaje ya salio.
"""
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from flask import Flask


# IMPORT TARDIO: queremos setear PANEL_TOKEN antes de importar, aunque
# `_check_token` lee del env en runtime, no al importar.
os.environ.setdefault("PANEL_TOKEN", "test-token")

from app import db, panel as panel_mod  # noqa: E402
from app import whatsapp as wa  # noqa: E402


@pytest.fixture
def app():
    app = Flask(__name__)
    app.register_blueprint(panel_mod.panel)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _make_direct_conn(executed, ultimo_in=None):
    """Fake de una conn directa de psycopg: `with db._conexion_directa()` o
    `with db._conexion_directa() as conn` → captura queries. `ultimo_in` es
    lo que devuelve el SELECT MAX(fecha) WHERE direccion='in'."""
    conn = MagicMock()

    def _exec(sql, params=None):
        executed.append((sql, params))
        cur = MagicMock()
        if "SELECT MAX(fecha)" in sql:
            cur.fetchone.return_value = (ultimo_in,)
        else:
            cur.fetchone.return_value = (None,)
        return cur

    conn.execute.side_effect = _exec
    conn.commit = MagicMock()
    conn.close = MagicMock()
    conn.__enter__ = lambda self: self
    conn.__exit__ = lambda self, *a: None
    return conn


def test_panel_responder_mandar_texto_envia_wa_y_log_directo(
    client, monkeypatch
):
    """Camino feliz: dentro de 24h, send_text(..., log=False) + INSERT
    directo con payload marcado como panel_responder."""
    executed = []
    hace_1h = datetime.now(timezone.utc) - timedelta(hours=1)
    # 1a conn: SELECT MAX(fecha). 2a conn: INSERT.
    conns = [
        _make_direct_conn(executed, ultimo_in=hace_1h),
        _make_direct_conn(executed, ultimo_in=hace_1h),
    ]
    conns_iter = iter(conns)
    monkeypatch.setattr(db, "_conexion_directa",
                        lambda timeout=10: next(conns_iter))

    enviados = []

    def fake_send_text(to, body, log=True):
        enviados.append({"to": to, "body": body, "log": log})
        return {"status": 200}

    monkeypatch.setattr(wa, "send_text", fake_send_text)

    resp = client.post(
        "/panel/573001234567/responder?token=test-token",
        data={"texto": "hola, soy Massi"},
    )

    assert resp.status_code == 302, resp.data
    # send_text llamado con log=False — este es el nucleo del fix.
    assert enviados == [
        {"to": "573001234567", "body": "hola, soy Massi", "log": False}
    ]
    # Debe haber un INSERT INTO mensajes con direccion='out' y el marcador
    # panel_responder en el payload.
    inserts = [sql_params for sql_params in executed
               if "INSERT INTO mensajes" in sql_params[0]]
    assert len(inserts) == 1, executed
    sql, params = inserts[0]
    assert "direccion" in sql and "'out'" in sql
    assert params[0] == "573001234567"
    assert params[1] == "hola, soy Massi"
    payload = json.loads(params[2])
    assert payload == {"origen": "panel_responder", "actor": "humano"}


def test_panel_responder_fuera_24h_bloquea_envio(client, monkeypatch):
    """Si el ultimo IN del lead es >= 24h, no se puede enviar texto libre."""
    executed = []
    hace_25h = datetime.now(timezone.utc) - timedelta(hours=25)
    monkeypatch.setattr(db, "_conexion_directa",
                        lambda timeout=10: _make_direct_conn(
                            executed, ultimo_in=hace_25h))

    enviados = []

    def fake_send_text(to, body, log=True):
        enviados.append((to, body, log))

    monkeypatch.setattr(wa, "send_text", fake_send_text)

    resp = client.post(
        "/panel/573001234567/responder?token=test-token",
        data={"texto": "hola"},
    )

    assert resp.status_code == 302
    assert "fuera_24h" in resp.headers.get("Location", "")
    # Ni se intento llamar a WhatsApp, ni se inserto el OUT.
    assert enviados == []
    inserts = [p for p in executed if "INSERT INTO mensajes" in p[0]]
    assert inserts == []


def test_panel_responder_log_directo_falla_no_rompe_envio(
    client, monkeypatch
):
    """El INSERT directo puede fallar (DB caida); el redirect sigue siendo
    'enviado' porque el mensaje ya salio por WhatsApp."""
    executed = []
    hace_1h = datetime.now(timezone.utc) - timedelta(hours=1)
    # Primera conn (ventana 24h): OK. Segunda (INSERT): revienta al abrirla.
    primera = _make_direct_conn(executed, ultimo_in=hace_1h)
    calls = {"n": 0}

    def fake_directa(timeout=10):
        calls["n"] += 1
        if calls["n"] == 1:
            return primera
        raise RuntimeError("boom: db caida")

    monkeypatch.setattr(db, "_conexion_directa", fake_directa)

    enviados = []

    def fake_send_text(to, body, log=True):
        enviados.append((to, body, log))
        return {"status": 200}

    monkeypatch.setattr(wa, "send_text", fake_send_text)

    resp = client.post(
        "/panel/573001234567/responder?token=test-token",
        data={"texto": "igual sale"},
    )

    # El fix debe garantizar que un fallo en el log directo NO rompa el envio.
    assert resp.status_code == 302
    assert "enviado" in resp.headers.get("Location", "")
    # WhatsApp SI recibio el mensaje.
    assert enviados == [("573001234567", "igual sale", False)]


def test_send_text_log_false_no_llama_log_out(monkeypatch):
    """Guard del API: send_text(..., log=False) no debe tocar _log_out,
    independiente del modo DRY_RUN o real."""
    monkeypatch.setattr(wa, "DRY_RUN", True)
    llamadas = []
    monkeypatch.setattr(wa, "_log_out",
                        lambda *a, **k: llamadas.append((a, k)))
    wa.send_text("573000000000", "sin log", log=False)
    assert llamadas == []


def test_send_text_default_sigue_logueando(monkeypatch):
    """Guard de regresion: los demas callers (send_text sin flag) deben
    seguir viendo el _log_out — ese es el camino del bot automatico que
    tiene su propio spool fallback via PR #64."""
    monkeypatch.setattr(wa, "DRY_RUN", True)
    llamadas = []
    monkeypatch.setattr(wa, "_log_out",
                        lambda *a, **k: llamadas.append((a, k)))
    wa.send_text("573000000000", "con log")
    assert len(llamadas) == 1
