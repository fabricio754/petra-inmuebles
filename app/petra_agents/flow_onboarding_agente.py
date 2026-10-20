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

log = logging.getLogger("petra")

CAMPOS = ["nombre", "cedula", "ciudad", "barrio", "conjunto"]

PREGUNTAS = {
    "nombre": "👋 ¡Qué alegría que quieras ser Agente Petra!\n\nTe voy a pedir 5 datos para registrarte. Empecemos por el primero:\n\n*¿Cuál es tu nombre completo?*",
    "cedula": "✅ Gracias, {nombre}.\n\n*¿Cuál es tu cédula?* (solo los números)",
    "ciudad": "Perfecto. *¿En qué ciudad vivís?*",
    "barrio": "*¿En qué barrio?*",
    "conjunto": "Último dato — *¿Cómo se llama el conjunto o edificio donde vivís?*",
}

NOMBRE_CAMPO = {
    "nombre": "nombre completo",
    "cedula": "cédula",
    "ciudad": "ciudad",
    "barrio": "barrio",
    "conjunto": "conjunto/edificio",
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
    return (
        "📋 *Confirmemos tus datos:*\n\n"
        f"• Nombre: {meta.get('nombre')}\n"
        f"• Cédula: {meta.get('cedula')}\n"
        f"• Ciudad: {meta.get('ciudad')}\n"
        f"• Barrio: {meta.get('barrio')}\n"
        f"• Conjunto: {meta.get('conjunto')}\n\n"
        "¿Están correctos?"
    )


def _enviar_resumen(telefono, meta):
    from app import petra_wa
    petra_wa.send_buttons(
        telefono, _resumen_texto(meta),
        [
            {"id": "ONBAG_CONFIRM",   "title": "✅ Registrarme"},
            {"id": "ONBAG_CORREGIR",  "title": "✏️ Corregir"},
            {"id": "ONBAG_CANCELAR",  "title": "❌ Cancelar"},
        ],
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
        _enviar(telefono,
            "Perdón, no te pude responder bien ahora. Un asesor te "
            "contacta pronto 💜")
        return True


def _enviar_material_autorizacion(conn, telefono):
    """Envía los 3 contenidos post-capacitación: video, demo, piezas.
    Silencio por tipo si no hay cargado. Se usa al terminar la capacitación
    y como respuesta a palabras clave en fase autorizacion_pendiente."""
    from app import petra_wa as _pwa
    import time as _t
    vid = conn.execute(
        "SELECT titulo, url FROM petra_contenidos "
        "WHERE tipo='video_autorizacion' AND activo=TRUE "
        "ORDER BY orden LIMIT 1"
    ).fetchone()
    if vid:
        _pwa.send_text(telefono, f"🎬 *{vid[0]}*\n\n{vid[1]}")
        _t.sleep(2)
    demo = conn.execute(
        "SELECT titulo, url FROM petra_contenidos "
        "WHERE tipo='demo_autorizacion' AND activo=TRUE "
        "ORDER BY orden LIMIT 1"
    ).fetchone()
    if demo:
        _pwa.send_text(telefono, f"📱 *{demo[0]}*\n\n{demo[1]}")
        _t.sleep(2)
    piezas = conn.execute(
        "SELECT titulo, url FROM petra_contenidos "
        "WHERE tipo='pieza_grafica_autorizacion' AND activo=TRUE "
        "ORDER BY orden LIMIT 2"
    ).fetchall()
    for p in piezas:
        _pwa.send_text(telefono, f"🖼 *{p[0]}*\n\n{p[1]}")
        _t.sleep(2)


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
        boton = ({"id": "ONBAG_EMPEZAR_AHORA", "title": "Empezar ahora"}
                 if cap_v >= 2
                 else {"id": "ONBAG_VER_VIDEOS_HOY", "title": "Ver videos de hoy"})
        petra_wa.send_buttons(telefono, journey, [boton])
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
        meta["estado_agente"] = "esperando_capacitacion_dia1"
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
            resp = ("¡Buenísimo! 🎉\n\nTu capacitación arranca mañana a las "
                    "*8am* con los primeros 2 videos. Y así durante 7 días.\n\n"
                    "Nos vemos mañana. 👋")
            _enviar(telefono, resp)
            _log(telefono, texto, resp, nota="v2 empezar_ahora fallback")
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
        petra_wa.send_buttons(telefono,
            f"Cuando {verbo} termines, pulsá el botón para arrancar con "
            "tu capacitación diaria. 💪",
            [{"id": "ONBAG_EMPEZAR_AHORA", "title": "Empezar ahora"}],
        )
        import datetime as _dt
        meta["estado_agente"] = "bienvenida_enviada"
        meta["bienvenida_enviada_at"] = _dt.datetime.utcnow().isoformat()
        _save_meta(conn, cid, meta)
        _log(telefono, texto, "[videos enviados + btn Ya los vi]",
             nota="paso 2: videos enviados")
        return True

    # Cualquier otra cosa: re-mostrar el botón del paso actual
    petra_wa.send_buttons(telefono,
        f"Para continuar tu onboarding, pulsá el botón 👇",
        [{"id": "ONBAG_VER_JOURNEY", "title": "Ver próximos pasos"}],
    )
    _log(telefono, texto, "[re-muestra btn Ver próximos pasos]",
         nota="fase inscrito: re-prompt")
    return True
    import datetime as _dt
    meta["estado_agente"] = "bienvenida_enviada"
    meta["bienvenida_enviada_at"] = _dt.datetime.utcnow().isoformat()
    _save_meta(conn, cid, meta)
    cargados = (("bienvenida " if bienvenida_row else "") +
                ("proximos_pasos" if proximos_row else "")).strip()
    _log(telefono, texto, f"[videos enviados: {cargados}]",
         nota="fase inscrito → videos enviados")
    return True


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
            meta["estado_agente"] = "autorizacion_pendiente"
            _save_meta(conn, cid, meta)
            from app import petra_wa as _pwa_cap
            import time as _t_cap
            # Mensaje de felicitación + 2 botones: ver material / ya tengo autorización.
            # El material se envía SOLO cuando pulsa el botón.
            _pwa_cap.send_text(telefono,
                f"🎓🎉 *¡Terminaste tu capacitación completa!*\n\n"
                "Ya tenés las herramientas para arrancar como Agente Petra 💜\n\n"
                "📍 *Siguiente paso: Autorización del conjunto* 🏢\n\n"
                "Ahora toca hablar con el administrador de tu edificio para "
                "que te autorice a representar a Petra ahí 👇"
            )
            _t_cap.sleep(1)
            _pwa_cap.send_buttons(telefono,
                "Preparé material de apoyo para que lo lleves a la reunión 💜",
                [
                    {"id": "ONBAG_VER_MATERIAL", "title": "📎 Ver material"},
                    {"id": "ONBAG_TENGO_DUDA", "title": "❓ Tengo una duda"},
                    {"id": "ONBAG_YA_TENGO_AUTORIZACION", "title": "✅ Ya tengo autorización"},
                ],
            )
            _log(telefono, texto, "[capacitación completa → esperando botón material/autorización]",
                 nota="capacitacion_completa → autorizacion_pendiente")
            return True
        else:
            _save_meta(conn, cid, meta)
            dia_actual = vistos // 2
            quedan = total - vistos
            dias_restantes = (total - vistos) // 2
            resp = (
                f"🎉 *¡Felicitaciones, cerraste el día {dia_actual} de {dias_total}!* 🌟\n\n"
                f"Ya viste *{vistos}/{total} videos* ✅ Cada uno te suma herramientas, "
                "confianza y conocimiento para arrancar como Agente Petra en tu edificio 🏢💜\n\n"
                f"Te quedan solo *{quedan} videos ({dias_restantes} días)* para completar "
                "toda tu capacitación. Vas re bien, dale con todo 💪\n\n"
                "Mañana a las *8am* te mando los próximos 2 videos, para que no pierdas "
                "el ritmo. O si tenés ganas de seguir avanzando ahora mismo, apretá el botón 👇"
            )
        if vistos >= total:
            _enviar(telefono, resp)
        else:
            from app import petra_wa as _pwa
            _pwa.send_buttons(telefono, resp, [
                {"id": "ONBAG_SIGUIENTE_DIA", "title": "⏭ Ver día siguiente"},
            ])
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
            _enviar(telefono, "Hmm, no pude mandarte el siguiente par ahora. "
                              "Mañana a las 8am te llega sin falta. 🙏")
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
            _enviar(telefono, "Por hoy ya está. Mañana a las 8am te mando el próximo. 👋")
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
                import time as _t
                _pwa.send_text(telefono,
                    f"¡Claro! Acá los tenés de nuevo 💜 — videos del día *{dia_actual_env}/{dias_total}*:")
                _t.sleep(1)
                _pwa.send_text(telefono, f"🎬 *{videos[0][0]}*\n\n{videos[0][1]}")
                _t.sleep(2)
                _pwa.send_text(telefono, f"🎬 *{videos[1][0]}*\n\n{videos[1][1]}")
                _t.sleep(2)
                _pwa.send_buttons(telefono,
                    "Cuando los termines, pulsá el botón 👇",
                    [{"id": "ONBAG_LISTO", "title": "✅ Listo"}],
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
                     "arrancar")):
        import datetime as _dt, json as _json, time as _time
        try:
            from zoneinfo import ZoneInfo
            hoy_bogota = _dt.datetime.now(ZoneInfo("America/Bogota")).date().isoformat()
        except Exception:
            hoy_bogota = _dt.datetime.utcnow().date().isoformat()
        meta["bienvenida_vista_at"] = _dt.datetime.utcnow().isoformat()
        meta["estado_agente"] = "esperando_capacitacion_dia1"
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
            # Fallback: no se pudo enviar (sin videos, pausa, etc). Mensaje
            # explicativo con la ruta normal del cron.
            resp = (
                "¡Buenísimo! 🎉\n\n"
                "Tu capacitación arranca mañana a las *8am* con los primeros "
                "2 videos. Y así durante 7 días.\n\n"
                "Nos vemos mañana. 👋"
            )
            _enviar(telefono, resp)
            _log(telefono, texto, resp, nota="empezar_ahora pero sin envío (fallback)")
        return True
    # Cualquier otra cosa: recordatorio con botón
    from app import petra_wa
    petra_wa.send_buttons(telefono,
        "Cuando termines de ver los videos, pulsá el botón para avanzar 🎬",
        [{"id": "ONBAG_EMPEZAR_AHORA", "title": "Empezar ahora"}],
    )
    _log(telefono, texto, "[recordatorio: btn Empezar ahora]",
         nota="bienvenida_enviada esperando confirmación")
    return True


def handle(telefono, texto, es_handoff=False, sesion_id=None):
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
            meta["estado_agente"] = "recolectando"
            _save_meta(conn, cid, meta)

        estado = meta.get("estado_agente", "recolectando")

        # ─── Fase 2: INSCRITO (ya pasó REGISTRARME, falta ver bienvenida) ───
        if estado == "inscrito":
            return _fase_inscrito(conn, telefono, cid, meta, texto)

        # ─── Fase 2b: BIENVENIDA_ENVIADA (ya recibió video, esperando confirmación) ───
        if estado == "bienvenida_enviada":
            return _fase_bienvenida_enviada(conn, telefono, cid, meta, texto)

        # ─── Fase 3: en capacitación o esperando día 1 ───
        if estado in ("esperando_capacitacion_dia1", "en_capacitacion"):
            return _fase_capacitacion(conn, telefono, cid, meta, texto, estado,
                                      sesion_id=sesion_id)

        if estado == "capacitacion_completa":
            _log(telefono, texto, "[delegado a LLM capacitacion_completa]")
            return _responder_con_llm(telefono, texto, sesion_id=sesion_id)

        # ─── Fase 4: autorización pendiente — esperando botón "✅ Ya tengo autorización" ───
        if estado == "autorizacion_pendiente":
            if texto == "ONBAG_YA_TENGO_AUTORIZACION" or (texto or "").lower().strip() in (
                "autorizado", "autorizacion", "autorización", "tengo autorizacion",
                "tengo autorización", "ya tengo"):
                meta["estado_agente"] = "kit_pendiente"
                _save_meta(conn, cid, meta)
                _enviar(telefono,
                    "🎉 *¡Buenísimo!*\n\n"
                    "📍 *Siguiente paso: Activar el QR* 📬\n\n"
                    "Te vamos a enviar un kit con el QR y un regalito a la "
                    "dirección de tu edificio. Te confirmamos acá cuando "
                    "esté en camino.\n\n"
                    "Mientras tanto, podés ir preparando: identificar "
                    "propietarios interesados en tu conjunto, pensar qué "
                    "inmuebles podrían salir, etc. 💜")
                _log(telefono, texto, "[autorizacion confirmada → kit_pendiente]")
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
                _pwa_mat.send_text(telefono,
                    "Perfecto 💜 Acá te paso el material de apoyo para que "
                    "lo lleves a la reunión con el administrador:")
                _t_mat.sleep(1)
                _enviar_material_autorizacion(conn, telefono)
                _pwa_mat.send_buttons(telefono,
                    "Cuando tengas la autorización del administrador, pulsá el botón 👇",
                    [
                        {"id": "ONBAG_TENGO_DUDA", "title": "❓ Tengo una duda"},
                        {"id": "ONBAG_YA_TENGO_AUTORIZACION", "title": "✅ Ya tengo autorización"},
                    ],
                )
                _log(telefono, texto, "[envío material autorización]",
                     nota="botón o palabra clave pidió material")
                return True
            # Botón "❓ Tengo una duda" → prompt para que escriba la pregunta
            if texto == "ONBAG_TENGO_DUDA":
                _enviar(telefono,
                    "¡Claro! 💜 Escribime tu pregunta y te respondo acá mismo.")
                _log(telefono, texto, "[prompt escribir duda]",
                     nota="botón tengo una duda")
                return True
            # Pregunta libre → delegamos al LLM
            _log(telefono, texto, "[delegado a LLM autorizacion_pendiente]")
            return _responder_con_llm(telefono, texto, sesion_id=sesion_id)

        if estado == "kit_pendiente":
            _log(telefono, texto, "[delegado a LLM kit_pendiente]")
            return _responder_con_llm(telefono, texto, sesion_id=sesion_id)

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
                    + PREGUNTAS[campo_fix].format(**{k: meta.get(k, "") for k in CAMPOS})
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
                meta["estado_agente"] = "inscrito"
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
                felicitaciones = (
                    f"🎉 *¡Felicitaciones {nombre_corto}, ya sos oficialmente Agente Petra!*\n\n"
                    f"Vas a representar a Petra en *{meta.get('conjunto')}*, "
                    f"{meta.get('barrio')}, {meta.get('ciudad')}."
                )
                # Botón para avanzar al journey — user controla el ritmo
                from app import petra_wa
                petra_wa.send_buttons(telefono, felicitaciones, [
                    {"id": "ONBAG_VER_JOURNEY", "title": "Ver próximos pasos"},
                ])
                _log(telefono, texto, felicitaciones + " [+btn Ver próximos pasos]",
                     nota="REGISTRARME confirmado")
                return True
            resp = (
                "Antes de confirmar me faltan estos datos: "
                + ", ".join(NOMBRE_CAMPO[c] for c in faltantes)
                + ". Empecemos por el primero.\n\n"
                + PREGUNTAS[faltantes[0]].format(**meta)
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
            resp = f"Dale, corrijamos. {PREGUNTAS[campo_a_corregir].format(**meta)}"
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
                resp = PREGUNTAS[campo].format(**{k: meta.get(k, "") for k in CAMPOS})
                _enviar(telefono, resp)
                _log(telefono, texto, resp, nota=f"handoff:pedir {campo}")
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
            resp = PREGUNTAS[prox].format(**{k: meta.get(k, "") for k in CAMPOS})
            _enviar(telefono, resp)
            _log(telefono, texto, resp, nota=f"capturó {campo}, pide {prox}")
        else:
            # Tenemos los 5 → mostrar resumen con botones
            _enviar_resumen(telefono, meta)
            _log(telefono, texto, _resumen_texto(meta) + " [+botones]",
                 nota="capturó último, muestra resumen")
        return True
