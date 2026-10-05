"""Obtención automática de datos catastrales (Bogotá, IDECA/ArcGIS).

Función principal:
    obtener_datos_catastrales(direccion, ciudad)
        → dict con chip, avaluo_catastral, matricula, area_construida,
               area_terreno, estrato, direccion_catastral
        → None si ciudad no es Bogotá o la consulta falla

Función de compatibilidad:
    obtener_chip(direccion, ciudad) → str | None

No requiere API key. Fuente: Catastro Bogotá (IDECA), uso público.
"""
import logging
import re

import requests

log = logging.getLogger("petra")

_GEOCODER_URL = (
    "https://geocodificador.catastrobogota.gov.co/arcgis/rest/services"
    "/Geocodificador/GeocodeServer/findAddressCandidates"
)
_PREDIOS_URL = (
    "https://serviciosgis.catastrobogota.gov.co/arcgis/rest/services"
    "/catastro/predios/FeatureServer/0/query"
)
_TIMEOUT = 12
_SCORE_MIN = 70

_BOGOTA = {"bogota", "bogotá", "santa fe de bogotá", "dc", "distrito capital", "bogota dc"}

_ABREVS = [
    (r"\b(calle|cl\.?)\s*", "CL "),
    (r"\b(carrera|cra?\.?|kr\.?)\s*", "KR "),
    (r"\b(diagonal|dg\.?)\s*", "DG "),
    (r"\b(transversal|tv\.?)\s*", "TV "),
    (r"\b(avenida|av\.?)\s+calle\b", "AC "),
    (r"\b(avenida|av\.?)\s+carrera\b", "AK "),
    (r"\b(avenida|av\.?)\s*", "AV "),
    (r"\s*#\s*", " "),
    (r"\s+", " "),
]

# Todos los campos que pedimos al FeatureServer de predios.
_OUT_FIELDS = (
    "CHIP_PREDIO,CHIP,NUMERO_CHIP,"
    "MATRICULA_INMOBILIARIA,MATRICULA,"
    "AVALUO_CATASTRAL,"
    "AREA_CONSTRUIDA,AREA_TERRENO,"
    "ESTRATO,ESTRATO_PREDIO,"
    "DIRECCION"
)


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def obtener_datos_catastrales(direccion: str, ciudad: str) -> dict | None:
    """Consulta IDECA y devuelve todos los datos catastrales disponibles.

    Claves del dict devuelto (pueden ser None si IDECA no las retorna):
        chip, avaluo_catastral (int), matricula (str),
        area_construida (float), area_terreno (float),
        estrato (int), direccion_catastral (str)
    """
    if ciudad.strip().lower() not in _BOGOTA:
        log.info("[Catastro] Ciudad '%s' — sin API pública disponible.", ciudad)
        return None
    return _datos_bogota(direccion)


def obtener_chip(direccion: str, ciudad: str) -> str | None:
    """Compatibilidad hacia atrás: devuelve solo el CHIP."""
    datos = obtener_datos_catastrales(direccion, ciudad)
    return datos.get("chip") if datos else None


# ---------------------------------------------------------------------------
# Internos
# ---------------------------------------------------------------------------

def _normalizar(direccion: str) -> str:
    d = direccion.upper().strip()
    for patron, reemplazo in _ABREVS:
        d = re.sub(patron, reemplazo, d, flags=re.IGNORECASE)
    return d.strip()


def _extraer_campos(atributos: dict) -> dict:
    """Mapea los campos de ArcGIS a nuestro esquema interno."""
    result: dict = {}

    for campo in ("CHIP_PREDIO", "CHIP", "Chip", "chip", "NUMERO_CHIP"):
        val = atributos.get(campo)
        if val and str(val).strip() not in ("", "None", "null"):
            result["chip"] = str(val).strip()
            break

    for campo in ("MATRICULA_INMOBILIARIA", "MATRICULA"):
        val = atributos.get(campo)
        if val and str(val).strip() not in ("", "None", "null"):
            result["matricula"] = str(val).strip()
            break

    for campo in ("AVALUO_CATASTRAL",):
        val = atributos.get(campo)
        if val is not None:
            try:
                result["avaluo_catastral"] = int(val)
            except (ValueError, TypeError):
                pass
            break

    for campo in ("AREA_CONSTRUIDA",):
        val = atributos.get(campo)
        if val is not None:
            try:
                result["area_construida"] = float(val)
            except (ValueError, TypeError):
                pass

    for campo in ("AREA_TERRENO",):
        val = atributos.get(campo)
        if val is not None:
            try:
                result["area_terreno"] = float(val)
            except (ValueError, TypeError):
                pass

    for campo in ("ESTRATO", "ESTRATO_PREDIO"):
        val = atributos.get(campo)
        if val is not None:
            try:
                result["estrato"] = int(val)
            except (ValueError, TypeError):
                pass
            break

    for campo in ("DIRECCION", "direccion"):
        val = atributos.get(campo)
        if val and str(val).strip():
            result["direccion_catastral"] = str(val).strip()
            break

    return result


def _datos_bogota(direccion: str) -> dict | None:
    """Estrategia: geocodificador → CHIP → FeatureServer por CHIP (todos los campos).
    Fallback: FeatureServer por dirección directamente."""
    chip = _geocoder_chip(direccion)
    if chip:
        datos = _feature_server_por_chip(chip)
        if datos:
            log.info("[Catastro] Datos completos vía geocoder+FS: %s", datos)
            return datos

    datos = _feature_server_por_direccion(direccion)
    if datos:
        log.info("[Catastro] Datos completos vía FS por dirección: %s", datos)
    return datos


def _geocoder_chip(direccion: str) -> str | None:
    """Geocodificador rápido: devuelve solo el CHIP si el score es suficiente."""
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
            log.info("[Catastro] Geocoder score bajo (%.0f) para '%s'.", top.get("score", 0), normalizada)
            return None
        campos = _extraer_campos(top.get("attributes", {}))
        return campos.get("chip")
    except Exception:
        log.exception("[Catastro] Error en geocodificador para '%s'.", direccion)
        return None


def _feature_server_por_chip(chip: str) -> dict | None:
    """Consulta todos los campos del predio dado su CHIP."""
    try:
        resp = requests.get(
            _PREDIOS_URL,
            params={
                "where": f"CHIP_PREDIO='{_sql_safe(chip)}' OR CHIP='{_sql_safe(chip)}'",
                "outFields": _OUT_FIELDS,
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
        return _extraer_campos(features[0].get("attributes", {})) or None
    except Exception:
        log.exception("[Catastro] Error en FeatureServer por CHIP '%s'.", chip)
        return None


def _feature_server_por_direccion(direccion: str) -> dict | None:
    """Consulta todos los campos buscando por dirección (LIKE)."""
    normalizada = _normalizar(direccion)
    try:
        resp = requests.get(
            _PREDIOS_URL,
            params={
                "where": f"UPPER(DIRECCION) LIKE UPPER('%{_like_safe(normalizada)}%')",
                "outFields": _OUT_FIELDS,
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
        return _extraer_campos(features[0].get("attributes", {})) or None
    except Exception:
        log.exception("[Catastro] Error en FeatureServer por dirección '%s'.", direccion)
        return None


def _sql_safe(s: str) -> str:
    return s.replace("'", "''")


def _like_safe(s: str) -> str:
    return s.replace("%", r"\%").replace("_", r"\_")
