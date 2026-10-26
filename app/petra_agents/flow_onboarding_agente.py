"""Flow handler DETERMINÍSTICO del onboarding de Agente Petra.

Zero LLM. Lógica Python pura con slot filling. Reemplaza al sub-agente
LLM solo para la fase inicial de captura de datos (5 campos).

Flujo:
    1. Pide dato → recibe respuesta → valida → guarda en DB → pide próximo
    2. Al tener los 5 datos: muestra resumen con botones Confirmar/Corregir
    3. Al pulsar Confirmar (o escribir REGISTRARME): marca estado=inscrito,
       felicita, dispara envío del video de bienvenida.
    4. Si pulsa Corregir: ofrece botones para elegir qué dato corregir.

Esto es un state machine. El estado se persiste en
petra_contacto_roles.metadata (campos del onboarding + estado_agente).

Entrada: router llama handle(telefono, texto) cuando detecta que:
  - rol = agente_petra con estado = 'recolectando' (está en onboarding)
  O
  - mensaje matchea intención "quiero ser agente" (recién empezando)
"""
import logging
import re

from app.petra_agents.textos import t as _t, boton as _b
from app.petra_agents import botones_estacion as _botones
from app.petra_agents import mensajes_extra as _extras


def _send_buttons(telefono, texto, defaults, estado_key=None):
    """Wrapper de petra_wa.send_buttons que aplica las reglas de
    `petra_botones_estacion`: oculta defaults marcados override+inactivo
    y agrega botones custom (cap 3 por mensaje WhatsApp)."""
    from app import petra_wa
    final = _botones.aplicar_reglas(estado_key, defaults) if estado_key else defaults
    if not final:
        petra_wa.send_text(telefono, texto)
        return
    petra_wa.send_buttons(telefono, texto, final)


_TC_MODULOS = {
    # Nota: inscripcion/autorizacion/kit/capacitacion ya no usan LLM
    # (migradas a *_v3 en M2-M4). Solo queda 'activa' por ahora.
    "activa": "app.petra_agents.tools_activa",
}


def _tc_delegar(fase, env_flag, telefono, texto, cid, meta, conn):
    """Delegación genérica a tool-calling para una fase. Devuelve True si
    el mensaje quedó manejado (LLM respondió, o falló y se encoló con el
    mensaje humano); False si el flag está off / el texto es un botón /
    hubo excepción no-retriable → el caller sigue por el flow determinístico."""
    import importlib
    import os as _os
    if _os.environ.get(env_flag, "0") != "1":
        return False
    if (texto or "").startswith(("ONBAG_", "CUSTOM_")):
        return False
    try:
        mod = importlib.import_module(_TC_MODULOS[fase])
        res = mod.procesar(telefono, texto, cid, meta, conn)
        if res.get("ok"):
            _log(telefono, texto, (res.get("texto_respuesta") or "")[:300],
                 nota=f"tc-{fase} tools={len(res.get('tools_llamadas', []))}")
            return True
        if res.get("retriable"):
            from app.petra_agents import toolcalling_queue as _tcq
            _tcq.encolar(telefono, texto, cid, fase, res.get("fallo"))
            _tcq.enviar_disculpame(telefono)
            _log(telefono, texto, "[LLM failing — queued]",
                 nota=f"tc-{fase} fallo={res.get('fallo')}")
            return True
        log.warning("[flow:tc] %s fallo non-retriable (%s) → determinístico",
                    fase, res.get("fallo"))
    except Exception:
        log.exception("[flow:tc] excepcion %s → determinístico", fase)
    return False


def _enviar_media_por_clave(telefono, clave_texto):
    """Envía la media/videos adjuntos a UN MENSAJE puntual (clave_texto),
    no a toda la estación. Silencio si no hay nada cargado."""
    from app import db as _db
    from app import petra_wa as _pwa_m
    import time as _tm_m
    try:
        with _db._conexion_directa(timeout=5) as conn:
            rows = conn.execute(
                "SELECT titulo, url FROM petra_media_estacion "
                "WHERE clave_texto=%s AND activo=TRUE "
                "ORDER BY orden, id",
                (clave_texto,),
            ).fetchall()
    except Exception:
        log.exception("[flow:media-msg] query fallo clave=%s", clave_texto)
        return
    for titulo, url in rows:
        try:
            _pwa_m.send_media_o_texto(telefono, titulo or "", url)
            _tm_m.sleep(1.5)
        except Exception:
            log.exception("[flow:media-msg] envío fallo url=%s", url)


def _sm(telefono, clave, fallback=None, **variables):
    """Helper central para enviar UN MENSAJE del flow:
    - lee el texto editable por `clave` (petra_textos). Si activo=FALSE, saltea.
    - si hay botones adjuntos a esa clave, lo manda con botones; sino texto plano.
    - después manda la media adjunta a esa clave.
    - después manda cualquier mensaje extra (petra_mensajes_extra) adjunto.

    Reemplaza al patrón `_sm(telefono, clave, fallback=..., **vars)`
    y lo mejora con los nuevos controles del panel.
    """
    from app import petra_wa
    texto = _t(clave, fallback=fallback, **variables)
    if not texto or texto.strip() == "":
        return  # mensaje ocultado desde el panel
    # 1) Texto primero (con botones si hay)
    botones_adjuntos = _botones.botones_de_clave(clave)
    if botones_adjuntos:
        petra_wa.send_buttons(telefono, texto, botones_adjuntos)
    else:
        petra_wa.send_text(telefono, texto)
    # 2) Media adjunta DESPUÉS del texto
    try:
        _enviar_media_por_clave(telefono, clave)
    except Exception:
        log.exception("[flow:_sm] media hook fallo clave=%s", clave)
    # 3) Extras al final
    try:
        _extras.enviar_extras(telefono, clave, variables)
    except Exception:
        log.exception("[flow:_sm] extras hook fallo clave=%s", clave)

log = logging.getLogger("petra")

CAMPOS = ["nombre", "cedula", "ciudad", "barrio", "conjunto", "torre_apto"]

# Hardcode fallback — se usa si petra_textos no tiene la clave en DB.
# Los textos editables desde panel sobreescriben estos valores (ver textos.py).
_PREGUNTAS_FALLBACK = {
    "nombre": "👋 ¡Qué alegría que quieras ser Agente Petra!\n\nTe voy a pedir 6 datos para registrarte. Empecemos por el primero:\n\n*¿Cuál es tu nombre completo?*",
    "cedula": "✅ Gracias, {nombre}.\n\n*¿Cuál es tu cédula?* (solo los números)",
    "ciudad": "Perfecto. *¿En qué ciudad vivís?*",
    "barrio": "*¿En qué barrio?*",
    "conjunto": "*¿Cómo se llama el conjunto o edificio donde vivís?*",
    "torre_apto": "Último dato — ¿En qué *torre y apartamento* vivís? (ej. _Torre 3 apto 702_, _Casa 49_, _Apto 15B_) 🏠",
}


def _pregunta(campo, **variables):
    """Lee el texto editable `inscripcion.pregunta_<campo>` desde DB.
    Cae al hardcode si la clave no existe.

    OJO: devuelve solo el texto crudo (sin disparar media/botones/extras
    adjuntos). Para enviar la pregunta Y respetar los adjuntos del panel,
    usá `_enviar_pregunta()`."""
    from app.petra_agents.textos import t
    return t(
        f"inscripcion.pregunta_{campo}",
        fallback=_PREGUNTAS_FALLBACK[campo],
        **variables,
    )


def _enviar_pregunta(telefono, campo, **variables):
    """Envía la pregunta del campo usando `_sm`: respeta activo, adjunta
    media/botones y manda extras asociados a `inscripcion.pregunta_<campo>`."""
    _sm(telefono,
        f"inscripcion.pregunta_{campo}",
        fallback=_PREGUNTAS_FALLBACK[campo],
        **variables)

NOMBRE_CAMPO = {
    "nombre": "nombre completo",
    "cedula": "cédula",
    "ciudad": "ciudad",
    "barrio": "barrio",
    "conjunto": "conjunto/edificio",
    "torre_apto": "torre y apto / casa",
}


def _leer_meta(conn, telefono):
    row = conn.execute(
        "SELECT c.id, r.metadata FROM petra_contactos c "
        "LEFT JOIN petra_contacto_roles r "
        "ON r.contacto_id=c.id AND r.rol='agente_petra' "
        "WHERE c.telefono=%s",
        (telefono,),
    ).fetchone()
    if not row:
        return None, None, {}
    cid, meta = row[0], (dict(row[1]) if row[1] else {})
    return cid, row[0], meta


def _upsert_contacto(conn, telefono):
    """Garantiza que exista el contacto, devuelve su id."""
    row = conn.execute(
        "SELECT id FROM petra_contactos WHERE telefono=%s", (telefono,)
    ).fetchone()
    if row:
        return row[0]
    r = conn.execute(
        "INSERT INTO petra_contactos (telefono) VALUES (%s) RETURNING id",
        (telefono,),
    ).fetchone()
    return r[0]


def _save_meta(conn, cid, meta):
    import json as _json
    existente = conn.execute(
        "SELECT 1 FROM petra_contacto_roles "
        "WHERE contacto_id=%s AND rol='agente_petra'",
        (cid,),
    ).fetchone()
    if existente:
        conn.execute(
            "UPDATE petra_contacto_roles SET metadata=%s::jsonb, "
            "updated_at=NOW() WHERE contacto_id=%s AND rol='agente_petra'",
            (_json.dumps(meta), cid),
        )
    else:
        conn.execute(
            "INSERT INTO petra_contacto_roles (contacto_id, rol, metadata) "
            "VALUES (%s, 'agente_petra', %s::jsonb)",
            (cid, _json.dumps(meta)),
        )


_SALUDOS = {"hola", "holaa", "holaaa", "ola", "buenas", "buenos",
            "hey", "hi", "hello", "qué tal", "que tal"}
_FRASES_RECHAZO = (
    "no vivo", "no vive", "no tengo", "no hay", "no es ", "no se ", "no sé",
    "ningun", "ninguna", "ninguno", "nada", "nadie",
    "perdon", "perdón", "disculpa", "sorry",
)
_EMOTICONES_NEG = (":(", ":-(", ":/", ":c", ":'(", "😔", "😢", "😞")
_RE_PALABRA_ALFA = re.compile(r"^[A-Za-zÁÉÍÓÚáéíóúÑñÜü'\-]+$")


def _parece_saludo(t_lc):
    palabras = t_lc.split()
    if not palabras:
        return False
    primera = palabras[0].strip(",.!?¡¿:;")
    return primera in _SALUDOS


def _parece_rechazo(t_lc):
    if any(em in t_lc for em in _EMOTICONES_NEG):
        return True
    for f in _FRASES_RECHAZO:
        if f in t_lc:
            return True
    return False


def _validar(campo, texto):
    """Valida/normaliza una respuesta. Devuelve (valido, valor_normalizado, msg_error)."""
    t = (texto or "").strip()
    if not t:
        return False, None, "Hmm, no recibí nada. Mandame tu respuesta en texto."
    t_lc = t.lower()

    if campo == "cedula":
        digitos = re.sub(r"\D", "", t)
        if len(digitos) < 6 or len(digitos) > 12:
            return False, None, "La cédula debería tener entre 6 y 12 dígitos. Mandámela otra vez por favor 🙏"
        return True, digitos, None

    if campo == "nombre":
        if _parece_saludo(t_lc):
            return False, None, "Eso parece un saludo 😊. Mandame tu nombre y apellido completos."
        palabras_alfa = [p for p in t.split()
                         if len(p) >= 2 and _RE_PALABRA_ALFA.match(p)]
        if len(palabras_alfa) < 2:
            return False, None, "Por favor mandame tu nombre y apellido completos (al menos 2 palabras, solo letras) 🙏"
        if len(t) > 80:
            return False, None, "Ese nombre es muy largo. Mandame solo nombre y apellido 🙏"
        return True, t, None

    if campo == "conjunto":
        if _parece_rechazo(t_lc):
            return False, None, (
                "Entiendo 💜. Para ser Agente Petra necesitás vivir en un "
                "conjunto o edificio residencial. ¿Vivís en alguno? "
                "Si sí, mandame el nombre; si no, escribí *CANCELAR*."
            )
        if len(t) < 2:
            return False, None, "Mandame el nombre del conjunto o edificio 🙏"
        if len(t) > 80:
            return False, None, "Es muy largo. Mandame solo el nombre del conjunto o edificio 🙏"
        return True, t, None

    if campo in ("ciudad", "barrio"):
        if _parece_saludo(t_lc):
            etiqueta = "la ciudad" if campo == "ciudad" else "el barrio"
            return False, None, f"Mandame el nombre de {etiqueta} 🙏"
        if len(t) < 2:
            return False, None, "Esa respuesta es muy corta. Mandame el nombre 🙏"
        if len(t) > 80:
            return False, None, "Es muy largo. Mandame algo más corto 🙏"
        return True, t, None

    # Fallback genérico (por si se agregan campos nuevos)
    if len(t) > 200:
        return False, None, "Esa respuesta es muy larga. Mandame algo más corto 🙏"
    return True, t, None


def _resumen_texto(meta):
    torre_apto = meta.get("torre_apto") or ""
    linea_torre = f"• Torre/Apto: {torre_apto}\n" if torre_apto else ""
    return (
        "📋 *Confirmemos tus datos:*\n\n"
        f"• Nombre: {meta.get('nombre')}\n"
        f"• Cédula: {meta.get('cedula')}\n"
        f"• Ciudad: {meta.get('ciudad')}\n"
        f"• Barrio: {meta.get('barrio')}\n"
        f"• Conjunto: {meta.get('conjunto')}\n"
        f"{linea_torre}\n"
        "¿Están correctos?"
    )


def _enviar_resumen(telefono, meta):
    _send_buttons(
        telefono, _resumen_texto(meta),
        [
            {"id": "ONBAG_CONFIRM", "title": _b("ONBAG_CONFIRM", "✅ Registrarme")},
            {"id": "ONBAG_CORREGIR", "title": _b("ONBAG_CORREGIR", "✏️ Corregir")},
            {"id": "ONBAG_CANCELAR", "title": _b("ONBAG_CANCELAR", "❌ Cancelar")},
        ],
        estado_key="registro_pendiente_confirmacion",
    )


def _enviar_lista_correccion(telefono, meta):
    """Envía una lista interactiva con los 5 campos para elegir cuál corregir."""
    from app import petra_wa
    petra_wa.send_list(
        telefono,
        "¿Qué dato querés corregir?",
        "Ver opciones",
        [{
            "title": "Datos del onboarding",
            "rows": [
                {"id": "ONBAG_FIX_nombre",   "title": "Nombre completo",
                 "description": meta.get("nombre", "")[:70]},
                {"id": "ONBAG_FIX_cedula",   "title": "Cédula",
                 "description": meta.get("cedula", "")[:70]},
                {"id": "ONBAG_FIX_ciudad",   "title": "Ciudad",
                 "description": meta.get("ciudad", "")[:70]},
                {"id": "ONBAG_FIX_barrio",   "title": "Barrio",
                 "description": meta.get("barrio", "")[:70]},
                {"id": "ONBAG_FIX_conjunto", "title": "Conjunto/Edificio",
                 "description": meta.get("conjunto", "")[:70]},
                {"id": "ONBAG_FIX_torre_apto", "title": "Torre / Apto",
                 "description": meta.get("torre_apto", "")[:70]},
            ],
        }],
    )


def _parse_correccion(texto):
    """Si el usuario dice 'corregir X' o nombra un campo, devuelve ese campo."""
    t = (texto or "").lower()
    for campo in CAMPOS:
        if campo in t:
            return campo
    # mapeos extras
    if "nombre" in t:
        return "nombre"
    if "edificio" in t or "edificio" in t:
        return "conjunto"
    return None


def _enviar(telefono, texto):
    from app import petra_wa
    petra_wa.send_text(telefono, texto)


ESTADOS_VALIDOS = (
    "recolectando", "inscrito", "bienvenida_enviada",
    "esperando_capacitacion_dia1", "en_capacitacion",
    "capacitacion_completa",
    "autorizacion_pendiente", "autorizacion_rechazada",
    "kit_pendiente", "kit_enviado", "activa",
)


def _set_estado(meta, nuevo_estado):
    """Setea estado_agente + estampa estado_desde (ISO 8601 UTC).
    Usado por el motor de nudges para saber hace cuánto está la agente
    en el estado actual.

    Estados válidos declarados en ESTADOS_VALIDOS (M3.C agrega
    `autorizacion_rechazada`, pausado). No bloqueamos — solo log si
    aparece algo inesperado."""
    if nuevo_estado not in ESTADOS_VALIDOS:
        log.warning("[flow] _set_estado: estado no declarado=%s", nuevo_estado)
    import datetime as _d
    anterior = meta.get("estado_agente")
    if anterior != nuevo_estado:
        meta["estado_agente"] = nuevo_estado
        meta["estado_desde"] = _d.datetime.utcnow().isoformat()
        # Al cambiar de estado, los nudges del estado anterior ya no aplican.
        # Dejamos el dict pero ya no se van a disparar porque el estado cambió.
    return meta


