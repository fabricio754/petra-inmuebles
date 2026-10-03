"""Extractor de datos del recibo de predial colombiano.

parsear_predial(ruta, mime_type) → {"avaluo": int|None, "direccion": str, "matricula": str}

Estrategia (en orden, para no gastar API innecesariamente):
  1. Si es PDF: extraer texto con pdfplumber.
  2. Sobre el texto, probar múltiples regex para avalúo, dirección y matrícula.
  3. Si el texto está vacío (PDF escaneado) o es imagen: llamar a Claude vision.
"""
import base64
import json
import logging
import os
import re

log = logging.getLogger("petra")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Haiku: rápido y económico para extracción de texto estructurado.
_CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# ---------------------------------------------------------------------------
# Patrones regex (orden de especificidad descendente)
# ---------------------------------------------------------------------------

# Avalúo catastral — maneja los tres formatos del enunciado:
#   "$125,340,000"  (coma como miles)
#   "125.340.000"   (punto como miles)
#   "125340000"     (sin separador)
_AVALUO_PATS = [
    # Formato 1: comas como miles → $125,340,000
    r"aval[uú]o\s+catastral\s*[:\$]?\s*\$?\s*([\d]{1,3}(?:,[\d]{3})+)",
    # Formato 2: puntos como miles → 125.340.000
    r"aval[uú]o\s+catastral\s*[:\$]?\s*\$?\s*([\d]{1,3}(?:\.[\d]{3})+)",
    # Formato 3: número plano → 125340000
    r"aval[uú]o\s+catastral\s*[:\$]?\s*(\d{6,})",
    # Relaxado: solo "avalúo:" (sin "catastral")
    r"aval[uú]o\s*[:\$]?\s*\$?\s*([\d]{1,3}(?:[,.][\d]{3})+|\d{6,})",
]

_DIREC_PATS = [
    r"direcci[oó]n\s+del\s+(?:predio|inmueble)\s*[:.]?\s*(.+?)(?:\n|$)",
    r"direcci[oó]n\s*[:.]?\s*(.+?)(?:\n|$)",
    r"ubicaci[oó]n\s*[:.]?\s*(.+?)(?:\n|$)",
]

_MATRIC_PATS = [
    r"matr[ií]cula\s+inmobiliaria\s*[:.]?\s*([\dA-Z]+-[\d]+)",
    r"matr[ií]cula\s*[:.]?\s*([\dA-Z]+-[\d]+)",
    r"no\.?\s+matr[ií]cula\s*[:.]?\s*([\dA-Z]+-[\d]+)",
]


def parsear_predial(ruta: str, mime_type: str) -> dict:
    """Extrae avaluo, direccion y matricula del documento dado."""
    resultado = {"avaluo": None, "direccion": "", "matricula": ""}

    if mime_type == "application/pdf":
        texto = _texto_pdf(ruta)
        if texto.strip():
            resultado = _regex_sobre_texto(texto)
            if resultado["avaluo"]:
                log.info("[Parser] PDF texto OK → avaluo=%s", resultado["avaluo"])
                return resultado
            log.info("[Parser] PDF sin avalúo en regex — prueba con Claude vision.")

    # Fallback: Claude vision (imagen o PDF escaneado)
    if not ANTHROPIC_API_KEY:
        log.info("[Parser] Sin ANTHROPIC_API_KEY — se omite visión.")
        return resultado
    return _claude_vision(ruta, mime_type) or resultado


# ---------------------------------------------------------------------------
# Paso 1: extracción de texto con pdfplumber
# ---------------------------------------------------------------------------

def _texto_pdf(ruta: str) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(ruta) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        log.exception("[Parser] pdfplumber falló para %s", ruta)
        return ""


# ---------------------------------------------------------------------------
# Paso 2: regex sobre texto extraído
# ---------------------------------------------------------------------------

def _regex_sobre_texto(texto: str) -> dict:
    flags = re.IGNORECASE | re.UNICODE
    return {
        "avaluo": _extraer_avaluo(texto, flags),
        "direccion": _primer_match(texto, _DIREC_PATS, flags),
        "matricula": _primer_match(texto, _MATRIC_PATS, flags),
    }


def _extraer_avaluo(texto: str, flags: int) -> int | None:
    for pat in _AVALUO_PATS:
        m = re.search(pat, texto, flags)
        if m:
            try:
                return _parse_numero(m.group(1))
            except ValueError:
                continue
    return None


def _primer_match(texto: str, patrones: list[str], flags: int) -> str:
    for pat in patrones:
        m = re.search(pat, texto, flags)
        if m:
            return m.group(1).strip()
    return ""


def _parse_numero(s: str) -> int:
    """Convierte '125,340,000' o '125.340.000' o '125340000' a int."""
    s = s.strip().replace("$", "").replace(" ", "")
    # Determina separador dominante
    comas = s.count(",")
    puntos = s.count(".")
    if comas > 0 and puntos == 0:
        # Comas como miles: 125,340,000
        return int(s.replace(",", ""))
    if puntos > 0 and comas == 0:
        # Puntos como miles: 125.340.000
        return int(s.replace(".", ""))
    # Sin separadores o mixto (poco probable)
    return int(re.sub(r"[^\d]", "", s))


# ---------------------------------------------------------------------------
# Paso 3: Claude vision
# ---------------------------------------------------------------------------

_PROMPT_VISION = (
    "Eres un asistente experto en documentos catastrales colombianos. "
    "Del documento adjunto extrae estos tres campos:\n"
    "  1. avaluo: El avalúo catastral en pesos (entero, sin puntos ni comas)\n"
    "  2. direccion: La dirección del inmueble\n"
    "  3. matricula: El número de matrícula inmobiliaria\n\n"
    "Responde SOLO con un JSON válido, sin texto adicional:\n"
    '{"avaluo": 125340000, "direccion": "CL 100 15 20", "matricula": "50N-12345"}\n'
    "Si un campo no aparece, usa null."
)


def _claude_vision(ruta: str, mime_type: str) -> dict | None:
    try:
        import anthropic
        cliente = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        with open(ruta, "rb") as f:
            datos = base64.standard_b64encode(f.read()).decode("utf-8")

        if mime_type == "application/pdf":
            contenido_doc = {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": datos},
            }
        else:
            contenido_doc = {
                "type": "image",
                "source": {"type": "base64", "media_type": mime_type, "data": datos},
            }

        resp = cliente.messages.create(
            model=_CLAUDE_MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": [contenido_doc, {"type": "text", "text": _PROMPT_VISION}]}],
        )
        raw = resp.content[0].text.strip()
        # Extraer JSON aunque venga con texto extra
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            log.warning("[Parser] Claude no devolvió JSON: %s", raw[:200])
            return None
        parsed = json.loads(m.group(0))
        avaluo_raw = parsed.get("avaluo")
        avaluo = int(avaluo_raw) if avaluo_raw is not None else None
        log.info("[Parser] Claude vision OK → avaluo=%s", avaluo)
        return {
            "avaluo": avaluo,
            "direccion": str(parsed.get("direccion") or ""),
            "matricula": str(parsed.get("matricula") or ""),
        }
    except Exception:
        log.exception("[Parser] Error en Claude vision para %s", ruta)
        return None
