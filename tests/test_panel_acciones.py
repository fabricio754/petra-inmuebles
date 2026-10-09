"""Tests para el módulo app.panel_acciones.

Cubre la lógica pura (mapping accion → updates, validaciones) sin tocar
Postgres: `db.aplicar_accion_panel` se mockea con un capture simple.
"""
import pytest

from app import panel_acciones


class _CaptureDb:
    """Fake de `db.aplicar_accion_panel` que captura cada llamada."""

    def __init__(self):
        self.llamadas = []

    def __call__(self, telefono, updates, accion_key, nota, actor):
        # Copia del dict para que el caller no mute después de la llamada.
        self.llamadas.append({
            "telefono": telefono,
            "updates": dict(updates),
            "accion_key": accion_key,
            "nota": nota,
            "actor": actor,
        })


@pytest.fixture
def captura(monkeypatch):
    cap = _CaptureDb()
    monkeypatch.setattr("app.db.aplicar_accion_panel", cap)
    return cap


def test_aplicar_broker_marca_no_contactar_e_inserta_audit(captura):
    res = panel_acciones.aplicar("57300", "broker", nota="broker claro",
                                 actor="Massi")
    assert res == {"ok": True, "accion": "broker",
                   "label": panel_acciones.ACCIONES["broker"]["label"]}
    assert len(captura.llamadas) == 1
    call = captura.llamadas[0]
    assert call["telefono"] == "57300"
    assert call["accion_key"] == "broker"
    assert call["actor"] == "Massi"
    assert call["nota"] == "broker claro"
    # Debe marcar no_contactar + resultado_contacto + metadata de cierre.
    assert call["updates"]["no_contactar"] is True
    assert call["updates"]["resultado_contacto"] == "broker"
    assert call["updates"]["resultado_contacto_at"] == "NOW()"
    assert call["updates"]["resultado_contacto_por"] == "Massi"
    assert call["updates"]["resultado_contacto_nota"] == "broker claro"


def test_aplicar_no_interesa_marca_no_contactar_y_firma(captura):
    res = panel_acciones.aplicar("57310", "no_interesa",
                                 nota="no quiere", actor="Massi")
    assert res["ok"] is True
    call = captura.llamadas[0]
    assert call["updates"]["no_contactar"] is True
    assert call["updates"]["resultado_contacto"] == "no_interesa"
    assert call["updates"]["resultado_contacto_por"] == "Massi"
    assert call["updates"]["resultado_contacto_nota"] == "no quiere"


def test_aplicar_sureti_setea_resultado_sin_tocar_no_contactar(captura):
    res = panel_acciones.aplicar("57301", "sureti", actor="Fab")
    assert res["ok"] is True
    call = captura.llamadas[0]
    assert call["updates"]["resultado_contacto"] == "sureti"
    # Importante: no debe tocar no_contactar ni requiere_humano.
    assert "no_contactar" not in call["updates"]
    assert "requiere_humano" not in call["updates"]
    # Pero sí debe grabar el actor para el audit.
    assert call["updates"]["resultado_contacto_por"] == "Fab"


def test_aplicar_recontactar_firma_y_libera_no_contactar(captura):
    res = panel_acciones.aplicar("57311", "recontactar", actor="Fab")
    assert res["ok"] is True
    call = captura.llamadas[0]
    assert call["updates"]["resultado_contacto"] == "recontactar"
    assert call["updates"]["no_contactar"] is False
    assert call["updates"]["resultado_contacto_por"] == "Fab"


def test_aplicar_accion_desconocida_retorna_error(captura):
    res = panel_acciones.aplicar("57303", "no_existe_este_key")
    assert res == {"ok": False, "error": "accion_desconocida"}
    assert captura.llamadas == []  # nada se persiste


