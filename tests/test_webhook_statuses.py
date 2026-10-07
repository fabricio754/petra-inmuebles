"""Tests para el procesamiento de `statuses` del webhook de Meta.

Meta manda, en un payload tipico:
  {
    "entry": [{
      "changes": [{
        "value": {
          "statuses": [
            {"id": "wamid.ABC", "status": "delivered", "timestamp": "1760000000",
             "recipient_id": "573001112233"},
            {"id": "wamid.ABC", "status": "read", "timestamp": "1760000005",
             "recipient_id": "573001112233"}
          ]
        }
      }]
    }]
  }

Verificamos que `_invocar_logica` extrae esos statuses y llama a
`db.actualizar_estado_entrega` para cada uno con los args correctos.
"""
from unittest.mock import MagicMock

import pytest

from app import webhook_worker


@pytest.fixture
def spy_actualizar(monkeypatch):
    spy = MagicMock()
    monkeypatch.setattr(webhook_worker.db, "actualizar_estado_entrega", spy)
    return spy


def test_webhook_procesa_un_status_delivered(spy_actualizar, monkeypatch):
    """Un payload con un status 'delivered' llama al UPDATE con los args
    correctos (wa_msg_id, estado, timestamp, error_text=None)."""
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "statuses": [
                        {"id": "wamid.ABC", "status": "delivered",
                         "timestamp": "1760000000",
                         "recipient_id": "573001112233"}
                    ]
                }
            }]
        }]
    }
    webhook_worker._invocar_logica(payload)
    spy_actualizar.assert_called_once_with(
        "wamid.ABC", "delivered", "1760000000", None,
    )


def test_webhook_procesa_varios_statuses_en_un_payload(spy_actualizar):
    """Un payload con sent + delivered + read llama 3 veces."""
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "statuses": [
                        {"id": "wamid.ABC", "status": "sent",
                         "timestamp": "1760000000"},
                        {"id": "wamid.ABC", "status": "delivered",
                         "timestamp": "1760000003"},
                        {"id": "wamid.ABC", "status": "read",
                         "timestamp": "1760000010"},
                    ]
                }
            }]
        }]
    }
    webhook_worker._invocar_logica(payload)
    assert spy_actualizar.call_count == 3
    estados = [c.args[1] for c in spy_actualizar.call_args_list]
    assert estados == ["sent", "delivered", "read"]


def test_webhook_procesa_status_failed_con_error(spy_actualizar):
    """Un status failed con `errors[]` ensambla el texto de error."""
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "statuses": [
                        {"id": "wamid.FAIL", "status": "failed",
                         "timestamp": "1760000000",
                         "errors": [{
                             "code": 131047,
                             "title": "Re-engagement message",
                             "message": "Fuera de la ventana de 24h"
                         }]}
                    ]
                }
            }]
        }]
    }
    webhook_worker._invocar_logica(payload)
    spy_actualizar.assert_called_once()
    args = spy_actualizar.call_args.args
    assert args[0] == "wamid.FAIL"
    assert args[1] == "failed"
    assert "Re-engagement message" in args[3]
    assert "Fuera de la ventana" in args[3]


def test_webhook_sin_statuses_no_llama_al_update(spy_actualizar):
    """Un payload de mensajes IN (sin `statuses`) no toca
    actualizar_estado_entrega. Y no debe romper si no hay nada."""
    payload = {
        "entry": [{
            "changes": [{
                "value": {}  # ni statuses ni messages
            }]
        }]
    }
    webhook_worker._invocar_logica(payload)
    spy_actualizar.assert_not_called()


def test_webhook_status_sin_id_o_estado_se_ignora(spy_actualizar):
    """Un item defectuoso (sin id o sin status) no debe llamar al UPDATE."""
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "statuses": [
                        {"status": "delivered"},        # sin id
                        {"id": "wamid.XYZ"},            # sin status
                        {"id": "wamid.OK", "status": "sent", "timestamp": "1760000000"},
                    ]
                }
            }]
        }]
    }
    webhook_worker._invocar_logica(payload)
    assert spy_actualizar.call_count == 1
    assert spy_actualizar.call_args.args[0] == "wamid.OK"


def test_webhook_statuses_coexisten_con_messages(spy_actualizar, monkeypatch):
    """Un payload con statuses + messages procesa ambos: los statuses
    emiten UPDATE y los messages siguen el flujo normal del bot."""
    # El flujo de messages termina llamando a _procesar_mensaje; lo
    # neutralizamos para que no toque DB real.
    monkeypatch.setattr(webhook_worker, "_procesar_mensaje",
                        lambda phone, event, message=None: None)
    # _to_event: aceptamos la import dinamica.
    import app.server as _server
    monkeypatch.setattr(_server, "_to_event",
                        lambda m: {"type": "text", "text": "hola"})

    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "statuses": [
                        {"id": "wamid.ABC", "status": "delivered",
                         "timestamp": "1760000000"},
                    ],
                    "messages": [
                        {"from": "573001112233", "id": "wamid.IN",
                         "type": "text", "text": {"body": "hola"}}
                    ]
                }
            }]
        }]
    }
    webhook_worker._invocar_logica(payload)
    spy_actualizar.assert_called_once()
    assert spy_actualizar.call_args.args[0] == "wamid.ABC"
