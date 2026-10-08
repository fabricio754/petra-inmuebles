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


# ---------- Humano-first: BOTON_SI y "Sí me interesa" ya no auto-disparan ----

def test_boton_si_sin_sesion_pasa_a_humano_sin_form(wa_stubs, state_stub, monkeypatch):
    """Humano-first (sept-2026): BOTON_SI sin sesión ya no reconstruye estado
    ni dispara FORM_REQUISITOS. Debe prometer asesor + marcar requiere_humano."""
    phone = "573181716257"

    import app.db as _db
    monkeypatch.setattr(_db, "esta_bloqueado", lambda t: False)
    marco_calls = []
    monkeypatch.setattr(
        _db, "marcar_requiere_humano",
        lambda t, m: marco_calls.append((t, m)) or {"nuevo": True, "debe_alertar": True},
    )
    alerta_calls = []
    monkeypatch.setattr(
        bot, "_enviar_alerta_lead_caliente",
        lambda p, t: alerta_calls.append((p, t)),
    )

    out = bot.handle_incoming(phone, {"type": "button_reply", "id": "BOTON_SI"})

    # No se disparó el Flow 1 ni ningún form.
    assert wa_stubs["send_form_requisitos"].calls == []
    assert wa_stubs["send_pregunta_si_no"].calls == []
    # Sí se marcó requiere_humano + alerta.
    assert len(marco_calls) == 1 and marco_calls[0][0] == phone
    assert alerta_calls == [(phone, "BOTON_SI sin flujo activo (humano-first)")]
    # Se mandó UN texto con "asesor te contactará".
    assert len(out) == 1
    assert len(wa_stubs["send_text"].calls) == 1
    (args, _kw) = wa_stubs["send_text"].calls[0]
    assert args[0] == phone
    assert "asesor" in args[1].lower()


def test_boton_si_con_sesion_autorizacion_pasa_a_humano_no_dispara_flow(
    wa_stubs, state_stub, monkeypatch,
):
    """BOTON_SI con sesión activa en AUTORIZACION: humano-first dice que
    el bot ya no manda FORM_REQUISITOS. Debe prometer asesor."""
    phone = "573181716257"
    import app.db as _db
    monkeypatch.setattr(_db, "esta_bloqueado", lambda t: False)
    monkeypatch.setattr(
        _db, "marcar_requiere_humano",
        lambda t, m: {"nuevo": True, "debe_alertar": False},
    )
    monkeypatch.setattr(bot.whatsapp, "USE_FLOWS", True)

    state_stub["sessions"][phone] = {
        "flow": "SURETI", "flow_step": "AUTORIZACION", "flow_data": {},
    }

    bot.handle_incoming(phone, {"type": "button_reply", "id": "BOTON_SI"})

    # El flujo YA NO dispara form_requisitos.
    assert wa_stubs["send_form_requisitos"].calls == []
    # En su lugar, texto humano-first.
    assert len(wa_stubs["send_text"].calls) == 1
    (args, _kw) = wa_stubs["send_text"].calls[0]
    assert "asesor" in args[1].lower()


def test_template_button_si_me_interesa_pasa_a_humano(wa_stubs, state_stub, monkeypatch):
    """Humano-first: 'Sí me interesa' en template_button ya no inicia el
    consent (`_iniciar`). Marca requiere_humano y promete asesor."""
    phone = "573111222333"
    import app.db as _db
    monkeypatch.setattr(_db, "esta_bloqueado", lambda t: False)
    marco_calls = []
    monkeypatch.setattr(
        _db, "marcar_requiere_humano",
        lambda t, m: marco_calls.append((t, m)) or {"nuevo": True, "debe_alertar": True},
    )
    alerta_calls = []
    monkeypatch.setattr(
        bot, "_enviar_alerta_lead_caliente",
        lambda p, t: alerta_calls.append((p, t)),
    )

    out = bot.handle_incoming(
        phone, {"type": "template_button", "text": "Sí me interesa"},
    )

    # NO disparó consent (send_autorizacion_datos).
    assert wa_stubs["send_autorizacion_datos"].calls == []
    # Marcó requiere_humano.
    assert len(marco_calls) == 1 and marco_calls[0][0] == phone
    assert "post-template" in marco_calls[0][1]
    # Disparó alerta WA.
    assert alerta_calls == [(phone, "Cliente dijo que le interesa post-template")]
    # UN out de texto humano-first.
    assert len(out) == 1
    assert len(wa_stubs["send_text"].calls) == 1
    (args, _kw) = wa_stubs["send_text"].calls[0]
    assert "asesor" in args[1].lower()


