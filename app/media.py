"""Descarga y almacenamiento de archivos multimedia enviados por WhatsApp.

Meta entrega un media_id; nosotros lo resolvemos a URL y descargamos el binario.
El archivo se guarda en MEDIA_DIR/{telefono}/{media_id}.{ext} y se registra
en la tabla documentos.
"""
import logging
import os
from pathlib import Path

import requests

from app import db

log = logging.getLogger("petra")

MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "/var/data/media"))
META_TOKEN = os.environ.get("META_ACCESS_TOKEN", "")
GRAPH_BASE = "https://graph.facebook.com/v18.0"

MIME_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "application/pdf": "pdf",
}


def download_and_save(telefono: str, media_id: str, mime_type: str) -> str | None:
    """
    Descarga el archivo del media_id de Meta y lo guarda localmente.
    Devuelve la ruta absoluta del archivo, o None si falla.
    """
    if not META_TOKEN:
        log.warning("[Media] META_ACCESS_TOKEN no configurado — dry-run.")
        return None

    ext = MIME_EXT.get(mime_type, "bin")
    destino = MEDIA_DIR / telefono / f"{media_id}.{ext}"
    destino.parent.mkdir(parents=True, exist_ok=True)

    if destino.exists():
        return str(destino)

    # 1. Resolver media_id → URL de descarga
    r = requests.get(
        f"{GRAPH_BASE}/{media_id}",
        headers={"Authorization": f"Bearer {META_TOKEN}"},
        timeout=10,
    )
    if not r.ok:
        log.error("[Media] No se pudo resolver media_id %s: %s", media_id, r.text[:200])
        return None

    url = r.json().get("url")
    if not url:
        log.error("[Media] Respuesta sin URL para media_id %s", media_id)
        return None

    # 2. Descargar binario
    r2 = requests.get(
        url,
        headers={"Authorization": f"Bearer {META_TOKEN}"},
        timeout=30,
        stream=True,
    )
    if not r2.ok:
        log.error("[Media] Error al descargar %s: %s", url, r2.status_code)
        return None

    with open(destino, "wb") as f:
        for chunk in r2.iter_content(chunk_size=8192):
            f.write(chunk)

    log.info("[Media] Guardado %s → %s", media_id, destino)
    return str(destino)


def registrar(telefono: str, media_id: str, mime_type: str, ruta: str | None, tipo_doc: str):
    """Persiste el documento en la tabla documentos."""
    db.registrar_documento({
        "telefono": telefono,
        "tipo": tipo_doc,
        "media_id": media_id,
        "url_storage": ruta,
        "obtenido_automaticamente": False,
    })
