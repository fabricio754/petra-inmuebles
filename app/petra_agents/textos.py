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
    """Devuelve el contenido guardado para `clave`, o None si no existe.
    Si el texto está marcado activo=FALSE, devuelve '' (string vacío)."""
    try:
        from app import db as _db
        with _db._conexion_directa(timeout=5) as conn:
            row = conn.execute(
                "SELECT contenido, activo FROM petra_textos WHERE clave=%s",
                (clave,),
            ).fetchone()
        if not row:
            return None
        contenido, activo = row[0], row[1]
        if activo is False:
            return ""  # ocultado desde panel — se saltea
        return contenido
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


def boton(id_boton, titulo_default):
    """Devuelve el título editable de un botón, con fallback al hardcode.
    Convención: cada botón tiene una clave `boton.<id_lowercase>` en
    petra_textos. El id del botón (ej. ONBAG_LISTO) sigue hardcoded en
    el código porque los handlers del flow dependen de él, pero el
    título visible lo editás desde el panel.
    WhatsApp limita a 20 caracteres el título."""
    clave = f"boton.{id_boton.lower()}"
    return t(clave, fallback=titulo_default)[:20]


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
                "updated_at, updated_by, activo "
                "FROM petra_textos ORDER BY categoria, clave"
            ).fetchall()
        return [
            {
                "clave": r[0], "contenido": r[1], "descripcion": r[2],
                "variables": list(r[3] or []), "categoria": r[4],
                "updated_at": r[5], "updated_by": r[6],
                "activo": (r[7] if r[7] is not None else True),
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


def set_activo(clave, activo):
    """Marca una clave como activa (bot la envía) o inactiva (la saltea).
    Más seguro que `eliminar_texto` para "ocultar" un mensaje, porque la
    config (categoría, variables) queda guardada para re-activar."""
    try:
        from app import db as _db
        with _db._conexion_directa(timeout=10) as conn:
            conn.execute(
                "UPDATE petra_textos SET activo=%s, updated_at=NOW() WHERE clave=%s",
                (bool(activo), clave),
            )
        invalidar_cache(clave)
        return True
    except Exception:
        log.exception("[textos] set_activo fallo clave=%s", clave)
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

    # ─── Inscripción ───
    ("inscripcion.msg_felicitaciones",
     "🎉 *¡Felicitaciones {nombre_corto}, ya sos oficialmente Agente Petra!*\n\n"
     "Vas a representar a Petra en *{conjunto}*, {barrio}, {ciudad}.",
     "Mensaje al confirmar REGISTRARME. Variables: nombre_corto, conjunto, barrio, ciudad.",
     ["nombre_corto", "conjunto", "barrio", "ciudad"], "inscripcion"),

    # ─── Capacitación ───
    ("capacitacion.msg_completada_header",
     "🎓🎉 *¡Terminaste tu capacitación completa!*\n\n"
     "Ya tenés las herramientas para arrancar como Agente Petra 💜\n\n"
     "📍 *Siguiente paso: Autorización del conjunto* 🏢\n\n"
     "Ahora toca hablar con el administrador de tu edificio para "
     "que te autorice a representar a Petra ahí.\n\n"
     "Pulsá el botón para arrancar 👇",
     "Mensaje de cierre de capacitación. Acompaña al botón [🚀 Iniciar] "
     "en el mismo mensaje.", [], "capacitacion"),
    ("capacitacion.msg_cron_diario",
     "🎬✨ *¡Nuevo día de capacitación!*\n\n"
     "Acá te dejo los 2 videos del día *{dia_a_enviar} de {max_dia}* 💜\n\n"
     "Mirálos con calma, cuando puedas — ni muy rápido ni muy lento. "
     "Aprendé a tu ritmo, pausá si lo necesitás, y volvé cuando estés lista.\n\n"
     "Cada video que ves te acerca un paso más a ser una Agente Petra "
     "experta en tu edificio 🏢💪\n\n"
     "Cuando los termines, apretá el botón 👇",
     "Mensaje del cron de las 8am que acompaña los 2 videos del día. "
     "Variables: dia_a_enviar, max_dia.",
     ["dia_a_enviar", "max_dia"], "capacitacion"),
    ("capacitacion.msg_dia_completo",
     "🎉 *¡Felicitaciones, cerraste el día {dia_actual} de {dias_total}!* 🌟\n\n"
     "Ya viste *{vistos}/{total} videos* ✅ Cada uno te suma herramientas, "
     "confianza y conocimiento para arrancar como Agente Petra en tu edificio 🏢💜\n\n"
     "Te quedan solo *{quedan} videos ({dias_restantes} días)* para completar "
     "toda tu capacitación. Vas re bien, dale con todo 💪\n\n"
     "Mañana a las *8am* te mando los próximos 2 videos, para que no pierdas "
     "el ritmo. O si tenés ganas de seguir avanzando ahora mismo, apretá el botón 👇",
     "Mensaje al marcar LISTO en un día de capacitación (si quedan días). "
     "Variables: dia_actual, dias_total, vistos, total, quedan, dias_restantes.",
     ["dia_actual", "dias_total", "vistos", "total", "quedan", "dias_restantes"],
     "capacitacion"),
    ("capacitacion.msg_arranca_manana",
     "¡Buenísimo! 🎉\n\nTu capacitación arranca mañana a las "
     "*8am* con los primeros 2 videos. Y así durante 7 días.\n\n"
     "Nos vemos mañana. 👋",
     "Mensaje al pulsar Empezar ahora cuando el día 1 aún no se puede "
     "enviar (fallback). También se usa en la bienvenida v2.",
     [], "capacitacion"),
    ("capacitacion.msg_reenvio_videos",
     "¡Claro! Acá los tenés de nuevo 💜 — videos del día *{dia_actual_env}/{dias_total}*:",
     "Mensaje al reenviar los videos del día por palabra clave. "
     "Variables: dia_actual_env, dias_total.",
     ["dia_actual_env", "dias_total"], "capacitacion"),
    ("capacitacion.msg_siguiente_dia_fallo",
     "Hmm, no pude mandarte el siguiente par ahora. "
     "Mañana a las 8am te llega sin falta. 🙏",
     "Mensaje cuando se pulsa 'Ver día siguiente' pero no se pudo enviar.",
     [], "capacitacion"),
    ("capacitacion.msg_por_hoy_ya",
     "Por hoy ya está. Mañana a las 8am te mando el próximo. 👋",
     "Mensaje cuando pide 'siguiente' pero ya no hay más por hoy.",
     [], "capacitacion"),
    ("capacitacion.msg_recordatorio_empezar",
     "Cuando termines de ver los videos, pulsá el botón para avanzar 🎬",
     "Mensaje con el botón Empezar ahora cuando la agente no reacciona.",
     [], "capacitacion"),

    # ─── Autorización ───
    ("autorizacion.cta_iniciar",
     "📍 *Siguiente paso:* pulsá el botón para arrancar con la gestión de la "
     "autorización 💜",
     "Mensaje al terminar capacitación. Un solo botón [🚀 Iniciar].",
     [], "autorizacion"),
    ("autorizacion.cta_botones",
     "Preparé material de apoyo para que lo lleves a la reunión 💜",
     "Texto que acompaña los 3 botones [Ver material][Tengo duda][Ya tengo "
     "autorización]. Se envía cuando pulsa 🚀 Iniciar.",
     [], "autorizacion"),
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

    # ─── Activación (kit + QR) ───
    ("activacion.msg_pedir_direccion_inicio",
     "🎉 *¡Buenísimo! Ya tenés la autorización del administrador.*\n\n"
     "📦 *Siguiente paso: te enviamos tu kit* (QR + un regalito 💜)\n\n"
     "Necesito tu dirección exacta para enviártelo. Empecemos por lo primero:\n\n"
     "*¿En qué torre vivís?* (ej. Torre 3, Bloque A, etc. Si no aplica, respondé *N/A*)",
     "Mensaje al confirmar autorización. Arranca captura de dirección.",
     [], "activacion"),
    ("activacion.pregunta_torre",
     "*¿En qué torre vivís?* (ej. Torre 3, Bloque A, etc. Si no aplica, respondé *N/A*)",
     "Pregunta por torre del edificio.", [], "activacion"),
    ("activacion.pregunta_apto",
     "Perfecto. *¿Cuál es tu apartamento?* (ej. 702, Apto 15B)",
     "Pregunta por número de apartamento.", [], "activacion"),
    ("activacion.pregunta_barrio",
     "¿En qué *barrio* está tu edificio?",
     "Pregunta por barrio.", [], "activacion"),
    ("activacion.pregunta_ciudad",
     "Último dato — *¿En qué ciudad?*",
     "Pregunta por ciudad.", [], "activacion"),
    ("activacion.msg_direccion_completa",
     "🎉 *¡Dirección confirmada!* 📍\n\n"
     "Ya pedimos tu kit de activación. En *2-3 días hábiles* lo recibís:\n\n"
     "📦 Tu QR personalizado para pegar en el edificio\n"
     "💜 Un regalito Petra para arrancar\n\n"
     "Te aviso acá apenas salga en camino 🚚\n\n"
     "Mientras tanto, podés ir contándoles a los vecinos que vos vas "
     "a ser la Agente Petra del conjunto 🏢✨",
     "Mensaje al terminar captura de dirección. Espera envío manual del kit.",
     [], "activacion"),
    ("activacion.msg_kit_enviado",
     "📦 *¡Tu kit ya está en camino!*\n\n"
     "Dentro vas a encontrar tu QR + un regalito 💜. "
     "Cuando lo recibas, instalá el QR en la cartelera del conjunto o donde "
     "lo autorizó el administrador.\n\n"
     "Te paso un video rápido con el paso a paso 👇",
     "Mensaje al marcar kit como enviado desde panel. Antes del video 'cómo usar QR'.",
     [], "activacion"),
    ("activacion.msg_qr_boton",
     "Cuando lo tengas instalado, pulsá el botón 👇",
     "Mensaje que acompaña el botón 'Ya instalé el QR'.", [], "activacion"),
    ("activacion.msg_foto_libro_recibida",
     "📸 *Acabas de enviar la foto. Decime:*\n\n"
     "¿Ya cargaste *todas* las fotos del libro?\n\n"
     "Apretá *Sí* y cargamos tu QR 🚀\n"
     "Apretá *No* si todavía te faltan 💜",
     "Mensaje que recibe la agente justo después de mandar una foto en "
     "el paso foto_libro_pedida — confirmación con botones Sí/No.",
     [], "activacion"),
    ("activacion.msg_libro_intro",
     "📸 *Ahora necesitamos una foto del libro de arriendos y ventas* "
     "de tu conjunto.\n\n"
     "Pasá por la *portería o el lobby* y pedí ver el libro. Tomale una "
     "foto bien clara y mandámela por acá 💜\n\n"
     "✨ Con eso vamos a *cargar tu QR con la info de tu conjunto*, para "
     "que cuando lo instales ya funcione completo 🚀",
     "Mensaje intro al paso del libro — se envía ANTES del msg con "
     "botones Sí/No, para explicar que la agente tiene que ir al lobby.",
     [], "activacion"),
    ("activacion.msg_qr_instalado",
     "🎉 *¡Felicitaciones! Ya sos una Agente Petra activa.* 💜\n\n"
     "Tu QR está en tu edificio y los residentes ya pueden escanearlo "
     "para contactarte. A partir de ahora te vamos a avisar cada vez "
     "que llegue un interesado 📲\n\n"
     "¡Mucho éxito! 🚀",
     "Mensaje final al confirmar QR instalado. Pasa a estado 'activa'.",
     [], "activacion"),

    # ─── Citas ───
    ("citas.msg_intro_video",
     "Antes de que empiecen a llegar interesados, mirá este video "
     "corto donde te explico cómo manejar las citas 💜 👇",
     "Intro al video de citas, se envía 1 sola vez al pasar a estado=activa.",
     [], "citas"),
    ("citas.msg_nueva_cita",
     "📅 *¡Nueva cita!*\n\n"
     "👤 *Interesado:* {interesado}\n"
     "🏠 *Inmueble:* {inmueble}\n"
     "🕒 *Propuesta:* {cuando}\n"
     "\n¿Podés atenderla?",
     "Mensaje que recibe la agente cuando se crea una cita desde el panel. "
     "Variables: interesado, inmueble, cuando.",
     ["interesado", "inmueble", "cuando"], "citas"),
    ("citas.msg_confirmada",
     "¡Dale! 💜 Confirmada la cita con *{interesado}* para el "
     "*{cuando}*. Te recuerdo 30 min antes 📲",
     "Mensaje al pulsar 'Puedo'. Variables: interesado, cuando.",
     ["interesado", "cuando"], "citas"),
    ("citas.msg_cancelada",
     "Ok, cancelo la cita 💜. Si querés proponer otro día más "
     "adelante, decime.",
     "Mensaje al pulsar 'No puedo'.", [], "citas"),
    ("citas.msg_pedir_contrapropuesta",
     "Dale, decime qué día y hora te quedarían bien y yo le "
     "paso tu propuesta al interesado 💜\n\n"
     "(Ej: *jueves a las 3pm*, *mañana 10am*, etc.)",
     "Mensaje al pulsar 'Proponer otro día'.", [], "citas"),
    ("citas.msg_contrapropuesta_recibida",
     "¡Perfecto! Le paso al interesado tu propuesta: *{propuesta}* 💜\n\n"
     "Te aviso cuando confirme.",
     "Mensaje al recibir la contrapropuesta en texto. Variable: propuesta.",
     ["propuesta"], "citas"),

    # ─── Inventario ───
    ("inventario.msg_arranque",
     "¡Buenísimo! 🏠 Vamos a cargar el inmueble en el sistema.\n\n"
     "Te voy a hacer unas preguntas para registrarlo. Si querés "
     "cancelar en cualquier momento, decime *cancelar inmueble*.",
     "Mensaje al arrancar la captura (palabra clave 'registrar inmueble').",
     [], "inventario"),
    ("inventario.pregunta_modalidad",
     "¿Es para *arriendo* o *venta*? Respondé con una de las dos palabras.",
     "Primer dato: modalidad.", [], "inventario"),
    ("inventario.pregunta_torre",
     "¿En qué *torre* está el inmueble? (si no aplica, respondé *N/A*)",
     "Torre.", [], "inventario"),
    ("inventario.pregunta_apto",
     "¿Cuál es el *número de apartamento*?", "Apto.", [], "inventario"),
    ("inventario.pregunta_area",
     "¿Cuántos *metros cuadrados* tiene? (solo el número)",
     "Área en m2.", [], "inventario"),
    ("inventario.pregunta_habitaciones",
     "¿Cuántas *habitaciones* tiene? (solo el número)",
     "Habitaciones.", [], "inventario"),
    ("inventario.pregunta_banos",
     "¿Cuántos *baños* tiene? (solo el número)",
     "Baños.", [], "inventario"),
    ("inventario.pregunta_parqueadero",
     "¿Tiene *parqueadero*? Respondé *sí* o *no*.",
     "Parqueadero sí/no.", [], "inventario"),
    ("inventario.pregunta_precio",
     "¿Cuál es el *precio*? Respondé en millones de pesos. "
     "Ej: *1.200* (si son 1.200 millones para venta), o *2.5* "
     "(si son 2.5 millones de arriendo mensual).",
     "Precio en millones.", [], "inventario"),
    ("inventario.pregunta_propietario_nombre",
     "¿Cuál es el *nombre del propietario*?",
     "Nombre del propietario.", [], "inventario"),
    ("inventario.pregunta_propietario_tel",
     "¿Cuál es el *teléfono del propietario*? (con indicativo, ej. 573001234567)",
     "Teléfono del propietario.", [], "inventario"),
    ("inventario.msg_completado",
     "✅ *¡Inmueble registrado!*\n\n"
     "Lo revisamos y lo publicamos pronto 💜. Si querés cargar otro, "
     "decime *registrar inmueble*.",
     "Mensaje al terminar la captura exitosamente.", [], "inventario"),

    # ─── Nudges (remarketing automático) ───
    ("nudges.recolectando_6h",
     "¡Hola {nombre}! 👋 Vi que empezaste a inscribirte como Agente "
     "Petra pero no terminaste. ¿Querés que te ayude a completar los "
     "datos? Si no, escribime *CANCELAR* y no te vuelvo a molestar 💜",
     "Agente en recolectando por 6h+. Variable: nombre.",
     ["nombre"], "nudges"),
    ("nudges.recolectando_12h",
     "¡Hola {nombre}! 👋 Vi que empezaste a inscribirte como Agente Petra "
     "pero no terminaste. ¿Querés que te ayude a completar los datos? "
     "Solo me faltan unas respuestas más 💜",
     "Agente en recolectando por 12h+. Variable: nombre.",
     ["nombre"], "nudges"),
    ("nudges.inscrito_12h",
     "¡Hola {nombre}! 💜 Ya estás inscrita como Agente Petra, pero vi que "
     "todavía no viste tu video de bienvenida. ¿Querés arrancar? Solo "
     "escribí *empezar* y te paso los primeros videos.",
     "Agente inscrita pero sin ver bienvenida. Variable: nombre.",
     ["nombre"], "nudges"),
    ("nudges.bienvenida_enviada_12h",
     "¡Hola {nombre}! 👋 Vi que recibiste el video de bienvenida pero no "
     "arrancaste la capacitación. Pulsá *Empezar ahora* o escribime "
     "*listo* y te mando los próximos videos 💪",
     "Agente que recibió bienvenida pero no inició capacitación. "
     "Variable: nombre.",
     ["nombre"], "nudges"),
    ("nudges.capacitacion_24h",
     "¡Hola {nombre}! 💜 Vi que pausaste la capacitación. ¿Querés que te "
     "mande los próximos videos? Si no, decime cuándo te queda bien y "
     "te espero.",
     "Agente en capacitación sin confirmar videos por 24h+. Variable: nombre.",
     ["nombre"], "nudges"),
    ("nudges.capacitacion_48h",
     "¡Hola {nombre}! 🎓 ¿Pudiste ver los videos de hoy? Cuando los "
     "veas pulsá *✅ Listo* y seguimos con el siguiente día.",
     "Agente en capacitación sin pulsar Listo por 48h+. Variable: nombre.",
     ["nombre"], "nudges"),
    ("nudges.autorizacion_pendiente_24h",
     "¡Hola {nombre}! 👋 ¿Cómo vas con la gestión de la autorización "
     "del administrador? Si necesitás repasar el material o un empujón, "
     "escribime 💜",
     "Chequeo amable 24h en autorización pendiente. Variable: nombre.",
     ["nombre"], "nudges"),
    ("nudges.autorizacion_pendiente_7d",
     "{nombre}, ya pasaron 7 días con la autorización pendiente 👀. "
     "¿Querés que te vuelva a pasar los argumentos para hablar con el "
     "admin o preferís que te acompañe de otra forma? Si querés "
     "frenar por ahora, escribime *cancelar*.",
     "Nudge 7d con argumentos para destrabar. Variable: nombre.",
     ["nombre"], "nudges"),
    ("nudges.autorizacion_pendiente_14d",
     "Hola {nombre} 💜 Vamos a pausar los recordatorios de la "
     "autorización. Cuando el admin te dé el ok, escribime *ya me "
     "autorizó* y seguimos. ¡Acá te espero!",
     "Nudge 14d: pausa amable. Variable: nombre.",
     ["nombre"], "nudges"),
    ("nudges.autorizacion_pendiente_48h",
     "¡Hola {nombre}! 👋 Vi que llevás unos días con la gestión de la "
     "autorización del admin. ¿Cómo vas? Si necesitás ayuda con qué "
     "decirle o repasar el material, decime y te lo paso de nuevo 💜",
     "Agente en autorización por 48h+. Variable: nombre.",
     ["nombre"], "nudges"),
    ("nudges.kit_pendiente_24h",
     "¡Hola {nombre}! 💜 Para enviarte el kit con tu QR necesito tu "
     "dirección completa. ¿Me pasás los datos que te pedí (torre, "
     "apto, barrio, ciudad)?",
     "Agente que empezó dirección pero no la completó en 24h+. "
     "Variable: nombre.",
     ["nombre"], "nudges"),
    ("nudges.kit_pendiente_72h",
     "{nombre}, hace 3 días que te estoy esperando con tu dirección "
     "para despachar el kit 📦. Si querés que lo haga ya, mandame la "
     "dirección completa en un solo mensaje; si no, escribime "
     "*cancelar*.",
     "Agente sin dirección después de 3 días. Variable: nombre.",
     ["nombre"], "nudges"),
    ("nudges.kit_enviado_7d",
     "¡Hola {nombre}! 📦 Hace una semana te enviamos el kit con el QR. "
     "¿Ya te llegó? Si sí, pulsá *✅ Ya instalé el QR*; si todavía "
     "no, avisame y lo rastreamos juntas 💜",
     "Agente con guía enviada: a los 7 días pregunta '¿llegó el kit?'. "
     "Variable: nombre.",
     ["nombre"], "nudges"),
    ("nudges.kit_enviado_72h",
     "¡Hola {nombre}! 👋 ¿Ya recibiste tu kit? Si ya instalaste el QR, "
     "pulsá *✅ Ya instalé el QR* para activarte. Si todavía no te "
     "llegó, decime y te ayudo a rastrearlo.",
     "Agente que recibió kit pero no confirmó instalación en 72h+. "
     "Variable: nombre.",
     ["nombre"], "nudges"),
    ("nudges.cita_pendiente",
     "👋 {nombre}, llegó una cita nueva con *{cliente}* y todavía no "
     "la respondiste. Confirmá o rechazá cuando puedas 💜",
     "Nudge cita pendiente (activa). Variables: nombre, cliente.",
     ["nombre", "cliente"], "nudges"),

    # ─── Botones (título visible — el ID queda fijo en código) ───
    ("boton.onbag_confirm", "✅ Registrarme",
     "Botón para confirmar los 5 datos de inscripción.", [], "botones"),
    ("boton.onbag_corregir", "✏️ Corregir",
     "Botón para corregir un dato del resumen.", [], "botones"),
    ("boton.onbag_cancelar", "❌ Cancelar",
     "Botón para cancelar la inscripción.", [], "botones"),
    ("boton.onbag_ver_journey", "Ver próximos pasos",
     "Botón para avanzar al journey después de inscribirse.", [], "botones"),
    ("boton.onbag_empezar_ahora", "Empezar ahora",
     "Botón para arrancar la capacitación sin esperar al cron 8am.",
     [], "botones"),
    ("boton.onbag_ver_videos_hoy", "Ver videos de hoy",
     "Botón para ver los videos del día actual.", [], "botones"),
    ("boton.onbag_listo", "✅ Listo",
     "Botón para marcar que vio los 2 videos del día.", [], "botones"),
    ("boton.onbag_iniciar_autorizacion", "🚀 Iniciar",
     "Botón al terminar capacitación — arranca la gestión de autorización.",
     [], "botones"),
    ("boton.onbag_ver_material", "📎 Ver material",
     "Botón para pedir el material de apoyo del admin.", [], "botones"),
    ("boton.onbag_tengo_duda", "❓ Tengo una duda",
     "Botón para pedir ayuda al LLM.", [], "botones"),
    ("boton.onbag_ya_tengo_autorizacion", "✅ Ya tengo autoriz",
     "Botón para confirmar que consiguió autorización del admin. "
     "WhatsApp trunca a 20 chars.", [], "botones"),
    ("boton.onbag_qr_instalado", "✅ Ya instalé el QR",
     "Botón para confirmar que instaló el QR en el edificio.", [], "botones"),
    ("boton.onbag_cita_puedo", "✅ Puedo",
     "Botón para confirmar asistencia a una cita.", [], "botones"),
    ("boton.onbag_cita_proponer", "📅 Proponer otro día",
     "Botón para proponer otra fecha a una cita.", [], "botones"),
    ("boton.onbag_cita_no_puedo", "❌ No puedo",
     "Botón para rechazar una cita.", [], "botones"),

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