# ---------- BOTON_NO sin flujo → opt-out ------------------------------------

def test_boton_no_sin_sesion_marca_opt_out(wa_stubs, state_stub, monkeypatch):
    """Edge case reportado en PR #54: BOTON_NO llega sin sesión activa.
    Antes caía a `_iniciar()` → re-template → loop. Ahora marca no_contactar
    y manda UNA confirmación corta, sin reiniciar."""
    phone = "573508463133"
    import app.db as _db
    monkeypatch.setattr(_db, "esta_bloqueado", lambda t: False)

    iniciar_calls = []
    monkeypatch.setattr(bot, "_iniciar", lambda p: iniciar_calls.append(p) or "iniciar")

    out = bot.handle_incoming(phone, {"type": "button_reply", "id": "BOTON_NO"})

    # Marcó opt-out.
    assert state_stub["no_contactar"].get(phone) is True
    # Mandó UNA sola respuesta y es un texto plano de confirmación.
    assert len(out) == 1
    assert len(wa_stubs["send_text"].calls) == 1
    (args, _kwargs) = wa_stubs["send_text"].calls[0]
    assert args[0] == phone
    assert "no te volveremos a contactar" in args[1].lower()
    # NO llamó a _iniciar (no reinicia la conversación).
    assert iniciar_calls == []
    # No mandó plantilla de apertura ni consent.
    assert wa_stubs["send_plantilla_apertura"].calls == []


def test_reconstruir_sesion_para_boton_si_pasa_a_humano(wa_stubs, state_stub, monkeypatch):
    """Smoke test humano-first: la función auxiliar ya no dispara el Flow;
    responde con el texto humano-first y marca requiere_humano."""
    import app.db as _db
    monkeypatch.setattr(
        _db, "marcar_requiere_humano",
        lambda t, m: {"nuevo": True, "debe_alertar": False},
    )

    result = bot._reconstruir_sesion_para_boton_si("573000000002")

    assert result is not None
    # No se dispara el Flow.
    assert wa_stubs["send_form_requisitos"].calls == []
    # Sí sale el texto humano-first.
    assert wa_stubs["send_text"].calls


# ---------- Pide llamada: lead caliente (piloto 6-oct) ----------------------

def test_pide_llamada_marca_requiere_humano_y_responde(wa_stubs, state_stub, monkeypatch):
    """Caso real: 573508463133 escribió 'Me pueden llamar?'. Debe marcar
    requiere_humano, disparar alerta (es la 1ª vez), y responder la
    confirmación corta. NO sigue flujo."""
    phone = "573508463133"
    import app.db as _db

    monkeypatch.setattr(_db, "esta_bloqueado", lambda t: False)

    marco_calls = []

    def _marcar(telefono, motivo):
        marco_calls.append((telefono, motivo))
        return {"nuevo": True, "debe_alertar": True}  # primera vez

    monkeypatch.setattr(_db, "marcar_requiere_humano", _marcar)

    alerta_calls = []
    monkeypatch.setattr(
        bot, "_enviar_alerta_lead_caliente",
        lambda p, t: alerta_calls.append((p, t)),
    )

    out = bot.handle_incoming(phone, {"type": "text", "text": "Me pueden llamar?"})

    # Marcó requiere_humano con motivo que incluye el texto original.
    assert len(marco_calls) == 1
    assert marco_calls[0][0] == phone
    assert "Me pueden llamar" in marco_calls[0][1]

    # Disparó la alerta externa (primera vez).
    assert alerta_calls == [(phone, "Me pueden llamar?")]

    # Respondió UNA sola vez con texto de confirmación.
    assert len(out) == 1
    assert len(wa_stubs["send_text"].calls) == 1
    (args, _kw) = wa_stubs["send_text"].calls[0]
    assert args[0] == phone
    assert "asesor" in args[1].lower()


def test_pide_llamada_dentro_del_cooldown_no_dispara_alerta(wa_stubs, state_stub, monkeypatch):
    """Si marcar_requiere_humano reporta debe_alertar=False (ya estaba
    marcado y la última alerta fue dentro del cooldown), NO disparar
    alerta otra vez; sí responder la confirmación."""
    phone = "573508463133"
    import app.db as _db

    monkeypatch.setattr(_db, "esta_bloqueado", lambda t: False)
    monkeypatch.setattr(
        _db, "marcar_requiere_humano",
        lambda telefono, motivo: {"nuevo": False, "debe_alertar": False},
    )

    alerta_calls = []
    monkeypatch.setattr(
        bot, "_enviar_alerta_lead_caliente",
        lambda p, t: alerta_calls.append((p, t)),
    )

    out = bot.handle_incoming(phone, {"type": "text", "text": "Me pueden llamar?"})

    # Alerta NO se dispara dentro del cooldown.
    assert alerta_calls == []
    # Pero sí re-respondemos la confirmación.
    assert len(out) == 1
    assert len(wa_stubs["send_text"].calls) == 1