def _enviar_media_estacion(conn, cid, meta, telefono, estado_key):
    """Envía media adjunta a un estado del flow (imágenes/videos cargadas
    desde el panel). Se envía UNA sola vez por contacto por estado, usando
    un flag en metadata.media_<estado_key>_enviada.

    Llamalo DESPUÉS de que el bot envió el mensaje principal de ese estado,
    o al entrar al estado. Silencio si no hay media cargada."""
    flag = f"media_{estado_key}_enviada"
    if meta.get(flag):
        return
    try:
        rows = conn.execute(
            "SELECT titulo, url FROM petra_media_estacion "
            "WHERE estado_key=%s AND clave_texto IS NULL AND activo=TRUE "
            "ORDER BY orden, id",
            (estado_key,),
        ).fetchall()
    except Exception:
        log.exception("[flow:media] query fallo estado=%s", estado_key)
        return
    if not rows:
        return
    import time as _tm_media
    from app import petra_wa as _pwa_media
    for titulo, url in rows:
        try:
            _pwa_media.send_media_o_texto(telefono, titulo or "", url)
            _tm_media.sleep(2)
        except Exception:
            log.exception("[flow:media] envío fallo url=%s", url)
    meta[flag] = True
    _save_meta(conn, cid, meta)


def _responder_con_llm(telefono, texto, sesion_id=None):
    """Delega el mensaje al sub-agente LLM agente_petra, que tiene el
    contexto del rol/estado y puede responder cualquier pregunta libre.
    Usado como fallback en las fases donde el flow determinístico no
    reconoce el mensaje como comando (listo, botón, reenvío, etc.)."""
    try:
        from app.petra_agents import agente_petra
        return agente_petra.handle(
            telefono, texto, es_handoff=False, sesion_id=sesion_id,
        )
    except Exception:
        log.exception("[flow] delegación a LLM fallo tel=%s", telefono)
        _sm(telefono, 
            "fallback.llm_error",
            fallback=(
                "Perdón, no te pude responder bien ahora. Un asesor te "
                "contacta pronto 💜"
            ),
        )
        return True


def _enviar_recordatorio_capacitacion(conn, telefono):
    """Envía el video de capacitación marcado como 'recordatorio' justo
    antes del material de autorización. Repasa el día clave donde se
    explicó cómo abordar al administrador. Silencio si no hay cargado.
    Devuelve True si envió algo, False si no había contenido."""
    from app import petra_wa as _pwa
    import time as _tm
    vid = conn.execute(
        "SELECT titulo, url FROM petra_contenidos "
        "WHERE tipo='video_recordatorio_autorizacion' AND activo=TRUE "
        "ORDER BY orden LIMIT 1"
    ).fetchone()
    if not vid:
        return False
    _pwa.send_text(telefono, _t(
        "autorizacion.msg_recordatorio_video",
        fallback=(
            "Antes de ir con el administrador, repasá este video de "
            "capacitación donde te expliqué cómo abordarlo 💜:"
        ),
    ))
    _tm.sleep(1)
    _pwa.send_media_o_texto(telefono, vid[0], vid[1])
    _tm.sleep(2)
    return True


def _enviar_material_autorizacion(conn, telefono):
    """Envía los 3 contenidos post-capacitación: video, demo, piezas.
    Silencio por tipo si no hay cargado. Se usa al terminar la capacitación
    y como respuesta a palabras clave en fase autorizacion_pendiente."""
    from app import petra_wa as _pwa
    import time as _tm
    vid = conn.execute(
        "SELECT titulo, url FROM petra_contenidos "
        "WHERE tipo='video_autorizacion' AND activo=TRUE "
        "ORDER BY orden LIMIT 1"
    ).fetchone()
    if vid:
        _pwa.send_media_o_texto(telefono, vid[0], vid[1])
        _tm.sleep(2)
    demo = conn.execute(
        "SELECT titulo, url FROM petra_contenidos "
        "WHERE tipo='demo_autorizacion' AND activo=TRUE "
        "ORDER BY orden LIMIT 1"
    ).fetchone()
    if demo:
        _pwa.send_media_o_texto(telefono, demo[0], demo[1])
        _tm.sleep(2)
    piezas = conn.execute(
        "SELECT titulo, url FROM petra_contenidos "
        "WHERE tipo='pieza_grafica_autorizacion' AND activo=TRUE "
        "ORDER BY orden LIMIT 2"
    ).fetchall()
    for p in piezas:
        _pwa.send_media_o_texto(telefono, p[0], p[1])
        _tm.sleep(2)


def _enviar_con_pausa(telefono, texto, pausa_seg=1.5):
    """Envía texto y pausa antes del próximo mensaje. Útil para encadenar
    varios mensajes sin que lleguen todos juntos y abrumen al usuario."""
    import time
    from app import petra_wa
    petra_wa.send_text(telefono, texto)
    time.sleep(pausa_seg)


def _log(telefono, texto_usuario, texto_asistente, nota=""):
    """Loguea en petra_llm_logs con rama 'flow:onboarding_agente'."""
    from app import db as _db
    try:
        with _db._conexion_directa(timeout=3) as conn:
            conn.execute(
                "INSERT INTO petra_llm_logs "
                "(telefono, texto_usuario, texto_asistente, error, rama) "
                "VALUES (%s, %s, %s, %s, 'flow:onboarding_agente')",
                (telefono, texto_usuario, texto_asistente, nota[:300] if nota else None),
            )
    except Exception:
        log.exception("[flow:onboarding] log fallo tel=%s", telefono)


def _fase_inscrito(conn, telefono, cid, meta, texto):
    """Fase post-REGISTRARME. Flujo guiado por botones:
      - btn 'Ver próximos pasos' → muestra journey + btn 'Ver videos de hoy'
      - btn 'Ver videos de hoy'  → envía videos + pide LISTO
    Cualquier otra cosa: re-muestra el botón del paso actual.
    """
    from app import petra_wa
    nombre_corto = (meta.get("nombre") or "").split()[0]

    cap_v = int(meta.get("cap_version", 1) or 1)

    # Paso 1: user pulsó "Ver próximos pasos" → journey + botón.
    # v2: botón "Empezar ahora" (dispara día 1 = bienvenida+próximos directo).
    # v1 (legacy): botón "Ver videos de hoy" (pre-step que envía videos).
    if texto == "ONBAG_VER_JOURNEY":
        journey = (
            "📋 *Tu camino como Agente Petra:*\n\n"
            "1️⃣ *Inscripción* ✅ — ya lo hiciste.\n"
            "2️⃣ *Capacitación* 🎓 — 2 videos cada día durante 7 días.\n"
            "3️⃣ *Autorización del conjunto* 🏢 — hablás con el administrador de tu conjunto.\n"
            "4️⃣ *Activar el QR* 📬 — te enviamos kit + regalo.\n"
            "5️⃣ *Gestionar citas* 📅 — recibís citas con propietarios/interesados.\n"
            "6️⃣ *Registrar inventario* 🔑 — recibís llaves y revisás inmuebles.\n\n"
            "¿Arrancamos con los videos de hoy?"
        )
        boton = ({"id": "ONBAG_EMPEZAR_AHORA", "title": _b("ONBAG_EMPEZAR_AHORA", "Empezar ahora")}
                 if cap_v >= 2
                 else {"id": "ONBAG_VER_VIDEOS_HOY", "title": _b("ONBAG_VER_VIDEOS_HOY", "Ver videos de hoy")})
        _send_buttons(telefono, journey, [boton], estado_key="inscrito")
        _log(telefono, texto, f"[journey + btn {boton['title']}]",
             nota=f"paso 1: journey enviado (cap_v{cap_v})")
        return True

    # v2 — paso directo: user pulsó "Empezar ahora" desde el journey.
    # Dispara día 1 (bienvenida+próximos pasos) inmediatamente vía cron.
    if texto == "ONBAG_EMPEZAR_AHORA" and cap_v >= 2:
        import datetime as _dt, json as _json, time as _time
        try:
            from zoneinfo import ZoneInfo
            hoy_bogota = _dt.datetime.now(ZoneInfo("America/Bogota")).date().isoformat()
        except Exception:
            hoy_bogota = _dt.datetime.utcnow().date().isoformat()
        _set_estado(meta, "esperando_capacitacion_dia1")
        meta["bienvenida_vista_at"] = _dt.datetime.utcnow().isoformat()
        _save_meta(conn, cid, meta)
        try:
            from app.scheduler import _cap_enviar_para_agente
            from app import petra_wa as _pwa
            enviado = _cap_enviar_para_agente(
                conn, cid, telefono, meta, hoy_bogota,
                _pwa, _time, _json, ignorar_dedup_fecha=True,
            )
        except Exception:
            log.exception("[flow:onboarding] fallo disparar día 1 v2 tel=%s", telefono)
            enviado = False
        if enviado:
            _log(telefono, texto, "[v2 día 1 enviado inmediato]",
                 nota="v2 empezar_ahora → día 1 disparado")
        else:
            _send_buttons(telefono, _t(
                "capacitacion.msg_listo_para_arrancar",
                fallback=(
                    "🎉✨ *¡Todo listo!*\n\n"
                    "Tu capacitación arranca cuando vos quieras 💜\n\n"
                    "🎬 Vas a ver *2 videos cortos por día* — a tu "
                    "ritmo, sin presión.\n"
                    "📚 En *7 días* vas a estar lista para ser una "
                    "Agente Petra experta en tu edificio 🏢\n\n"
                    "¿Querés ver los videos de hoy? Tocá el botón 👇"
                ),
            ), [{"id": "ONBAG_VER_VIDEOS_HOY", "title": _b("ONBAG_VER_VIDEOS_HOY", "▶️ Ver mis videos")}],
               estado_key="bienvenida_enviada")
            _log(telefono, texto, "[v2 empezar_ahora: sin envío → botón ver videos]",
                 nota="fallback con botón retry")
        return True

    # Paso 2: user pulsó "Ver videos de hoy" → envía videos reales
    if texto == "ONBAG_VER_VIDEOS_HOY":
        bienvenida_row = conn.execute(
            "SELECT titulo, url FROM petra_contenidos "
            "WHERE tipo='bienvenida' AND activo=TRUE ORDER BY orden LIMIT 1"
        ).fetchone()
        proximos_row = conn.execute(
            "SELECT titulo, url FROM petra_contenidos "
            "WHERE tipo='proximos_pasos' AND activo=TRUE ORDER BY orden LIMIT 1"
        ).fetchone()
        if not bienvenida_row and not proximos_row:
            _enviar(telefono,
                f"{nombre_corto}, los videos están en preparación. "
                "Te aviso apenas estén cargados ❤️")
            _log(telefono, texto, "[sin videos cargados]")
            return True
        if bienvenida_row:
            titulo, url = bienvenida_row
            _enviar_con_pausa(telefono, f"🎬 *{titulo}*\n\n{url}", pausa_seg=2)
        if proximos_row:
            titulo, url = proximos_row
            _enviar_con_pausa(telefono, f"🗺 *{titulo}*\n\n{url}", pausa_seg=2)
        cuantos = (1 if bienvenida_row else 0) + (1 if proximos_row else 0)
        verbo = "los" if cuantos > 1 else "lo"
        _send_buttons(telefono,
            f"Cuando {verbo} termines, pulsá el botón para arrancar con "
            "tu capacitación diaria. 💪",
            [{"id": "ONBAG_EMPEZAR_AHORA", "title": _b("ONBAG_EMPEZAR_AHORA", "Empezar ahora")}],
            estado_key="bienvenida_enviada",
        )
        import datetime as _dt
        _set_estado(meta, "bienvenida_enviada")
        meta["bienvenida_enviada_at"] = _dt.datetime.utcnow().isoformat()
        _save_meta(conn, cid, meta)
        _log(telefono, texto, "[videos enviados + btn Ya los vi]",
             nota="paso 2: videos enviados")
        return True

    # Texto libre que no matchea comando: delegamos al LLM (agente_petra)
    # para responder preguntas sobre Petra. Si falla, caemos al re-prompt.
    if texto:
        try:
            _log(telefono, texto, "[inscrito → LLM por pregunta libre]")
            return _responder_con_llm(telefono, texto)
        except Exception:
            log.exception("[flow:inscrito] delegación LLM fallo tel=%s", telefono)
    # Fallback: re-mostrar el botón
    _send_buttons(telefono,
        f"Para continuar tu onboarding, pulsá el botón 👇",
        [{"id": "ONBAG_VER_JOURNEY", "title": _b("ONBAG_VER_JOURNEY", "Ver próximos pasos")}],
        estado_key="inscrito",
    )
    _log(telefono, texto, "[re-muestra btn Ver próximos pasos]",
         nota="fase inscrito: re-prompt")
    return True
    import datetime as _dt
    _set_estado(meta, "bienvenida_enviada")
    meta["bienvenida_enviada_at"] = _dt.datetime.utcnow().isoformat()
    _save_meta(conn, cid, meta)
    cargados = (("bienvenida " if bienvenida_row else "") +
                ("proximos_pasos" if proximos_row else "")).strip()
    _log(telefono, texto, f"[videos enviados: {cargados}]",
         nota="fase inscrito → videos enviados")
    return True


DIRECCION_CAMPOS = ["torre", "apto", "barrio", "ciudad"]
DIRECCION_PREGUNTAS = {
    "torre": "activacion.pregunta_torre",
    "apto": "activacion.pregunta_apto",
    "barrio": "activacion.pregunta_barrio",
    "ciudad": "activacion.pregunta_ciudad",
}
DIRECCION_FALLBACKS = {
    "torre": "*¿En qué torre vivís?* (ej. Torre 3, Bloque A, etc. Si no aplica, respondé *N/A*)",
    "apto": "Perfecto. *¿Cuál es tu apartamento?* (ej. 702, Apto 15B)",
    "barrio": "¿En qué *barrio* está tu edificio?",
    "ciudad": "Último dato — *¿En qué ciudad?*",
}


def _fase_kit_pendiente(conn, telefono, cid, meta, texto, sesion_id=None):
    """Captura la dirección COMPLETA en UN solo mensaje libre.
    Al validar, avanza directo a kit_enviado y envía el bloque de
    activación (confirmación + botón 'Ya instalé el QR').
    """
    import datetime as _dt
    import re as _re
    direccion = meta.get("direccion") or {}
    direccion_txt = meta.get("direccion_texto") or direccion.get("completa") or ""
    t_lc = (texto or "").lower().strip()

    # Shortcut: la agente pregunta por su kit → recordatorio.
    if direccion_txt and t_lc in PALABRAS_KIT:
        _sm(telefono,
            "activacion.msg_direccion_completa",
            fallback=(
                "🎉 *¡Dirección confirmada!* 📍\n\n"
                "Ya pedimos tu kit. En *2-3 días hábiles* te llega 🚚"
            ),
        )
        _log(telefono, texto, "[kit_pendiente: re-aviso por palabra clave]")
        return True

    # Pregunta libre → delega al LLM.
    if texto and any(w in t_lc for w in (
            "duda", "pregunta", "ayuda", "no entiendo", "por qué", "porque")):
        _log(telefono, texto, "[kit_pendiente → LLM por pregunta libre]")
        return _responder_con_llm(telefono, texto, sesion_id=sesion_id)

    if not texto:
        return False

    # Validación simple: dirección = texto libre con mín 6 chars y >=8
    # chars útiles (letras/dígitos). Rechaza "?", "ok", emoji suelto.
    v = texto.strip()
    utiles = _re.sub(r"[^a-záéíóúñA-ZÁÉÍÓÚÑ0-9]", "", v)
    sospechoso = any(s in t_lc for s in (
        "?", "ya te", "ya les", "cuanto", "cuándo", "cómo", "dónde",
        "disculp", "olvid", "no sé", "no se", "mandame"))
    if len(v) < 6 or len(utiles) < 8 or sospechoso:
        _sm(telefono,
            "activacion.msg_direccion_invalida",
            fallback=(
                "📍 Esa dirección no me quedó clara. Mandámela "
                "*completa en un solo mensaje*, con todo lo que "
                "tengas: torre o casa, apto, barrio y ciudad.\n\n"
                "_Ejemplos:_\n"
                "• _Torre 3 apto 702, Chapinero, Bogotá_\n"
                "• _Casa 49, Portal del Norte, Zipaquirá_"
            ),
        )
        _log(telefono, texto, "[kit_pendiente: dirección inválida — repide]")
        return True

    # Dirección válida — guardamos y avanzamos.
    limpio = v[:280]
    meta["direccion"] = {"completa": limpio}
    meta["direccion_texto"] = limpio
    meta["direccion_capturada_at"] = _dt.datetime.utcnow().isoformat()
    meta.pop("direccion_paso", None)
    _set_estado(meta, "kit_enviado")
    meta["kit_enviado_at"] = _dt.datetime.utcnow().isoformat()
    meta["kit_enviado_tras_direccion"] = True
    _save_meta(conn, cid, meta)
    # Mensaje "Dirección confirmada" ya trae el botón ✨ Perfecto embebido.
    # Los adjuntos (videos) van después en un segundo mensaje porque
    # WhatsApp no permite media + botones en el mismo mensaje interactivo.
    _send_buttons(telefono, _t(
        "activacion.msg_direccion_completa",
        fallback=(
            "🎉 *¡Dirección confirmada!* 📍\n\n"
            "Ya pedimos tu kit de activación 📦\n"
            "En *2 a 3 días hábiles* te llega a esa dirección.\n\n"
            "💜 Mientras tanto, podés seguir con tu capacitación."
        ),
    ), [{"id": "ONBAG_KIT_PERFECTO", "title": "✨ Perfecto"}])
    try:
        _enviar_media_por_clave(telefono, "activacion.msg_direccion_completa")
    except Exception:
        log.exception("[flow:kit_pendiente] media hook fallo tel=%s", telefono)
    _log(telefono, texto, "[kit_pendiente: direccion completa → kit_enviado]")
    return True


