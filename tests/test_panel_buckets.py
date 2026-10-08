"""Tests para la columna "Estado" (bucket humano-first) en /panel.

Hotfix: la columna "Estado" de `panel_lista.html` seguía mostrando los
labels viejos (`en_flujo:SURETI`, `en_flujo`, …) mientras que los chips
de filtro arriba ya eran los nuevos (`enviar a sureti`, `respondió`, …).
Esto verifica que el bucket que se muestra en la fila sea EXACTAMENTE
el mismo que el que calculan los chips (via `bucket_de()`).
"""
import os
from unittest.mock import MagicMock

import pytest
from flask import Flask


os.environ.setdefault("PANEL_TOKEN", "test-token")

from app import db, panel as panel_mod  # noqa: E402
from app.panel import BUCKETS_LABELS, bucket_de  # noqa: E402


# ---------------------------------------------------------------------------
# bucket_de(): lógica pura — un contrato con sí mismo.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("contacto,tiene_in,autorizo,completo_flow,esperado", [
    # 1. no_contactar gana sobre todo
    ({"no_contactar": True, "contactado": True, "resultado_contacto": "sureti"},
     True, True, True, "no_contactar"),
    # 2. resultado_contacto sureti/entregado → enviar_a_sureti
    ({"no_contactar": False, "contactado": True, "resultado_contacto": "sureti"},
     True, False, False, "enviar_a_sureti"),
    ({"no_contactar": False, "contactado": True, "resultado_contacto": "entregado"},
     True, False, False, "enviar_a_sureti"),
    # 3. completo_flow o resultado flow/enviar_formulario
    ({"no_contactar": False, "contactado": True, "resultado_contacto": None},
     True, True, True, "enviar_formulario"),
    ({"no_contactar": False, "contactado": True,
      "resultado_contacto": "enviar_formulario"},
     False, False, False, "enviar_formulario"),
    # 4. tiene IN → respondio
    ({"no_contactar": False, "contactado": True, "resultado_contacto": None},
     True, False, False, "respondio"),
    # 5. contactado sin IN → contactado
    ({"no_contactar": False, "contactado": True, "resultado_contacto": None},
     False, False, False, "contactado"),
    # 6. default → nuevo
    ({"no_contactar": False, "contactado": False, "resultado_contacto": None},
     False, False, False, "nuevo"),
    # 7. resultado_contacto='recontactar' → "recontactar"
    ({"no_contactar": False, "contactado": True,
      "resultado_contacto": "recontactar"},
     False, False, False, "recontactar"),
])
def test_bucket_de_reglas(contacto, tiene_in, autorizo, completo_flow, esperado):
    assert bucket_de(contacto, tiene_in, autorizo, completo_flow) == esperado


def test_bucket_de_recontactar_tiene_precedencia_sobre_derivados():
    """Un contacto cerrado con `resultado_contacto='recontactar'` que
    además tiene IN (respondió) o completó el Flow debe aparecer en el
    bucket "recontactar" — no diluirse en "respondio" ni en
    "enviar_formulario"."""
    contacto = {
        "no_contactar": False,
        "contactado": True,
        "resultado_contacto": "recontactar",
    }
    # Tiene IN pero el cierre humano manda:
    assert bucket_de(contacto, tiene_in=True, autorizo=False,
                     completo_flow=False) == "recontactar"
    # Incluso con Flow completo, el cierre "recontactar" tiene precedencia:
    assert bucket_de(contacto, tiene_in=True, autorizo=True,
                     completo_flow=True) == "recontactar"


def test_bucket_recontactar_ignorado_si_no_contactar():
    """`no_contactar=True` es precedencia 1 y gana incluso sobre un
    cierre `recontactar` (coherencia con la regla pre-existente)."""
    contacto = {
        "no_contactar": True,
        "contactado": True,
        "resultado_contacto": "recontactar",
    }
    assert bucket_de(contacto, tiene_in=True, autorizo=False,
                     completo_flow=False) == "no_contactar"


def test_todos_los_buckets_tienen_label():
    """Si bucket_de devuelve una clave, BUCKETS_LABELS debe tenerla —
    si no, la columna "Estado" caería al fallback crudo."""
    from app.panel import BUCKETS_ORDEN
    for k in BUCKETS_ORDEN:
        assert k in BUCKETS_LABELS
        # El label visible debe ser texto humano, no el id interno
        # (excepto `nuevo` y `contactado` donde ambos coinciden).
        assert BUCKETS_LABELS[k]


# ---------------------------------------------------------------------------
# Integración: /panel renderea el bucket en la fila, mismo que los chips.
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    app = Flask(__name__, template_folder="../app/templates")
    app.register_blueprint(panel_mod.panel)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _desc(cols):
    class C:
        def __init__(self, n): self.name = n
    return [C(c) for c in cols]


