"""Parser del texto prellenado que trae el QR del edificio.

Formato esperado (ejemplos):
    "Estoy en el edificio Prados 1 en Zipaquira. Cod: 2530"
    "Estoy en el edificio Arrayanes en Bogota. Cod: 1001"

La única pieza *obligatoria* es el marker `Cod:\\s*(\\w+)` — con eso
asociamos al visitante a un `conjuntos_visitante`. Edificio y ciudad
son extras: si no se pueden parsear, caemos a None y después el router
los resuelve consultando la DB con el código.

API:
    parsear_qr(texto) -> dict | None
        {"codigo": "2530", "edificio": "Prados 1", "ciudad": "Zipaquira"}
"""
import re

# Case-insensitive. Permite "Cod:", "COD :", "cod  :", etc.
_RE_COD = re.compile(r"\bcod\s*:\s*([A-Za-z0-9_-]+)", re.IGNORECASE)

# "edificio <X> en <Y>." — tolerante a mayúsculas, a mas de un espacio y
# a que la ciudad termine con punto, coma o fin de línea.
_RE_EDIFICIO = re.compile(
    r"edificio\s+(?P<edificio>.+?)\s+en\s+(?P<ciudad>[^.,\n]+)",
    re.IGNORECASE,
)

# Palabra mágica para pruebas internas: simula un QR del edificio
# conjuntos_visitante.codigo=1001 (Arrayanes). Útil para probar el flow
# visitante sin tener un QR físico. Debe ser el único texto del mensaje
# (ignorando espacios y signos) para evitar falsos positivos.
_RE_DEMO = re.compile(r"^\s*like\s*[!.?]*\s*$", re.IGNORECASE)
_DEMO_CODIGO = "1001"


def parsear_qr(texto):
    """Devuelve dict con `codigo`, `edificio`, `ciudad` o None si no hay
    marker `Cod:` en el texto. edificio/ciudad pueden ser None si el
    texto no sigue el formato completo."""
    if not texto:
        return None
    if _RE_DEMO.match(texto):
        return {"codigo": _DEMO_CODIGO, "edificio": None, "ciudad": None}
    m_cod = _RE_COD.search(texto)
    if not m_cod:
        return None
    codigo = (m_cod.group(1) or "").strip()
    if not codigo:
        return None
    edificio = None
    ciudad = None
    m_ed = _RE_EDIFICIO.search(texto)
    if m_ed:
        edificio = (m_ed.group("edificio") or "").strip() or None
        ciudad = (m_ed.group("ciudad") or "").strip() or None
    return {"codigo": codigo, "edificio": edificio, "ciudad": ciudad}


def tiene_marker_qr(texto):
    """Chequeo barato: ¿este texto parece venir de un QR? Mira el marker
    `Cod:` o la palabra mágica de prueba `like`. Se usa en `es_visitante()`
    para rutear sin parsear el resto."""
    if not texto:
        return False
    if _RE_DEMO.match(texto):
        return True
    return bool(_RE_COD.search(texto))
