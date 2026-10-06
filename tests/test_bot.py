"""Tests para los fixes del bot (piloto 6-oct):

- B1: BOTON_SI con sesión expirada no debe responder "no entendí"; debe
  reconstruir estado y enviar el consent / FORM_REQUISITOS.
- B3: tras un opt-out (no_contactar=TRUE) el bot debe quedarse mudo.
"""
from unittest.mock import patch

import pytest

from app import bot


class _Fake:
    """Dummy que registra las llamadas; su `__repr__` es su nombre."""

    def __init__(self, name):
        self.name = name
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return f"<sent {self.name}>"


@pytest.fixture
def wa_stubs(monkeypatch):
    """Reemplaza TODOS los `send_*` de whatsapp por fakes, para capturar
    qué habría salido sin tocar la red."""
    stubs = {}
    for attr in dir(bot.whatsapp):
        if attr.startswith("send_"):
            f = _Fake(attr)
            stubs[attr] = f
            monkeypatch.setattr(bot.whatsapp, attr, f)
    return stubs


@pytest.fixture
def state_stub(monkeypatch):
    """Memoria en lugar de DB/Sheets para `state`."""
    store = {"sessions": {}, "no_contactar": {}, "pipeline": [], "respuestas": []}

    def get_session(phone):
        return dict(store["sessions"].get(phone, {}))

    def set_session(phone, **fields):
        s = store["sessions"].setdefault(phone, {})
        s.update(fields)
        return dict(s)

    def set_no_contactar(phone, valor=True):
        store["no_contactar"][phone] = valor
        set_session(phone, no_contactar=valor)

    def marcar_respuesta(phone, resultado):
        store["respuestas"].append((phone, resultado))

    def save_pipeline(data):
        store["pipeline"].append(data)

    monkeypatch.setattr(bot.state, "get_session", get_session)
    monkeypatch.setattr(bot.state, "set_session", set_session)
    monkeypatch.setattr(bot.state, "set_no_contactar", set_no_contactar)
    monkeypatch.setattr(bot.state, "marcar_respuesta", marcar_respuesta)
    monkeypatch.setattr(bot.state, "save_pipeline", save_pipeline)
    return store


# ---------- B3: post opt-out ------------------------------------------------

def test_b3_texto_despues_de_opt_out_no_responde(wa_stubs, state_stub, monkeypatch):
    """Caso real: 573508463133 dijo NO → recibió 6 consents. Ahora debe quedar
    mudo tras `no_contactar=TRUE`."""
    phone = "573508463133"

    import app.db as _db
    monkeypatch.setattr(_db, "esta_bloqueado", lambda t: t == phone)

    out = bot.handle_incoming(phone, {"type": "text", "text": "Buenos días"})

    assert out == []
    # Ningún send_* fue llamado.
    for name, f in wa_stubs.items():
        assert f.calls == [], f"se envió {name} con calls={f.calls}"


def test_b3_boton_si_despues_de_opt_out_no_responde(wa_stubs, state_stub, monkeypatch):
    """Un BOTON_SI tardío de un contacto ya opt-out tampoco despierta al bot."""
    phone = "573508463133"
    import app.db as _db
    monkeypatch.setattr(_db, "esta_bloqueado", lambda t: True)

    out = bot.handle_incoming(phone, {"type": "button_reply", "id": "BOTON_SI"})

    assert out == []
    for name, f in wa_stubs.items():
        assert f.calls == [], f"se envió {name} con calls={f.calls}"


def test_b3_fallo_db_no_rompe_bot(wa_stubs, state_stub, monkeypatch):
    """Si `esta_bloqueado` tira excepción, el bot sigue andando (fail-open).
    Nota: `esta_bloqueado` ya atrapa internamente, pero este test cubre que
    el handler no se cae ante un fallo transitorio."""
    phone = "573000000001"
    import app.db as _db

    def _boom(_t):
        raise RuntimeError("db down")

    monkeypatch.setattr(_db, "esta_bloqueado", _boom)
    # No debe lanzar.
    bot.handle_incoming(phone, {"type": "text", "text": "hola"})


