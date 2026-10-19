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

    # Paso 1: user pulsó "Ver próximos pasos" → journey + botón videos
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
        petra_wa.send_buttons(telefono, journey, [
            {"id": "ONBAG_VER_VIDEOS_HOY", "title": "Ver videos de hoy"},
        ])
        _log(telefono, texto, "[journey + btn Ver videos de hoy]",
             nota="paso 1: journey enviado")
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
            [{"id": "ONBAG_YA_VI_VIDEOS", "title": "Ya los vi"}],
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


def _fase_capacitacion(conn, telefono, cid, meta, texto, estado):
    """Fase: la agente está en capacitación activa. Los 12 videos se envían
    por cron a las 8am (2 por día x 6 días). Acá solo gestionamos
    confirmaciones ("vi", "listo") y preguntas sobre el calendario."""
    t_lc = (texto or "").lower().strip()
    import datetime as _dt
    vistos = meta.get("videos_vistos", 0)
    if t_lc in ("vi", "visto", "listo", "ya", "ok", "terminé", "termine"):
        vistos += 1
        meta["videos_vistos"] = min(vistos, 12)
        meta[f"video_{vistos}_visto_at"] = _dt.datetime.utcnow().isoformat()
        if vistos >= 12:
            meta["estado_agente"] = "capacitacion_completa"
            _save_meta(conn, cid, meta)
            resp = ("¡Terminaste los 12 videos! 🎓🎉\n\n"
                    "Pasás a la etapa final: nuestro equipo revisa tu "
                    "caso y te contacta con el kit para la administración "
                    "de tu edificio. Te avisamos acá apenas esté listo.")
        else:
            _save_meta(conn, cid, meta)
            dia_actual = (vistos - 1) // 2 + 1
            vid_dia = ((vistos - 1) % 2) + 1
            quedan = 12 - vistos
            resp = (f"¡Bien hecho! ✅ Marcado video {vistos}/12 "
                    f"(día {dia_actual}, video {vid_dia}).\n\n"
                    f"Te quedan {quedan} videos. "
                    + ("El próximo llega mañana a las 8am." if vid_dia == 2 or vistos == 12
                       else "Si ya querés ver el siguiente del día, mandame *SIGUIENTE*."))
        _enviar(telefono, resp)
        _log(telefono, texto, resp, nota=f"video marcado {vistos}/12")
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
    # Preguntas abiertas en capacitación: 3 variantes rotativas + anti-spam.
    # Nunca ofrecemos "humano" ni pausamos el bot — la conversación sigue
    # fluida y nos ganamos espacio hasta que haya comando real (LISTO,
    # SIGUIENTE) o cambio de día.
    import datetime as _dt
    now = _dt.datetime.utcnow()
    ultimo_at = meta.get("ultimo_fallback_at")
    if ultimo_at:
        try:
            dt_last = _dt.datetime.fromisoformat(ultimo_at)
            if (now - dt_last).total_seconds() < 120:  # 2 min
                _log(telefono, texto, "[silencio: fallback <2min]",
                     nota="anti-spam fallback capacitación")
                return True
        except Exception:
            pass
    variantes = [
        f"👋 ¡Hola! Vas {vistos}/12 videos. El próximo llega mañana a las 8am ⏰",
        f"Vi tu mensaje 💜. Seguimos con los videos mañana a las 8am — vas {vistos}/12.",
        f"¡Dale! 💪 Mañana a las 8am te paso el próximo. Vas {vistos}/12.",
    ]
    idx = int(meta.get("ultimo_fallback_idx", -1) or -1)
    idx = (idx + 1) % len(variantes)
    resp = variantes[idx]
    meta["ultimo_fallback_idx"] = idx
    meta["ultimo_fallback_at"] = now.isoformat()
    _save_meta(conn, cid, meta)
    _enviar(telefono, resp)
    _log(telefono, texto, resp, nota=f"fallback capacitación v{idx}")
    return True


def _fase_bienvenida_enviada(conn, telefono, cid, meta, texto):
    """Fase: ya recibió los videos, esperamos que confirme."""
    t_lc = (texto or "").lower().strip()
    if (texto == "ONBAG_YA_VI_VIDEOS" or
            t_lc in ("listo", "vi", "visto", "ya", "ya lo vi", "ok", "si",
                     "sí", "terminé", "termine")):
        import datetime as _dt
        meta["bienvenida_vista_at"] = _dt.datetime.utcnow().isoformat()
        meta["estado_agente"] = "esperando_capacitacion_dia1"
        _save_meta(conn, cid, meta)
        resp = (
            "¡Buenísimo! 🎉\n\n"
            "Tu capacitación ya arrancó con los 2 videos que te acabo de "
            "pasar. Mañana a las *8am* te envío la *segunda tanda de "
            "2 videos*, y así durante los próximos 6 días.\n\n"
            "Nos vemos mañana. 👋"
        )
        _enviar(telefono, resp)
        _log(telefono, texto, resp, nota="bienvenida confirmada, pasa a capacitación")
        return True
    # Cualquier otra cosa: recordatorio con botón
    from app import petra_wa
    petra_wa.send_buttons(telefono,
        "Cuando termines de ver los videos, pulsá el botón para avanzar 🎬",
        [{"id": "ONBAG_YA_VI_VIDEOS", "title": "Ya los vi"}],
    )
    _log(telefono, texto, "[recordatorio: btn Ya los vi]",
         nota="bienvenida_enviada esperando confirmación")
    return True


def handle(telefono, texto, es_handoff=False):
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
            return _fase_capacitacion(conn, telefono, cid, meta, texto, estado)

        if estado == "capacitacion_completa":
            _enviar(telefono,
                "¡Felicitaciones! Terminaste tu capacitación 🎓\n\n"
                "El equipo humano de Petra está revisando tu caso para "
                "activarte oficialmente. Te avisamos acá cuando esté listo "
                "con las instrucciones para hablar con la administración "
                "de tu edificio.")
            _log(telefono, texto, "[capacitacion_completa stub]")
            return True

        if estado == "certificado":
            _enviar(telefono,
                "¡Buenísimo! Estás certificada. El próximo paso es el kit "
                "para la administración del edificio. Un asesor te va a "
                "contactar pronto por este chat. 💪")
            _log(telefono, texto, "[certificado stub]")
            return True

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
                # Marcar como inscrito oficial
                meta["estado_agente"] = "inscrito"
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
