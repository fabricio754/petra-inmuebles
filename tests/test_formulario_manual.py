"""Tests para la vista y guardado del formulario manual del panel.

Cubre:

- GET renderiza y pre-llena desde la fila de `contactos`.
- POST valida avaluo, cedula, email, nombre y autorización verbal.
- POST feliz: llama a `state.save_pipeline` con el shape que escribe
  `bot._guardar`, llama a `db.aplicar_accion_panel` con la acción
  `completar_formulario_manual`, cambia el bucket a enviar_a_sureti y
  requiere_humano=false, e inserta un mensaje sintético
  `flow_reply_manual` para que quede en el chat.
"""
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from flask import Flask


# IMPORT TARDIO: setear PANEL_TOKEN antes de importar panel (el check_token
# lo lee del env en runtime pero es más defensivo así).
os.environ.setdefault("PANEL_TOKEN", "test-token")

from app import db, panel as panel_mod, state  # noqa: E402


@pytest.fixture
def app():
    app = Flask(__name__,
                template_folder=os.path.join(
                    os.path.dirname(panel_mod.__file__), "templates"))
    app.register_blueprint(panel_mod.panel)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _make_conn(filas_contacto=None):
    """Fake de conn directa de psycopg: `with` + conn.execute(...) devuelve
    un cursor con .fetchall()/.fetchone()/.description configurado.

    `filas_contacto` es la lista de tuplas que el SELECT * FROM contactos
    debe devolver. Si es None, devuelve lista vacía.
    """
    conn = MagicMock()

    def _exec(sql, params=None):
        cur = MagicMock()
        if "FROM contactos" in sql and "SELECT *" in sql:
            cols = [
                "telefono", "nombre", "ciudad", "barrio", "tipo_inmueble",
                "precio_publicado", "monto_hasta_millones",
                "resultado_contacto", "no_contactar",
            ]
            # columnas de psycopg3: lista con .name
            cur.description = [MagicMock(name=c) for c in cols]
            # El MagicMock por default usa `name=` como repr, no para la
            # propiedad `.name`. Lo seteamos explícito.
            for mock, col in zip(cur.description, cols):
                mock.name = col
            cur.fetchall.return_value = filas_contacto or []
        else:
            cur.description = []
            cur.fetchall.return_value = []
            cur.fetchone.return_value = (None,)
        return cur

    conn.execute.side_effect = _exec
    conn.commit = MagicMock()
    conn.close = MagicMock()
    conn.__enter__ = lambda self: self
    conn.__exit__ = lambda self, *a: None
    return conn


