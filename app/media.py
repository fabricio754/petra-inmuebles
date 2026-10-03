"""Descarga y almacenamiento de archivos multimedia recibidos por WhatsApp.

Meta no devuelve el binario directamente: primero hay que resolver la URL con
el media_id (GET /{version}/{media_id} → {"url": "..."}), y luego descargar
el binario con el mismo Bearer token.

Si no hay credenciales (DRY-RUN), download_and_save devuelve None y no toca
el disco; registrar() sigue funcionando para dejar constancia en la BD.
"""
import logging
import os
import tempfile
from pathlib import Path

import requests

log = logging.getLogger("petra")

_DEFAULT_MEDIA = os.path.join(tempfile.gettempdir(), "massi_media")
MEDIA_DIR = os.environ.get("MEDIA_DIR", _DEFAULT_MEDIA)

MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "application/pdf": ".pdf",
}
MIME_SOPORTADOS = set(MIME_EXT.keys())


def download_and_save(phone: str, media_id: str, mime_type: str) -> str | None:
    """Descarga el archivo de Meta y lo guarda en disco.

    Retorna la ruta local o None (DRY-RUN / error).
    """
    from app.whatsapp import GRAPH_API_VERSION, ACCESS_TOKEN
    if not ACCESS_TOKEN:
        log.info("[Media DRY-RUN] media_id=%s mime=%s", media_id, mime_type)
        return None

    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    try:
        # Paso 1: resolver URL del binario
        meta = requests.get(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/{media_id}",
            headers=headers,
            timeout=15,
        )
        meta.raise_for_status()
        url = meta.json().get("url")
        if not url:
            log.error("[Media] Sin URL para media_id=%s", media_id)
            return None

        # Paso 2: descargar el binario
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()

        # Paso 3: guardar en disco
        ext = MIME_EXT.get(mime_type, ".bin")
        directorio = Path(MEDIA_DIR) / phone
        directorio.mkdir(parents=True, exist_ok=True)
        ruta = directorio / f"{media_id}{ext}"
        ruta.write_bytes(resp.content)

        log.info("[Media] Guardado: %s (%d bytes)", ruta, len(resp.content))
        return str(ruta)

    except Exception:
        log.exception("[Media] Error descargando media_id=%s", media_id)
        return None


def registrar(phone: str, media_id: str, mime_type: str,
               ruta_local: str | None, tipo: str = "documento") -> None:
    """Registra el documento en la tabla documentos."""
    from app import db
    try:
        db.registrar_documento(phone, tipo, media_id, ruta_local)
    except Exception:
        log.exception("[Media] Error registrando documento en BD.")