def test_pide_llamada_despues_del_cooldown_dispara_alerta(wa_stubs, state_stub, monkeypatch):
    """Si pasaron >= cooldown_minutos desde la última alerta, marcar_requiere_humano
    reporta debe_alertar=True aunque nuevo=False. Debe re-disparar la alerta
    (el cliente volvió a pedir llamada y nadie lo atendió todavía)."""
    phone = "573508463133"
    import app.db as _db

    monkeypatch.setattr(_db, "esta_bloqueado", lambda t: False)
    monkeypatch.setattr(
        _db, "marcar_requiere_humano",
        lambda telefono, motivo: {"nuevo": False, "debe_alertar": True},
    )

    alerta_calls = []
    monkeypatch.setattr(
        bot, "_enviar_alerta_lead_caliente",
        lambda p, t: alerta_calls.append((p, t)),
    )

    out = bot.handle_incoming(phone, {"type": "text", "text": "Me pueden llamar?"})

    # Alerta SÍ se re-dispara pasado el cooldown.
    assert alerta_calls == [(phone, "Me pueden llamar?")]
    assert len(out) == 1
    assert len(wa_stubs["send_text"].calls) == 1


def test_bloqueado_tiene_precedencia_sobre_pide_llamada(wa_stubs, state_stub, monkeypatch):
    """Guard esta_bloqueado tiene precedencia absoluta: si no_contactar=TRUE,
    no respondemos NADA (ni confirmación) ni marcamos requiere_humano."""
    phone = "573508463133"
    import app.db as _db

    monkeypatch.setattr(_db, "esta_bloqueado", lambda t: True)

    marco_calls = []

    def _marcar(telefono, motivo):
        marco_calls.append((telefono, motivo))
        return {"nuevo": True, "debe_alertar": True}

    monkeypatch.setattr(_db, "marcar_requiere_humano", _marcar)

    alerta_calls = []
    monkeypatch.setattr(
        bot, "_enviar_alerta_lead_caliente",
        lambda p, t: alerta_calls.append((p, t)),
    )

    out = bot.handle_incoming(phone, {"type": "text", "text": "Me pueden llamar?"})

    assert out == []
    assert marco_calls == []
    assert alerta_calls == []
    for name, f in wa_stubs.items():
        assert f.calls == [], f"se envió {name} aun estando bloqueado"


def test_pide_llamada_db_falla_no_rompe_handler(wa_stubs, state_stub, monkeypatch):
    """Si la DB tira excepción al marcar, el bot sigue y responde igual la
    confirmación (fail-open) sin disparar alerta (no sabemos si es nueva)."""
    phone = "573000000003"
    import app.db as _db

    monkeypatch.setattr(_db, "esta_bloqueado", lambda t: False)

    def _boom(telefono, motivo):
        raise RuntimeError("db down")

    monkeypatch.setattr(_db, "marcar_requiere_humano", _boom)

    alerta_calls = []
    monkeypatch.setattr(
        bot, "_enviar_alerta_lead_caliente",
        lambda p, t: alerta_calls.append((p, t)),
    )

    out = bot.handle_incoming(phone, {"type": "text", "text": "Me pueden llamar?"})

    # Alerta NO se dispara si no supimos si era nueva.
    assert alerta_calls == []
    # Sí respondemos confirmación igual (no dejamos al user sin respuesta).
    assert len(out) == 1
    assert len(wa_stubs["send_text"].calls) == 1


def test_enviar_alerta_lead_caliente_sin_env_no_rompe(monkeypatch):
    """Si no hay ALERT_WEBHOOK_URL ni ALERTAS_WHATSAPP, la función solo
    logea; no debe lanzar ni tocar la red."""
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALERTAS_WHATSAPP", raising=False)
    monkeypatch.setattr(bot, "ALERT_WEBHOOK_URL", "")
    monkeypatch.setattr(bot, "ALERTAS_WHATSAPP", "")
    # No debe lanzar.
    bot._enviar_alerta_lead_caliente("573000000000", "test")


# ---------- Alerta WA a asesores -------------------------------------------

