"""Helper de textos editables desde panel.

Lee `petra_textos` con cache in-memory TTL 60s. Si la clave no existe,
cae al `fallback` hardcodeado (así nunca rompe el flow en producción).

Uso:
    from app.petra_agents.textos import t
    msg = t("autorizacion.msg_bienvenida", nombre=nombre, conjunto=cj)

Convención de claves (dot-notation):
    <categoria>.<nombre>
Ejemplos:
    inscripcion.pregunta_nombre
    capacitacion.msg_dia_completo
    autorizacion.msg_material_cta
"""
import logging
import threading
import time

log = logging.getLogger("petra")

# cache = {clave: (contenido, expira_at)}
_CACHE = {}
_CACHE_TTL = 60  # segundos
_CACHE_LOCK = threading.Lock()


def _leer_db(clave):
    """Devuelve el contenido guardado para `clave`, o None si no existe."""
    try:
        from app import db as _db
        with _db._conexion_directa(timeout=5) as conn:
            row = conn.execute(
                "SELECT contenido FROM petra_textos WHERE clave=%s",
                (clave,),
            ).fetchone()
        return row[0] if row else None
    except Exception:
        log.exception("[textos] lectura DB fallo clave=%s", clave)
        return None


def _get_contenido(clave, fallback):
    """Devuelve contenido de DB (con cache) o fallback. Nunca lanza."""
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(clave)
        if cached and cached[1] > now:
            return cached[0] if cached[0] is not None else fallback
    # Cache miss / expirado
    val = _leer_db(clave)
    with _CACHE_LOCK:
        _CACHE[clave] = (val, now + _CACHE_TTL)
    return val if val is not None else fallback


def t(clave, fallback=None, **variables):
    """Renderiza el texto bajo `clave`, con variables de reemplazo.

    - clave: dot-notation (ej. "autorizacion.msg_cta")
    - fallback: texto por defecto si la clave no está en DB
    - variables: substituye {var} en el contenido

    Si DB falla, usa fallback. Si fallback es None y DB no tiene la clave,
    devuelve `[FALTA TEXTO: <clave>]` (visible para que el humano se dé cuenta).
    """
    contenido = _get_contenido(clave, fallback)
    if contenido is None:
        return f"[FALTA TEXTO: {clave}]"
    try:
        return contenido.format(**variables) if variables else contenido
    except (KeyError, IndexError) as e:
        # Variable faltante — devolvemos el contenido crudo y logueamos
        log.warning("[textos] variable faltante en %s: %s", clave, e)
        return contenido


def invalidar_cache(clave=None):
    """Fuerza re-lectura de DB. clave=None → limpia todo."""
    with _CACHE_LOCK:
        if clave is None:
            _CACHE.clear()
        else:
            _CACHE.pop(clave, None)


def listar_textos():
    """Lee todos los textos de DB, con metadata. Usado por el panel."""
    try:
        from app import db as _db
        with _db._conexion_directa(timeout=10) as conn:
            rows = conn.execute(
                "SELECT clave, contenido, descripcion, variables, categoria, "
                "updated_at, updated_by "
                "FROM petra_textos ORDER BY categoria, clave"
            ).fetchall()
        return [
            {
                "clave": r[0], "contenido": r[1], "descripcion": r[2],
                "variables": list(r[3] or []), "categoria": r[4],
                "updated_at": r[5], "updated_by": r[6],
            }
            for r in rows
        ]
    except Exception:
        log.exception("[textos] listar fallo")
        return []


def upsert_texto(clave, contenido, descripcion=None, variables=None,
                 categoria="otro", updated_by=None):
    """Crea o actualiza un texto. Invalida el cache de esa clave."""
    try:
        from app import db as _db
        with _db._conexion_directa(timeout=10) as conn:
            conn.execute(
                "INSERT INTO petra_textos "
                "(clave, contenido, descripcion, variables, categoria, "
                " updated_at, updated_by) "
                "VALUES (%s, %s, %s, %s, %s, NOW(), %s) "
                "ON CONFLICT (clave) DO UPDATE SET "
                "  contenido = EXCLUDED.contenido, "
                "  descripcion = COALESCE(EXCLUDED.descripcion, petra_textos.descripcion), "
                "  variables = EXCLUDED.variables, "
                "  categoria = EXCLUDED.categoria, "
                "  updated_at = NOW(), "
                "  updated_by = EXCLUDED.updated_by",
                (clave, contenido, descripcion, variables or [], categoria,
                 updated_by),
            )
        invalidar_cache(clave)
        return True
    except Exception:
        log.exception("[textos] upsert fallo clave=%s", clave)
        return False


def eliminar_texto(clave):
    """Borra una clave. El flow volverá a usar su fallback hardcodeado."""
    try:
        from app import db as _db
        with _db._conexion_directa(timeout=10) as conn:
            conn.execute("DELETE FROM petra_textos WHERE clave=%s", (clave,))
        invalidar_cache(clave)
        return True
    except Exception:
        log.exception("[textos] eliminar fallo clave=%s", clave)
        return False


# =============================================================================
# SEED — baseline con los textos que hoy están hardcodeados en el flow.
# Esto permite al humano editarlos desde el panel sin tocar código.
# Se ejecuta on-demand (botón en panel) o al desplegar (idempotente).
# =============================================================================

