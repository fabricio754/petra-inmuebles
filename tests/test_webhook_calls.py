"""Tests para el procesamiento de `calls` del webhook de Meta.

Cuando el cliente abre el chat de Massi en WhatsApp y toca el icono de
llamada, Meta manda un payload con:
  {
    "entry": [{
      "changes": [{
        "value": {
          "calls": [
            {"id": "wacid.ABC", "from": "573001112233",
             "event": "connect", "timestamp": "1760000000"}
          ]
        }
      }]
    }]
  }

Verificamos que `_invocar_logica`:
- Dispara una alerta WA al asesor cuando `event=connect`.
- Para otros eventos (ej. `terminate`) NO alerta, solo loguea.
- Persiste un row sintetico en `mensajes` con tipo='call_in'.
- No rompe cuando el payload no trae `calls[]`.
"""
from unittest.mock import MagicMock

import pytest

from app import webhook_worker


@pytest.fixture
def spy_log_mensaje(monkeypatch):
    spy = MagicMock()
    monkeypatch.setattr(webhook_worker.db, "log_mensaje", spy)
    return spy


@pytest.fixture
def spy_send_text(monkeypatch):
    """Interceptamos `send_text` del modulo whatsapp (importado dinamicamente
    dentro de `_procesar_llamada_entrante`)."""
    from app import whatsapp as wa
    spy = MagicMock()
    monkeypatch.setattr(wa, "send_text", spy)
    return spy


@pytest.fixture
def env_alertas(monkeypatch):
    """Configura las env vars tipicas para que la alerta se dispare."""
    monkeypatch.setenv("ALERTAS_WHATSAPP", "573009990000,573009990001")
    monkeypatch.setenv("PANEL_URL", "https://panel.example.com")
    monkeypatch.setenv("PANEL_TOKEN", "tok-xyz")


def test_webhook_procesa_call_connect_notifica_asesor(
    env_alertas, spy_log_mensaje, spy_send_text
):
    """Un payload con `calls[]` y event='connect' dispara alerta WA a
    cada asesor en `ALERTAS_WHATSAPP`, con link al panel del contacto."""
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "calls": [
                        {"id": "wacid.ABC",
                         "from": "573001112233",
                         "event": "connect",
                         "timestamp": "1760000000"}
                    ]
                }
            }]
        }]
    }
    webhook_worker._invocar_logica(payload)

    # Dos asesores → dos envios.
    assert spy_send_text.call_count == 2
    destinos = {c.args[0] for c in spy_send_text.call_args_list}
    assert destinos == {"573009990000", "573009990001"}

    # Mensaje debe incluir el telefono del cliente y el link al panel con token.
    for call in spy_send_text.call_args_list:
        mensaje = call.args[1]
        assert "573001112233" in mensaje
        assert "https://panel.example.com/panel/573001112233?token=tok-xyz" in mensaje


def test_webhook_procesa_call_terminate_solo_loguea(
    env_alertas, spy_log_mensaje, spy_send_text
):
    """Un payload con `calls[]` y event='terminate' NO dispara alerta WA
    (solo loguea y persiste). Solo `connect` alerta."""
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "calls": [
                        {"id": "wacid.XYZ",
                         "from": "573001112233",
                         "event": "terminate",
                         "timestamp": "1760000005"}
                    ]
                }
            }]
        }]
    }
    webhook_worker._invocar_logica(payload)
    spy_send_text.assert_not_called()
    # Pero igual persiste la row sintetica para que aparezca en el chat.
    assert spy_log_mensaje.called


def test_webhook_sin_calls_no_rompe(monkeypatch, spy_log_mensaje):
    """Un payload sin `calls[]` (solo messages/statuses) no rompe el
    procesamiento normal."""
    # Neutralizamos branches de messages/statuses para aislar la rama de calls.
    monkeypatch.setattr(webhook_worker, "_procesar_status", lambda s: None)
    monkeypatch.setattr(webhook_worker, "_procesar_mensaje",
                        lambda phone, event, message=None: None)
    import app.server as _server
    monkeypatch.setattr(_server, "_to_event",
                        lambda m: {"type": "text", "text": "hola"})

    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "statuses": [
                        {"id": "wamid.A", "status": "delivered",
                         "timestamp": "1760000000"}
                    ],
                    "messages": [
                        {"from": "573001112233", "id": "wamid.IN",
                         "type": "text", "text": {"body": "hola"}}
                    ]
                    # Sin `calls`.
                }
            }]
        }]
    }
    # No lanza — el payload sin `calls[]` se procesa normalmente.
    webhook_worker._invocar_logica(payload)


def test_webhook_calls_persiste_mensaje_sintetico(
    env_alertas, spy_log_mensaje, spy_send_text
):
    """Una llamada entrante persiste un row sintetico en `mensajes` con
    direccion='in', tipo='call_in' y payload con call_id + evento."""
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "calls": [
                        {"id": "wacid.ABC",
                         "from": "573001112233",
                         "event": "connect",
                         "timestamp": "1760000000"}
                    ]
                }
            }]
        }]
    }
    webhook_worker._invocar_logica(payload)

    spy_log_mensaje.assert_called_once()
    args = spy_log_mensaje.call_args.args
    # Signature: log_mensaje(telefono, direccion, tipo, resumen, payload)
    assert args[0] == "573001112233"
    assert args[1] == "in"
    assert args[2] == "call_in"
    assert "connect" in args[3]
    assert args[4] == {"call_id": "wacid.ABC", "evento": "connect"}