def _datos_feliz(**over):
    """POST form-encoded feliz: todos los campos obligatorios + algunos opcionales."""
    base = {
        "actor": "Massi",
        "nota": "lead por teléfono",
        # Flow 1
        "direccion": "Cra 45 # 80-20",
        "apto": "Apto 302",
        "barrio": "Chapinero",
        "ciudad": "BOGOTA",
        "estrato": "4",
        "es_ph": "SI",
        "hipoteca": "NO",
        "patrimonio": "NO",
        "edad": "NO",
        "paz_salvo": "SI",
        "avaluo": "350",
        # Flow 2
        "tipo_persona": "NATURAL",
        "nombre": "Pedro Pérez",
        "cedula": "1020304050",
        "edad_propietario": "45",
        "email": "pedro@example.com",
        "objetivo": "OBJ_CAPITAL",
        # Consent obligatorio.
        "autorizacion_verbal": "1",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Vista GET
# ---------------------------------------------------------------------------

def test_vista_renderiza_con_datos_prellenados(client, monkeypatch):
    fila_contacto = (
        "573001234567", "Pedro Pérez", "Bogotá", "Chapinero",
        "apartamento", 500_000_000, 350,
        None, False,
    )
    monkeypatch.setattr(db, "_conexion_directa",
                        lambda timeout=10: _make_conn([fila_contacto]))
    resp = client.get(
        "/panel/573001234567/formulario-manual?token=test-token")
    assert resp.status_code == 200, resp.data
    html = resp.data.decode("utf-8")
    # Teléfono y nombre pre-llenado del contacto deben verse.
    assert "573001234567" in html
    assert "Pedro Pérez" in html
    assert "Chapinero" in html  # barrio pre-llenado
    assert "Bogotá" in html     # ciudad del scraping
    # El checkbox de autorización verbal debe estar presente y marcado como required.
    assert 'name="autorizacion_verbal"' in html
    assert "Ley 1581" in html
    # Botones.
    assert "Guardar formulario" in html
    assert "Cancelar" in html


def test_vista_sin_token_devuelve_401(client):
    resp = client.get("/panel/573001234567/formulario-manual")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST — validaciones
# ---------------------------------------------------------------------------

def _mock_persistencia(monkeypatch, filas_contacto=None):
    """Monkeypatch de state.save_pipeline + db.aplicar_accion_panel + conn
    directa. Devuelve el dict con las capturas."""
    cap = {"pipeline": [], "accion": [], "mensajes_insert": []}

    def _save_pipeline(data):
        cap["pipeline"].append(dict(data))

    def _aplicar(telefono, updates, accion_key, nota, actor):
        cap["accion"].append({
            "telefono": telefono,
            "updates": dict(updates),
            "accion_key": accion_key,
            "nota": nota,
            "actor": actor,
        })

    monkeypatch.setattr(state, "save_pipeline", _save_pipeline)
    monkeypatch.setattr(db, "aplicar_accion_panel", _aplicar)

    def _directa(timeout=10):
        c = _make_conn(filas_contacto)
        original_side = c.execute.side_effect

        def _spy(sql, params=None):
            if "INSERT INTO mensajes" in sql:
                cap["mensajes_insert"].append((sql, params))
            return original_side(sql, params)

        c.execute.side_effect = _spy
        return c

    monkeypatch.setattr(db, "_conexion_directa", _directa)
    return cap


def test_guardar_sin_avaluo_falla(client, monkeypatch):
    cap = _mock_persistencia(monkeypatch)
    data = _datos_feliz(avaluo="")
    resp = client.post(
        "/panel/573001234567/formulario-manual?token=test-token", data=data)
    assert resp.status_code == 302
    assert "error_campos" in resp.headers.get("Location", "")
    # Nada se persistió.
    assert cap["pipeline"] == []
    assert cap["accion"] == []
    assert cap["mensajes_insert"] == []


def test_guardar_sin_cedula_falla(client, monkeypatch):
    cap = _mock_persistencia(monkeypatch)
    data = _datos_feliz(cedula="")
    resp = client.post(
        "/panel/573001234567/formulario-manual?token=test-token", data=data)
    assert resp.status_code == 302
    assert "error_campos" in resp.headers.get("Location", "")
    assert cap["pipeline"] == []
    assert cap["accion"] == []


def test_guardar_sin_email_falla(client, monkeypatch):
    cap = _mock_persistencia(monkeypatch)
    data = _datos_feliz(email="")
    resp = client.post(
        "/panel/573001234567/formulario-manual?token=test-token", data=data)
    assert resp.status_code == 302
    assert "error_campos" in resp.headers.get("Location", "")
    assert cap["pipeline"] == []


def test_guardar_sin_nombre_falla(client, monkeypatch):
    cap = _mock_persistencia(monkeypatch)
    data = _datos_feliz(nombre="")
    resp = client.post(
        "/panel/573001234567/formulario-manual?token=test-token", data=data)
    assert resp.status_code == 302
    assert "error_campos" in resp.headers.get("Location", "")
    assert cap["pipeline"] == []


def test_guardar_sin_autorizacion_verbal_falla(client, monkeypatch):
    """Sin el consent explícito del asesor no se guarda nada. Ley 1581."""
    cap = _mock_persistencia(monkeypatch)
    data = _datos_feliz()
    del data["autorizacion_verbal"]
    resp = client.post(
        "/panel/573001234567/formulario-manual?token=test-token", data=data)
    assert resp.status_code == 302
    assert "error_campos" in resp.headers.get("Location", "")
    assert cap["pipeline"] == []
    assert cap["accion"] == []
    assert cap["mensajes_insert"] == []


def test_guardar_email_invalido_falla(client, monkeypatch):
    """Email que no parece correo → error, sin tocar DB."""
    cap = _mock_persistencia(monkeypatch)
    data = _datos_feliz(email="no es un correo")
    resp = client.post(
        "/panel/573001234567/formulario-manual?token=test-token", data=data)
    assert resp.status_code == 302
    assert "error_campos" in resp.headers.get("Location", "")
    assert cap["pipeline"] == []


# ---------------------------------------------------------------------------
# POST — camino feliz
# ---------------------------------------------------------------------------

def test_guardar_feliz_escribe_pipeline_y_cambia_bucket(client, monkeypatch):
    cap = _mock_persistencia(monkeypatch)
    resp = client.post(
        "/panel/573001234567/formulario-manual?token=test-token",
        data=_datos_feliz())
    assert resp.status_code == 302
    assert "guardado" in resp.headers.get("Location", "")

    # Pipeline escrito con el shape del bot (_guardar).
    assert len(cap["pipeline"]) == 1
    p = cap["pipeline"][0]
    assert p["telefono"] == "573001234567"
    assert p["nombre"] == "Pedro Pérez"
    assert p["cedula"] == "1020304050"
    assert p["email"] == "pedro@example.com"
    assert p["ciudad"] == "Bogotá"            # id BOGOTA → "Bogotá"
    assert p["estrato"] == 4
    assert p["es_ph"] is True
    assert p["requiere_paz_salvo"] is True
    assert p["avaluo_comercial"] == 350_000_000  # avaluo en millones → pesos
    assert p["objetivo_prestamo"] == "capital de trabajo"
    # direccion_inmueble concatena direccion + apto + barrio.
    assert "Cra 45 # 80-20" in p["direccion_inmueble"]
    assert "Chapinero" in p["direccion_inmueble"]

    # Panel_acciones audit + updates sobre contactos.
    assert len(cap["accion"]) == 1
    a = cap["accion"][0]
    assert a["accion_key"] == "completar_formulario_manual"
    assert a["telefono"] == "573001234567"
    assert a["actor"] == "Massi"
    assert a["nota"] == "lead por teléfono"
    assert a["updates"]["resultado_contacto"] == "enviar_a_sureti"
    assert a["updates"]["resultado_contacto_at"] == "NOW()"
    assert a["updates"]["resultado_contacto_por"] == "Massi"
    assert a["updates"]["resultado_contacto_nota"] == "lead por teléfono"
    assert a["updates"]["requiere_humano"] is False


def test_guardar_inserta_audit_en_panel_acciones(client, monkeypatch):
    """Guard específico: el audit en panel_acciones queda con el accion_key
    nuevo `completar_formulario_manual` (no reusa un key existente)."""
    cap = _mock_persistencia(monkeypatch)
    client.post(
        "/panel/573009999999/formulario-manual?token=test-token",
        data=_datos_feliz(actor="Fab"))
    assert len(cap["accion"]) == 1
    assert cap["accion"][0]["accion_key"] == "completar_formulario_manual"
    assert cap["accion"][0]["actor"] == "Fab"


def test_guardar_inserta_mensaje_sintetico_en_chat(client, monkeypatch):
    """El guardado deja un `flow_reply_manual` en `mensajes` para que el
    cierre humano aparezca en el chat del panel."""
    cap = _mock_persistencia(monkeypatch)
    client.post(
        "/panel/573001234567/formulario-manual?token=test-token",
        data=_datos_feliz())
    assert len(cap["mensajes_insert"]) == 1
    sql, params = cap["mensajes_insert"][0]
    assert "flow_reply_manual" in sql
    # params: telefono, resumen JSON, payload JSON
    assert params[0] == "573001234567"
    assert "Pedro Pérez" in params[1]
    assert "capital de trabajo" in params[1]
    # Payload JSON lleva el origen y el actor.
    assert "panel_formulario_manual" in params[2]
    assert "Massi" in params[2]


def test_guardar_avaluo_bajo_igual_guarda_con_warning(client, monkeypatch):
    """Avalúo < 50 M → igual se guarda, pero el flash es
    `guardado_warn_avaluo` para que el detalle muestre un warning."""
    cap = _mock_persistencia(monkeypatch)
    resp = client.post(
        "/panel/573001234567/formulario-manual?token=test-token",
        data=_datos_feliz(avaluo="30"))
    assert resp.status_code == 302
    assert "guardado_warn_avaluo" in resp.headers.get("Location", "")
    # El pipeline sí se escribió.
    assert len(cap["pipeline"]) == 1
    assert cap["pipeline"][0]["avaluo_comercial"] == 30_000_000


def test_accion_completar_formulario_manual_esta_registrada():
    """La entrada en ACCIONES debe existir con label/descripcion/updates
    para que el detalle pueda mostrar el label del cierre en el badge."""
    from app import panel_acciones
    assert "completar_formulario_manual" in panel_acciones.ACCIONES
    cfg = panel_acciones.ACCIONES["completar_formulario_manual"]
    assert cfg["label"]
    assert cfg["descripcion"]
    assert cfg["updates"]["resultado_contacto"] == "enviar_a_sureti"
    assert cfg["updates"]["requiere_humano"] is False