# Columnas del SELECT de contactos en `panel_lista`.
_COLS_CONTACTOS = [
    "telefono", "nombre", "ciudad", "tipo_inmueble", "portal",
    "monto_hasta", "no_contactar", "contactado", "fecha_contacto",
    "fecha_scraping", "resultado_contacto", "resultado_contacto_at",
    "resultado_contacto_por", "requiere_humano",
    "tiene_in", "autorizo", "completo_flow",
]


def _c(**over):
    base = {k: None for k in _COLS_CONTACTOS}
    base.update({
        "no_contactar": False, "contactado": False,
        "tiene_in": False, "autorizo": False, "completo_flow": False,
        "requiere_humano": False,
    })
    base.update(over)
    return base


def _make_conn(contactos_rows):
    conn = MagicMock()

    def _exec(sql, params=()):
        cur = MagicMock()
        if "FROM contactos" in sql:
            cur.description = _desc(_COLS_CONTACTOS)
            cur.fetchall.return_value = [
                tuple(r.get(c) for c in _COLS_CONTACTOS) for r in contactos_rows
            ]
        elif "FROM sesiones" in sql:
            cur.description = _desc(["telefono", "estado", "ultima_actividad"])
            cur.fetchall.return_value = []
        elif "FROM pipeline" in sql:
            cur.description = _desc([
                "telefono", "estado", "sureti_lead_id", "fecha_ingreso",
                "fecha_aprobacion", "fecha_desembolso", "monto_aprobado",
                "avaluo_comercial"])
            cur.fetchall.return_value = []
        else:
            cur.description = None
            cur.fetchall.return_value = []
        return cur

    conn.execute.side_effect = _exec
    conn.close = MagicMock()
    conn.__enter__ = lambda self: self
    conn.__exit__ = lambda self, *a: None
    return conn


def test_lista_muestra_bucket_coherente_con_chip(client, monkeypatch):
    """Cada fila muestra el mismo bucket que usa el chip del filtro.

    Un contacto con `resultado_contacto='sureti'` debe aparecer con el
    label "enviar a sureti" en la columna "Estado" — NO con el label
    viejo "en_flujo:SURETI". Y debe filtrarse exactamente al clickear
    el chip `?bucket=enviar_a_sureti`.
    """
    contactos = [
        _c(telefono="573000000001", nombre="Sureti Lead",
           resultado_contacto="sureti", contactado=True),
        _c(telefono="573000000002", nombre="Respondió",
           contactado=True, tiene_in=True),
        _c(telefono="573000000003", nombre="Nuevo"),
        _c(telefono="573000000004", nombre="Flow Completo",
           contactado=True, tiene_in=True, completo_flow=True),
        _c(telefono="573000000005", nombre="Opt-out",
           no_contactar=True, contactado=True, tiene_in=True,
           resultado_contacto="sureti"),
    ]
    conn = _make_conn(contactos)
    monkeypatch.setattr(db, "_conexion_directa", lambda timeout=10: conn)

    r = client.get("/panel?token=test-token")
    assert r.status_code == 200
    html = r.get_data(as_text=True)

    # Los chips del PR #71 están en la página (nuevos labels, no viejos).
    assert "enviar a sureti" in html
    assert "respondió" in html
    assert "enviar formulario" in html
    # El label viejo `en_flujo:SURETI` NO debe salir como texto principal
    # (puede quedar como tooltip, pero no como contenido visible del badge).
    # El badge usa `bucket-<key>` como CSS class — chequear que esas estén.
    assert "bucket-enviar_a_sureti" in html
    assert "bucket-respondio" in html
    assert "bucket-nuevo" in html
    assert "bucket-enviar_formulario" in html
    assert "bucket-no_contactar" in html

    # Filtro por bucket: ?bucket=enviar_a_sureti deja sólo los que
    # bucket_de() mapea a "enviar_a_sureti". El opt-out NO cuenta porque
    # no_contactar gana sobre resultado_contacto.
    r2 = client.get("/panel?token=test-token&bucket=enviar_a_sureti")
    assert r2.status_code == 200
    html2 = r2.get_data(as_text=True)
    assert "573000000001" in html2  # el único enviar_a_sureti puro
    assert "573000000005" not in html2  # opt-out → no_contactar bucket
    assert "573000000002" not in html2  # respondió, no sureti
    assert "573000000003" not in html2  # nuevo


def test_lista_contacto_sureti_no_muestra_label_viejo(client, monkeypatch):
    """Un contacto que antes salía como `en_flujo:SURETI` ahora debe
    salir con el label humano "enviar a sureti" en la columna visible."""
    contactos = [
        _c(telefono="573111111111", nombre="X",
           resultado_contacto="sureti", contactado=True),
    ]
    conn = _make_conn(contactos)
    monkeypatch.setattr(db, "_conexion_directa", lambda timeout=10: conn)

    r = client.get("/panel?token=test-token")
    assert r.status_code == 200
    html = r.get_data(as_text=True)

    # El label visible (dentro del badge) es "enviar a sureti".
    assert ">enviar a sureti<" in html