def _formatear_direccion(direccion):
    """Formatea la dirección para mostrarle a la agente.
    Soporta formato nuevo ({'completa': '...'}) y legacy
    ({torre, apto, barrio, ciudad})."""
    if not direccion:
        return ""
    completa = (direccion.get("completa") or "").strip()
    if completa:
        return f"📍 *{completa}*"
    partes = []
    for k, label in [("torre", "Torre"), ("apto", "Apto"),
                     ("barrio", "Barrio"), ("ciudad", "Ciudad")]:
        v = (direccion.get(k) or "").strip()
        if v and v.upper() != "N/A":
            partes.append(f"*{label}:* {v}")
    return "\n".join(partes)


PALABRAS_KIT = (
    "kit", "mi kit", "donde está mi kit", "donde esta mi kit",
    "info kit", "estado kit", "que paso con mi kit", "qué pasó con mi kit",
    "link", "link del qr", "mi qr", "donde está el link",
    "donde esta el link",
)


def _reenviar_kit_enviado(telefono, cid, meta):
    """Re-envía el bloque completo del mensaje de activación (texto +
    media + botón). Útil cuando la agente pregunta "kit", "mi kit", etc.
    después de perder el mensaje en el scroll.

    Usa la clave `activacion.msg_direccion_completa` porque es ahí donde
    están adjuntos los 3 contenidos (Vid 2.0, Tu QR único, Como usar el QR)."""
    _sm(telefono,
        "activacion.msg_direccion_completa",
        fallback=(
            "🎉 *¡Dirección confirmada!* 📍\n\n"
            "Ya pedimos tu kit de activación. En *2-3 días hábiles* lo recibís."
        ),
    )
    import time as _tm_rk
    _tm_rk.sleep(1)
    _send_buttons(telefono, _t(
        "activacion.msg_qr_boton",
        fallback="Cuando lo tengas instalado, pulsá el botón 👇",
    ), [{"id": "ONBAG_QR_INSTALADO", "title": "✅ Ya instalé el QR"}])


def _fase_kit_enviado(conn, telefono, cid, meta, texto, sesion_id=None):
    """Humano ya marcó el kit como enviado desde panel. Esperamos que la
    agente mande FOTO del QR instalado como soporte. El pase a 'activa'
    ocurre en `capturar_foto_qr()` (llamado desde webhook_worker cuando
    detecta un image IN con la agente en este estado)."""
    t_lc = (texto or "").lower().strip()
    # Shortcut: la agente pregunta por su kit → re-enviamos el bloque
    if t_lc in PALABRAS_KIT:
        _reenviar_kit_enviado(telefono, cid, meta)
        _log(telefono, texto, "[kit_enviado: re-envío por palabra clave]")
        return True
    if texto == "ONBAG_KIT_PERFECTO":
        _send_buttons(telefono, _t(
            "activacion.msg_pedir_foto_libro",
            fallback=(
                "📸 *Ahora necesitamos una foto del libro de "
                "arriendos y ventas* de tu conjunto.\n\n"
                "Pasá por la *portería o el lobby* y pedí ver el "
                "libro. Tomale una foto bien clara y mandámela por "
                "acá 💜\n\n"
                "✨ Con eso vamos a *cargar tu QR con la info de "
                "tu conjunto*, para que cuando lo instales ya "
                "funcione completo 🚀"
            ),
        ), [{"id": "ONBAG_FOTO_LIBRO", "title": "✨ Perfecto"}])
        _log(telefono, texto, "[kit_enviado: perfecto → pide foto libro]")
        return True
    if texto == "ONBAG_FOTO_LIBRO":
        _send_buttons(telefono, _t(
            "activacion.msg_aviso_kit",
            fallback=(
                "📦 *Avisanos cuando te llegue el kit* y mandanos "
                "una foto 📸\n\n"
                "Así confirmamos que te llegó bien y seguimos con "
                "la activación 💜"
            ),
        ), [{"id": "ONBAG_KIT_AVISO", "title": "✨ Perfecto"}])
        _log(telefono, texto, "[kit_enviado: paso aviso kit]")
        return True
    if texto == "ONBAG_KIT_AVISO":
        _send_buttons(telefono, _t(
            "activacion.msg_red_conectado",
            fallback=(
                "🎉 *¡Perfecto! Tu conjunto ya está conectado a la "
                "red Petra* 💜\n\n"
                "Desde ahora formás parte oficialmente de la red "
                "de agentes Petra 🚀"
            ),
        ), [{"id": "ONBAG_RED_PETRA", "title": "✨ Perfecto"}])
        _log(telefono, texto, "[kit_enviado: paso red petra]")
        return True
    if texto == "ONBAG_RED_PETRA":
        _send_buttons(telefono, _t(
            "activacion.msg_panel_intro",
            fallback=(
                "📊 *Ahora aparecerá tu nuevo panel de control*\n\n"
                "Desde ahí vas a poder ver:\n"
                "• Los *inmuebles* de tu conjunto 🏢\n"
                "• Tus *citas* 📅\n"
                "• Tus *nuevas capacitaciones* 🎓\n\n"
                "¡Vamos! 💜"
            ),
        ), [{"id": "ONBAG_IR_PANEL", "title": "✨ Perfecto"}])
        _log(telefono, texto, "[kit_enviado: paso intro panel]")
        return True
    if texto == "ONBAG_IR_PANEL":
        _mostrar_panel_control(telefono, meta)
        _log(telefono, texto, "[kit_enviado: muestra panel control]")
        return True
    if texto == "ONBAG_QR_INSTALADO" or t_lc in (
            "instalado", "instalado qr", "ya instale", "ya instalé",
            "ya instalé el qr", "qr instalado"):
        # NO avanza — pide foto como soporte
        _sm(telefono,
            "activacion.msg_pedir_foto_qr",
            fallback=(
                "¡Casi listo! 💜\n\n"
                "Para activarte oficialmente necesito *una foto del QR instalado*. "
                "Seguí estos 3 pasos:\n\n"
                "1️⃣ Pegá el QR en un lugar *bien visible* (portería, lobby o ascensor).\n"
                "2️⃣ Ponete a *50 cm* y verificá que el código *completo* entre en la toma.\n"
                "3️⃣ Mandámela por este chat 📸\n\n"
                "💡 _Tip: con buena luz y sin reflejos se lee mejor._\n\n"
                "Apenas me llegue, te activo y empezás a recibir leads 🚀"
            ),
        )
        _log(telefono, texto, "[kit_enviado → pide foto QR]")
        return True
    # Pregunta libre → LLM
    if texto:
        _log(telefono, texto, "[kit_enviado → LLM]")
        return _responder_con_llm(telefono, texto, sesion_id=sesion_id)
    return True


def capturar_foto_qr(telefono, local_path, media_id, mime, caption):
    """Hook de imágenes entrantes para Agente Petra en estado 'kit_enviado'.

    Según el sub-paso del kit (meta.kit_paso) interpreta la imagen como:
      - 'foto_libro_pedida' → foto(s) del libro de arriendos. Guarda ref,
        marca paso 'libro_recibido' y ack a la agente. Humano sube la
        info del libro + dispara /panel/petra/_admin/kit-qr-cargado?tel=
      - 'qr_foto_pendiente' → foto del QR instalado. Guarda ref, transita
        a 'activa' y envía bienvenida.
      - otros pasos → ignora (fallback al handler genérico de media).

    Devuelve True si consumió la imagen (webhook_worker salta el resto
    del processing de media), False en caso contrario."""
    from app import db as _db
    import datetime as _dt
    try:
        with _db._conexion_directa(timeout=5) as conn:
            row = conn.execute(
                "SELECT c.id, r.metadata FROM petra_contactos c "
                "JOIN petra_contacto_roles r ON r.contacto_id=c.id "
                "AND r.rol='agente_petra' WHERE c.telefono=%s", (telefono,),
            ).fetchone()
    except Exception:
        log.exception("[flow:foto-qr] query fallo tel=%s", telefono)
        return False
    if not row:
        return False
    cid, meta = row[0], dict(row[1] or {})
    if meta.get("estado_agente") != "kit_enviado":
        return False

    kit_paso = (meta.get("kit_paso") or "").strip()

    # ── Rama A: foto del libro de arriendos ────────────────────────────
    if kit_paso == "foto_libro_pedida":
        fotos = meta.get("libro_fotos") or []
        fotos.append({
            "local_path": local_path or "",
            "media_id": media_id or "",
            "mime": mime or "",
            "caption": caption or "",
            "at": _dt.datetime.utcnow().isoformat(),
        })
        meta["libro_fotos"] = fotos
        meta["libro_recibido_at"] = _dt.datetime.utcnow().isoformat()
        # Avanzamos directo al siguiente paso: cualquier foto hace avanzar.
        # Si llegan varias fotos en la misma tanda, solo la primera envía
        # el msg de qr_cargado; las siguientes solo persisten.
        ya_avanzado = meta.get("kit_paso") == "qr_cargado"
        if not ya_avanzado:
            meta["kit_paso"] = "qr_cargado"
            meta["qr_cargado_at"] = _dt.datetime.utcnow().isoformat()
        try:
            with _db._conexion_directa(timeout=10) as conn:
                _save_meta(conn, cid, meta)
            log.info("[flow:foto-libro] meta guardada tel=%s fotos=%d ya=%s",
                     telefono, len(fotos), ya_avanzado)
        except Exception:
            log.exception("[flow:foto-libro] save meta fallo tel=%s", telefono)
            return False
        if ya_avanzado:
            return True
        try:
            _send_buttons(
                telefono,
                _t(
                    "activacion.msg_qr_cargado",
                    fallback=(
                        "📸 *¡Hemos recibido tus fotos y hemos cargado el QR!* ✨\n\n"
                        "Tu QR ya está listo para funcionar con la info de tu conjunto 🚀\n\n"
                        "Apretá para seguir 👇"
                    ),
                ),
                [{"id": "ONBAG_QR_CARGADO",
                  "title": "✨ Perfecto"}],
            )
            _log(telefono, "[imagen]", "[kit:foto_libro → qr_cargado auto]",
                 nota=f"foto en {local_path} total={len(fotos)}")
        except Exception:
            log.exception("[flow:foto-libro] envio msgs fallo tel=%s", telefono)
        return True

    # ── Pasos intermedios (qr_cargado, guia_enviada) ──────────────────
    # La agente ya pasó la etapa del libro pero todavía no llegó al QR
    # instalado. Si manda una foto acá, es ruido (una del libro que llegó
    # tarde, un screenshot, etc.). Guardamos silencioso y NO avanzamos
    # al estado activa.
    if kit_paso in ("qr_cargado", "guia_enviada"):
        extras = meta.get("libro_fotos_extra") or []
        extras.append({
            "local_path": local_path or "",
            "media_id": media_id or "",
            "mime": mime or "",
            "caption": caption or "",
            "at": _dt.datetime.utcnow().isoformat(),
        })
        meta["libro_fotos_extra"] = extras
        try:
            with _db._conexion_directa(timeout=10) as conn:
                _save_meta(conn, cid, meta)
            log.info("[flow:foto-extra] tel=%s paso=%s guardada silenciosa",
                     telefono, kit_paso)
        except Exception:
            log.exception("[flow:foto-extra] save fallo tel=%s", telefono)
            return False
        return True

    # ── Rama B: foto del QR instalado (último paso) ────────────────────
    if kit_paso in ("qr_foto_pendiente", "pasos_mostrados", "kit_recibido", ""):
        # Si ya avanzamos a activa (foto previa llegó primero en la misma
        # tanda), solo persistimos la foto adicional y salimos.
        ya_activa = meta.get("estado_agente") == "activa"
        meta["qr_foto_local_path"] = local_path or ""
        meta["qr_foto_media_id"] = media_id or ""
        meta["qr_foto_mime"] = mime or ""
        meta["qr_foto_caption"] = caption or ""
        meta["qr_foto_at"] = _dt.datetime.utcnow().isoformat()
        if not ya_activa:
            _set_estado(meta, "activa")
            meta["activada_at"] = _dt.datetime.utcnow().isoformat()
        try:
            with _db._conexion_directa(timeout=10) as conn:
                _save_meta(conn, cid, meta)
            log.info("[flow:foto-qr] meta guardada tel=%s ya=%s",
                     telefono, ya_activa)
        except Exception:
            log.exception("[flow:foto-qr] save meta fallo tel=%s", telefono)
            return False
        if ya_activa:
            return True
        try:
            _sm(telefono,
                "activacion.msg_qr_foto_recibida",
                fallback=(
                    "📸 *¡Foto recibida!* Gracias por el soporte 💜\n\n"
                    "🎉 *¡Ya sos una Agente Petra activa!* Tu QR está en tu "
                    "edificio y los residentes ya pueden escanearlo para "
                    "contactarte. A partir de ahora te vamos a avisar cada "
                    "vez que llegue un interesado 📲\n\n"
                    "¡Mucho éxito! 🚀"
                ),
            )
            with _db._conexion_directa(timeout=5) as conn:
                _enviar_video_citas_una_vez(conn, cid, meta, telefono)
            # Mostramos el panel de agente activa (list con Mis inmuebles /
            # citas / capacitación / Pedir ayuda).
            try:
                _mostrar_panel_control(telefono, meta)
            except Exception:
                log.exception("[flow:foto-qr] panel-control fallo tel=%s", telefono)
            _log(telefono, "[imagen]", "[kit_enviado → foto QR → activa + panel]",
                 nota=f"foto en {local_path}")
        except Exception:
            log.exception("[flow:foto-qr] envio msgs fallo tel=%s", telefono)
        return True

    # Otro paso del kit (ej. dir_confirmada) — foto inesperada. La
    # guardamos silenciosa como extra para que el humano pueda revisarla
    # pero NO avanzamos ni respondemos (el flow sigue en su paso actual).
    extras = meta.get("fotos_extra_kit") or []
    extras.append({
        "paso_al_recibir": kit_paso or "",
        "local_path": local_path or "",
        "media_id": media_id or "",
        "mime": mime or "",
        "caption": caption or "",
        "at": _dt.datetime.utcnow().isoformat(),
    })
    meta["fotos_extra_kit"] = extras
    try:
        with _db._conexion_directa(timeout=10) as conn:
            _save_meta(conn, cid, meta)
        log.info("[flow:foto-extra-generica] tel=%s paso=%s guardada",
                 telefono, kit_paso)
    except Exception:
        log.exception("[flow:foto-extra-generica] save fallo tel=%s", telefono)
    return True


def _enviar_video_citas_una_vez(conn, cid, meta, telefono):
    """Envía el video 'cómo manejar las citas' solo la primera vez
    (flag en metadata). Silencio si no hay video cargado."""
    import time as _tm
    if meta.get("video_citas_enviado"):
        return
    vid = conn.execute(
        "SELECT titulo, url FROM petra_contenidos "
        "WHERE tipo='video_citas' AND activo=TRUE "
        "ORDER BY orden LIMIT 1"
    ).fetchone()
    if not vid:
        return
    _tm.sleep(2)
    from app import petra_wa as _pwa
    _pwa.send_text(telefono, _t(
        "citas.msg_intro_video",
        fallback=(
            "Antes de que empiecen a llegar interesados, mirá este video "
            "corto donde te explico cómo manejar las citas 💜 👇"
        ),
    ))
    _tm.sleep(1)
    _pwa.send_media_o_texto(telefono, vid[0], vid[1])
    meta["video_citas_enviado"] = True
    _save_meta(conn, cid, meta)


def _fase_activa(conn, telefono, cid, meta, texto, sesion_id=None):
    """Fase operativa: la agente ya tiene QR instalado. Maneja:
    - Botones de respuesta a citas (ONBAG_CITA_*)
    - Captura de contrapropuesta de día/hora si está esperando
    - Captura de inventario (step-by-step)
    - Preguntas libres → LLM"""
    import datetime as _dt
    t_lc = (texto or "").lower().strip()

    # 1. Respuestas a botones de cita
    if texto and texto.startswith("ONBAG_CITA_"):
        return _manejar_boton_cita(conn, telefono, cid, meta, texto)

    # 2. ¿Está esperando contrapropuesta de fecha?
    cita_esperando = meta.get("cita_esperando_propuesta_id")
    if cita_esperando and texto:
        return _capturar_contrapropuesta_cita(
            conn, telefono, cid, meta, texto, cita_esperando)

    # 3. ¿Está en medio de la captura de un inmueble?
    inv_paso = meta.get("inventario_paso")
    if inv_paso and texto:
        return _capturar_paso_inventario(conn, telefono, cid, meta, texto, inv_paso)

    # 4. ¿Palabra clave para arrancar inventario? ("registrar inmueble", "cargar inmueble", "nuevo inmueble")
    PALABRAS_INVENTARIO = (
        "registrar inmueble", "cargar inmueble", "nuevo inmueble",
        "agregar inmueble", "registrar apto", "cargar apto",
        "subir inmueble", "subir apto", "ingresar inmueble",
    )
    if texto and any(p in t_lc for p in PALABRAS_INVENTARIO):
        return _iniciar_inventario(conn, telefono, cid, meta)

    # 5. Pregunta libre → LLM
    _log(telefono, texto, "[activa → LLM]")
    return _responder_con_llm(telefono, texto, sesion_id=sesion_id)


