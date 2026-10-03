"""Obtención automática de datos catastrales.

obtener_chip(direccion, ciudad)
  → str  si ciudad es Bogotá y ArcGIS responde con un candidato válido
  → None para cualquier otra ciudad (no existe equivalente público) o si falla

El CHIP (Código Homologado de Inmueble y Predio) se consulta al geocodificador
público de Catastro Bogotá (IDECA). No requiere API key.
"""
import logging
import re

import requests

log = logging.getLogger("petra")

# Geocodificador de Catastro Bogotá (IDECA) — público, sin auth.
_GEOCODER_URL = (
    "https://geocodificador.catastrobogota.gov.co/arcgis/rest/services"
    "/Geocodificador/GeocodeServer/findAddressCandidates"
)
# Fallback: consulta directa a la capa de predios por dirección normalizada.
_PREDIOS_URL = (
    "https://serviciosgis.catastrobogota.gov.co/arcgis/rest/services"
    "/catastro/predios/FeatureServer/0/query"
)
_TIMEOUT = 12
_SCORE_MIN = 70  # confianza mínima del geocodificador (0-100)

# Todos los aliases que el bot usa para "Bogotá".
_BOGOTA = {"bogota", "bogotá", "santa fe de bogotá", "dc", "distrito capital", "bogota dc"}

# Abreviaciones del catastro bogotano.
_ABREVS = [
    (r"\b(calle|cl\.?)\s*", "CL "),
    (r"\b(carrera|cra?\.?|kr\.?)\s*", "KR "),
    (r"\b(diagonal|dg\.?)\s*", "DG "),
    (r"\b(transversal|tv\.?)\s*", "TV "),
    (r"\b(avenida|av\.?)\s+calle\b", "AC "),
    (r"\b(avenida|av\.?)\s+carrera\b", "AK "),
    (r"\b(avenida|av\.?)\s*", "AV "),
    (r"\s*#\s*", " "),       # elimina el símbolo #
    (r"\s+", " "),           # colapsa espacios
]


def obtener_chip(direccion: str, ciudad: str) -> str | None:
    """Devuelve el CHIP catastral o None.

    Para ciudades distintas a Bogotá retorna None de inmediato: no hay
    equivalente ArcGIS público para Medellín, Barranquilla, etc.
    """
    if ciudad.strip().lower() not in _BOGOTA:
        log.info("[CHIP] Ciudad '%s' — sin consulta ArcGIS disponible; continúa sin CHIP.", ciudad)
        return None
    return _chip_bogota(direccion)


# ---------------------------------------------------------------------------
# Internos
# ---------------------------------------------------------------------------

def _normalizar(direccion: str) -> str:
    """Convierte abreviaciones al formato que espera el geocodificador."""
    d = direccion.upper().strip()
    for patron, reemplazo in _ABREVS:
        d = re.sub(patron, reemplazo, d, flags=re.IGNORECASE)
    return d.strip()


def _extraer_chip(atributos: dict) -> str | None:
    """Busca el campo CHIP en las claves que puede devolver ArcGIS."""
    for campo in ("CHIP_PREDIO", "CHIP", "Chip", "chip", "NUMERO_CHIP", "numero_chip"):
        val = atributos.get(campo)
        if val and str(val).strip() not in ("", "None", "null"):
            return str(val).strip()
    return None


def _chip_bogota(direccion: str) -> str | None:
    """Primer intento: geocodificador. Segundo: FeatureServer."""
    chip = _geocoder(direccion)
    if chip:
        return chip
    return _feature_server(direccion)


def _geocoder(direccion: str) -> str | None:
    normalizada = _normalizar(direccion)
    try:
        resp = requests.get(
            _GEOCODER_URL,
            params={
                "SingleLine": normalizada,
                "outFields": "CHIP_PREDIO,CHIP,NUMERO_CHIP",
                "returnGeometry": "false",
                "maxLocations": 1,
                "f": "json",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        candidates = resp.json().get("candidates", [])
        if not candidates:
            return None
        top = candidates[0]
        if top.get("score", 0) < _SCORE_MIN:
            log.info("[CHIP] Geocoder: score bajo (%.0f) para '%s'.", top.get("score", 0), normalizada)
            return None
        chip = _extraer_chip(top.get("attributes", {}))
        if chip:
            log.info("[CHIP] Geocoder OK: %s → %s", normalizada, chip)
        return chip
    except Exception:
        log.exception("[CHIP] Error consultando geocodificador para '%s'.", direccion)
        return None


def _feature_server(direccion: str) -> str | None:
    normalizada = _normalizar(direccion)
    try:
        resp = requests.get(
            _PREDIOS_URL,
            params={
                "where": f"UPPER(DIRECCION) LIKE UPPER('%{_like_safe(normalizada)}%')",
                "outFields": "CHIP_PREDIO,CHIP,DIRECCION",
                "returnGeometry": "false",
                "resultRecordCount": 1,
                "f": "json",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        features = resp.json().get("features", [])
        if not features:
            return None
        chip = _extraer_chip(features[0].get("attributes", {}))
        if chip:
            log.info("[CHIP] FeatureServer OK: %s → %s", normalizada, chip)
        return chip
    except Exception:
        log.exception("[CHIP] Error consultando FeatureServer para '%s'.", direccion)
        return None


def _like_safe(s: str) -> str:
    """Escapa % y _ para evitar inyección en cláusula LIKE."""
    return s.replace("%", r"\%").replace("_", r"\_")
