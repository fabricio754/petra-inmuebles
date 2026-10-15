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


def _resumen(meta):
    return (
        "📋 *Confirmemos tus datos:*\n\n"
        f"• Nombre: {meta.get('nombre')}\n"
        f"• Cédula: {meta.get('cedula')}\n"
        f"• Ciudad: {meta.get('ciudad')}\n"
        f"• Barrio: {meta.get('barrio')}\n"
        f"• Conjunto: {meta.get('conjunto')}\n\n"
        "¿Están correctos? Respondé *REGISTRARME* para confirmar, "
        "o decime qué dato corregir."
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

        # Si ya está inscrita, no es nuestro flujo
        if estado != "recolectando":
            log.info("[flow:onboarding] tel=%s estado=%s (no es nuestro)",
                     telefono, estado)
            return False  # devuelve False para que el router caiga a otro handler

        # Confirmación final
        if texto.lower() in ("registrarme", "registrar", "confirmo",
                             "confirmar", "si", "sí", "listo"):
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
                mensaje = (
                    f"🎉 *¡Felicitaciones {nombre_corto}, ya sos oficialmente Agente Petra!*\n\n"
                    f"Vas a representar a Petra en *{meta.get('conjunto')}*, "
                    f"{meta.get('barrio')}, {meta.get('ciudad')}.\n\n"
                    "Próximos pasos:\n"
                    "1️⃣ Te mando ahora un video corto que te explica cómo arranca.\n"
                    "2️⃣ Durante los próximos 6 días recibirás 2 videos cortos "
                    "cada mañana (a las 8am) para tu capacitación.\n"
                    "3️⃣ Al terminar, te enviamos el kit para que lo entregues "
                    "a la administración de tu edificio.\n\n"
                    "Dale, voy a buscar tu video de bienvenida… 📹"
                )
                _enviar(telefono, mensaje)
                _log(telefono, texto, mensaje, nota="inscripción confirmada")
                # TODO: disparar envío del video de bienvenida
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
            # Todos los campos ya están → directo al resumen
            resp = _resumen(meta)
            _enviar(telefono, resp)
            _log(telefono, texto, resp, nota="handoff:ya tiene todos")
            return True

        # Mensaje regular del user → es respuesta al campo pendiente
        if not faltantes:
            resp = _resumen(meta)
            _enviar(telefono, resp)
            _log(telefono, texto, resp, nota="ya tiene todos, re-mostrar resumen")
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
            # Tenemos los 5 → mostrar resumen
            resp = _resumen(meta)
            _enviar(telefono, resp)
            _log(telefono, texto, resp, nota="capturó último, muestra resumen")
        return True