# ─── FASE E — Inventario (captura step-by-step) ───

INVENTARIO_PASOS = [
    "modalidad", "torre", "apto", "area_m2", "habitaciones", "banos",
    "parqueadero", "precio", "propietario_nombre", "propietario_tel",
]
INVENTARIO_PREGUNTAS = {
    "modalidad": (
        "inventario.pregunta_modalidad",
        "¿Es para *arriendo* o *venta*? Respondé con una de las dos palabras.",
    ),
    "torre": (
        "inventario.pregunta_torre",
        "¿En qué *torre* está el inmueble? (si no aplica, respondé *N/A*)",
    ),
    "apto": (
        "inventario.pregunta_apto",
        "¿Cuál es el *número de apartamento*?",
    ),
    "area_m2": (
        "inventario.pregunta_area",
        "¿Cuántos *metros cuadrados* tiene? (solo el número)",
    ),
    "habitaciones": (
        "inventario.pregunta_habitaciones",
        "¿Cuántas *habitaciones* tiene? (solo el número)",
    ),
    "banos": (
        "inventario.pregunta_banos",
        "¿Cuántos *baños* tiene? (solo el número)",
    ),
    "parqueadero": (
        "inventario.pregunta_parqueadero",
        "¿Tiene *parqueadero*? Respondé *sí* o *no*.",
    ),
    "precio": (
        "inventario.pregunta_precio",
        "¿Cuál es el *precio*? Respondé en millones de pesos. "
        "Ej: *1.200* (si son 1.200 millones para venta), o *2.5* "
        "(si son 2.5 millones de arriendo mensual).",
    ),
    "propietario_nombre": (
        "inventario.pregunta_propietario_nombre",
        "¿Cuál es el *nombre del propietario*?",
    ),
    "propietario_tel": (
        "inventario.pregunta_propietario_tel",
        "¿Cuál es el *teléfono del propietario*? (con indicativo, ej. 573001234567)",
    ),
}


def _iniciar_inventario(conn, telefono, cid, meta):
    """Arranca captura de un nuevo inmueble. Envía video explicativo
    (si está cargado) y hace la primera pregunta."""
    meta["inventario_paso"] = INVENTARIO_PASOS[0]
    meta["inventario_datos"] = {}
    _save_meta(conn, cid, meta)
    # Video explicativo (si cargado)
    vid = conn.execute(
        "SELECT titulo, url FROM petra_contenidos "
        "WHERE tipo='video_inventario' AND activo=TRUE "
        "ORDER BY orden LIMIT 1"
    ).fetchone()
    _sm(telefono, 
        "inventario.msg_arranque",
        fallback=(
            "¡Buenísimo! 🏠 Vamos a cargar el inmueble en el sistema.\n\n"
            "Te voy a hacer unas preguntas para registrarlo. Si querés "
            "cancelar en cualquier momento, decime *cancelar inmueble*."
        ),
    )
    if vid:
        import time as _tm
        _tm.sleep(1)
        _enviar(telefono, f"🏠 *{vid[0]}*\n\n{vid[1]}")
        _tm.sleep(2)
    # Primera pregunta
    clave, fallback = INVENTARIO_PREGUNTAS[INVENTARIO_PASOS[0]]
    _sm(telefono, clave, fallback=fallback)
    _log(telefono, "", "[inventario: arranque]")
    return True


def _capturar_paso_inventario(conn, telefono, cid, meta, texto, paso):
    """Procesa la respuesta a la pregunta del paso actual.
    Valida, guarda y avanza al próximo. Al terminar, inserta en DB."""
    t_lc = texto.lower().strip()
    # Cancelación
    if t_lc in ("cancelar inmueble", "cancelar", "cancelo", "stop"):
        meta.pop("inventario_paso", None)
        meta.pop("inventario_datos", None)
        _save_meta(conn, cid, meta)
        _enviar(telefono, "Ok, cancelo la carga del inmueble. "
                "Cuando quieras arrancar de nuevo decime *registrar inmueble*.")
        _log(telefono, texto, "[inventario cancelado por user]")
        return True

    datos = meta.get("inventario_datos") or {}
    valido, valor, err = _validar_paso_inventario(paso, texto)
    if not valido:
        _enviar(telefono, err or "Ese dato no me parece bien. ¿Me lo mandás de nuevo?")
        return True
    datos[paso] = valor
    meta["inventario_datos"] = datos

    # Próximo paso
    idx = INVENTARIO_PASOS.index(paso)
    if idx + 1 < len(INVENTARIO_PASOS):
        prox = INVENTARIO_PASOS[idx + 1]
        meta["inventario_paso"] = prox
        _save_meta(conn, cid, meta)
        clave, fallback = INVENTARIO_PREGUNTAS[prox]
        _sm(telefono, clave, fallback=fallback)
        _log(telefono, texto, f"[inventario: capturó {paso}, pide {prox}]")
        return True

    # Terminó la captura → insertar en DB
    return _finalizar_inventario(conn, telefono, cid, meta, datos)


def _validar_paso_inventario(paso, texto):
    """Devuelve (valido, valor_normalizado, mensaje_error)."""
    t = (texto or "").strip()
    t_lc = t.lower()
    if paso == "modalidad":
        if t_lc in ("arriendo", "arrendar", "alquiler"):
            return True, "arriendo", None
        if t_lc in ("venta", "vender", "venta/arriendo"):
            return True, "venta", None
        return False, None, "Decime *arriendo* o *venta*, por favor."
    if paso in ("torre", "apto", "propietario_nombre"):
        if 1 <= len(t) <= 60:
            return True, t, None
        return False, None, "Ese valor parece raro. ¿Me lo volvés a mandar?"
    if paso in ("area_m2", "habitaciones", "banos"):
        import re
        m = re.search(r"\d+", t.replace(",", "."))
        if not m:
            return False, None, "Necesito un número. ¿Me lo volvés a mandar?"
        try:
            n = float(m.group()) if paso == "area_m2" else int(m.group())
            if n <= 0 or n > 10000:
                return False, None, "Ese número parece raro. ¿Me lo revisás?"
            return True, n, None
        except Exception:
            return False, None, "No entendí el número. ¿Me lo mandás así: *85* o *3*?"
    if paso == "parqueadero":
        if t_lc in ("si", "sí", "yes", "y", "tiene"):
            return True, True, None
        if t_lc in ("no", "n", "no tiene"):
            return True, False, None
        return False, None, "Decime *sí* o *no*."
    if paso == "precio":
        import re
        m = re.search(r"[\d.,]+", t)
        if not m:
            return False, None, "Necesito el precio en número. Ej: *1200* o *2.5*."
        try:
            # Normalización: "1.200" → 1200.0 ; "2.5" → 2.5
            raw = m.group()
            if "," in raw and "." not in raw:
                raw = raw.replace(",", ".")
            if raw.count(".") > 1:
                # 1.200.000 → 1200000
                raw = raw.replace(".", "")
            n = float(raw)
            if n <= 0 or n > 100000:
                return False, None, "Ese precio parece raro. ¿Me lo revisás?"
            return True, n, None
        except Exception:
            return False, None, "No entendí el precio. ¿Me lo mandás como número?"
    if paso == "propietario_tel":
        import re
        digitos = re.sub(r"\D", "", t)
        if 7 <= len(digitos) <= 15:
            return True, digitos, None
        return False, None, "Ese teléfono parece raro. Mandámelo con indicativo, ej. *573001234567*."
    return True, t, None


def _finalizar_inventario(conn, telefono, cid, meta, datos):
    """Inserta petra_inmuebles con los datos capturados. Resuelve
    edificio_id a partir del conjunto de la agente (crea si falta)."""
    try:
        conjunto = (meta.get("conjunto") or "Sin conjunto").strip()
        ciudad = (meta.get("ciudad") or "").strip()
        # Resolver edificio
        row = conn.execute(
            "SELECT id FROM petra_edificios WHERE nombre=%s AND ciudad=%s",
            (conjunto, ciudad),
        ).fetchone()
        if row:
            edificio_id = row[0]
        else:
            edificio_id = conn.execute(
                "INSERT INTO petra_edificios (nombre, ciudad, activo) "
                "VALUES (%s, %s, TRUE) RETURNING id",
                (conjunto, ciudad),
            ).fetchone()[0]
        # Insert inmueble
        modalidad = datos["modalidad"]
        precio_venta = datos["precio"] if modalidad == "venta" else None
        precio_arriendo = datos["precio"] if modalidad == "arriendo" else None
        conn.execute(
            "INSERT INTO petra_inmuebles "
            "(edificio_id, contacto_id, tipo, modalidad, torre, piso, "
            " area_m2, parqueadero, precio_millones, "
            " precio_arriendo_mensual_millones, nombre_anunciante, "
            " telefono_contacto, habitaciones, banos, "
            " capturado_por_agente_id, estado) "
            "VALUES (%s, %s, 'apartamento', %s, %s, %s, %s, %s, %s, %s, "
            "        %s, %s, %s, %s, %s, 'borrador')",
            (
                edificio_id, cid, modalidad,
                datos.get("torre"), datos.get("apto"),
                datos.get("area_m2"), datos.get("parqueadero"),
                precio_venta, precio_arriendo,
                datos.get("propietario_nombre"),
                datos.get("propietario_tel"),
                datos.get("habitaciones"), datos.get("banos"),
                cid,
            ),
        )
    except Exception:
        log.exception("[inventario] insert fallo tel=%s datos=%s", telefono, datos)
        _enviar(telefono,
            "Perdón, hubo un error guardando el inmueble 😔. "
            "Un asesor te contacta pronto para resolverlo.")
        meta.pop("inventario_paso", None)
        meta.pop("inventario_datos", None)
        _save_meta(conn, cid, meta)
        return True
    # Limpiar estado y confirmar
    meta.pop("inventario_paso", None)
    meta.pop("inventario_datos", None)
    _save_meta(conn, cid, meta)
    resumen = _formatear_resumen_inmueble(datos)
    _sm(telefono, 
        "inventario.msg_completado",
        fallback=(
            "✅ *¡Inmueble registrado!*\n\n"
            f"{resumen}\n\n"
            "Lo revisamos y lo publicamos pronto 💜. Si querés cargar otro, "
            "decime *registrar inmueble*."
        ),
    )
    _log(telefono, "", "[inventario: completado]")
    return True


def _formatear_resumen_inmueble(datos):
    partes = [
        f"📍 *Modalidad:* {datos.get('modalidad','?').title()}",
        f"🏢 *Torre/Apto:* {datos.get('torre','?')} / {datos.get('apto','?')}",
        f"📐 *Área:* {datos.get('area_m2','?')} m²",
        f"🛏 *Hab:* {datos.get('habitaciones','?')} · 🚿 *Baños:* {datos.get('banos','?')}",
        f"🚗 *Parqueadero:* {'sí' if datos.get('parqueadero') else 'no'}",
        f"💰 *Precio:* ${datos.get('precio','?')} M",
        f"👤 *Propietario:* {datos.get('propietario_nombre','?')}",
        f"📞 *Tel:* {datos.get('propietario_tel','?')}",
    ]
    return "\n".join(partes)


def _manejar_boton_cita(conn, telefono, cid, meta, texto):
    """Procesa botones ONBAG_CITA_{PUEDO|PROPONER|NO_PUEDO}_<visita_id>."""
    import datetime as _dt
    partes = texto.replace("ONBAG_CITA_", "").rsplit("_", 1)
    if len(partes) != 2 or not partes[1].isdigit():
        _enviar(telefono, "No reconocí ese botón. ¿Me podés decir qué quisiste decir?")
        return True
    accion, vid = partes[0], int(partes[1])
    # Verificar que la cita exista y le corresponda a esta agente
    row = conn.execute(
        "SELECT v.estado, v.programada_at, c.nombre "
        "FROM petra_visitas v "
        "LEFT JOIN petra_contactos c ON c.id = v.interesado_id "
        "WHERE v.id=%s AND v.agente_id=%s",
        (vid, cid),
    ).fetchone()
    if not row:
        _enviar(telefono, "No encontré esa cita 🤔. Si tenés dudas, escribime.")
        return True
    estado_cita, programada_at, interesado = row
    if estado_cita in ("realizada", "cancelada"):
        _enviar(telefono,
            f"Esa cita ya está marcada como *{estado_cita}* — no la puedo modificar.")
        return True

    if accion == "PUEDO":
        conn.execute(
            "UPDATE petra_visitas SET estado='confirmada', updated_at=NOW() "
            "WHERE id=%s", (vid,),
        )
        _sm(telefono, 
            "citas.msg_confirmada",
            fallback=(
                "¡Dale! 💜 Confirmada la cita con *{interesado}* para el "
                "*{cuando}*. Te recuerdo 30 min antes 📲"
            ),
            interesado=interesado or "el interesado",
            cuando=_fmt_fecha(programada_at),
        )
        meta.pop("cita_esperando_propuesta_id", None)
        _save_meta(conn, cid, meta)
        _log(telefono, texto, f"[cita {vid} confirmada]")
        return True

    if accion == "NO_PUEDO":
        conn.execute(
            "UPDATE petra_visitas SET estado='cancelada', updated_at=NOW() "
            "WHERE id=%s", (vid,),
        )
        _sm(telefono, 
            "citas.msg_cancelada",
            fallback=(
                "Ok, cancelo la cita 💜. Si querés proponer otro día más "
                "adelante, decime."
            ),
        )
        meta.pop("cita_esperando_propuesta_id", None)
        _save_meta(conn, cid, meta)
        _log(telefono, texto, f"[cita {vid} cancelada]")
        return True

    if accion == "PROPONER":
        meta["cita_esperando_propuesta_id"] = vid
        _save_meta(conn, cid, meta)
        _sm(telefono, 
            "citas.msg_pedir_contrapropuesta",
            fallback=(
                "Dale, decime qué día y hora te quedarían bien y yo le "
                "paso tu propuesta al interesado 💜\n\n"
                "(Ej: *jueves a las 3pm*, *mañana 10am*, etc.)"
            ),
        )
        _log(telefono, texto, f"[cita {vid} → pide contrapropuesta]")
        return True

    _enviar(telefono, "No reconocí esa acción. ¿Podés contarme qué querías hacer?")
    return True


def _capturar_contrapropuesta_cita(conn, telefono, cid, meta, texto, vid):
    """La agente escribió su contrapropuesta tras pulsar 'Proponer otro día'.
    La guardamos en observaciones y marcamos la visita como 'cancelada' con
    nota (el humano del panel decide si re-abrir con nueva fecha)."""
    conn.execute(
        "UPDATE petra_visitas SET estado='cancelada', "
        "observaciones=COALESCE(observaciones,'') || "
        "'\n[Agente propone: ' || %s || ']', updated_at=NOW() "
        "WHERE id=%s", (texto[:500], vid),
    )
    _sm(telefono, 
        "citas.msg_contrapropuesta_recibida",
        fallback=(
            "¡Perfecto! Le paso al interesado tu propuesta: *{propuesta}* 💜\n\n"
            "Te aviso cuando confirme."
        ),
        propuesta=texto[:200],
    )
    meta.pop("cita_esperando_propuesta_id", None)
    _save_meta(conn, cid, meta)
    _log(telefono, texto, f"[cita {vid} contrapropuesta: {texto[:100]}]")
    return True


def _fmt_fecha(dt):
    """Formatea un datetime para WhatsApp en español Colombia."""
    if not dt:
        return "fecha por confirmar"
    try:
        import datetime as _d
        if isinstance(dt, str):
            dt = _d.datetime.fromisoformat(dt.replace("Z", "+00:00"))
        dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
        meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
                 "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
        return (f"{dias[dt.weekday()]} {dt.day} de {meses[dt.month-1]} "
                f"a las {dt.hour:02d}:{dt.minute:02d}")
    except Exception:
        return str(dt)


