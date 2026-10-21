"""Knowledge base de FAQs para inyectar en el system prompt del LLM.

Lee petra_contenidos tipo='faq' AND activo=TRUE una vez cada FAQ_TTL_SEG
y las sirve formateadas listas para pegar al system prompt.

El LLM las usa como material de referencia para responder preguntas
abiertas de las agentes (comisiones, cómo abordar al admin, etc.) en vez
de inventar la respuesta desde su propio conocimiento.
"""
import logging
import time
import threading

log = logging.getLogger("petra")

FAQ_TTL_SEG = 300  # 5 minutos — se recarga si han pasado más de 5 min
_cache = {"ts": 0.0, "texto": "", "n": 0}
_cache_lock = threading.Lock()


def _cargar_desde_db():
    """Lee las FAQs activas de petra_contenidos y arma el bloque de texto
    listo para inyectar en el system prompt. Si la DB falla, devuelve ''."""
    try:
        from app import db as _db
        with _db._conexion_directa(timeout=5) as conn:
            rows = conn.execute(
                "SELECT titulo, texto FROM petra_contenidos "
                "WHERE tipo='faq' AND activo=TRUE AND texto IS NOT NULL "
                "ORDER BY id"
            ).fetchall()
    except Exception:
        log.exception("[faq_kb] query fallo — devuelvo cache vacío")
        return "", 0
    if not rows:
        return "", 0
    partes = []
    for titulo, texto in rows:
        titulo = (titulo or "").strip()
        texto = (texto or "").strip()
        if not titulo or not texto:
            continue
        partes.append(f"P: {titulo}\nR: {texto}")
    return "\n\n".join(partes), len(partes)


def obtener_bloque_faqs():
    """Devuelve el bloque de FAQs formateado para pegar al system prompt.
    Usa cache en memoria con TTL de 5 min para no pegarle a la DB en cada
    turno. Thread-safe."""
    ahora = time.time()
    if ahora - _cache["ts"] < FAQ_TTL_SEG and _cache["texto"]:
        return _cache["texto"]
    with _cache_lock:
        # Doble check por si otro thread ya refrescó
        if ahora - _cache["ts"] < FAQ_TTL_SEG and _cache["texto"]:
            return _cache["texto"]
        texto, n = _cargar_desde_db()
        _cache["ts"] = ahora
        _cache["texto"] = texto
        _cache["n"] = n
        log.info("[faq_kb] cache refrescado: %d FAQs cargadas", n)
    return texto


def seccion_para_prompt():
    """Devuelve la sección completa (con header de instrucciones + FAQs)
    lista para pegar al final de un system prompt. Si no hay FAQs
    cargadas, devuelve string vacío."""
    bloque = obtener_bloque_faqs()
    if not bloque:
        return ""
    return (
        "\n\n═══════════════════════════════════════════════\n"
        "📚 PREGUNTAS FRECUENTES (knowledge base)\n"
        "═══════════════════════════════════════════════\n"
        "Si la agente te hace una pregunta ABIERTA (no de captura de datos "
        "ni confirmación de acción), buscá en estas FAQs una respuesta que "
        "aplique y respondé con ESO (podés adaptar el tono pero mantené el "
        "sentido). NUNCA inventes datos como comisiones, tiempos o precios "
        "si están acá. Si la pregunta NO está acá y no estás 100% seguro, "
        "decí 'déjame averiguar y te confirmo' — nunca inventes.\n\n"
        f"{bloque}"
    )
