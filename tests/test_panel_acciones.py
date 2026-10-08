"""Tests para el módulo app.panel_acciones.

Cubre la lógica pura (mapping accion → updates, validaciones) sin tocar
Postgres: `db.aplicar_accion_panel` se mockea con un capture simple.
"""
from unittest.mock import patch

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


def test_aplicar_levantar_no_contactar_no_cambia_resultado(captura):
    """Levantar no_contactar es una corrección de opt-out: preserva el
    historial de resultado_contacto previo (no setea `resultado_contacto`)."""
    res = panel_acciones.aplicar("57302", "levantar_no_contactar",
                                 actor="Fab")
    assert res["ok"] is True
    call = captura.llamadas[0]
    assert call["updates"] == {"no_contactar": False}
    # No debe aparecer ningún campo de resultado.
    assert "resultado_contacto" not in call["updates"]
    assert "resultado_contacto_at" not in call["updates"]
    assert "resultado_contacto_por" not in call["updates"]


def test_aplicar_accion_desconocida_retorna_error(captura):
    res = panel_acciones.aplicar("57303", "no_existe_este_key")
    assert res == {"ok": False, "error": "accion_desconocida"}
    assert captura.llamadas == []  # nada se persiste


def test_aplicar_otro_requiere_nota(captura):
    """La acción "otro" sin nota no tiene valor para el audit — se rechaza."""
    res = panel_acciones.aplicar("57304", "otro", nota="", actor="Fab")
    assert res == {"ok": False, "error": "nota_requerida"}
    assert captura.llamadas == []


def test_aplicar_otro_con_nota_ok(captura):
    res = panel_acciones.aplicar("57305", "otro",
                                 nota="pide hablar en 2 semanas",
                                 actor="Fab")
    assert res["ok"] is True
    call = captura.llamadas[0]
    assert call["updates"]["resultado_contacto"] == "otro"
    assert call["updates"]["resultado_contacto_nota"] == "pide hablar en 2 semanas"


def test_aplicar_enviar_formulario_destraba_flags_y_dispara_flow(captura, monkeypatch):
    """`enviar_formulario` destraba no_contactar/requiere_humano, dispara el
    Flow 1 al cliente y marca resultado_contacto='enviar_formulario'."""
    envios = []
    monkeypatch.setattr(
        "app.whatsapp.send_form_requisitos",
        lambda to: envios.append(to) or "<flow sent>",
    )
    res = panel_acciones.aplicar("57306", "enviar_formulario", actor="Fab")
    assert res["ok"] is True
    # El Flow 1 se disparó al cliente.
    assert envios == ["57306"]
    call = captura.llamadas[0]
    assert call["updates"]["no_contactar"] is False
    assert call["updates"]["requiere_humano"] is False
    assert call["updates"]["resultado_contacto"] == "enviar_formulario"


def test_aplicar_enviar_formulario_falla_envio_no_toca_flags(captura, monkeypatch):
    """Si el send del Flow falla, no se aplican flags ni se inserta audit."""
    def _boom(to):
        raise RuntimeError("API meta down")

    monkeypatch.setattr("app.whatsapp.send_form_requisitos", _boom)
    res = panel_acciones.aplicar("57309", "enviar_formulario", actor="Fab")
    assert res == {"ok": False, "error": "envio_flow_fallo"}
    assert captura.llamadas == []  # nada se persiste si falla el envío


def test_aplicar_marcar_lead_caliente_fuerza_requiere_humano(captura):
    res = panel_acciones.aplicar("57307", "marcar_lead_caliente", actor="Fab")
    assert res["ok"] is True
    call = captura.llamadas[0]
    assert call["updates"]["requiere_humano"] is True
    assert call["updates"]["resultado_contacto"] == "lead_caliente"


def test_aplicar_marcar_lead_caliente_setea_resultado(captura):
    """El fix: `marcar_lead_caliente` también debe setear resultado_contacto
    para que la columna 'Resultado' del panel no quede en '— pendiente —'."""
    res = panel_acciones.aplicar("57308", "marcar_lead_caliente",
                                 nota="pidió llamar ya", actor="Massi")
    assert res["ok"] is True
    call = captura.llamadas[0]
    assert call["updates"]["resultado_contacto"] == "lead_caliente"
    assert call["updates"]["resultado_contacto_at"] == "NOW()"
    assert call["updates"]["resultado_contacto_por"] == "Massi"
    assert call["updates"]["resultado_contacto_nota"] == "pidió llamar ya"


def test_todas_las_acciones_tienen_label_y_descripcion():
    """Guard: no se cuela una acción sin label/descripcion que rompa el modal."""
    for key, cfg in panel_acciones.ACCIONES.items():
        assert cfg.get("label"), f"accion {key} sin label"
        assert cfg.get("descripcion"), f"accion {key} sin descripcion"
        assert isinstance(cfg.get("updates"), dict), f"accion {key} sin updates"