def test_aplicar_enviar_formulario_marca_bucket_sin_disparar_flow(
        captura, monkeypatch):
    """Refactor humano-first: `enviar_formulario` YA NO dispara el Flow,
    sólo marca el bucket."""
    envios = []

    def _spy(to):
        envios.append(to)
        return "<flow sent>"

    # Si por error se intentara llamar a send_form_requisitos, esto lo
    # captura. Después de la acción, envios DEBE seguir vacío.
    monkeypatch.setattr("app.whatsapp.send_form_requisitos", _spy)

    res = panel_acciones.aplicar("57306", "enviar_formulario", actor="Fab")
    assert res["ok"] is True
    # El Flow NO se dispara desde la acción del panel.
    assert envios == []
    call = captura.llamadas[0]
    # Marca de bucket: solo setea el resultado + firma.
    assert call["updates"]["resultado_contacto"] == "enviar_formulario"
    assert call["updates"]["resultado_contacto_at"] == "NOW()"
    assert call["updates"]["resultado_contacto_por"] == "Fab"
    # No toca no_contactar ni requiere_humano (los dejó el bot si existían).
    assert "no_contactar" not in call["updates"]
    assert "requiere_humano" not in call["updates"]


def test_regresar_a_contactado_setea_resultado_contactado(captura):
    """La acción "Regresar a contactado" fuerza el bucket `contactado`
    aunque el cliente ya haya respondido (vence a la auto-derivación
    `tiene_in → respondio`). Para eso escribe `resultado_contacto=
    'contactado'` con firma humana, no NULL."""
    res = panel_acciones.aplicar("57312", "regresar_a_contactado",
                                 actor="Fab", nota="era falso positivo")
    assert res["ok"] is True
    call = captura.llamadas[0]
    # Firma humana explícita: resultado 'contactado' + metadatos.
    assert call["updates"]["resultado_contacto"] == "contactado"
    assert call["updates"]["resultado_contacto_at"] == "NOW()"
    assert call["updates"]["resultado_contacto_por"] == "Fab"
    assert call["updates"]["resultado_contacto_nota"] == "era falso positivo"
    # No toca no_contactar ni requiere_humano (del bot).
    assert "no_contactar" not in call["updates"]
    assert "requiere_humano" not in call["updates"]
    # Audit se escribe igual.
    assert call["accion_key"] == "regresar_a_contactado"


def test_acciones_modal_devuelve_6_opciones_en_orden():
    """El modal muestra sólo las 6 acciones humanas, en el orden fijado."""
    modal = panel_acciones.acciones_modal()
    assert list(modal.keys()) == [
        "regresar_a_contactado",
        "enviar_formulario",
        "sureti",
        "recontactar",
        "no_interesa",
        "broker",
    ]
    # completar_formulario_manual NO aparece en el modal (lo dispara el
    # botón verde separado).
    assert "completar_formulario_manual" not in modal


def test_completar_formulario_manual_sigue_existiendo_fuera_del_modal():
    """`completar_formulario_manual` sigue en ACCIONES (lo necesita
    panel.formulario_manual_guardar y el historial de acciones), pero
    no aparece en el modal manual."""
    assert "completar_formulario_manual" in panel_acciones.ACCIONES
    assert "completar_formulario_manual" not in panel_acciones.acciones_modal()


@pytest.mark.parametrize("accion_removida", [
    "marcar_lead_caliente",
    "levantar_no_contactar",
    "otro",
])
def test_acciones_removidas_no_existen(accion_removida):
    """Las acciones viejas fueron eliminadas con el refactor humano-first."""
    assert accion_removida not in panel_acciones.ACCIONES


def test_todas_las_acciones_tienen_label_y_descripcion():
    """Guard: no se cuela una acción sin label/descripcion que rompa el modal."""
    for key, cfg in panel_acciones.ACCIONES.items():
        assert cfg.get("label"), f"accion {key} sin label"
        assert cfg.get("descripcion"), f"accion {key} sin descripcion"
        assert isinstance(cfg.get("updates"), dict), f"accion {key} sin updates"
