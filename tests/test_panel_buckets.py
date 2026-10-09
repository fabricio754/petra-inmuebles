"""Tests para la columna "Estado" (bucket humano-first) en /panel.

Refactor sept/oct-2026: los buckets dependen SOLO de acciones humanas
(firma `resultado_contacto_por NOT NULL`) o auto-derivación simple
(`tiene_in`, `contactado`). Las flags que el bot setea
(`no_contactar=true` por detección semántica, `requiere_humano=true` por
pide-llamada) NO afectan el bucket del panel.
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
    # 1. Cierres humanos (requieren firma = resultado_contacto_por NOT NULL)
    ({"resultado_contacto_por": "Massi", "resultado_contacto": "broker",
      "contactado": True}, True, False, False, "no_contactar"),
    ({"resultado_contacto_por": "Fab", "resultado_contacto": "no_interesa",
      "contactado": True}, True, False, False, "no_contactar"),
    ({"resultado_contacto_por": "Massi", "resultado_contacto": "recontactar",
      "contactado": True}, True, False, False, "recontactar"),
    ({"resultado_contacto_por": "Fab", "resultado_contacto": "sureti",
      "contactado": True}, True, False, False, "enviar_a_sureti"),
    ({"resultado_contacto_por": "Fab", "resultado_contacto": "enviar_formulario",
      "contactado": True}, True, False, False, "enviar_formulario"),

    # 2. Legacy / backward-compat: valores viejos siguen matcheando.
    ({"resultado_contacto_por": "Fab", "resultado_contacto": "enviar_a_sureti",
      "contactado": True}, True, False, False, "enviar_a_sureti"),
    ({"resultado_contacto_por": "Fab", "resultado_contacto": "entregado",
      "contactado": True}, True, False, False, "enviar_a_sureti"),
    ({"resultado_contacto_por": "Fab", "resultado_contacto": "flow",
      "contactado": True}, True, False, False, "enviar_formulario"),

    # 3. Auto-derivaciones: tiene_in → respondio
    ({"resultado_contacto": None, "contactado": True},
     True, False, False, "respondio"),
    # 4. contactado sin IN → contactado
    ({"resultado_contacto": None, "contactado": True},
     False, False, False, "contactado"),
    # 5. default → nuevo
    ({"resultado_contacto": None, "contactado": False},
     False, False, False, "nuevo"),
])
def test_bucket_de_reglas(contacto, tiene_in, autorizo, completo_flow, esperado):
    assert bucket_de(contacto, tiene_in, autorizo, completo_flow) == esperado


def test_bucket_de_resultado_no_interesa_firmado_va_a_no_contactar():
    """Un cierre humano `no_interesa` con firma debe caer en bucket
    `no_contactar` (independientemente de que tenga IN)."""
    contacto = {
        "resultado_contacto": "no_interesa",
        "resultado_contacto_por": "Massi",
        "contactado": True,
    }
    assert bucket_de(contacto, tiene_in=True, autorizo=False,
                     completo_flow=False) == "no_contactar"


def test_bucket_de_no_contactar_flag_del_bot_sin_firma_cae_a_respondio():
    """Regla clave del refactor humano-first: la flag `no_contactar=True`
    que el bot setea por detección semántica (sin firma humana) YA NO
    afecta el bucket. Si el bot marcó "no_contactar" por detectar
    "no me interesa", el contacto sigue mostrándose en bucket
    "respondio" hasta que un humano cierre con una acción."""
    contacto = {
        "no_contactar": True,            # el bot lo marcó por semántica
        "resultado_contacto_por": None,  # pero NO hay firma humana
        "resultado_contacto": None,
        "contactado": True,
    }
    assert bucket_de(contacto, tiene_in=True, autorizo=False,
                     completo_flow=False) == "respondio"


def test_bucket_de_requiere_humano_flag_del_bot_sin_firma_cae_a_respondio():
    """Igual que `no_contactar`: la flag `requiere_humano=True` del bot
    (por detección "pide llamada") NO decide el bucket. Si no hay firma,
    el bucket viene de la auto-derivación."""
    contacto = {
        "requiere_humano": True,         # el bot lo marcó
        "resultado_contacto_por": None,  # pero NO hay firma humana
        "resultado_contacto": None,
        "contactado": True,
    }
    assert bucket_de(contacto, tiene_in=True, autorizo=False,
                     completo_flow=False) == "respondio"


def test_bucket_de_contactado_sin_in_sin_firma():
    """Sin firma humana y sin IN, pero contactado=True → bucket
    `contactado`."""
    contacto = {
        "resultado_contacto": None,
        "resultado_contacto_por": None,
        "contactado": True,
    }
    assert bucket_de(contacto, tiene_in=False, autorizo=False,
                     completo_flow=False) == "contactado"


def test_bucket_de_tiene_in_sin_firma_va_a_respondio():
    """Con IN y sin firma humana → bucket `respondio` (incluso si el bot
    marcó no_contactar o requiere_humano)."""
    contacto = {
        "no_contactar": True,
        "requiere_humano": True,
        "resultado_contacto": None,
        "resultado_contacto_por": None,
        "contactado": True,
    }
    assert bucket_de(contacto, tiene_in=True, autorizo=False,
                     completo_flow=False) == "respondio"


def test_bucket_de_completo_flow_sin_firma_sigue_en_respondio():
    """Antes del refactor, `completo_flow=True` subía al bucket
    `enviar_formulario`. Ahora, sin firma humana, queda en `respondio`
    (el Flow completo señala interés, pero no es una acción humana).
    """
    contacto = {
        "resultado_contacto": None,
        "resultado_contacto_por": None,
        "contactado": True,
    }
    assert bucket_de(contacto, tiene_in=True, autorizo=True,
                     completo_flow=True) == "respondio"


def test_bucket_de_resultado_contacto_sin_firma_cae_a_derivacion_automatica():
    """Si existe `resultado_contacto` pero NO hay firma (`_por` es None),
    es un valor huérfano (ej. legacy del bot) y NO debe producir bucket
    humano. Cae a la auto-derivación."""
    contacto = {
        "resultado_contacto": "sureti",  # valor sin firma humana
        "resultado_contacto_por": None,
        "contactado": True,
    }
    assert bucket_de(contacto, tiene_in=True, autorizo=False,
                     completo_flow=False) == "respondio"
    assert bucket_de(contacto, tiene_in=False, autorizo=False,
                     completo_flow=False) == "contactado"


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

    Un contacto con cierre humano `resultado_contacto='sureti'` (firmado)
    debe aparecer con el label "enviar a sureti" en la columna "Estado".
    Y debe filtrarse exactamente al clickear el chip
    `?bucket=enviar_a_sureti`.
    """
    contactos = [
        _c(telefono="573000000001", nombre="Sureti Lead",
           resultado_contacto="sureti", resultado_contacto_por="Massi",
           contactado=True),
        _c(telefono="573000000002", nombre="Respondió",
           contactado=True, tiene_in=True),
        _c(telefono="573000000003", nombre="Nuevo"),
        _c(telefono="573000000004", nombre="Flow Completo",
           contactado=True, tiene_in=True, completo_flow=True),
        # Opt-out del bot (sin firma humana) → bucket "respondio",
        # NO "no_contactar" (regla clave del refactor humano-first).
        _c(telefono="573000000005", nombre="Opt-out bot",
           no_contactar=True, contactado=True, tiene_in=True),
        _c(telefono="573000000006", nombre="Form enviado",
           contactado=True, tiene_in=True,
           resultado_contacto="enviar_formulario",
           resultado_contacto_por="Fab"),
    ]
    conn = _make_conn(contactos)
    monkeypatch.setattr(db, "_conexion_directa", lambda timeout=10: conn)

    r = client.get("/panel?token=test-token")
    assert r.status_code == 200
    html = r.get_data(as_text=True)

    # Los chips tienen los labels humanos esperados.
    assert "enviar a sureti" in html
    assert "respondió" in html
    assert "enviar formulario" in html
    # El badge usa `bucket-<key>` como CSS class — chequear que estén.
    assert "bucket-enviar_a_sureti" in html
    assert "bucket-respondio" in html
    assert "bucket-nuevo" in html
    assert "bucket-enviar_formulario" in html

    # Filtro por bucket: ?bucket=enviar_a_sureti deja sólo los que
    # bucket_de() mapea a "enviar_a_sureti".
    r2 = client.get("/panel?token=test-token&bucket=enviar_a_sureti")
    assert r2.status_code == 200
    html2 = r2.get_data(as_text=True)
    assert "573000000001" in html2  # el único sureti firmado
    assert "573000000005" not in html2  # opt-out del bot → respondio
    assert "573000000002" not in html2  # respondió, no sureti
    assert "573000000003" not in html2  # nuevo