SEED_TEXTOS = [
    # ─── Inscripción ───
    ("inscripcion.pregunta_nombre",
     "👋 ¡Qué alegría que quieras ser Agente Petra!\n\n"
     "Te voy a pedir 5 datos para registrarte. Empecemos por el primero:\n\n"
     "*¿Cuál es tu nombre completo?*",
     "Primer mensaje del onboarding — pide el nombre.",
     [], "inscripcion"),
    ("inscripcion.pregunta_cedula",
     "✅ Gracias, {nombre}.\n\n*¿Cuál es tu cédula?* (solo los números)",
     "Pide cédula tras recibir nombre.", ["nombre"], "inscripcion"),
    ("inscripcion.pregunta_ciudad",
     "Perfecto. *¿En qué ciudad vivís?*",
     "Pide ciudad.", [], "inscripcion"),
    ("inscripcion.pregunta_barrio",
     "*¿En qué barrio?*",
     "Pide barrio.", [], "inscripcion"),
    ("inscripcion.pregunta_conjunto",
     "Último dato — *¿Cómo se llama el conjunto o edificio donde vivís?*",
     "Pide nombre del conjunto.", [], "inscripcion"),

    # ─── Capacitación (completada) ───
    ("capacitacion.msg_completada_header",
     "🎓🎉 *¡Terminaste tu capacitación completa!*\n\n"
     "Ya tenés las herramientas para arrancar como Agente Petra 💜\n\n"
     "📍 *Siguiente paso: Autorización del conjunto* 🏢\n\n"
     "Ahora toca hablar con el administrador de tu edificio para "
     "que te autorice a representar a Petra ahí 👇",
     "Mensaje de cierre de capacitación. Se envía antes de los botones de "
     "material/autorización.", [], "capacitacion"),

    # ─── Autorización ───
    ("autorizacion.cta_botones",
     "Preparé material de apoyo para que lo lleves a la reunión 💜",
     "Texto que acompaña los 3 botones [Ver material][Tengo duda][Ya tengo "
     "autorización].", [], "autorizacion"),
    ("autorizacion.msg_envio_material",
     "Perfecto 💜 Acá te paso el material de apoyo para que "
     "lo lleves a la reunión con el administrador:",
     "Mensaje al pulsar 'Ver material', antes de enviar los contenidos.",
     [], "autorizacion"),
    ("autorizacion.msg_recordatorio_video",
     "Antes de ir con el administrador, repasá este video de "
     "capacitación donde te expliqué cómo abordarlo 💜:",
     "Introducción al video recordatorio (si está cargado en Contenidos "
     "como tipo 'video_recordatorio_autorizacion').",
     [], "autorizacion"),
    ("autorizacion.msg_post_material",
     "Cuando tengas la autorización del administrador, pulsá el botón 👇",
     "Texto que acompaña los botones después de enviar el material.",
     [], "autorizacion"),
    ("autorizacion.msg_prompt_duda",
     "¡Claro! 💜 Escribime tu pregunta y te respondo acá mismo.",
     "Respuesta al botón '❓ Tengo una duda'.", [], "autorizacion"),
    ("autorizacion.msg_confirmada",
     "🎉 *¡Buenísimo!*\n\n"
     "📍 *Siguiente paso: Activar el QR* 📬\n\n"
     "Te vamos a enviar un kit con el QR y un regalito a la "
     "dirección de tu edificio. Te confirmamos acá cuando "
     "esté en camino.\n\n"
     "Mientras tanto, podés ir preparando: identificar "
     "propietarios interesados en tu conjunto, pensar qué "
     "inmuebles podrían salir, etc. 💜",
     "Mensaje al confirmar que ya tiene autorización. Pasa a kit_pendiente.",
     [], "autorizacion"),

    # ─── Fallbacks genéricos ───
    ("fallback.llm_error",
     "Perdón, no te pude responder bien ahora. Un asesor te "
     "contacta pronto 💜",
     "Mensaje de corte cuando la llamada al LLM falla.",
     [], "fallback"),
]


def seed_textos(forzar_sobrescribir=False):
    """Inserta los textos baseline. Idempotente: por defecto NO sobrescribe
    los que ya existan (para no perder ediciones del humano).
    forzar_sobrescribir=True → reemplaza todo.
    Devuelve (insertados, actualizados, omitidos)."""
    try:
        from app import db as _db
        insertados = actualizados = omitidos = 0
        with _db._conexion_directa(timeout=30) as conn:
            for clave, contenido, descripcion, variables, categoria in SEED_TEXTOS:
                existente = conn.execute(
                    "SELECT 1 FROM petra_textos WHERE clave=%s", (clave,),
                ).fetchone()
                if existente and not forzar_sobrescribir:
                    omitidos += 1
                    continue
                if existente:
                    conn.execute(
                        "UPDATE petra_textos SET contenido=%s, descripcion=%s, "
                        "variables=%s, categoria=%s, updated_at=NOW(), "
                        "updated_by='seed' WHERE clave=%s",
                        (contenido, descripcion, variables, categoria, clave),
                    )
                    actualizados += 1
                else:
                    conn.execute(
                        "INSERT INTO petra_textos "
                        "(clave, contenido, descripcion, variables, categoria, "
                        " updated_by) VALUES (%s, %s, %s, %s, %s, 'seed')",
                        (clave, contenido, descripcion, variables, categoria),
                    )
                    insertados += 1
        invalidar_cache()
        return insertados, actualizados, omitidos
    except Exception:
        log.exception("[textos] seed fallo")
        return 0, 0, 0