def test_alerta_lead_caliente_manda_wa_a_todos_los_asesores(monkeypatch):
    """Con ALERTAS_WHATSAPP=csv, debe llamar whatsapp.send_text una vez por
    cada asesor con un mensaje que incluye telefono, fragmento y link al panel
    (si PANEL_URL + PANEL_TOKEN estan configurados)."""
    monkeypatch.setenv("ALERTAS_WHATSAPP", "573001112233, 573004445566")
    monkeypatch.setenv("PANEL_URL", "https://panel.example.com")
    monkeypatch.setenv("PANEL_TOKEN", "tok-secreto")
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(bot, "ALERT_WEBHOOK_URL", "")

    sent = []
    monkeypatch.setattr(
        bot.whatsapp, "send_text",
        lambda to, body: sent.append((to, body)),
    )

    bot._enviar_alerta_lead_caliente("573508463133", "Me pueden llamar?")

    assert len(sent) == 2
    destinos = [to for to, _b in sent]
    assert destinos == ["573001112233", "573004445566"]
    # El cuerpo contiene las piezas clave.
    for _to, body in sent:
        assert "LEAD CALIENTE" in body
        assert "573508463133" in body
        assert "Me pueden llamar?" in body
        assert "https://panel.example.com/panel/573508463133?token=tok-secreto" in body


def test_alerta_lead_caliente_sin_asesores_no_falla(monkeypatch):
    """ALERTAS_WHATSAPP vacio: no debe llamar send_text ni lanzar."""
    monkeypatch.delenv("ALERTAS_WHATSAPP", raising=False)
    monkeypatch.setattr(bot, "ALERTAS_WHATSAPP", "")
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(bot, "ALERT_WEBHOOK_URL", "")

    sent = []
    monkeypatch.setattr(
        bot.whatsapp, "send_text",
        lambda to, body: sent.append((to, body)),
    )

    # No debe lanzar.
    bot._enviar_alerta_lead_caliente("573000000000", "test")
    assert sent == []