# ---------- B1: BOTON_SI con sesión expirada --------------------------------

def test_b1_boton_si_sin_sesion_manda_form_requisitos(wa_stubs, state_stub, monkeypatch):
    """Caso real: 573181716257 clickeó Sí tardío. El bot no debe decir
    "no entendí": debe reconstruir estado y mandar FORM_REQUISITOS."""
    phone = "573181716257"

    import app.db as _db
    monkeypatch.setattr(_db, "esta_bloqueado", lambda t: False)
    monkeypatch.setattr(_db, "get_contacto", lambda t: {"tipo_inmueble": "casa", "monto_hasta": 100})
    # Forzamos USE_FLOWS=True para esperar FORM_REQUISITOS.
    monkeypatch.setattr(bot.whatsapp, "USE_FLOWS", True)

    out = bot.handle_incoming(phone, {"type": "button_reply", "id": "BOTON_SI"})

    assert len(out) == 1
    assert wa_stubs["send_form_requisitos"].calls, "no mandó FORM_REQUISITOS"
    # Y la sesión quedó reconstruida.
    s = state_stub["sessions"][phone]
    assert s["flow"] == "SURETI"
    assert s["flow_step"] == "FORM_REQUISITOS"
    assert "autorizacion_en" in s["flow_data"]
    assert s["flow_data"]["tipo_inmueble"] == "casa"
    assert s["flow_data"]["valor_solicitado"] == 100 * 1_000_000


def test_b1_boton_si_sin_sesion_sin_flows_pregunta_hipoteca(wa_stubs, state_stub, monkeypatch):
    """Si USE_FLOWS=False (modo chat), debe mandar la primera pregunta de
    descarte (DESC_HIPOTECA), no "no entendí"."""
    phone = "573181716257"
    import app.db as _db
    monkeypatch.setattr(_db, "esta_bloqueado", lambda t: False)
    monkeypatch.setattr(_db, "get_contacto", lambda t: None)
    monkeypatch.setattr(bot.whatsapp, "USE_FLOWS", False)

    out = bot.handle_incoming(phone, {"type": "button_reply", "id": "BOTON_SI"})

    assert len(out) == 1
    assert wa_stubs["send_pregunta_si_no"].calls, "no mandó pregunta de descarte"
    s = state_stub["sessions"][phone]
    assert s["flow"] == "SURETI"
    assert s["flow_step"] == "DESC_HIPOTECA"


def test_b1_boton_si_con_sesion_activa_sigue_flujo_normal(wa_stubs, state_stub, monkeypatch):
    """Un BOTON_SI con sesión en AUTORIZACION debe seguir por el flujo normal
    (no entra a la reconstrucción B1)."""
    phone = "573181716257"
    import app.db as _db
    monkeypatch.setattr(_db, "esta_bloqueado", lambda t: False)
    monkeypatch.setattr(bot.whatsapp, "USE_FLOWS", True)

    state_stub["sessions"][phone] = {"flow": "SURETI", "flow_step": "AUTORIZACION", "flow_data": {}}

    bot.handle_incoming(phone, {"type": "button_reply", "id": "BOTON_SI"})

    # El flujo normal manda form_requisitos cuando USE_FLOWS=True.
    assert wa_stubs["send_form_requisitos"].calls, "flujo normal no corrió"


def test_b1_funcion_reconstruir_devuelve_algo(wa_stubs, state_stub, monkeypatch):
    """Smoke test de la función auxiliar."""
    import app.db as _db
    monkeypatch.setattr(_db, "get_contacto", lambda t: None)
    monkeypatch.setattr(bot.whatsapp, "USE_FLOWS", True)

    result = bot._reconstruir_sesion_para_boton_si("573000000002")

    assert result is not None
    assert wa_stubs["send_form_requisitos"].calls
