"""Extrae datos del recibo de predial (y otros documentos catastrales).

Estrategia:
  1. pdfplumber con 4 patrones de regex para PDFs con texto embebido.
  2. Si el PDF es escaneado o viene como imagen, Claude Haiku vision como fallback.
Devuelve {"avaluo": int, "direccion": str, "matricula": str} (campos pueden ser None).
"""
import base64
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("petra")

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Patrones para avalúo catastral en texto del predial bogotano.
# Los formatos encontrados: "123.456.789", "123,456,789", "123456789", "$123.456.789"
_PATRONES_AVALUO = [
    re.compile(r"val[uú]o\s+catastral[^\d]{0,30}([\d]{1,3}(?:[.,]\d{3})+)", re.I),
    re.compile(r"avaluo[^\d]{0,30}\$?\s*([\d]{1,3}(?:[.,]\d{3})+)", re.I),
    re.compile(r"avaluo[^\d]{0,30}([\d]{7,12})", re.I),
    re.compile(r"([\d]{1,3}(?:\.\d{3})+)\s*(?:pesos|cop)?[^\n]{0,30}avaluo", re.I),
]
_PATRON_DIRECCION = re.compile(
    r"(?:direcci[oó]n|predio|ubicaci[oó]n)[^\n]{0,20}:\s*([^\n]{5,80})", re.I
)
_PATRON_MATRICULA = re.compile(r"matr[íi]cula\s+inmobiliaria[^\d]{0,20}([\d\-]{5,20})", re.I)


def _limpiar_numero(s: str) -> int | None:
    s = s.replace(".", "").replace(",", "")
    return int(s) if s.isdigit() else None


def _extraer_texto_pdf(ruta: str) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(ruta) as pdf:
            return "\n".join(
                (p.extract_text() or "") for p in pdf.pages
            )
    except Exception as e:
        log.warning("[Parser] pdfplumber falló en %s: %s", ruta, e)
        return ""


def _parsear_texto(texto: str) -> dict:
    avaluo = None
    for pat in _PATRONES_AVALUO:
        m = pat.search(texto)
        if m:
            avaluo = _limpiar_numero(m.group(1))
            if avaluo:
                break

    direccion = None
    m = _PATRON_DIRECCION.search(texto)
    if m:
        direccion = m.group(1).strip()

    matricula = None
    m = _PATRON_MATRICULA.search(texto)
    if m:
        matricula = m.group(1).strip()

    return {"avaluo": avaluo, "direccion": direccion, "matricula": matricula}


def _parsear_con_vision(ruta: str) -> dict:
    if not ANTHROPIC_KEY:
        log.info("[Parser] Sin ANTHROPIC_API_KEY — omitiendo fallback visión.")
        return {"avaluo": None, "direccion": None, "matricula": None}

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

        p = Path(ruta)
        if p.suffix.lower() == ".pdf":
            # Rasterizar primera página con pdfplumber
            try:
                import pdfplumber
                with pdfplumber.open(ruta) as pdf:
                    img = pdf.pages[0].to_image(resolution=150)
                    tmp = str(p.with_suffix(".tmp.png"))
                    img.save(tmp)
                    img_path = tmp
            except Exception:
                return {"avaluo": None, "direccion": None, "matricula": None}
        else:
            img_path = ruta

        with open(img_path, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode()

        ext = Path(img_path).suffix.lower().lstrip(".")
        media_type = "image/png" if ext == "png" else "image/jpeg"

        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": data},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Este es un recibo de predial colombiano. Extrae estos tres valores "
                            "y responde SOLO con JSON: "
                            "{\"avaluo\": <número entero sin puntos ni comas>, "
                            "\"direccion\": \"<dirección del predio>\", "
                            "\"matricula\": \"<matrícula inmobiliaria>\"}. "
                            "Si no encuentras algún campo usa null."
                        ),
                    },
                ],
            }],
        )

        import json
        texto = resp.content[0].text.strip()
        # Extraer JSON aunque haya texto extra
        m = re.search(r"\{.*\}", texto, re.S)
        if m:
            return json.loads(m.group())
    except Exception as e:
        log.warning("[Parser] Fallback visión falló: %s", e)

    return {"avaluo": None, "direccion": None, "matricula": None}


def parsear_predial(ruta: str) -> dict:
    """
    Parsea un documento de predial (PDF o imagen).
    Devuelve dict con claves: avaluo (int), direccion (str), matricula (str).
    """
    texto = _extraer_texto_pdf(ruta) if ruta.endswith(".pdf") else ""

    if texto.strip():
        resultado = _parsear_texto(texto)
        if resultado.get("avaluo"):
            return resultado

    # Fallback: visión
    log.info("[Parser] Sin texto o sin avalúo en %s — intentando visión.", ruta)
    return _parsear_con_vision(ruta)
