"""Obtención automática de documentos sin pedirle nada al vendedor.

Flujo por inmueble:
  1. obtener_chip(direccion, ciudad)   → CHIP catastral (solo Bogotá por ahora)
  2. descargar_ctl(chip)               → PDF de Certificado de Tradición y Libertad (SNR)
  3. descargar_predial(dir, ced, city) → PDF/imagen del recibo de predial

El CHIP de Bogotá viene de la API pública de catastro (ArcGIS IDECA).
El CTL viene de certificados.supernotariado.gov.co (Playwright + CAPTCHA matemático simple).
El predial de Bogotá viene de shd.gov.co (Playwright).
Otras ciudades: TODO a medida que se expanda cobertura.
"""
import logging
import os
import re
import unicodedata
from pathlib import Path

import requests

log = logging.getLogger("petra")

MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "/var/data/media"))

# === ArcGIS IDECA — catastro Bogotá =========================================

_ARCGIS_URL = (
    "https://serviciosgis.catastrobogota.gov.co"
    "/arcgis/rest/services/catastro/lote/MapServer/3/query"
)


def _normalizar_dir(d: str) -> str:
    """
    Convierte "Calle 100 # 15-20" → "CL 100 15 20" (formato catastral).
    Solo aplica a Bogotá; otras ciudades devuelven tal cual.
    """
    d = unicodedata.normalize("NFD", d.lower())
    d = "".join(c for c in d if unicodedata.category(c) != "Mn")
    d = re.sub(r"[#\-]", " ", d)
    replacements = [
        (r"\bcalle\b", "CL"), (r"\bcarrera\b", "CR"), (r"\bavenida calle\b", "AC"),
        (r"\bavenida carrera\b", "AK"), (r"\bdiagonal\b", "DG"), (r"\btransversal\b", "TV"),
        (r"\bcl\b", "CL"), (r"\bcr\b", "CR"), (r"\bkr\b", "CR"),
    ]
    d = d.upper()
    for pat, rep in replacements:
        d = re.sub(pat, rep, d, flags=re.I)
    d = re.sub(r"\s+", " ", d).strip()
    return d


def obtener_chip(direccion: str, ciudad: str) -> str | None:
    """
    Busca el CHIP catastral por dirección.
    Solo implementado para Bogotá. Otras ciudades devuelven None.
    Retorna el CHIP (str) o None si no se encuentra.
    """
    if "bogot" not in ciudad.lower():
        return None

    dir_norm = _normalizar_dir(direccion)

    def _consultar(where_clause):
        try:
            r = requests.get(
                _ARCGIS_URL,
                params={
                    "where": where_clause,
                    "outFields": "PRECHIP,PREDIRECC,PRENUPRE",
                    "f": "json",
                    "returnGeometry": "false",
                },
                timeout=15,
            )
            r.raise_for_status()
            features = r.json().get("features", [])
            if not features:
                return None
            # Tomar el primer resultado con score suficiente
            attr = features[0]["attributes"]
            return attr.get("PRECHIP") or attr.get("PRENUPRE")
        except Exception as e:
            log.warning("[DocsAuto] ArcGIS error: %s", e)
            return None

    # Intento 1: LIKE exacto normalizado
    chip = _consultar(f"PREDIRECC LIKE '%{dir_norm}%'")
    if chip:
        return chip

    # Intento 2: solo el segmento de vía (primeras palabras)
    partes = dir_norm.split()[:3]
    if len(partes) >= 2:
        chip = _consultar(f"PREDIRECC LIKE '%{' '.join(partes)}%'")

    return chip


# === SNR — Certificado de Tradición y Libertad ==============================

_SNR_URL = "https://certificados.supernotariado.gov.co"


