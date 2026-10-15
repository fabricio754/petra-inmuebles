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


def _validar(campo, texto):
    """Valida/normaliza una respuesta. Devuelve (valido, valor_normalizado, msg_error)."""
    t = (texto or "").strip()
    if not t:
        return False, None, "Hmm, no recibí nada. Mandame tu respuesta en texto."
    if campo == "cedula":
        digitos = re.sub(r"\D", "", t)
        if len(digitos) < 6 or len(digitos) > 12:
            return False, None, "La cédula debería tener entre 6 y 12 dígitos. Mandámela otra vez por favor 🙏"
        return True, digitos, None
    if campo == "nombre":
        if len(t.split()) < 2:
            return False, None, "Por favor mandame tu nombre y apellido completos 🙏"
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
    """Fase post-REGISTRARME: enviar video de bienvenida + video de
    próximos pasos, luego pasar a estado 'bienvenida_enviada' esperando
    confirmación de la agente."""
    nombre_corto = (meta.get("nombre") or "").split()[0]
    bienvenida_row = conn.execute(
        "SELECT titulo, url FROM petra_contenidos "
        "WHERE tipo='bienvenida' AND activo=TRUE "
        "ORDER BY orden LIMIT 1"
    ).fetchone()
    proximos_row = conn.execute(
        "SELECT titulo, url FROM petra_contenidos "
        "WHERE tipo='proximos_pasos' AND activo=TRUE "
        "ORDER BY orden LIMIT 1"
    ).fetchone()
    if not bienvenida_row and not proximos_row:
        _enviar(telefono,
                f"Hola {nombre_corto}! Los videos de bienvenida están en "
                "preparación — pronto te los mando por acá. ❤️")
        _log(telefono, texto, "[sin videos cargados]",
             nota="fase inscrito sin videos")
        return True
    cuantos = (1 if bienvenida_row else 0) + (1 if proximos_row else 0)
    plural_video = "videos cortos" if cuantos > 1 else "un video corto"
    cuantos_str = f"{cuantos} {plural_video}" if cuantos > 1 else plural_video
    _enviar(telefono,
            f"Hola {nombre_corto}! Te paso {cuantos_str} para arrancar 🎬")
    if bienvenida_row:
        titulo, url = bienvenida_row
        _enviar(telefono, f"🎬 *{titulo}*\n\n{url}")
    if proximos_row:
        titulo, url = proximos_row
        _enviar(telefono, f"🗺 *{titulo}*\n\n{url}")
    verbo = "los" if cuantos > 1 else "lo"
    _enviar(telefono,
            f"Cuando {verbo} termines, mandame *LISTO* y arrancamos tu capacitación. 💪")
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
    # Preguntas abiertas: respuesta estándar sin inventar
    dia = (vistos // 2) + 1
    resp = (f"Estás en capacitación 👩‍🏫 — vas {vistos}/12 videos.\n\n"
            f"Los próximos llegan mañana a las 8am. Si tenés alguna duda, "
            f"escribí *AYUDA* y te paso con un humano.")
    _enviar(telefono, resp)
    _log(telefono, texto, resp, nota="pregunta abierta en capacitación")
    return True


def _fase_bienvenida_enviada(conn, telefono, cid, meta, texto):
    """Fase: ya recibió el video de bienvenida, esperamos que confirme."""
    t_lc = (texto or "").lower().strip()
    if t_lc in ("listo", "vi", "visto", "ya", "ya lo vi", "ok", "si",
                "sí", "terminé", "termine"):
        import datetime as _dt
        meta["bienvenida_vista_at"] = _dt.datetime.utcnow().isoformat()
        meta["estado_agente"] = "esperando_capacitacion_dia1"
        _save_meta(conn, cid, meta)
        resp = (
            "¡Buenísimo! 🎉\n\n"
            "Mañana a las *8am* te envío los primeros 2 videos de tu "
            "capacitación. Durante 6 días recibirás 2 videos cada mañana.\n\n"
            "Nos vemos mañana. 👋"
        )
        _enviar(telefono, resp)
        _log(telefono, texto, resp, nota="bienvenida confirmada, pasa a capacitación")
        return True
    # Cualquier otra cosa: recordatorio
    _enviar(telefono,
            "Cuando termines de ver el video, mandame *LISTO* para avanzar 🎬")
    _log(telefono, texto, "[recordatorio: mandar LISTO]",
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
                    f"{meta.get('barrio')}, {meta.get('ciudad')}.\n\n"
                    "Ahora te paso el material para que arranques 📹"
                )
                _enviar(telefono, felicitaciones)
                _log(telefono, texto, felicitaciones, nota="REGISTRARME confirmado")
                # Delegamos al flow de Fase 2 el envío del video + transición
                return _fase_inscrito(conn, telefono, cid, meta, texto)
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
