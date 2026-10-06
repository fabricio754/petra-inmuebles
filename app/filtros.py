"""Filtros de calidad de lista para Massi.

Dos clasificadores por regex:
- `es_broker(texto)`: el emisor parece una inmobiliaria / agencia / asesor /
  auto-reply de agencia. Usado para descartar anuncios al scrapear y para
  silenciar conversaciones entrantes que son chat corporativo, no un dueño.
- `es_opt_out(texto)`: el texto expresa rechazo o pedido de no contactar.
  Reemplaza el matching por conjunto fijo de palabras (STOP/BAJA/SALIR/PARA)
  por un conjunto de patrones semanticos.

Las listas son editables; cada patron es un regex insensible a mayusculas y
acentos (el texto se normaliza antes de aplicarlos).
"""
import re
import unicodedata


def _normalizar(texto: str) -> str:
    """Lowercase + sin acentos. Pensado para comparar texto libre."""
    if not texto:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(texto).lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


# Patrones que indican que el emisor es un broker / inmobiliaria / auto-reply
BROKER_PATRONES = [
    r"\binmobiliaria\b",
    r"\bagencia\b",
    r"\basesor(?:a|es|as)?\b",
    r"\bbienvenid[ao]s? a\b",
    r"esta(?:s)? comunicad[ao]",
    r"indique(?:nos|me) cu[aá]l es su requerimiento",
    r"muchas gracias por contactar",
    r"nuestro horario de atenc",
    r"respondemos (?:en|dentro de)",
    r"\bejecutiv[ao]s?\b.*\b(?:arriendo|venta|comercial)",
    r"\bcorredora?\b.*\b(?:inmobiliari|finca ra[ií]z)",
    r"finca ra[ií]z",
]

# Patrones que indican opt-out o rechazo claro
OPT_OUT_PATRONES = [
    r"\bstop\b",
    r"\bbaja\b",
    r"\bsalir\b",
    r"\bpara\b",
    r"no (?:me )?interesa",
    r"no (?:lo )?quiero",
    r"no (?:estoy|estamos) vendiendo",
    r"no (?:estoy|estamos) arrendando",
    r"no (?:lo )?vend[eo]",
    r"ya (?:se|lo) vend",
    r"ya no (?:est[aá] disponible|lo tengo|arriendo|vendo)",
    r"soy (?:el |la )?(?:arrendador|arrendatari|inquilin)",
    r"no soy (?:el |la )?(?:dueñ|propietari)",
    r"\bequivocad[ao]\b",
    r"n[uú]mero equivocado",
    r"no (?:conozco|s[eé]) de qu[eé]",
    r"d[eé]jenm[eé] en paz",
    r"no (?:me )?molest",
    r"borra(?:r|me) de (?:la )?(?:lista|base)",
]

BROKER_REGEX = [re.compile(p) for p in BROKER_PATRONES]
OPT_OUT_REGEX = [re.compile(p) for p in OPT_OUT_PATRONES]


def es_broker(texto: str) -> bool:
    """True si el texto parece venir de un broker/inmobiliaria/auto-reply."""
    norm = _normalizar(texto)
    if not norm:
        return False
    return any(r.search(norm) for r in BROKER_REGEX)


def es_opt_out(texto: str) -> bool:
    """True si el texto expresa opt-out / rechazo claro."""
    norm = _normalizar(texto)
    if not norm:
        return False
    return any(r.search(norm) for r in OPT_OUT_REGEX)