def descargar_ctl(chip: str, destino_dir: Path) -> str | None:
    """
    Descarga el CTL desde el SNR usando Playwright.
    Requiere SURETI_EMAIL / SURETI_PASSWORD o SNR_USER / SNR_PASSWORD.

    ⚠️  Los selectores son estimados — verificar en primera ejecución real.

    Retorna la ruta del PDF descargado, o None si falla.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("[DocsAuto] playwright no instalado.")
        return None

    snr_user = os.environ.get("SNR_USER", "")
    snr_pass = os.environ.get("SNR_PASS", "")
    if not snr_user or not snr_pass:
        log.warning("[DocsAuto] SNR_USER / SNR_PASS no configurados — omitiendo CTL.")
        return None

    destino_dir.mkdir(parents=True, exist_ok=True)
    ruta_pdf = destino_dir / f"ctl_{chip}.pdf"
    if ruta_pdf.exists():
        return str(ruta_pdf)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page()
        try:
            page.goto(f"{_SNR_URL}/certificados/", timeout=30_000)
            page.wait_for_load_state("networkidle")

            # ⚠️ Selectores estimados — verificar con producción
            page.fill("#usuario", snr_user)
            page.fill("#contrasena", snr_pass)

            # CAPTCHA matemático simple: "¿Cuánto es 3 + 5?"
            cap_text = page.inner_text(".captcha-pregunta, #captcha-text, .question").strip()
            m = re.search(r"(\d+)\s*\+\s*(\d+)", cap_text)
            if m:
                page.fill("#captcha-respuesta, #captchaAnswer", str(int(m.group(1)) + int(m.group(2))))

            page.click("button[type='submit'], #btnIngresar, input[value='Ingresar']")
            page.wait_for_load_state("networkidle")

            # Buscar por CHIP/número predial
            page.fill("#chipInput, #numeroPredial, [name='chip']", chip)
            page.click("#btnBuscar, button:has-text('Buscar'), [value='Buscar']")
            page.wait_for_load_state("networkidle")

            # Descargar PDF
            with page.expect_download() as dl:
                page.click(".descargar-ctl, a:has-text('Descargar'), button:has-text('PDF')")
            download = dl.value
            download.save_as(str(ruta_pdf))
            log.info("[DocsAuto] CTL descargado → %s", ruta_pdf)
            return str(ruta_pdf)

        except Exception as e:
            log.error("[DocsAuto] Error descargando CTL para CHIP %s: %s", chip, e)
            return None
        finally:
            browser.close()


# === Predial Bogotá — SHD ===================================================

def descargar_predial(direccion: str, cedula: str, ciudad: str, destino_dir: Path) -> str | None:
    """
    Descarga el recibo de predial desde el portal municipal.
    Actualmente solo implementado para Bogotá (shd.gov.co).

    ⚠️  Los selectores son estimados — verificar en primera ejecución real.

    Retorna la ruta del archivo descargado, o None si falla.
    """
    if "bogot" not in ciudad.lower():
        log.info("[DocsAuto] Predial solo para Bogotá por ahora (ciudad: %s).", ciudad)
        return None

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("[DocsAuto] playwright no instalado.")
        return None

    destino_dir.mkdir(parents=True, exist_ok=True)
    ruta = destino_dir / f"predial_{cedula}.pdf"
    if ruta.exists():
        return str(ruta)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page()
        try:
            # Portal SHD Bogotá
            page.goto("https://shd.gov.co/predial", timeout=30_000)
            page.wait_for_load_state("networkidle")

            # ⚠️ Selectores estimados — verificar
            page.fill("[name='cedula'], #cedula, #identificacion", cedula)
            page.fill("[name='direccion'], #direccion", direccion[:80])
            page.click("button[type='submit'], #btnConsultar, button:has-text('Consultar')")
            page.wait_for_load_state("networkidle")

            # Intentar descargar recibo
            with page.expect_download(timeout=30_000) as dl:
                page.click(".btn-predial, a:has-text('Recibo'), button:has-text('Descargar')")
            download = dl.value
            download.save_as(str(ruta))
            log.info("[DocsAuto] Predial descargado → %s", ruta)
            return str(ruta)

        except Exception as e:
            log.error("[DocsAuto] Error descargando predial (cedula %s): %s", cedula, e)
            return None
        finally:
            browser.close()


def obtener_docs_automaticos(telefono: str, direccion: str, cedula: str, ciudad: str) -> dict:
    """
    Función principal: obtiene CHIP + CTL + predial para un lead.
    Retorna dict con rutas (puede tener None en campos que no se consiguieron).
    """
    from app import db

    destino = MEDIA_DIR / telefono

    chip = obtener_chip(direccion, ciudad)
    if chip:
        db.update_chip(telefono, chip)
        log.info("[DocsAuto] CHIP para %s: %s", telefono, chip)

    ctl = None
    if chip:
        ctl = descargar_ctl(chip, destino)

    predial = descargar_predial(direccion, cedula, ciudad, destino)

    return {
        "chip": chip,
        "ctl": ctl,
        "predial": predial,
    }
