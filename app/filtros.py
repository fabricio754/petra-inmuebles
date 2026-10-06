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


# Patrones que indican que el emisor es un broker / inmobiliaria / auto-reply.
# IMPORTANTE: los patrones se aplican sobre texto normalizado (lowercase + sin
# acentos ni enies), asi que aqui tampoco deben llevar acentos ni enies.
BROKER_PATRONES = [
    r"\binmobiliaria\b",
    r"\bagencia\b",
    r"\basesor(?:a|es|as)?\b",
    r"\bbienvenid[ao]s? a\b",
    r"esta(?:s)? comunicad[ao]",
    r"indique(?:nos|me) cual es su requerimiento",
    r"muchas gracias por contactar",
    r"nuestro horario de atenc",
    r"respondemos (?:en|dentro de)",
    r"\bejecutiv[ao]s?\b.*\b(?:arriendo|venta|comercial)",
    r"\bcorredora?\b.*\b(?:inmobiliari|finca raiz)",
    r"finca raiz",
]

# Patrones que indican opt-out o rechazo claro.
# IMPORTANTE: ver nota en BROKER_PATRONES — nada de acentos ni enies.
# `\bpara\b` NO se incluye (preposicion comun, falseaba sobre texto largo);
# se usa `para de (?:escribir|mandar|molest)` para el sentido imperativo.
OPT_OUT_PATRONES = [
    r"\bstop\b",
    r"\bbaja\b",
    r"\bsalir\b",
    r"para de (?:escribir|mandar|molest)",
    r"no (?:me )?interesa",
    r"no (?:lo )?quiero",
    r"no (?:estoy|estamos) vendiendo",
    r"no (?:estoy|estamos) arrendando",
    r"no (?:lo )?vend[eo]",
    r"ya (?:se|lo) vend",
    r"ya no (?:esta disponible|lo tengo|arriendo|vendo)",
    r"soy (?:el |la )?(?:arrendador|arrendatari|inquilin)",
    r"no soy (?:el |la )?(?:duen|propietari)",
    r"\bequivocad[ao]\b",
    r"numero equivocado",
    r"no (?:conozco|se) de que",
    r"dejenme en paz",
    r"no (?:me )?molest",
    r"borra(?:r|me) de (?:la )?(?:lista|base)",
]

# Patrones que indican que el usuario quiere que lo llamen o hablar con un
# humano. IMPORTANTE: aplicados sobre texto normalizado (lowercase sin
# acentos), por eso NO llevan tildes ni enies. Por ejemplo "llámame" / "llamen"
# / "llámenme" quedan como "llamame" / "llamen" / "llamenme" tras normalizar
# y los cubre `\bllam[aeior]r\b` + `\bllamen(?:me|nos)?\b` + el match literal
# del verbo "llam" en "me pueden llam..." .
PEDIR_LLAMADA_PATRONES = [
    r"\bllam[aeior]r\b",                          # llamar, llamarme, llamarnos (post-normalizar)
    r"me (?:pueden|podrian|podrias|podria) llam",
    r"\btelefono\b",                              # pide teléfono
    r"numero (?:de |para )?(?:contact|llamar|hablar)",
    r"con qui[e]?n hablo",
    r"qui[e]?n (?:me )?(?:contesta|habla|atiende)",
    r"\batender(?:me|nos)?\b",
    r"necesito hablar",
    r"quiero (?:que me )?llamen",
    r"quiero hablar con (?:un[ao]? )?(?:asesor|humano|persona)",
    r"hablar con (?:un[ao]? )?(?:asesor|humano|persona|alguien)",
]

BROKER_REGEX = [re.compile(p) for p in BROKER_PATRONES]
OPT_OUT_REGEX = [re.compile(p) for p in OPT_OUT_PATRONES]
PEDIR_LLAMADA_REGEX = [re.compile(p) for p in PEDIR_LLAMADA_PATRONES]


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


def es_pedido_llamada(texto: str) -> bool:
    """True si el texto pide ser llamado o hablar con un humano/asesor.

    Casos reales del piloto 6-oct (lead 573508463133 que dijo tres veces "Me
    pueden llamar?" y no se escaló a humano): esta señal es la que dispara el
    routing a asesor y la alerta externa (ver `bot.handle_incoming`).
    """
    norm = _normalizar(texto)
    if not norm:
        return False
    return any(r.search(norm) for r in PEDIR_LLAMADA_REGEX)