def test_alerta_lead_caliente_falla_un_asesor_sigue_con_otros(monkeypatch):
    """Si send_text revienta para un asesor, debe seguir intentando con los
    demas (no abortar el loop)."""
    monkeypatch.setenv("ALERTAS_WHATSAPP", "573001112233,573004445566,573007778899")
    monkeypatch.delenv("PANEL_URL", raising=False)
    monkeypatch.delenv("PANEL_TOKEN", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(bot, "ALERT_WEBHOOK_URL", "")

    calls = []

    def _send(to, body):
        calls.append(to)
        if to == "573004445566":
            raise RuntimeError("API down para este numero")

    monkeypatch.setattr(bot.whatsapp, "send_text", _send)

    # No debe lanzar.
    bot._enviar_alerta_lead_caliente("573508463133", "Llamenme por favor")

    # Se intento con los tres aunque el segundo haya tirado.
    assert calls == ["573001112233", "573004445566", "573007778899"]


def test_alerta_lead_caliente_sin_panel_omite_link(monkeypatch):
    """Si PANEL_URL o PANEL_TOKEN no estan, el mensaje se arma sin link."""
    monkeypatch.setenv("ALERTAS_WHATSAPP", "573001112233")
    monkeypatch.delenv("PANEL_URL", raising=False)
    monkeypatch.delenv("PANEL_TOKEN", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(bot, "ALERT_WEBHOOK_URL", "")

    sent = []
    monkeypatch.setattr(
        bot.whatsapp, "send_text",
        lambda to, body: sent.append((to, body)),
    )

    bot._enviar_alerta_lead_caliente("573508463133", "hola")

    assert len(sent) == 1
    _to, body = sent[0]
    assert "panel" not in body.lower()
    assert "http" not in body


def test_alerta_lead_caliente_webhook_y_wa_coexisten(monkeypatch):
    """Si estan los dos canales, ambos se disparan (el webhook sigue
    funcionando como complementario)."""
    monkeypatch.setenv("ALERTAS_WHATSAPP", "573001112233")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hook.example.com/alert")
    monkeypatch.setattr(bot, "ALERT_WEBHOOK_URL", "https://hook.example.com/alert")

    sent = []
    monkeypatch.setattr(
        bot.whatsapp, "send_text",
        lambda to, body: sent.append((to, body)),
    )

    posts = []

    class _FakeRequests:
        @staticmethod
        def post(url, json=None, timeout=None):
            posts.append((url, json, timeout))

    import sys
    monkeypatch.setitem(sys.modules, "requests", _FakeRequests)

    bot._enviar_alerta_lead_caliente("573508463133", "me llaman?")

    assert len(sent) == 1
    assert len(posts) == 1
    assert posts[0][0] == "https://hook.example.com/alert"
    assert posts[0][1]["phone"] == "573508463133"


# ---------- FAQ pre-grabadas (5 intents canónicos) --------------------------

def test_bot_responde_faq_tasa(wa_stubs, state_stub, monkeypatch):
    """Caso real: cliente pregunta 'cuanto cobran?'. Antes el bot caía al
    clasificador y le mandaba la plantilla de apertura (consent). Ahora
    debe responder la FAQ canónica de tasa_interes."""
    phone = "573001234567"
    import app.db as _db
    monkeypatch.setattr(_db, "esta_bloqueado", lambda t: False)

    out = bot.handle_incoming(phone, {"type": "text", "text": "cuanto cobran?"})

    assert len(out) == 1
    assert wa_stubs["send_text"].calls, "no mandó la FAQ"
    (args, _kwargs) = wa_stubs["send_text"].calls[0]
    assert args[0] == phone
    assert "tasa" in args[1].lower()
    # No disparó el consent ni la plantilla de apertura.
    assert wa_stubs["send_autorizacion_datos"].calls == []
    assert wa_stubs["send_plantilla_apertura"].calls == []


def test_bot_responde_faq_quienes_somos(wa_stubs, state_stub, monkeypatch):
    """'quienes son?' debe mandar la respuesta canónica de quienes_somos,
    no el consent."""
    phone = "573001234568"
    import app.db as _db
    monkeypatch.setattr(_db, "esta_bloqueado", lambda t: False)

    out = bot.handle_incoming(phone, {"type": "text", "text": "quienes son?"})

    assert len(out) == 1
    assert wa_stubs["send_text"].calls
    (args, _kwargs) = wa_stubs["send_text"].calls[0]
    assert "Petra" in args[1]
    assert wa_stubs["send_autorizacion_datos"].calls == []


def test_bot_no_interrumpe_flow_activo_con_faq(wa_stubs, state_stub, monkeypatch):
    """Si hay flow SURETI activo, la FAQ NO debe disparar — dejar que el
    Flow siga su curso. Ej: usuario en DATOS_ESTRATO responde '2' pero
    luego pregunta 'cuanto cobran?'; el handler normal debe correr."""
    phone = "573001234569"
    import app.db as _db
    monkeypatch.setattr(_db, "esta_bloqueado", lambda t: False)

    # Simulamos sesión con flow activo en un paso de texto libre.
    state_stub["sessions"][phone] = {
        "flow": "SURETI",
        "flow_step": "DATOS_NOMBRE",
        "flow_data": {},
    }

    bot.handle_incoming(phone, {"type": "text", "text": "cuanto cobran?"})

    # La FAQ canónica NO debería haber salido. El flow debería seguir y
    # pedir el nombre otra vez (o aceptar 'cuanto cobran?' como nombre).
    # Lo clave: la respuesta canónica de tasa NO está entre los sends.
    for args, _kwargs in wa_stubs["send_text"].calls:
        assert "trabajamos en rangos competitivos" not in args[1]


def test_bot_bloqueado_no_responde_faq(wa_stubs, state_stub, monkeypatch):
    """Precedencia: si no_contactar=true, el guard esta_bloqueado gana y
    el bot queda mudo aunque el texto sea FAQ pura."""
    phone = "573001234570"
    import app.db as _db
    monkeypatch.setattr(_db, "esta_bloqueado", lambda t: True)

    out = bot.handle_incoming(phone, {"type": "text", "text": "quienes son?"})

    assert out == []
    for name, f in wa_stubs.items():
        assert f.calls == [], f"bloqueado pero se envió {name}"


def test_bot_pide_llamada_tiene_prioridad_sobre_faq(wa_stubs, state_stub, monkeypatch):
    """Precedencia: 'me pueden llamar?' es señal más caliente que una FAQ.
    Debe ir a la rama de pide_llamada, no a FAQ."""
    phone = "573001234571"
    import app.db as _db
    monkeypatch.setattr(_db, "esta_bloqueado", lambda t: False)
    monkeypatch.setattr(
        _db, "marcar_requiere_humano",
        lambda p, m: {"nuevo": True, "debe_alertar": False},
    )

    bot.handle_incoming(phone, {"type": "text", "text": "me pueden llamar?"})

    # Respondió la confirmación de pide_llamada, no la FAQ.
    assert wa_stubs["send_text"].calls
    textos = [a[1] for a, _ in wa_stubs["send_text"].calls]
    assert any("asesor te contactar" in t.lower() for t in textos)
    # NO salió una respuesta FAQ.
    for t in textos:
        assert "trabajamos en rangos competitivos" not in t
        assert "Petra, una empresa" not in t
