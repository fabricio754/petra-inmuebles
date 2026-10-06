"""Tests para el handler de media entrante en webhook_worker.

Hoy perdimos audios de un cliente porque el bot ignoraba todo tipo distinto
de text/button/flow. Estos tests cubren el nuevo flujo:

- Audio/imagen/etc. se persisten en `mensajes` con el tipo correcto y
  payload con media_id + local_path + mime.
- La descarga a disco se dispara a través de `whatsapp.descargar_media`.
- Si la descarga falla, el mensaje igual queda en DB (`local_path=None`).
- Marca `requiere_humano` y envía confirmación corta — salvo que el
  contacto esté bloqueado (`no_contactar=true`), en cuyo caso NADA.
"""
import os
from unittest.mock import patch

import pytest

from app import webhook_worker as ww


@pytest.fixture
def media_tmp(tmp_path, monkeypatch):
    """Redirige MEDIA_DIR al tmp del test — sin tocar /var/data."""
    monkeypatch.setattr(ww, "MEDIA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def db_stubs(monkeypatch):
    """Stubs de db.* usados por _procesar_media_in. Captura las llamadas en
    `log["mensajes"]` y `log["requiere_humano"]`."""
    store = {
        "mensajes": [],
        "requiere_humano": [],
        "bloqueado": set(),
        "marco_nuevo": True,
    }

    def log_mensaje(telefono, direccion, tipo, resumen, payload=None):
        store["mensajes"].append({
            "telefono": telefono,
            "direccion": direccion,
            "tipo": tipo,
            "resumen": resumen,
            "payload": payload,
        })

    def esta_bloqueado(telefono):
        return telefono in store["bloqueado"]

    def marcar_requiere_humano(telefono, motivo):
        store["requiere_humano"].append((telefono, motivo))
        return store["marco_nuevo"]

    monkeypatch.setattr(ww.db, "log_mensaje", log_mensaje)
    monkeypatch.setattr(ww.db, "esta_bloqueado", esta_bloqueado)
    monkeypatch.setattr(ww.db, "marcar_requiere_humano", marcar_requiere_humano)
    return store


@pytest.fixture
def wa_stubs(monkeypatch):
    """Captura las llamadas a whatsapp.send_text y whatsapp.descargar_media."""
    calls = {"send_text": [], "descargar_media": []}

    def send_text(to, body):
        calls["send_text"].append((to, body))
        return "<sent>"

    def descargar_media(media_id, destino):
        calls["descargar_media"].append((media_id, destino))
        with open(destino, "wb") as f:
            f.write(b"\x00\x01\x02\x03")  # 4 bytes dummy
        return {"mime_type": "audio/ogg", "file_size": 4, "sha256": "ab"}

    from app import whatsapp as _wa
    monkeypatch.setattr(_wa, "send_text", send_text)
    monkeypatch.setattr(_wa, "descargar_media", descargar_media)

    # Interceptar también la alerta de lead caliente (no debería tocar la red).
    from app import bot as _bot
    monkeypatch.setattr(_bot, "_enviar_alerta_lead_caliente", lambda p, t: calls.setdefault("alertas", []).append((p, t)))
    return calls


# ---------- Audio entrante ---------------------------------------------------

def test_audio_in_persiste_mensaje_y_marca_requiere_humano(
        media_tmp, db_stubs, wa_stubs):
    """Un audio entrante debe:
      - descargarse a MEDIA_DIR/{wa_msg_id}.ogg
      - loguearse como tipo='audio' con payload completo
      - marcar requiere_humano
      - responder confirmación corta al cliente
    """
    phone = "573001112233"
    event = {
        "type": "media",
        "media_subtipo": "audio",
        "media_id": "AUDIO123",
        "mime_type": "audio/ogg",
        "voice": True,
        "caption": "",
        "filename": "",
    }
    message = {"id": "wamid.XYZ", "type": "audio", "audio": {"id": "AUDIO123", "voice": True}}

    ww._procesar_mensaje(phone, event, message)

    # Descarga a disco en MEDIA_DIR con la extensión correcta.
    assert len(wa_stubs["descargar_media"]) == 1
    media_id, destino = wa_stubs["descargar_media"][0]
    assert media_id == "AUDIO123"
    assert destino == os.path.join(str(media_tmp), "wamid.XYZ.ogg")
    assert os.path.exists(destino)

    # Mensaje persistido con tipo audio y payload completo.
    assert len(db_stubs["mensajes"]) == 1
    row = db_stubs["mensajes"][0]
    assert row["telefono"] == phone
    assert row["direccion"] == "in"
    assert row["tipo"] == "audio"
    assert row["resumen"] == "[nota de voz]"
    payload = row["payload"]
    assert payload["wa_msg_id"] == "wamid.XYZ"
    assert payload["media_id"] == "AUDIO123"
    assert payload["mime"] == "audio/ogg"
    assert payload["local_path"] == destino
    assert payload["voice"] is True
    assert payload["size"] == 4

    # Marcar requiere_humano con un motivo que menciona el subtipo.
    assert len(db_stubs["requiere_humano"]) == 1
    tel, motivo = db_stubs["requiere_humano"][0]
    assert tel == phone
    assert "audio" in motivo

    # Confirmación al cliente.
    assert len(wa_stubs["send_text"]) == 1
    assert wa_stubs["send_text"][0][0] == phone
    assert "asesor" in wa_stubs["send_text"][0][1].lower()


# ---------- Imagen con caption ---------------------------------------------

def test_imagen_in_con_caption(media_tmp, db_stubs, wa_stubs):
    """Imagen con caption: el caption va como `resumen` y queda en payload."""
    phone = "573002223344"
    event = {
        "type": "media",
        "media_subtipo": "image",
        "media_id": "IMG1",
        "mime_type": "image/jpeg",
        "caption": "aca esta la escritura",
        "filename": "",
    }
    message = {"id": "wamid.ABC", "type": "image"}

    ww._procesar_mensaje(phone, event, message)

    row = db_stubs["mensajes"][0]
    assert row["tipo"] == "image"
    assert row["resumen"] == "aca esta la escritura"
    assert row["payload"]["caption"] == "aca esta la escritura"
    assert row["payload"]["mime"] == "image/jpeg"
    assert row["payload"]["local_path"].endswith(".jpg")

    # requiere_humano marcado + confirmación enviada.
    assert len(db_stubs["requiere_humano"]) == 1
    assert len(wa_stubs["send_text"]) == 1


# ---------- Descarga falla pero el mensaje queda ---------------------------

def test_descarga_media_falla_pero_mensaje_queda_en_db(
        media_tmp, db_stubs, wa_stubs, monkeypatch):
    """Si descargar_media lanza, el mensaje debe quedar en DB con
    local_path=None (reintento humano posible). requiere_humano + confirmación
    igual se disparan — el audio existe, solo no lo tenemos en disco."""
    phone = "573003334455"

    def _boom(media_id, destino):
        raise RuntimeError("Meta 500")

    from app import whatsapp as _wa
    monkeypatch.setattr(_wa, "descargar_media", _boom)

    event = {
        "type": "media",
        "media_subtipo": "audio",
        "media_id": "AUDIOX",
        "mime_type": "audio/ogg",
        "voice": True,
        "caption": "",
    }
    message = {"id": "wamid.FAIL", "type": "audio"}

    # No debe lanzar.
    ww._procesar_mensaje(phone, event, message)

    assert len(db_stubs["mensajes"]) == 1
    row = db_stubs["mensajes"][0]
    assert row["tipo"] == "audio"
    assert row["payload"]["media_id"] == "AUDIOX"
    assert row["payload"]["local_path"] is None
    assert row["payload"]["size"] is None

    # El flujo sigue: marcamos requiere_humano y respondemos.
    assert len(db_stubs["requiere_humano"]) == 1
    assert len(wa_stubs["send_text"]) == 1


# ---------- Bloqueado: nada se responde ------------------------------------

def test_media_de_bloqueado_no_responde(media_tmp, db_stubs, wa_stubs):
    """Si el contacto está bloqueado (no_contactar=true):
      - El mensaje se persiste igual (auditoría).
      - NO se marca requiere_humano.
      - NO se envía confirmación."""
    phone = "573004445566"
    db_stubs["bloqueado"].add(phone)

    event = {
        "type": "media",
        "media_subtipo": "audio",
        "media_id": "AUDBLOCK",
        "mime_type": "audio/ogg",
        "voice": True,
        "caption": "",
    }
    message = {"id": "wamid.BLOCK", "type": "audio"}

    ww._procesar_mensaje(phone, event, message)

    # El mensaje se persistió.
    assert len(db_stubs["mensajes"]) == 1
    assert db_stubs["mensajes"][0]["tipo"] == "audio"

    # Pero NO marcamos requiere_humano ni respondimos nada.
    assert db_stubs["requiere_humano"] == []
    assert wa_stubs["send_text"] == []


# ---------- Idempotencia alerta --------------------------------------------

def test_media_segunda_vez_no_dispara_alerta_pero_sigue_respondiendo(
        media_tmp, db_stubs, wa_stubs):
    """marcar_requiere_humano devuelve False (ya estaba marcado): no se
    dispara la alerta externa, pero sí se responde la confirmación al cliente.
    """
    phone = "573005556677"
    db_stubs["marco_nuevo"] = False  # ya estaba marcado

    event = {
        "type": "media",
        "media_subtipo": "audio",
        "media_id": "AUD2",
        "mime_type": "audio/ogg",
        "voice": False,
    }
    message = {"id": "wamid.SECOND", "type": "audio"}

    ww._procesar_mensaje(phone, event, message)

    # No se dispara alerta (marco_nuevo=False).
    assert wa_stubs.get("alertas", []) == []
    # Pero sí respondimos.
    assert len(wa_stubs["send_text"]) == 1


# ---------- Extensiones por mime -------------------------------------------

def test_extensiones_por_mime():
    assert ww._extension_para_mime("audio/ogg") == ".ogg"
    assert ww._extension_para_mime("audio/mp4") == ".m4a"
    assert ww._extension_para_mime("image/jpeg") == ".jpg"
    assert ww._extension_para_mime("video/mp4") == ".mp4"
    assert ww._extension_para_mime("application/pdf") == ".pdf"
    assert ww._extension_para_mime("image/webp") == ".webp"
    # Mime desconocido cae a .bin.
    assert ww._extension_para_mime("application/vnd.inventado") == ".bin"
    # Mime con charset es tolerado.
    assert ww._extension_para_mime("image/jpeg; charset=binary") == ".jpg"
    # Vacío / None cae a .bin.
    assert ww._extension_para_mime("") == ".bin"
    assert ww._extension_para_mime(None) == ".bin"


# ---------- Resumen humano por subtipo -------------------------------------

def test_resumen_media_por_subtipo():
    assert ww._resumen_media("audio", "", False) == "[audio]"
    assert ww._resumen_media("audio", "", True) == "[nota de voz]"
    assert ww._resumen_media("audio", "hola", True) == "hola"
    assert ww._resumen_media("image", "", False) == "[imagen]"
    assert ww._resumen_media("video", "", False) == "[video]"
    assert ww._resumen_media("sticker", "", False) == "[sticker]"
    assert ww._resumen_media("document", "", False) == "[documento]"