def test_lista_opt_out_del_bot_sin_firma_cae_a_respondio(client, monkeypatch):
    """Caso regresivo: un contacto con `no_contactar=True` seteado por
    el bot (sin firma humana) debe mostrarse en bucket `respondio`
    cuando tiene IN. No debe aparecer en el bucket `no_contactar`."""
    contactos = [
        _c(telefono="573222222222", nombre="Bot Opt-out",
           no_contactar=True, contactado=True, tiene_in=True),
    ]
    conn = _make_conn(contactos)
    monkeypatch.setattr(db, "_conexion_directa", lambda timeout=10: conn)

    r = client.get("/panel?token=test-token")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "bucket-respondio" in html

    # Chip `no_contactar` filtra 0 filas.
    r2 = client.get("/panel?token=test-token&bucket=no_contactar")
    assert r2.status_code == 200
    html2 = r2.get_data(as_text=True)
    assert "573222222222" not in html2


def test_lista_contacto_sureti_firmado_muestra_label_humano(client, monkeypatch):
    """Un contacto con cierre humano `sureti` debe salir con el label
    'enviar a sureti' en la columna visible."""
    contactos = [
        _c(telefono="573111111111", nombre="X",
           resultado_contacto="sureti", resultado_contacto_por="Massi",
           contactado=True),
    ]
    conn = _make_conn(contactos)
    monkeypatch.setattr(db, "_conexion_directa", lambda timeout=10: conn)

    r = client.get("/panel?token=test-token")
    assert r.status_code == 200
    html = r.get_data(as_text=True)

    # El label visible (dentro del badge) es "enviar a sureti".
    assert ">enviar a sureti<" in html