def _fase_capacitacion(conn, telefono, cid, meta, texto, estado, sesion_id=None):
    """Fase: la agente está en capacitación activa. Los 12 videos se envían
    por cron a las 8am (2 por día x 6 días). Acá solo gestionamos
    confirmaciones ("vi", "listo") y preguntas sobre el calendario."""
    t_lc = (texto or "").lower().strip()
    import datetime as _dt
    vistos = meta.get("videos_vistos", 0)
    cap_v = int(meta.get("cap_version", 1) or 1)
    total = 14 if cap_v >= 2 else 12
    dias_total = 7 if cap_v >= 2 else 6
    if (texto == "ONBAG_LISTO" or
            t_lc in ("vi", "visto", "listo", "ya", "ok", "terminé", "termine")):
        # UN "listo" cuenta por los 2 videos del día. Lleva al próximo
        # múltiplo de 2 y cap al total según cap_version.
        vistos = min(((vistos // 2) + 1) * 2, total)
        meta["videos_vistos"] = vistos
        meta[f"dia_{vistos // 2}_visto_at"] = _dt.datetime.utcnow().isoformat()
        if vistos >= total:
            _set_estado(meta, "autorizacion_pendiente")
            _save_meta(conn, cid, meta)
            from app import petra_wa as _pwa_cap
            # Felicitación + panel de control directo. El estado interno
            # queda en autorizacion_pendiente para que el modelo de datos
            # siga consistente, pero no mostramos el paso de autorización
            # en WA — la agente entra directo al panel de control.
            _pwa_cap.send_text(telefono, _t(
                "capacitacion.msg_completada_header",
                fallback=(
                    "🎓🎉 *¡Terminaste tu capacitación!* ✨\n\n"
                    "Ya tenés las herramientas para arrancar como Agente "
                    "Petra 💜\n\n"
                    "Aquí tenés tu *panel de control* 👇"
                ),
            ))
            _mostrar_panel_control(telefono, meta)
            _log(telefono, texto, "[capacitación completa → panel de control]",
                 nota="capacitacion_completa → panel de control")
            return True
        else:
            _save_meta(conn, cid, meta)
            dia_actual = vistos // 2
            quedan = total - vistos
            dias_restantes = (total - vistos) // 2
            resp = _t(
                "capacitacion.msg_dia_completo",
                fallback=(
                    "🎉 *¡Felicitaciones, cerraste el día {dia_actual} de {dias_total}!* 🌟\n\n"
                    "Ya viste *{vistos}/{total} videos* ✅ Cada uno te suma herramientas, "
                    "confianza y conocimiento para arrancar como Agente Petra en tu edificio 🏢💜\n\n"
                    "Te quedan solo *{quedan} videos ({dias_restantes} días)* para completar "
                    "toda tu capacitación. Vas re bien, dale con todo 💪\n\n"
                    "Mañana a las *8am* te mando los próximos 2 videos, para que no pierdas "
                    "el ritmo. O si tenés ganas de seguir avanzando ahora mismo, apretá el botón 👇"
                ),
                dia_actual=dia_actual, dias_total=dias_total,
                vistos=vistos, total=total,
                quedan=quedan, dias_restantes=dias_restantes,
            )
        _send_buttons(telefono, resp, [
            {"id": "ONBAG_SIGUIENTE_DIA",
             "title": _b("ONBAG_SIGUIENTE_DIA", "⏭ Ver día siguiente")},
        ], estado_key="en_capacitacion")
        _log(telefono, texto, resp, nota=f"día marcado {vistos}/{total}")
        return True
    # Botón "Ver día siguiente": dispara el próximo día inmediatamente,
    # sin esperar al cron de las 8am. Reusa la función del cron.
    if texto == "ONBAG_SIGUIENTE_DIA":
        import datetime as _dt_, json as _json_, time as _time_
        try:
            from zoneinfo import ZoneInfo
            hoy_bogota = _dt_.datetime.now(ZoneInfo("America/Bogota")).date().isoformat()
        except Exception:
            hoy_bogota = _dt_.datetime.utcnow().date().isoformat()
        try:
            from app.scheduler import _cap_enviar_para_agente
            from app import petra_wa as _pwa
            enviado = _cap_enviar_para_agente(
                conn, cid, telefono, meta, hoy_bogota,
                _pwa, _time_, _json_, ignorar_dedup_fecha=True,
            )
        except Exception:
            log.exception("[flow:cap] fallo disparar día siguiente tel=%s", telefono)
            enviado = False
        if not enviado:
            _sm(telefono, 
                "capacitacion.msg_siguiente_dia_fallo",
                fallback=(
                    "Hmm, no pude mandarte el siguiente par ahora. "
                    "Mañana a las 8am te llega sin falta. 🙏"
                ),
            )
        _log(telefono, texto, f"[ver día siguiente — enviado={enviado}]",
             nota="siguiente_dia manual")
        return True
    if t_lc in ("siguiente", "proximo", "próximo", "mas", "más"):
        # Enviar el siguiente video pendiente del día si queda uno
        row = conn.execute(
            "SELECT titulo, url FROM petra_contenidos "
            "WHERE tipo='video_capacitacion' AND activo=TRUE "
            "AND orden > %s ORDER BY orden LIMIT 1",
            (vistos,),
        ).fetchone()
        if not row:
            _sm(telefono, 
                "capacitacion.msg_por_hoy_ya",
                fallback="Por hoy ya está. Mañana a las 8am te mando el próximo. 👋",
            )
            return True
        titulo, url = row
        _enviar(telefono, f"🎬 *{titulo}*\n\n{url}")
        _enviar(telefono, "Cuando lo veas mandame *LISTO* 💪")
        _log(telefono, texto, f"[enviado video manual: {titulo}]")
        return True
    # Intento de intención: "reenviar/perdí/mandame los videos" → re-enviamos
    # los 2 videos del día actual (cap_dia_enviado) sin tocar metadata.
    if any(kw in t_lc for kw in ("reenviar", "reenvia", "re-enviar", "perdi", "perdí",
                                   "mandame", "mándame", "manda los videos",
                                   "volver a enviar", "volver ah enviar",
                                   "no vi", "no me llego", "no me llegó")):
        dia_actual_env = int(meta.get("cap_dia_enviado", 0) or 0)
        if dia_actual_env >= 1:
            if cap_v >= 2 and dia_actual_env == 1:
                row_b = conn.execute(
                    "SELECT titulo, url FROM petra_contenidos "
                    "WHERE tipo='bienvenida' AND activo=TRUE ORDER BY orden LIMIT 1"
                ).fetchone()
                row_p = conn.execute(
                    "SELECT titulo, url FROM petra_contenidos "
                    "WHERE tipo='proximos_pasos' AND activo=TRUE ORDER BY orden LIMIT 1"
                ).fetchone()
                videos = [r for r in (row_b, row_p) if r]
            else:
                offset_dia = (dia_actual_env - 2) if cap_v >= 2 else (dia_actual_env - 1)
                offset = offset_dia * 2
                cur_v = conn.execute(
                    "SELECT titulo, url FROM petra_contenidos "
                    "WHERE tipo='video_capacitacion' AND activo=TRUE "
                    "ORDER BY orden LIMIT 2 OFFSET %s", (offset,),
                )
                videos = cur_v.fetchall()
            if len(videos) >= 2:
                from app import petra_wa as _pwa
                import time as _tm
                _pwa.send_text(telefono, _t(
                    "capacitacion.msg_reenvio_videos",
                    fallback="¡Claro! Acá los tenés de nuevo 💜 — videos del día *{dia_actual_env}/{dias_total}*:",
                    dia_actual_env=dia_actual_env, dias_total=dias_total,
                ))
                _tm.sleep(1)
                _pwa.send_media_o_texto(telefono, videos[0][0], videos[0][1])
                _tm.sleep(2)
                _pwa.send_media_o_texto(telefono, videos[1][0], videos[1][1])
                _tm.sleep(2)
                _send_buttons(telefono,
                    "Cuando los termines, pulsá el botón 👇",
                    [{"id": "ONBAG_LISTO", "title": _b("ONBAG_LISTO", "✅ Listo")}],
                    estado_key="en_capacitacion",
                )
                _log(telefono, texto, f"[reenvío día {dia_actual_env}]",
                     nota="reenvío manual por intención")
                return True

    # Preguntas abiertas en capacitación: delegamos al LLM agente_petra
    # que tiene el contexto del estado/fase y puede responder cualquier duda.
    # Anti-spam 30s para no gatillar múltiples llamadas al LLM por mensajes
    # rapidísimos del mismo contacto.
    import datetime as _dt
    now = _dt.datetime.utcnow()
    ultimo_at = meta.get("ultimo_llm_at")
    if ultimo_at:
        try:
            dt_last = _dt.datetime.fromisoformat(ultimo_at)
            if (now - dt_last).total_seconds() < 30:
                _log(telefono, texto, "[silencio: LLM <30s]",
                     nota="anti-spam LLM capacitación")
                return True
        except Exception:
            pass
    meta["ultimo_llm_at"] = now.isoformat()
    _save_meta(conn, cid, meta)
    _log(telefono, texto, "[delegado a LLM agente_petra]",
         nota=f"fase capacitación {vistos}/{total}")
    return _responder_con_llm(telefono, texto, sesion_id=sesion_id)


def _fase_bienvenida_enviada(conn, telefono, cid, meta, texto):
    """Fase: ya recibió los videos de bienvenida, esperamos confirmación.
    Al pulsar 'Empezar ahora' (o listo/vi/etc), disparamos día 1 de
    capacitación en el momento — sin esperar al cron de las 8am."""
    t_lc = (texto or "").lower().strip()
    acepta = ONBAG_EMPEZAR_AHORA = "ONBAG_EMPEZAR_AHORA"
    legacy_ya_vi = "ONBAG_YA_VI_VIDEOS"  # botón viejo, por si queda en WhatsApp
    if (texto in (ONBAG_EMPEZAR_AHORA, legacy_ya_vi) or
            t_lc in ("listo", "vi", "visto", "ya", "ya lo vi", "ok", "si",
                     "sí", "terminé", "termine", "empezar", "empezar ahora",
                     "arrancar", "ver videos", "ver mis videos")):
        import datetime as _dt, json as _json, time as _time
        try:
            from zoneinfo import ZoneInfo
            hoy_bogota = _dt.datetime.now(ZoneInfo("America/Bogota")).date().isoformat()
        except Exception:
            hoy_bogota = _dt.datetime.utcnow().date().isoformat()
        meta["bienvenida_vista_at"] = _dt.datetime.utcnow().isoformat()
        _set_estado(meta, "esperando_capacitacion_dia1")
        _save_meta(conn, cid, meta)
        # Disparar día 1 inmediatamente reusando la función del cron.
        try:
            from app.scheduler import _cap_enviar_para_agente
            from app import petra_wa as _pwa
            enviado = _cap_enviar_para_agente(
                conn, cid, telefono, meta, hoy_bogota,
                _pwa, _time, _json, ignorar_dedup_fecha=True,
            )
        except Exception:
            log.exception("[flow:onboarding] fallo disparar día 1 inmediato tel=%s", telefono)
            enviado = False
        if enviado:
            _log(telefono, texto, "[día 1 enviado inmediato por botón Empezar ahora]",
                 nota="empezar_ahora → día 1 disparado")
        else:
            # Fallback: no se pudo enviar (sin videos, pausa, dedup, etc).
            # En vez de decir "nos vemos mañana", mandamos un mensaje con
            # botón "▶️ Ver mis videos" (ONBAG_VER_VIDEOS_HOY, que ya tiene
            # handler) para que la agente pueda arrancar.
            _send_buttons(telefono, _t(
                "capacitacion.msg_listo_para_arrancar",
                fallback=(
                    "🎉✨ *¡Todo listo!*\n\n"
                    "Tu capacitación arranca cuando vos quieras 💜\n\n"
                    "🎬 Vas a ver *2 videos cortos por día* — a tu "
                    "ritmo, sin presión.\n"
                    "📚 En *7 días* vas a estar lista para ser una "
                    "Agente Petra experta en tu edificio 🏢\n\n"
                    "¿Querés ver los videos de hoy? Tocá el botón 👇"
                ),
            ), [{"id": "ONBAG_VER_VIDEOS_HOY", "title": _b("ONBAG_VER_VIDEOS_HOY", "▶️ Ver mis videos")}],
               estado_key="bienvenida_enviada")
            _log(telefono, texto, "[empezar_ahora: sin envío → botón ver videos]",
                 nota="fallback con botón retry")
        return True
    # Cualquier otra cosa: recordatorio con botón
    _send_buttons(telefono, _t(
        "capacitacion.msg_recordatorio_empezar",
        fallback="Cuando termines de ver los videos, pulsá el botón para avanzar 🎬",
    ), [{"id": "ONBAG_EMPEZAR_AHORA", "title": _b("ONBAG_EMPEZAR_AHORA", "Empezar ahora")}],
       estado_key="bienvenida_enviada")
    _log(telefono, texto, "[recordatorio: btn Empezar ahora]",
         nota="bienvenida_enviada esperando confirmación")
    return True


def handle(telefono, texto, es_handoff=False, sesion_id=None):
    """Entry-point del flow — wrapper que llama al core y después dispara
    el envío de media adjunta al estado actual (una vez por contacto)."""
    try:
        result = _handle_core(telefono, texto, es_handoff, sesion_id)
    finally:
        try:
            _hook_media_post_dispatch(telefono)
        except Exception:
            log.exception("[flow:media-hook] fallo tel=%s", telefono)
    return result


def _hook_media_post_dispatch(telefono):
    """Después de cada tick del flow, revisa el estado actual del contacto
    y envía la media adjunta a ese estado si no fue enviada aún."""
    from app import db as _db
    with _db._conexion_directa(timeout=5) as conn:
        row = conn.execute(
            "SELECT c.id, r.metadata FROM petra_contactos c "
            "JOIN petra_contacto_roles r ON r.contacto_id=c.id "
            "AND r.rol='agente_petra' WHERE c.telefono=%s", (telefono,),
        ).fetchone()
        if not row:
            return
        cid, meta = row[0], dict(row[1] or {})
        estado = meta.get("estado_agente")
        if estado:
            _enviar_media_estacion(conn, cid, meta, telefono, estado)


def _handle_core(telefono, texto, es_handoff=False, sesion_id=None):
    """Procesa un mensaje del user en el flujo de onboarding.
    Determinístico: sin LLM, siempre predecible."""
    from app import db as _db
    texto = (texto or "").strip()
    with _db._conexion_directa(timeout=10) as conn:
        cid = _upsert_contacto(conn, telefono)
        row = conn.execute(
            "SELECT metadata FROM petra_contacto_roles "
            "WHERE contacto_id=%s AND rol='agente_petra'",
            (cid,),
        ).fetchone()
        meta = dict(row[0]) if row and row[0] else {}

        # Si no hay rol todavía o estado no setado, iniciar
        if "estado_agente" not in meta:
            _set_estado(meta, "recolectando")
            _save_meta(conn, cid, meta)

        estado = meta.get("estado_agente", "recolectando")

        # ─── M4.B: modo "Pedir ayuda" (FAQ) ────────────────────────────
        # Si la agente activó el modo FAQ desde el panel de control, los
        # textos libres (no botones ONBAG_*) van directamente al mini-agente
        # FAQ. Palabras tipo "listo", "gracias" cierran el modo.
        if meta.get("en_faq") and texto and not texto.startswith(
                ("ONBAG_", "CUSTOM_")):
            try:
                from app.petra_agents import agente_faq as _faq
                boton_volver = {
                    "id": "ONBAG_FAQ_VOLVER_MENU",
                    "title": _b("ONBAG_FAQ_VOLVER_MENU", "↩ Volver al menú"),
                }
                res = _faq.responder(telefono, texto, meta,
                                     ultimo_paso_descripcion="volver al menú",
                                     ultimo_boton=boton_volver, cid=cid)
                if res.get("cerrar_faq"):
                    meta["en_faq"] = False
                    meta.pop("faq_ofrecimos_chat", None)
                    _save_meta(conn, cid, meta)
                    _mostrar_panel_control(telefono, meta)
                    _log(telefono, texto, "[faq → cerrado, panel reofrecido]")
                    return True
                if res.get("texto"):
                    _send_buttons(telefono, res["texto"], [boton_volver])
                if res.get("escalar"):
                    try:
                        conn.execute(
                            "INSERT INTO pilot_leads "
                            "(telefono, requiere_humano, "
                            " requiere_humano_motivo, requiere_humano_at) "
                            "VALUES (%s, TRUE, %s, NOW()) "
                            "ON CONFLICT (telefono) DO UPDATE SET "
                            " requiere_humano=TRUE, "
                            " requiere_humano_motivo=EXCLUDED.requiere_humano_motivo, "
                            " requiere_humano_at=NOW()",
                            (telefono, "faq_pedido_llamada"),
                        )
                    except Exception:
                        log.exception("[faq] marcar_requiere_humano fallo")
                # Marcamos que ya ofrecimos chat para que una siguiente
                # insistencia escale.
                if res.get("fuente") == "heuristica" and not res.get("escalar"):
                    meta["faq_ofrecimos_chat"] = True
                    _save_meta(conn, cid, meta)
                _log(telefono, texto, "[faq fase7]",
                     nota=f"fuente={res.get('fuente')}")
                return True
            except Exception:
                log.exception("[faq] fallo en modo pedir_ayuda tel=%s", telefono)
                # Caemos al flow normal.

        # ─── Botón global "Volver al menú" del modo FAQ ─────────────────
        # Cierra el modo FAQ y devuelve a la agente al paso donde quedó.
        # En capacitación la lleva de vuelta a terminar sus videos del
        # día (re-envía los 2 videos + botón Listo).
        if texto == "ONBAG_FAQ_VOLVER_MENU":
            meta["en_faq"] = False
            meta.pop("faq_ofrecimos_chat", None)
            _save_meta(conn, cid, meta)
            reenviado = False
            if estado in ("en_capacitacion", "esperando_capacitacion_dia1"):
                try:
                    from app.petra_agents import capacitacion_v3 as _cv3
                    reenviado = bool(_cv3._reenviar_dia_actual(conn, telefono, meta))
                except Exception:
                    log.exception("[faq] reenvío día actual fallo tel=%s", telefono)
            if not reenviado:
                _mostrar_panel_control(telefono, meta)
            _log(telefono, texto, "[faq volver al menú]",
                 nota=f"estado={estado} reenviado={reenviado}")
            return True

        # ─── Shortcut de prueba (Fabricio): 'primero' ─────────────────
        # Escribir 'primero' desde el 3227057375 en cualquier estado
        # resetea a inscripción (recolectando) y manda el mensaje de
        # bienvenida con los 6 datos a pedir.
        if (texto or "").lower().strip() in ("primero", "mundo") and (telefono or "").endswith("3227057375"):
            _set_estado(meta, "recolectando")
            # Reset total de los 5 campos + flag de bienvenida para
            # poder probar el flow desde cero (recibir el saludo otra vez).
            for _c in ("nombre", "cedula", "ciudad", "barrio", "conjunto",
                       "inscripcion_prompted_at"):
                meta.pop(_c, None)
            _save_meta(conn, cid, meta)
            _sm(telefono,
                "inscripcion.msg_bienvenida",
                fallback=(
                    "¡Hola! 💜 Bienvenida a Petra.\n\n"
                    "Perfecto, vamos a inscribirte en menos de *2 minutos*. "
                    "Solo necesito 6 datos:\n\n"
                    "1. *Tu nombre completo*\n"
                    "2. Tu cédula\n"
                    "3. Tu ciudad\n"
                    "4. Tu barrio\n"
                    "5. El nombre del conjunto o edificio donde vivís\n"
                    "6. Tu torre y apartamento (o número de casa)\n\n"
                    "¿Empezamos? Decime tu *nombre completo* primero. 👤"
                ),
            )
            _log(telefono, texto, "[shortcut global 'primero' → bienvenida]",
                 nota=f"estado={estado}")
            return True

        # ─── Shortcut de prueba (Fabricio): 'aviso' ───────────────────
        # Simula el final de la capacitación: felicita y abre el panel
        # de control. Útil para probar el flujo post-capacitación sin
        # tener que ver los 12 videos.
        if (texto or "").lower().strip() == "aviso":
            _set_estado(meta, "autorizacion_pendiente")
            _save_meta(conn, cid, meta)
            from app import petra_wa as _pwa_aviso
            _pwa_aviso.send_text(telefono, _t(
                "capacitacion.msg_completada_header",
                fallback=(
                    "🎓🎉 *¡Terminaste tu capacitación!* ✨\n\n"
                    "Ya tenés las herramientas para arrancar como Agente "
                    "Petra 💜\n\n"
                    "Aquí tenés tu *panel de control* 👇"
                ),
            ))
            _mostrar_panel_control(telefono, meta)
            _log(telefono, texto, "[shortcut 'aviso' → fin capacitación + panel]",
                 nota="shortcut aviso")
            return True

        # ─── Shortcut de prueba (Fabricio): 'suerte' ──────────────────
        # Escribir 'suerte' desde el 3227057375 en cualquier estado
        # dispara el mensaje "¡Perfecto! Te deseamos mucha suerte..."
        # con botones [✅ Autorización / ❓ Tengo una duda]. Está acá
        # al principio para que no lo intercepte ningún otro bloque.
        if (texto or "").lower().strip() == "suerte" and (telefono or "").endswith("3227057375"):
            # Forzamos estado a autorizacion_pendiente para que los
            # botones ✅ Autorización / ❓ Tengo una duda que vienen a
            # continuación tengan handler y no caigan al LLM.
            _set_estado(meta, "autorizacion_pendiente")
            _save_meta(conn, cid, meta)
            estado = "autorizacion_pendiente"
            _send_buttons(
                telefono,
                _t(
                    "autorizacion.msg_voy_agendar",
                    fallback=(
                        "¡Perfecto! 🙌✨\n\n"
                        "Te deseamos *mucha suerte* con la reunión 🤝 "
                        "Acordate de llevar el material que te pasé — vas "
                        "a estar genial.\n\n"
                        "Estamos acá si necesitás ayuda 💜 Apenas termines, "
                        "elegí abajo según cómo te fue:"
                    ),
                ),
                [
                    {"id": "ONBAG_YA_TENGO_AUTORIZACION", "title": _b("ONBAG_YA_TENGO_AUTORIZACION", "✅ Autorización")},
                    {"id": "ONBAG_TENGO_DUDA", "title": _b("ONBAG_TENGO_DUDA", "❓ Tengo una duda")},
                ],
                estado_key="autorizacion_pendiente",
            )
            _log(telefono, texto, "[shortcut global 'suerte' → msg agendar]",
                 nota=f"estado={estado}")
            return True

        # ─── Shortcut de prueba (Fabricio): 'guia' ────────────────────
        # Escribir 'guia' (o 'guía') desde el 3227057375 fuerza el flow
        # del kit (Fase 6) desde el inicio: estado=kit_pendiente, reset
        # del kit_paso y pedido de la dirección completa.
        _texto_guia = (texto or "").lower().strip().replace("í", "i")
        if _texto_guia == "guia" and (telefono or "").endswith("3227057375"):
            _set_estado(meta, "kit_pendiente")
            meta.pop("kit_paso", None)
            meta.pop("libro_fotos", None)
            _save_meta(conn, cid, meta)
            _enviar(telefono, _t(
                "activacion.msg_pedir_direccion_inicio",
                fallback=(
                    "🎉 *¡Buenísimo! Ya tenés la autorización del administrador* ✨\n\n"
                    "📦 Ahora te enviamos tu *kit de activación* a tu casa:\n"
                    "• Tu *QR físico* para pegar en el edificio 📱\n"
                    "• Un *regalito* de bienvenida 💜\n\n"
                    "📍 *Mandame la dirección de donde vivís* — en *un solo mensaje*, "
                    "con todo lo que tengas: torre o casa, apto, barrio y ciudad.\n\n"
                    "_Ejemplos:_\n"
                    "• _Torre 3 apto 702, Chapinero, Bogotá_\n"
                    "• _Casa 49, Portal del Norte, Zipaquirá_\n"
                    "• _Apto 15B, Chicó, Bogotá_"
                ),
            ))
            _log(telefono, texto, "[shortcut global 'guia' → kit_pendiente]",
                 nota=f"estado={estado}")
            return True

        # ─── Panel de control (atajo global) ──────────────────────────
        # Si la agente escribe "panel", "menu", etc. — abrimos el menú
        # con 4 opciones: mis inmuebles / mis citas / mi capacitación /
        # pedir ayuda. Funciona en cualquier estado post-inscripción.
        if _es_keyword_panel(texto) and estado not in ("recolectando",):
            _mostrar_panel_control(telefono, meta)
            _log(telefono, texto, "[panel-control abierto]")
            return True
        # También si pulsa un botón del list del panel.
        if texto in ("ONBAG_PC_EDIFICIOS", "ONBAG_PC_INMUEBLES",
                     "ONBAG_PC_CITAS", "ONBAG_PC_CAPACITACION",
                     "ONBAG_PC_AYUDA",
                     "ONBAG_ED_AUTORIZAR", "ONBAG_ED_ACTIVAR") \
                or (texto and texto.startswith("ONBAG_REVID_")):
            if _handle_panel_control_boton(telefono, cid, meta, conn, texto):
                _log(telefono, texto, f"[panel-control {texto}]")
                return True

        # ─── Motor v3 (determinístico puro, sin LLM) — opt-in ─────────
        # Si PETRA_V3=1 y el agente está en fase de inscripción, usamos
        # el nuevo motor en `inscripcion_v3.py`. Rollout gradual:
        # PETRA_V3=0 → ruta legacy / tool-calling (según env).
        # PETRA_V3=1 → nuevo motor determinístico puro.
        import os as _os_tc
        if (_os_tc.environ.get("PETRA_V3", "0") == "1"
                and estado == "recolectando"):
            try:
                from app.petra_agents import inscripcion_v3 as _v3
                if _v3.handle(telefono, texto, cid, meta, conn,
                              es_handoff=es_handoff):
                    _log(telefono, texto, "[v3:inscripcion]",
                         nota="PETRA_V3=1")
                    return True
            except Exception:
                log.exception("[flow:v3] excepcion → fallback legacy")

        # Shortcut global: la agente escribe "kit", "mi kit", etc. en
        # CUALQUIER estado → le mostramos info del kit según su fase.
        _texto_lc_kit = (texto or "").lower().strip()
        if _texto_lc_kit in PALABRAS_KIT:
            if estado == "kit_enviado":
                _reenviar_kit_enviado(telefono, cid, meta)
                _log(telefono, texto, "[shortcut kit → re-envío bloque]",
                     nota=f"estado={estado}")
                return True
            if estado == "kit_pendiente":
                direccion = meta.get("direccion") or {}
                direccion_txt = meta.get("direccion_texto") or direccion.get("completa")
                tiene_direccion_vieja = all(direccion.get(c) for c in DIRECCION_CAMPOS)
                if direccion_txt or tiene_direccion_vieja:
                    # Recovery: agente con dirección completa pero estado
                    # kit_pendiente (ej. flow viejo). Avanzamos a kit_enviado
                    # y mandamos el bloque completo (texto + media + botón).
                    import datetime as _dtk
                    _set_estado(meta, "kit_enviado")
                    meta["kit_enviado_at"] = _dtk.datetime.utcnow().isoformat()
                    meta["kit_enviado_tras_direccion"] = True
                    _save_meta(conn, cid, meta)
                    _sm(telefono, "activacion.msg_direccion_completa",
                        fallback="🎉 ¡Dirección confirmada! Ya pedimos tu kit, "
                        "en 2-3 días hábiles te llega 📦")
                    import time as _tmk
                    _tmk.sleep(1)
                    _send_buttons(telefono, _t(
                        "activacion.msg_qr_boton",
                        fallback="Cuando lo tengas instalado, pulsá el botón 👇",
                    ), [{"id": "ONBAG_QR_INSTALADO",
                         "title": "✅ Ya instalé el QR"}])
                else:
                    _enviar(telefono,
                        "Estamos en medio de capturar tu dirección. "
                        "Respondé la pregunta que te hice arriba 👆 y "
                        "continuamos con el envío del kit 📦")
                _log(telefono, texto, "[shortcut kit en kit_pendiente]",
                     nota=f"estado={estado}")
                return True
            if estado == "activa":
                _enviar(telefono,
                    "Tu kit ya está activado y tu QR está en tu edificio 🎉💜\n"
                    "Si querés revisar algo puntual, decime qué.")
                _log(telefono, texto, "[shortcut kit en activa]",
                     nota=f"estado={estado}")
                return True
            # otros estados (recolectando, inscrito, bienvenida_enviada,
            # en_capacitacion, autorizacion_pendiente): aún no toca el kit
            _enviar(telefono,
                "Aún no estamos en la etapa del kit. Primero terminás "
                "capacitación + autorización y después te lo enviamos. "
                "Seguimos con el paso actual 👇")
            _log(telefono, texto, "[shortcut kit prematuro]",
                 nota=f"estado={estado}")
            return True

        # Shortcut de prueba: SOLO el teléfono 3227057375 (Fabricio) puede
        # escribir "suerte" en cualquier estado y disparar el mensaje
        # "¡Perfecto! Te deseamos mucha suerte con la reunión" + botones.
        if _texto_lc_kit == "suerte" and (telefono or "").endswith("3227057375"):
            _send_buttons(
                telefono,
                _t(
                    "autorizacion.msg_voy_agendar",
                    fallback=(
                        "¡Perfecto! 🙌✨\n\n"
                        "Te deseamos *mucha suerte* con la reunión 🤝 "
                        "Acordate de llevar el material que te pasé — vas "
                        "a estar genial.\n\n"
                        "Estamos acá si necesitás ayuda 💜 Apenas termines, "
                        "elegí abajo según cómo te fue:"
                    ),
                ),
                [
                    {"id": "ONBAG_YA_TENGO_AUTORIZACION", "title": _b("ONBAG_YA_TENGO_AUTORIZACION", "✅ Autorización")},
                    {"id": "ONBAG_TENGO_DUDA", "title": _b("ONBAG_TENGO_DUDA", "❓ Tengo una duda")},
                ],
                estado_key="autorizacion_pendiente",
            )
            _log(telefono, texto, "[shortcut global 'suerte' → mensaje agendar]",
                 nota=f"estado={estado}")
            return True

        # Intercept de botones CUSTOM_<id> definidos desde el panel.
        if texto and texto.startswith("CUSTOM_"):
            info = _botones.resolver_custom(texto)
            if info:
                accion = info.get("accion") or "enviar_texto"
                if accion == "disparar_boton":
                    # El botón custom dispara un botón del flow (ONBAG_*).
                    # Re-entramos a _handle_core con el payload como `texto`,
                    # así el flow lo procesa como si pulsaran el botón real.
                    payload = (info.get("payload") or "").strip()
                    if payload:
                        _log(telefono, texto, f"[dispara {payload}]",
                             nota=f"custom→disparar estado={estado}")
                        return _handle_core(telefono, payload,
                                            es_handoff=False, sesion_id=sesion_id)
                # Default: enviar_texto
                _enviar(telefono, info.get("texto_respuesta") or "")
                _log(telefono, texto, info.get("texto_respuesta") or "",
                     nota=f"custom-boton estado={estado}")
                return True

        # ─── Fase 2: INSCRITO (ya pasó REGISTRARME, falta ver bienvenida) ───
        if estado == "inscrito":
            return _fase_inscrito(conn, telefono, cid, meta, texto)

        # ─── Fase 2b: BIENVENIDA_ENVIADA (ya recibió video, esperando confirmación) ───
        if estado == "bienvenida_enviada":
            return _fase_bienvenida_enviada(conn, telefono, cid, meta, texto)

        # ─── Fase 3: en capacitación o esperando día 1 ───
        if estado in ("esperando_capacitacion_dia1", "en_capacitacion"):
            # M3.A: motor v3 determinístico (opt-in con PETRA_V3=1).
            if _os_tc.environ.get("PETRA_V3", "0") == "1":
                try:
                    from app.petra_agents import capacitacion_v3 as _capv3
                    if _capv3.handle(telefono, texto, cid, meta, conn,
                                     es_handoff=es_handoff):
                        _log(telefono, texto, "[v3:capacitacion]",
                             nota="PETRA_V3=1")
                        return True
                except Exception:
                    log.exception("[flow:v3] excepcion capacitacion → fallback")
            return _fase_capacitacion(conn, telefono, cid, meta, texto, estado,
                                      sesion_id=sesion_id)

        if estado == "capacitacion_completa":
            # M3.B: motor v3 determinístico — maneja ONBAG_INICIAR.
            if _os_tc.environ.get("PETRA_V3", "0") == "1":
                try:
                    from app.petra_agents import autorizacion_v3 as _autv3
                    if _autv3.handle(telefono, texto, cid, meta, conn,
                                     es_handoff=es_handoff):
                        _log(telefono, texto, "[v3:autorizacion capacitacion_completa]",
                             nota="PETRA_V3=1")
                        return True
                except Exception:
                    log.exception("[flow:v3] excepcion autorizacion/completa → fallback")
            _log(telefono, texto, "[delegado a LLM capacitacion_completa]")
            return _responder_con_llm(telefono, texto, sesion_id=sesion_id)

        # ─── Fase 4b: autorización rechazada (pausada, M3.C) ───
        if estado == "autorizacion_rechazada":
            if _os_tc.environ.get("PETRA_V3", "0") == "1":
                try:
                    from app.petra_agents import autorizacion_v3 as _autv3
                    if _autv3.handle(telefono, texto, cid, meta, conn,
                                     es_handoff=es_handoff):
                        _log(telefono, texto, "[v3:autorizacion rechazada]",
                             nota="PETRA_V3=1")
                        return True
                except Exception:
                    log.exception("[flow:v3] excepcion autorizacion_rechazada")
            # Fallback cuando PETRA_V3 off: tratamos como pendiente.
            _log(telefono, texto, "[flow legacy: autorizacion_rechazada sin v3]")
            return _responder_con_llm(telefono, texto, sesion_id=sesion_id)

        # ─── Fase 4: autorización pendiente — esperando botón "✅ Ya tengo autorización" ───
        if estado == "autorizacion_pendiente":
            # M3.B: motor v3 determinístico (opt-in con PETRA_V3=1).
            if _os_tc.environ.get("PETRA_V3", "0") == "1":
                try:
                    from app.petra_agents import autorizacion_v3 as _autv3
                    if _autv3.handle(telefono, texto, cid, meta, conn,
                                     es_handoff=es_handoff):
                        _log(telefono, texto, "[v3:autorizacion pendiente]",
                             nota="PETRA_V3=1")
                        return True
                except Exception:
                    log.exception("[flow:v3] excepcion autorizacion_pendiente → fallback")
            # Botón "🚀 Iniciar" → muestra los 3 botones de autorización.
            if texto == "ONBAG_INICIAR_AUTORIZACION":
                _send_buttons(telefono, _t(
                    "autorizacion.cta_botones",
                    fallback="Preparé material de apoyo para que lo lleves a la reunión 💜",
                ), [
                    {"id": "ONBAG_VER_MATERIAL", "title": _b("ONBAG_VER_MATERIAL", "📎 Ver material")},
                ], estado_key="autorizacion_pendiente")
                _log(telefono, texto, "[iniciar autorización → 3 botones]",
                     nota="botón Iniciar")
                return True
            if texto == "ONBAG_YA_TENGO_AUTORIZACION" or (texto or "").lower().strip() in (
                "autorizado", "autorizacion", "autorización", "tengo autorizacion",
                "tengo autorización", "ya tengo"):
                # Pasa a kit_pendiente — pedimos la dirección COMPLETA
                # en un solo mensaje libre (sin flow per-campo).
                _set_estado(meta, "kit_pendiente")
                meta.pop("direccion_paso", None)
                _save_meta(conn, cid, meta)
                _sm(telefono,
                    "activacion.msg_pedir_direccion_inicio",
                    fallback=(
                        "🎉 *¡Buenísimo! Ya tenés la autorización del administrador* ✨\n\n"
                        "📦 Ahora te enviamos tu *kit de activación* a tu casa:\n"
                        "• Tu *QR físico* para pegar en el edificio 📱\n"
                        "• Un *regalito* de bienvenida 💜\n\n"
                        "📍 *Mandame la dirección de donde vivís* — en *un solo mensaje*, "
                        "con todo lo que tengas: torre o casa, apto, barrio y ciudad.\n\n"
                        "_Ejemplos:_\n"
                        "• _Torre 3 apto 702, Chapinero, Bogotá_\n"
                        "• _Casa 49, Portal del Norte, Zipaquirá_\n"
                        "• _Apto 15B, Chicó, Bogotá_"
                    ),
                )
                _log(telefono, texto,
                     "[autorizacion confirmada → kit_pendiente/pide torre]")
                return True
            # Botón "📎 Ver material" (nuevo) o palabra clave de reenvío.
            t_norm = (texto or "").lower().strip()
            PALABRAS_REENVIO_MAT = (
                "material", "materiales", "reenvi", "reenví", "mándame", "mandame",
                "manda", "volver a enviar", "no me llego", "no me llegó", "no llego",
                "no llegó", "video", "videos", "demo", "pieza", "piezas", "gráfica",
                "grafica", "ejemplo", "ejemplos", "apoyo",
            )
            if texto == "ONBAG_VER_MATERIAL" or any(p in t_norm for p in PALABRAS_REENVIO_MAT):
                from app import petra_wa as _pwa_mat
                import time as _t_mat
                _pwa_mat.send_text(telefono, _t(
                    "autorizacion.msg_envio_material",
                    fallback=(
                        "Perfecto 💜 Acá te paso el material de apoyo para que "
                        "lo lleves a la reunión con el administrador:"
                    ),
                ))
                _t_mat.sleep(1)
                # Primero: recordatorio del video de capacitación (si cargado)
                _enviar_recordatorio_capacitacion(conn, telefono)
                # Después: material propiamente dicho (video, demo, piezas)
                _enviar_material_autorizacion(conn, telefono)
                _send_buttons(telefono, _t(
                    "autorizacion.msg_post_material",
                    fallback="Cuando tengas la autorización del administrador, pulsá el botón 👇",
                ), [
                    {"id": "ONBAG_VOY_AGENDAR_CITA", "title": _b("ONBAG_VOY_AGENDAR_CITA", "📅 Voy a agendar")},
                    {"id": "ONBAG_TENGO_DUDA", "title": _b("ONBAG_TENGO_DUDA", "❓ Tengo una duda")},
                ], estado_key="autorizacion_pendiente")
                _log(telefono, texto, "[envío material autorización]",
                     nota="botón o palabra clave pidió material")
                return True
            # Botón "❓ Tengo una duda" → prompt para que escriba la pregunta
            # Atajo por palabra clave: escribir "suerte" dispara el mismo
            # mensaje del botón "📅 Voy a agendar".
            if texto == "ONBAG_VOY_AGENDAR_CITA" or (texto or "").lower().strip() == "suerte":
                _send_buttons(
                    telefono,
                    _t(
                        "autorizacion.msg_voy_agendar",
                        fallback=(
                            "¡Perfecto! 🙌✨\n\n"
                            "Te deseamos *mucha suerte* con la reunión 🤝 "
                            "Acordate de llevar el material que te pasé — vas "
                            "a estar genial.\n\n"
                            "Estamos acá si necesitás ayuda 💜 Apenas termines, "
                            "elegí abajo según cómo te fue:"
                        ),
                    ),
                    [
                        {"id": "ONBAG_YA_TENGO_AUTORIZACION", "title": _b("ONBAG_YA_TENGO_AUTORIZACION", "✅ Autorización")},
                        {"id": "ONBAG_TENGO_DUDA", "title": _b("ONBAG_TENGO_DUDA", "❓ Tengo una duda")},
                    ],
                    estado_key="autorizacion_pendiente",
                )
                _log(telefono, texto, "[voy a agendar cita]",
                     nota="botón voy a agendar")
                return True
            if texto == "ONBAG_TENGO_DUDA":
                _sm(telefono,
                    "autorizacion.msg_prompt_duda",
                    fallback="¡Claro! 💜 Escribime tu pregunta y te respondo acá mismo.",
                )
                _log(telefono, texto, "[prompt escribir duda]",
                     nota="botón tengo una duda")
                return True
            # Pregunta libre → delegamos al LLM
            _log(telefono, texto, "[delegado a LLM autorizacion_pendiente]")
            return _responder_con_llm(telefono, texto, sesion_id=sesion_id)

        if estado == "kit_pendiente":
            # M4.A: motor v3 determinístico (opt-in con PETRA_V3=1).
            if _os_tc.environ.get("PETRA_V3", "0") == "1":
                try:
                    from app.petra_agents import kit_v3 as _kv3
                    if _kv3.handle(telefono, texto, cid, meta, conn,
                                   es_handoff=es_handoff):
                        _log(telefono, texto, "[v3:kit kit_pendiente]",
                             nota="PETRA_V3=1")
                        return True
                except Exception:
                    log.exception("[flow:v3] excepcion kit_pendiente → fallback")
            return _fase_kit_pendiente(conn, telefono, cid, meta, texto,
                                       sesion_id=sesion_id)

        if estado == "kit_enviado":
            # M4.A: motor v3 determinístico (opt-in con PETRA_V3=1).
            if _os_tc.environ.get("PETRA_V3", "0") == "1":
                try:
                    from app.petra_agents import kit_v3 as _kv3
                    if _kv3.handle(telefono, texto, cid, meta, conn,
                                   es_handoff=es_handoff):
                        _log(telefono, texto, "[v3:kit kit_enviado]",
                             nota="PETRA_V3=1")
                        return True
                except Exception:
                    log.exception("[flow:v3] excepcion kit_enviado → fallback")
            return _fase_kit_enviado(conn, telefono, cid, meta, texto,
                                     sesion_id=sesion_id)

        if estado == "activa":
            if _tc_delegar("activa", "PETRA_TOOLCALLING_ACTIVA",
                           telefono, texto, cid, meta, conn):
                return True
            return _fase_activa(conn, telefono, cid, meta, texto,
                                sesion_id=sesion_id)

        if estado == "certificado":
            _log(telefono, texto, "[delegado a LLM certificado]")
            return _responder_con_llm(telefono, texto, sesion_id=sesion_id)

        if estado != "recolectando":
            log.info("[flow:onboarding] tel=%s estado=%s (no es nuestro)",
                     telefono, estado)
            return False

        # Botones del resumen
        texto_lc = texto.lower()
        if texto == "ONBAG_CORREGIR" or texto_lc in ("corregir", "editar"):
            if any(meta.get(c) for c in CAMPOS):
                _enviar_lista_correccion(telefono, meta)
                _log(telefono, texto, "[lista corrección enviada]",
                     nota="pulsó corregir")
                return True
        if texto == "ONBAG_CANCELAR" or texto_lc in ("cancelar", "cancelo"):
            # Borrar rol + cerrar sesión
            conn.execute(
                "DELETE FROM petra_contacto_roles "
                "WHERE contacto_id=%s AND rol='agente_petra'",
                (cid,),
            )
            resp = ("Dale, cancelamos tu registro. Si cambiás de idea, "
                    "volvenos a escribir cuando quieras. 👋")
            _enviar(telefono, resp)
            _log(telefono, texto, resp, nota="cancelado por user")
            from app.petra_agents import sesion, intent_store
            intent_store.cerrar(telefono)
            sesion.cerrar(telefono, razon="cancelado_por_user")
            return True
        if texto.startswith("ONBAG_FIX_"):
            campo_fix = texto.replace("ONBAG_FIX_", "")
            if campo_fix in CAMPOS:
                meta[campo_fix] = None
                _save_meta(conn, cid, meta)
                resp = (
                    f"Dale, corrijamos *{NOMBRE_CAMPO[campo_fix]}*.\n\n"
                    + _pregunta(campo_fix, **{k: meta.get(k, "") for k in CAMPOS})
                )
                _enviar(telefono, resp)
                _log(telefono, texto, resp, nota=f"correccion seleccionada:{campo_fix}")
                return True

        # Confirmación final
        if (texto == "ONBAG_CONFIRM" or
                texto_lc in ("registrarme", "registrar", "confirmo",
                             "confirmar", "si", "sí", "listo")):
            faltantes = [c for c in CAMPOS if not meta.get(c)]
            if not faltantes:
                # Marcar como inscrito oficial + flow v2 (14 videos / 7 días,
                # día 1 = bienvenida+próximos pasos). Agentes antiguas sin
                # cap_version siguen en v1 (12 videos / 6 días).
                _set_estado(meta, "inscrito")
                meta["cap_version"] = 2
                import datetime as _dt
                meta["inscrito_at"] = _dt.datetime.utcnow().isoformat()
                _save_meta(conn, cid, meta)
                # Actualizar también nombre en petra_contactos (para que
                # aparezca en el panel con nombre)
                conn.execute(
                    "UPDATE petra_contactos SET nombre=%s WHERE id=%s",
                    (meta.get("nombre"), cid),
                )
                nombre_corto = meta.get("nombre", "").split()[0] if meta.get("nombre") else ""
                _conj = (meta.get("conjunto") or "").strip()
                if _conj.lower().startswith(("onbag_", "custom_")):
                    _conj = "tu edificio"
                felicitaciones = _t(
                    "inscripcion.msg_felicitaciones",
                    fallback=(
                        "🎉✨ *¡Felicitaciones {nombre_corto}!* ✨🎉\n\n"
                        "Ya sos oficialmente *Agente Petra* 💜\n\n"
                        "🏢 Vas a representar a Petra en *{conjunto}*\n"
                        "📍 {barrio}, {ciudad}\n\n"
                        "Vamos a arrancar con tu capacitación paso a paso 🚀\n"
                        "Tocá el botón de abajo para ver los próximos pasos 👇"
                    ),
                    nombre_corto=nombre_corto,
                    conjunto=_conj,
                    barrio=meta.get("barrio") or "",
                    ciudad=meta.get("ciudad") or "",
                )
                # Botón para avanzar al journey — user controla el ritmo
                _send_buttons(telefono, felicitaciones, [
                    {"id": "ONBAG_VER_JOURNEY", "title": _b("ONBAG_VER_JOURNEY", "Ver próximos pasos")},
                ], estado_key="inscrito")
                _log(telefono, texto, felicitaciones + " [+btn Ver próximos pasos]",
                     nota="REGISTRARME confirmado")
                return True
            resp = (
                "Antes de confirmar me faltan estos datos: "
                + ", ".join(NOMBRE_CAMPO[c] for c in faltantes)
                + ". Empecemos por el primero.\n\n"
                + _pregunta(faltantes[0], **meta)
            )
            _enviar(telefono, resp)
            _log(telefono, texto, resp, nota="faltantes al confirmar")
            return True

        # Corrección (user dice "corregir X" o nombra un campo)
        campo_a_corregir = None
        if any(w in texto.lower() for w in ("corregir", "cambiar", "modificar", "arreglar")):
            campo_a_corregir = _parse_correccion(texto) or CAMPOS[0]
            meta[campo_a_corregir] = None
            _save_meta(conn, cid, meta)
            resp = f"Dale, corrijamos. {_pregunta(campo_a_corregir, **meta)}"
            _enviar(telefono, resp)
            _log(telefono, texto, resp, nota=f"correccion:{campo_a_corregir}")
            return True

        # Determinar qué dato pedir / qué respuesta estamos recibiendo
        faltantes = [c for c in CAMPOS if not meta.get(c)]

        # Es handoff del router (recién arranca) → saludo + primera pregunta
        if es_handoff:
            # Si ya tenía algún dato, no re-saludamos: pedimos el próximo
            if faltantes:
                campo = faltantes[0]
                _enviar_pregunta(telefono, campo,
                                 **{k: meta.get(k, "") for k in CAMPOS})
                _log(telefono, texto,
                     _pregunta(campo, **{k: meta.get(k, "") for k in CAMPOS}),
                     nota=f"handoff:pedir {campo}")
                return True
            # Todos los campos ya están → directo al resumen (con botones)
            _enviar_resumen(telefono, meta)
            _log(telefono, texto, _resumen_texto(meta) + " [+botones]",
                 nota="handoff:ya tiene todos")
            return True

        # Mensaje regular del user → es respuesta al campo pendiente
        if not faltantes:
            _enviar_resumen(telefono, meta)
            _log(telefono, texto, _resumen_texto(meta) + " [+botones]",
                 nota="ya tiene todos, re-mostrar resumen")
            return True

        campo = faltantes[0]
        valido, valor, msg_err = _validar(campo, texto)
        if not valido:
            resp = msg_err
            _enviar(telefono, resp)
            _log(telefono, texto, resp, nota=f"validacion falló: {campo}")
            return True

        meta[campo] = valor
        _save_meta(conn, cid, meta)
        log.info("[flow:onboarding] tel=%s capturó %s=%s",
                 telefono, campo, valor)

        # Siguiente paso
        nuevos_faltantes = [c for c in CAMPOS if not meta.get(c)]
        if nuevos_faltantes:
            prox = nuevos_faltantes[0]
            _enviar_pregunta(telefono, prox,
                             **{k: meta.get(k, "") for k in CAMPOS})
            _log(telefono, texto,
                 _pregunta(prox, **{k: meta.get(k, "") for k in CAMPOS}),
                 nota=f"capturó {campo}, pide {prox}")
        else:
            # Tenemos los 5 → mostrar resumen con botones
            _enviar_resumen(telefono, meta)
            _log(telefono, texto, _resumen_texto(meta) + " [+botones]",
                 nota="capturó último, muestra resumen")
        return True


# =============================================================================
# Panel de control para la agente — menú interactivo WhatsApp
# =============================================================================

def _es_keyword_panel(texto):
    """True si el texto es un pedido de abrir el panel de control."""
    t = (texto or "").strip().lower()
    if not t:
        return False
    return t in (
        "menu", "menú", "panel", "panel de control", "mi info",
        "mi menu", "mi menú", "opciones", "ver menu", "ver menú",
    )


def _mostrar_panel_control(telefono, meta):
    """Envía el menú list con las 4 opciones del panel personal."""
    from app import petra_wa
    nombre = (meta.get("nombre") or "").split()[0] if meta.get("nombre") else ""
    saludo = f"¡Hola {nombre}!" if nombre else "¡Hola!"
    body = (
        f"{saludo} 💜\n\n"
        f"Este es tu *panel de control*. Elegí qué querés ver:"
    )
    sections = [{
        "title": "Mi cuenta",
        "rows": [
            {"id": "ONBAG_PC_EDIFICIOS",
             "title": "🏢 Mis edificios",
             "description": "Autorizar y activar tus conjuntos"},
            {"id": "ONBAG_PC_INMUEBLES",
             "title": "🏠 Mis inmuebles",
             "description": "Ver los inmuebles cargados en tu edificio"},
            {"id": "ONBAG_PC_CITAS",
             "title": "📅 Mis citas",
             "description": "Visitas próximas y pasadas"},
            {"id": "ONBAG_PC_CAPACITACION",
             "title": "🎓 Mi capacitación",
             "description": "Progreso y volver a ver videos"},
        ],
    }]
    try:
        petra_wa.send_list(telefono, body, "Ver opciones", sections)
    except Exception:
        log.exception("[panel-control] send fallo tel=%s", telefono)
        # Fallback a texto si falla el list
        petra_wa.send_text(telefono, body + (
            "\n\n• 🏢 Escribí *mis edificios*\n"
            "• 🏠 Escribí *mis inmuebles*\n"
            "• 📅 Escribí *mis citas*\n"
            "• 🎓 Escribí *mi capacitación*"
        ))
    # Segundo mensaje con saludo + próximos pasos (pedido Fabricio).
    proximos = (
        f"{saludo} 💜\n\n"
        "Estos son los *próximos pasos* para tu edificio:\n\n"
        "1. 🏢 *Autorizar* — hablá con el administrador de tu edificio "
        "para que te autorice a representar a Petra ahí.\n\n"
        "2. 📸 *Activar* — una vez autorizada, instalá el QR en el "
        "edificio y mandanos la foto para activarlo.\n\n"
        "¡Vamos! 💜"
    )
    try:
        petra_wa.send_text(telefono, proximos)
    except Exception:
        log.exception("[panel-control] send proximos fallo tel=%s", telefono)
    # Link Watt.1 (editable desde el panel de contenidos). Mandado como
    # tercer mensaje — si falta o está inactivo, se omite silencioso.
    try:
        from app import db as _db_watt
        with _db_watt._conexion_directa(timeout=5) as conn:
            row = conn.execute(
                "SELECT url FROM petra_contenidos "
                "WHERE titulo=%s AND activo=TRUE "
                "ORDER BY id DESC LIMIT 1",
                ("Watt.1",),
            ).fetchone()
            if row and row[0]:
                petra_wa.send_text(
                    telefono,
                    "¿Dudas o querés hablar con nosotros? Escribinos 👇\n\n"
                    + row[0]
                )
    except Exception:
        log.exception("[panel-control] send watt fallo tel=%s", telefono)


def _mostrar_mis_inmuebles(telefono, cid, conn):
    from app import petra_wa
    try:
        rows = conn.execute(
            "SELECT id, modalidad, torre, piso, area_m2, "
            "       COALESCE(precio_millones, precio_arriendo_mensual_millones) AS precio, "
            "       estado "
            "FROM petra_inmuebles WHERE contacto_id=%s "
            "ORDER BY id DESC LIMIT 10", (cid,),
        ).fetchall()
    except Exception:
        log.exception("[panel-control] inmuebles query fallo cid=%s", cid)
        rows = []
    if not rows:
        # M5.B: nunca mostramos vacío sin contexto. Si no hay inmuebles,
        # el humano todavía no subió la data del libro.
        texto = _t(
            "panel.mis_inmuebles_vacio",
            fallback=(
                "🏠 *Mis inmuebles*\n\n"
                "Estamos cargando los inmuebles de tu edificio, te "
                "avisamos cuando estén listos 💜"
            ),
        )
    else:
        lineas = [f"🏠 *Tus inmuebles ({len(rows)})*", ""]
        for r in rows:
            inm_id, modalidad, torre, piso, area, precio, estado = r
            parts = []
            if torre:
                parts.append(f"Torre {torre}")
            if piso:
                parts.append(f"piso {piso}")
            if area:
                parts.append(f"{area}m²")
            precio_txt = (f" · {precio}M" if precio else "")
            modalidad_emoji = "💰" if modalidad == "venta" else "🔑"
            lineas.append(
                f"{modalidad_emoji} #{inm_id} — {modalidad or '-'}"
                f"{(': ' + ', '.join(parts)) if parts else ''}"
                f"{precio_txt}"
                f" _[{estado}]_"
            )
        lineas.append("")
        lineas.append("_Escribime *cargar inmueble* para sumar otro._")
        texto = "\n".join(lineas)
    petra_wa.send_text(telefono, texto)


def _mostrar_mis_citas(telefono, cid, conn):
    from app import petra_wa
    try:
        rows = conn.execute(
            "SELECT v.id, v.programada_at, v.estado, "
            "       COALESCE(ci.nombre, '(sin interesado)') AS interesado, "
            "       v.inmueble_id "
            "FROM petra_visitas v "
            "LEFT JOIN petra_contactos ci ON ci.id = v.interesado_id "
            "WHERE v.agente_id=%s "
            "ORDER BY v.programada_at DESC NULLS LAST LIMIT 10", (cid,),
        ).fetchall()
    except Exception:
        log.exception("[panel-control] citas query fallo cid=%s", cid)
        rows = []
    if not rows:
        # M5.B: nunca vacío sin contexto.
        texto = _t(
            "panel.mis_citas_vacio",
            fallback=(
                "📅 *Mis citas*\n\n"
                "Por ahora no tenés citas. Estamos trabajando para "
                "enviarte clientes 💜"
            ),
        )
    else:
        lineas = [f"📅 *Tus citas ({len(rows)})*", ""]
        for vid, cuando, estado, interesado, inm_id in rows:
            fecha = _fmt_fecha(cuando) if cuando else "(sin fecha)"
            estado_emoji = {
                "programada": "⏳",
                "confirmada": "✅",
                "realizada": "✓",
                "cancelada": "❌",
                "no_show": "👻",
            }.get(estado, "•")
            lineas.append(
                f"{estado_emoji} #{vid} — {interesado}\n"
                f"   📆 {fecha} · _{estado}_"
                + (f" · inm#{inm_id}" if inm_id else "")
            )
        lineas.append("")
        lineas.append("_Para responder una cita puntual, escribime el número ej. 'cita 7'._")
        texto = "\n".join(lineas)
    petra_wa.send_text(telefono, texto)


def _mostrar_mi_capacitacion(telefono, meta):
    """M5.B — Panel Fase 7: muestra el summary + lista interactiva con
    los 14 videos iniciales (bienvenida + proximos_pasos + 12 videos
    diarios de los 7 días) y espacio para 'Nuevos' si hay. Al clickear
    una row (ONBAG_REVID_<idx>) se re-envía ese video."""
    from app import db as _db
    from app import petra_wa
    vistos = int(meta.get("videos_vistos", 0) or 0)
    cap_v = int(meta.get("cap_version", 1) or 1)
    total = 14 if cap_v >= 2 else 12
    dia_enviado = int(meta.get("cap_dia_enviado", 0) or 0)
    pct = int((vistos / total) * 100) if total else 0
    barra_llenos = int(pct / 10)
    barra = "🟪" * barra_llenos + "⬜" * (10 - barra_llenos)
    header = (
        f"🎓 *Mi capacitación*\n\n"
        f"Videos vistos: *{vistos}/{total}* ({pct}%)\n{barra}\n\n"
    )
    # 1) Traer los videos (iniciales + diarios + nuevos)
    try:
        with _db._conexion_directa(timeout=5) as conn:
            bienvenida = conn.execute(
                "SELECT titulo, url FROM petra_contenidos "
                "WHERE tipo='bienvenida' AND activo=TRUE "
                "ORDER BY orden LIMIT 1"
            ).fetchone()
            proximos = conn.execute(
                "SELECT titulo, url FROM petra_contenidos "
                "WHERE tipo='proximos_pasos' AND activo=TRUE "
                "ORDER BY orden LIMIT 1"
            ).fetchone()
            diarios = conn.execute(
                "SELECT titulo, url FROM petra_contenidos "
                "WHERE tipo='video_capacitacion' AND activo=TRUE "
                "ORDER BY orden LIMIT 12"
            ).fetchall()
            nuevos = conn.execute(
                "SELECT titulo, url FROM petra_contenidos "
                "WHERE tipo='video_capacitacion_nuevo' AND activo=TRUE "
                "ORDER BY orden LIMIT 10"
            ).fetchall()
    except Exception:
        log.exception("[panel-control] capacitacion query fallo tel=%s", telefono)
        bienvenida = proximos = None
        diarios = []
        nuevos = []
    # Secuencia estable: [bienvenida, proximos, diarios(12), nuevos(…)]
    iniciales = []
    if bienvenida:
        iniciales.append(("Bienvenida", bienvenida[1]))
    if proximos:
        iniciales.append(("Próximos pasos", proximos[1]))
    for i, (tit, url) in enumerate(diarios, 1):
        iniciales.append((tit or f"Video día {i}", url))
    nuevos_pack = [(tit or f"Nuevo {i+1}", url)
                   for i, (tit, url) in enumerate(nuevos)]
    # Guardar índice de reenvío en meta para resolverlo luego sin DB id.
    # Lo cacheamos en memoria con una tupla (titulo, url). El handler
    # vuelve a leer DB con el mismo orden, así no depende de persistir.
    if not iniciales and not nuevos_pack:
        # Sin contenido cargado — mostramos solo el summary.
        petra_wa.send_text(telefono,
                           header + "_Los videos están cargándose._")
        return
    petra_wa.send_text(telefono, header +
                       "Elegí un video para volver a verlo 👇")
    sections = []
    rows_iniciales = []
    for i, (titulo, _url) in enumerate(iniciales):
        rows_iniciales.append({
            "id": f"ONBAG_REVID_INI_{i}",
            "title": titulo[:24],
            "description": "Volver a ver",
        })
    if rows_iniciales:
        sections.append({"title": "Capacitación inicial",
                         "rows": rows_iniciales[:10]})
    if nuevos_pack:
        rows_nuevos = []
        for i, (titulo, _url) in enumerate(nuevos_pack):
            rows_nuevos.append({
                "id": f"ONBAG_REVID_NUEVO_{i}",
                "title": titulo[:24],
                "description": "Nuevo",
            })
        sections.append({"title": "Nuevos",
                         "rows": rows_nuevos[:10]})
    try:
        petra_wa.send_list(telefono, "Tocá un video para volver a verlo",
                           "Ver videos", sections)
    except Exception:
        log.exception("[panel-control] send_list capacitacion fallo tel=%s", telefono)


def _reenviar_video_capacitacion(telefono, boton_id):
    """Handler de ONBAG_REVID_INI_<i> / ONBAG_REVID_NUEVO_<i>: re-envía
    ese video al agente. Devuelve True si manejó algo."""
    from app import db as _db
    from app import petra_wa
    m = re.match(r"ONBAG_REVID_(INI|NUEVO)_(\d+)$", boton_id or "")
    if not m:
        return False
    cat, idx = m.group(1), int(m.group(2))
    try:
        with _db._conexion_directa(timeout=5) as conn:
            if cat == "INI":
                bienvenida = conn.execute(
                    "SELECT titulo, url FROM petra_contenidos "
                    "WHERE tipo='bienvenida' AND activo=TRUE "
                    "ORDER BY orden LIMIT 1"
                ).fetchone()
                proximos = conn.execute(
                    "SELECT titulo, url FROM petra_contenidos "
                    "WHERE tipo='proximos_pasos' AND activo=TRUE "
                    "ORDER BY orden LIMIT 1"
                ).fetchone()
                diarios = conn.execute(
                    "SELECT titulo, url FROM petra_contenidos "
                    "WHERE tipo='video_capacitacion' AND activo=TRUE "
                    "ORDER BY orden LIMIT 12"
                ).fetchall()
                lista = []
                if bienvenida:
                    lista.append(("Bienvenida", bienvenida[1]))
                if proximos:
                    lista.append(("Próximos pasos", proximos[1]))
                for i, (tit, url) in enumerate(diarios, 1):
                    lista.append((tit or f"Video día {i}", url))
            else:
                nuevos = conn.execute(
                    "SELECT titulo, url FROM petra_contenidos "
                    "WHERE tipo='video_capacitacion_nuevo' AND activo=TRUE "
                    "ORDER BY orden LIMIT 10"
                ).fetchall()
                lista = [(tit or f"Nuevo {i+1}", url)
                         for i, (tit, url) in enumerate(nuevos)]
    except Exception:
        log.exception("[panel-control] revid query fallo tel=%s id=%s",
                      telefono, boton_id)
        return True
    if idx < 0 or idx >= len(lista):
        petra_wa.send_text(telefono, "Ese video ya no está disponible 💜")
        return True
    titulo, url = lista[idx]
    try:
        petra_wa.send_media_o_texto(telefono, titulo or "", url)
    except Exception:
        log.exception("[panel-control] revid envío fallo tel=%s", telefono)
    return True


def _mostrar_pedir_ayuda(telefono, cid=None, meta=None, conn=None):
    """M4.B: activa modo FAQ (meta.en_faq=true) y pide la duda.
    Mientras `en_faq` esté activo, los textos libres caen al agente FAQ
    (ver `_handle_core`) en vez del menú."""
    from app import petra_wa
    prompt = _t(
        "activacion.msg_pedir_ayuda_prompt",
        fallback=(
            "💬 *¡Dale, decime en qué te puedo ayudar!* 💜\n\n"
            "Escribime tu *pregunta* o *duda* en una línea y te respondo. "
            "Ejemplos:\n\n"
            "• _¿cuánto gano por una venta?_\n"
            "• _¿cómo le hablo al admin?_\n"
            "• _no me llegó el kit_\n\n"
            "Cuando quieras volver al menú, escribime *listo* o *gracias*."
        ),
    )
    if conn is not None and cid is not None and meta is not None:
        try:
            meta["en_faq"] = True
            _save_meta(conn, cid, meta)
        except Exception:
            log.exception("[faq] no pude activar en_faq cid=%s", cid)
    petra_wa.send_text(telefono, prompt)


def _mostrar_mis_edificios(telefono, cid, conn, meta):
    """Muestra el/los conjunto(s) del agente con botones Autorizar/Activar.

    Hoy `meta['conjunto']` es un string único; cuando sea una lista, el
    render se adapta. Si no hay conjunto cargado, mostramos texto plano
    y volvemos.
    """
    from app import petra_wa
    conjunto = meta.get("conjunto")
    # Future-proof: soportar lista.
    if isinstance(conjunto, (list, tuple)):
        conjuntos = [c for c in conjunto if c]
    elif conjunto:
        conjuntos = [conjunto]
    else:
        conjuntos = []

    if not conjuntos:
        petra_wa.send_text(telefono, (
            "🏢 *Mis edificios*\n\n"
            "Aún no tenés edificios registrados. "
            "Escribí *menú* para volver."
        ))
        return

    estado_ag = (meta.get("estado_agente") or "").strip()
    if estado_ag in ("autorizacion_pendiente", "autorizacion_rechazada",
                     "inscrito", "en_capacitacion"):
        estado_resumen = "Autorización pendiente"
    elif estado_ag in ("autorizada", "kit_pendiente", "kit_enviado"):
        estado_resumen = "Autorizada · falta activar"
    elif estado_ag == "activa":
        estado_resumen = "Autorizada y activa ✅"
    else:
        estado_resumen = "En proceso"

    lineas = ["🏢 *Mis edificios*", ""]
    for i, c in enumerate(conjuntos, 1):
        lineas.append(f"{i}. {c}")
        lineas.append(f"   Estado: {estado_resumen}")
    body = "\n".join(lineas)

    _send_buttons(telefono, body, [
        {"id": "ONBAG_ED_AUTORIZAR", "title": "🏢 Autorizar"},
        {"id": "ONBAG_ED_ACTIVAR", "title": "📸 Activar"},
    ], estado_key=None)


def _handle_panel_control_boton(telefono, cid, meta, conn, boton_id):
    """Despacha los IDs del panel de control. Devuelve True si manejó algo."""
    if boton_id == "ONBAG_PC_EDIFICIOS":
        _mostrar_mis_edificios(telefono, cid, conn, meta)
        return True
    if boton_id == "ONBAG_PC_INMUEBLES":
        _mostrar_mis_inmuebles(telefono, cid, conn)
        return True
    if boton_id == "ONBAG_PC_CITAS":
        _mostrar_mis_citas(telefono, cid, conn)
        return True
    if boton_id == "ONBAG_PC_CAPACITACION":
        _mostrar_mi_capacitacion(telefono, meta)
        return True
    if boton_id == "ONBAG_PC_AYUDA":
        _mostrar_pedir_ayuda(telefono, cid=cid, meta=meta, conn=conn)
        return True
    if boton_id == "ONBAG_ED_AUTORIZAR":
        # Mismo efecto que ONBAG_INICIAR_AUTORIZACION: material + botones.
        from app import petra_wa as _pwa_ed
        import time as _t_ed
        _pwa_ed.send_text(telefono, _t(
            "autorizacion.msg_envio_material",
            fallback=(
                "Perfecto 💜 Acá te paso el material de apoyo para que "
                "lo lleves a la reunión con el administrador:"
            ),
        ))
        _t_ed.sleep(1)
        _enviar_recordatorio_capacitacion(conn, telefono)
        _enviar_material_autorizacion(conn, telefono)
        _send_buttons(telefono, _t(
            "autorizacion.msg_post_material",
            fallback="Cuando tengas la autorización del administrador, pulsá el botón 👇",
        ), [
            {"id": "ONBAG_YA_TENGO_AUTORIZACION",
             "title": _b("ONBAG_YA_TENGO_AUTORIZACION",
                         "✅ Ya tengo autorización")},
        ], estado_key="autorizacion_pendiente")
        return True
    if boton_id == "ONBAG_ED_ACTIVAR":
        from app import petra_wa as _pwa_act
        estado_ag = (meta.get("estado_agente") or "").strip()
        if estado_ag == "activa":
            _pwa_act.send_text(telefono, (
                "Tu edificio ya está activo ✅\n\n"
                "Escribí *menú* para volver al panel."
            ))
            return True
        if estado_ag in ("kit_pendiente", "kit_enviado"):
            _pwa_act.send_text(telefono, (
                "📸 *Activar edificio*\n\n"
                "Para activar tu conjunto, mandame una *foto del QR ya "
                "instalado* en el edificio 💜"
            ))
            return True
        _pwa_act.send_text(telefono, (
            "📸 *Activar edificio*\n\n"
            "Primero necesitás que el administrador te autorice. "
            "Tocá *🏢 Autorizar* en Mis edificios para arrancar ese paso."
        ))
        return True
    if boton_id and boton_id.startswith("ONBAG_REVID_"):
        return _reenviar_video_capacitacion(telefono, boton_id)
    return False
