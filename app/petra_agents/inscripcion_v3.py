"""Motor DETERMINÍSTICO (sin LLM) para la fase de inscripción.

Reemplaza:
  - `_fase_recolectando` del flow legacy (ruta determinística vieja), y
  - `tools_inscripcion.py` (ruta LLM con tool-calling).

Objetivo M2.A: una sola implementación, Python puro, slot-filling de los
5 datos: nombre, cédula, ciudad, barrio, conjunto. Sin `torre_apto` — ese
campo se pide en Fase 6 (direccion/kit), no acá.

Entry-point:
    handle(telefono, texto, cid, meta, conn, es_handoff=False) -> bool

Devuelve True si manejó el mensaje. El caller (`flow_onboarding_agente.
_handle_core`) lo invoca cuando PETRA_V3=1 y estado == "recolectando".

Soporta:
  - Varios campos en un solo mensaje (ej. "Juana Pérez cédula 1020800837
    ciudad Bogotá") vía regex suaves.
  - Botones del resumen: ONBAG_CONFIRM / ONBAG_CORREGIR / ONBAG_CANCELAR.
  - Lista de corrección: ONBAG_FIX_<campo>.
  - Rechazo de inputs sospechosos (preguntas, "ya te lo dije", emoji
    suelto, IDs ONBAG_*).

Política de validadores (acordada con usuario):
  - cedula: solo dígitos, 6–12.
  - nombre: >= 2 palabras separadas por espacio.
  - ciudad / barrio / conjunto: aceptar casi cualquier texto no vacío.
    Si está mal, el usuario lo corrige con el botón "Corregir" después
    de ver el resumen.
"""
import json
import logging
import re

log = logging.getLogger("petra")

# ─── Config ─────────────────────────────────────────────────────────────

CAMPOS = ["nombre", "cedula", "ciudad", "barrio"]

NOMBRE_CAMPO = {
    "nombre": "nombre completo",
    "cedula": "cédula",
    "ciudad": "ciudad",
    "barrio": "barrio",
    "conjunto": "conjunto/edificio",
}

# Clave de texto editable + fallback hardcodeado para cada pregunta.
PREGUNTA_CAMPO = {
    "nombre": (
        "inscripcion.pedir_nombre",
        "👋 ¡Qué alegría que quieras ser Agente Petra!\n\n"
        "Te voy a pedir 4 datos para registrarte. Empecemos:\n\n"
        "¿Cuál es tu *nombre completo*? 👤",
    ),
    "cedula": (
        "inscripcion.pedir_cedula",
        "¿Cuál es tu *cédula*? (solo los números) 🪪",
    ),
    "ciudad": (
        "inscripcion.pedir_ciudad",
        "¿En qué *ciudad* vivís? 🏙️",
    ),
    "barrio": (
        "inscripcion.pedir_barrio",
        "¿En qué *barrio* vivís? 📍",
    ),
    "conjunto": (
        "inscripcion.pedir_conjunto",
        "¿Cómo se llama tu *conjunto o edificio*? 🏢",
    ),
}

# Mensaje de confirmación tras guardar un campo. {valor} = lo guardado.
CONFIRMACION_CAMPO = {
    "nombre":   ("inscripcion.ok_nombre",   "✅ Perfecto, {valor}."),
    "cedula":   ("inscripcion.ok_cedula",   "✅ Perfecto, guardé tu cédula."),
    "ciudad":   ("inscripcion.ok_ciudad",   "✅ Perfecto, tu ciudad es {valor}."),
    "barrio":   ("inscripcion.ok_barrio",   "✅ Anotado el barrio {valor}."),
    "conjunto": ("inscripcion.ok_conjunto", "✅ Anotado {valor}."),
}

# Mensajes de rechazo (solo para los campos con validación dura).
RECHAZO_CAMPO = {
    "nombre": (
        "No me quedó claro tu nombre. Mandámelo como *Juana Pérez* "
        "(nombre + apellido) 🙏"
    ),
    "cedula": (
        "La cédula tiene que ser solo números, entre 6 y 12 dígitos. "
        "Mandámela de nuevo 🙏"
    ),
}

# Un input sospechoso: pregunta, protesta, botón-id, emoji suelto…
# No lo guardamos, pedimos aclaración.
_PATRONES_SOSPECHOSOS = (
    "ya te", "ya les", "ya te lo", "ya te la",
    "no sé", "no se", "no sé qué", "no se que",
    "cuanto", "cuánto", "cómo", "como se", "donde", "dónde",
    "mandame", "mandáme", "escribí", "escribi",
    "disculp", "olvid", "perdon", "perdón",
)
_SALUDOS = {
    "hola", "holaa", "holaaa", "ola", "buenas", "buenos días", "buenos dias",
    "hey", "hi", "hello", "qué tal", "que tal", "saludos",
}


def _es_sospechoso(valor):
    """True si `valor` NO parece un dato real (pregunta, protesta,
    emoji suelto, payload de botón, saludo). Rechaza sin guardar."""
    if not valor or not isinstance(valor, str):
        return True
    v = valor.strip()
    if not v:
        return True
    v_lc = v.lower()
    if len(v) < 2 or len(v) > 160:
        return True
    if v.startswith("?") or v == "?":
        return True
    # Payloads de botones del bot — nunca son datos.
    if v_lc.startswith(("onbag_", "custom_")):
        return True
    # Saludo suelto.
    first_word = v_lc.split()[0].strip(",.!?¡¿:;")
    if first_word in _SALUDOS and len(v.split()) <= 2:
        return True
    for p in _PATRONES_SOSPECHOSOS:
        if p in v_lc:
            return True
    # Solo emojis / sin letras ni dígitos.
    if not re.search(r"[A-Za-zÁÉÍÓÚÑÜáéíóúñü0-9]", v):
        return True
    return False


# ─── Validadores por campo ─────────────────────────────────────────────

_RE_PALABRA_ALFA = re.compile(r"^[A-Za-zÁÉÍÓÚáéíóúÑñÜü'\-]+$")


def _validar(campo, texto):
    """Devuelve (ok, valor_limpio, error_msg). error_msg solo tiene
    sentido cuando ok=False."""
    if not texto or not isinstance(texto, str):
        return False, None, "vacío"
    v = texto.strip()
    if _es_sospechoso(v):
        return False, None, "sospechoso"

    if campo == "cedula":
        digitos = re.sub(r"\D", "", v)
        if not (6 <= len(digitos) <= 12):
            return False, None, "cédula fuera de rango"
        return True, digitos, None

    if campo == "nombre":
        # Rechazar frases-intent tipo "quiero ser agente petra" que la gente
        # vuelve a tipear como texto (botón del template, atajo, re-saludo).
        v_lc = v.lower()
        if (("quiero ser" in v_lc or "quisiera ser" in v_lc
             or "me gustar" in v_lc or "ser agente" in v_lc)
                and ("petra" in v_lc or "agente" in v_lc or "asesora" in v_lc)):
            return False, None, "intent, no nombre"
        palabras = [p for p in v.split()
                    if len(p) >= 2 and _RE_PALABRA_ALFA.match(p)]
        if len(palabras) < 2:
            return False, None, "falta apellido"
        return True, v[:100], None

    # ciudad / barrio / conjunto — política permisiva.
    return True, v[:100], None


# ─── Multi-campo: extraer varios datos de un mismo mensaje ─────────────

# Regex con patrones del tipo "cédula 1020800837", "ciudad Bogotá",
# "barrio Chapinero", "conjunto Edificio X". Permiten que la agente
# mande todo junto en un solo mensaje.
_RE_CEDULA_INLINE = re.compile(
    r"(?:c[eé]dula|cc|c\.c\.?|documento|doc)\D{0,4}(\d[\d\s\.\-]{5,15}\d)",
    re.IGNORECASE,
)
_RE_CIUDAD_INLINE = re.compile(
    r"ciudad[:\s]+([A-Za-zÁÉÍÓÚÑÜáéíóúñü][A-Za-zÁÉÍÓÚÑÜáéíóúñü \-]{1,40})",
    re.IGNORECASE,
)
_RE_BARRIO_INLINE = re.compile(
    r"barrio[:\s]+([A-Za-zÁÉÍÓÚÑÜáéíóúñü0-9][A-Za-zÁÉÍÓÚÑÜáéíóúñü0-9 \-]{1,60})",
    re.IGNORECASE,
)
_RE_CONJUNTO_INLINE = re.compile(
    r"(?:conjunto|edificio|torre(?:s)?\s+del|residencias?|urbanizaci[oó]n)"
    r"[:\s]+([A-Za-zÁÉÍÓÚÑÜáéíóúñü0-9][A-Za-zÁÉÍÓÚÑÜáéíóúñü0-9 \-\.]{1,60})",
    re.IGNORECASE,
)
_RE_NOMBRE_INLINE = re.compile(
    r"(?:me llamo|mi nombre es|soy)\s+"
    r"([A-ZÁÉÍÓÚÑÜ][a-záéíóúñü]+(?:\s+[A-ZÁÉÍÓÚÑÜ][a-záéíóúñü]+)+)",
)


def _extraer_multiples(texto):
    """Intenta extraer campos marcados con palabras clave en el mismo
    mensaje. Devuelve un dict {campo: valor} con lo que pudo parsear
    (ya validado). Nunca lanza — si no matchea nada, dict vacío."""
    hallados = {}
    if not texto:
        return hallados
    # Nombre explícito ("me llamo X Y").
    m = _RE_NOMBRE_INLINE.search(texto)
    if m:
        ok, v, _ = _validar("nombre", m.group(1).strip())
        if ok:
            hallados["nombre"] = v
    # Cédula etiquetada.
    m = _RE_CEDULA_INLINE.search(texto)
    if m:
        ok, v, _ = _validar("cedula", m.group(1))
        if ok:
            hallados["cedula"] = v
    # Ciudad etiquetada.
    m = _RE_CIUDAD_INLINE.search(texto)
    if m:
        ok, v, _ = _validar("ciudad", m.group(1).strip(" .,;:"))
        if ok:
            hallados["ciudad"] = v
    # Barrio etiquetado.
    m = _RE_BARRIO_INLINE.search(texto)
    if m:
        ok, v, _ = _validar("barrio", m.group(1).strip(" .,;:"))
        if ok:
            hallados["barrio"] = v
    # Conjunto etiquetado.
    m = _RE_CONJUNTO_INLINE.search(texto)
    if m:
        ok, v, _ = _validar("conjunto", m.group(1).strip(" .,;:"))
        if ok:
            hallados["conjunto"] = v
    return hallados


# ─── DB helpers ─────────────────────────────────────────────────────────

# Estados en los que un rol agente_petra ya tiene (o está por tener) un QR
# activo en el edificio — bloquean una nueva inscripción en el mismo
# conjunto. 'activa' = QR instalado y confirmado por foto;
# 'kit_enviado' = kit ya se despachó, la agente aún no instala el QR pero
# el edificio ya está asignado a ella.
_ESTADOS_QR_OCUPAN_CONJUNTO = ("activa", "kit_enviado")


def _conjunto_ya_tomado(conn, conjunto, cid_actual=None):
    """True si ya existe un rol agente_petra con `conjunto` ILIKE match
    en algún estado que ocupa el edificio (QR activo o kit en camino).
    Excluye al cid actual para que el propio registro en curso no cuente.
    """
    if not conjunto or not isinstance(conjunto, str):
        return False
    cj = conjunto.strip()
    if not cj:
        return False
    try:
        q = (
            "SELECT 1 FROM petra_contacto_roles r "
            "WHERE r.rol='agente_petra' "
            "AND r.metadata->>'conjunto' ILIKE %s "
            "AND r.metadata->>'estado_agente' = ANY(%s) "
        )
        params = [cj, list(_ESTADOS_QR_OCUPAN_CONJUNTO)]
        if cid_actual is not None:
            q += "AND r.contacto_id <> %s "
            params.append(cid_actual)
        q += "LIMIT 1"
        row = conn.execute(q, tuple(params)).fetchone()
        return bool(row)
    except Exception:
        log.exception("[v3] fallo chequeo conjunto_ya_tomado conjunto=%s", conjunto)
        return False


def _save_meta(conn, cid, meta):
    """Upsert del rol agente_petra + metadata."""
    existente = conn.execute(
        "SELECT 1 FROM petra_contacto_roles "
        "WHERE contacto_id=%s AND rol='agente_petra'",
        (cid,),
    ).fetchone()
    if existente:
        conn.execute(
            "UPDATE petra_contacto_roles SET metadata=%s::jsonb, "
            "updated_at=NOW() WHERE contacto_id=%s AND rol='agente_petra'",
            (json.dumps(meta), cid),
        )
    else:
        conn.execute(
            "INSERT INTO petra_contacto_roles (contacto_id, rol, metadata) "
            "VALUES (%s, 'agente_petra', %s::jsonb)",
            (cid, json.dumps(meta)),
        )


# ─── Senders ────────────────────────────────────────────────────────────

def _t(clave, fallback, **vars):
    from app.petra_agents.textos import t as _tfn
    return _tfn(clave, fallback=fallback, **vars)


def _b(id_boton, titulo):
    from app.petra_agents.textos import boton as _bfn
    return _bfn(id_boton, titulo)


def _send_text(telefono, texto):
    from app import petra_wa
    petra_wa.send_text(telefono, texto)


def _send_buttons(telefono, body, botones, estado_key=None):
    """Wrapper que respeta reglas del panel de botones. Fallback a texto
    si no hay botones finales."""
    from app import petra_wa
    try:
        from app.petra_agents import botones_estacion as _bots
        final = _bots.aplicar_reglas(estado_key, botones) if estado_key else botones
    except Exception:
        final = botones
    if not final:
        petra_wa.send_text(telefono, body)
        return
    petra_wa.send_buttons(telefono, body, final)


def _enviar_pregunta(telefono, campo):
    clave, fallback = PREGUNTA_CAMPO[campo]
    _send_text(telefono, _t(clave, fallback))


def _mensaje_ok(campo, valor):
    clave, fallback = CONFIRMACION_CAMPO[campo]
    return _t(clave, fallback, valor=valor)


def _resumen_texto(meta):
    """Resumen de los 5 datos. NO incluye torre_apto — ese se pide en
    fase 6, no acá."""
    return (
        "📋 *Confirmemos tus datos:*\n\n"
        f"• Nombre: {meta.get('nombre') or ''}\n"
        f"• Cédula: {meta.get('cedula') or ''}\n"
        f"• Ciudad: {meta.get('ciudad') or ''}\n"
        f"• Barrio: {meta.get('barrio') or ''}\n\n"
        "¿Están correctos?"
    )


def _enviar_resumen(telefono, meta):
    _send_buttons(
        telefono, _resumen_texto(meta),
        [
            {"id": "ONBAG_CONFIRM",  "title": _b("ONBAG_CONFIRM",  "✅ Registrarme")},
            {"id": "ONBAG_CORREGIR", "title": _b("ONBAG_CORREGIR", "✏️ Corregir")},
            {"id": "ONBAG_CANCELAR", "title": _b("ONBAG_CANCELAR", "❌ Cancelar")},
        ],
        estado_key="registro_pendiente_confirmacion",
    )


def _enviar_lista_correccion(telefono, meta):
    """List interactivo con los 5 campos para elegir cuál corregir."""
    from app import petra_wa
    rows = []
    for c in CAMPOS:
        rows.append({
            "id": f"ONBAG_FIX_{c}",
            "title": NOMBRE_CAMPO[c][:24],
            "description": (meta.get(c) or "")[:70],
        })
    body = (
        "¿Qué dato querés corregir?\n\n"
        "Elegí uno: " + ", ".join(NOMBRE_CAMPO[c] for c in CAMPOS) + "."
    )
    try:
        petra_wa.send_list(
            telefono, body, "Ver opciones",
            [{"title": "Datos del onboarding", "rows": rows}],
        )
    except Exception:
        log.exception("[v3] send_list fallo tel=%s", telefono)
        _send_text(telefono,
            "¿Qué dato querés corregir? Escribime: "
            + ", ".join(NOMBRE_CAMPO[c] for c in CAMPOS))


# ─── Felicitación / cancelación ────────────────────────────────────────

def _felicitar_y_marcar_inscrito(conn, cid, telefono, meta):
    """Pasa el estado a `inscrito`, borra `torre_apto` legacy de meta si
    existía (ya no se pide en Fase 1), persiste nombre en petra_contactos
    y envía el mensaje de felicitación con el botón del journey."""
    import datetime as _dt
    # Importamos del flow legacy para reusar _set_estado y _send_buttons
    # (idéntico shape de respuesta a lo que la golden espera).
    from app.petra_agents.flow_onboarding_agente import _set_estado, _send_buttons as _flow_send_btns
    _set_estado(meta, "inscrito")
    meta["cap_version"] = 2
    meta["inscrito_at"] = _dt.datetime.utcnow().isoformat()
    # Removemos torre_apto si quedó colgado de alguna versión anterior:
    # en v3 el campo no se pregunta en Fase 1 (se pide luego, en kit).
    meta.pop("torre_apto", None)
    _save_meta(conn, cid, meta)
    try:
        conn.execute(
            "UPDATE petra_contactos SET nombre=%s WHERE id=%s",
            (meta.get("nombre"), cid),
        )
    except Exception:
        log.exception("[v3] update nombre fallo cid=%s", cid)

    nombre_corto = (meta.get("nombre") or "").split()[0] if meta.get("nombre") else ""

    felicitaciones = _t(
        "inscripcion.msg_felicitaciones",
        fallback=(
            "🎉✨ *¡Felicitaciones {nombre_corto}!* ✨🎉\n\n"
            "Ya sos oficialmente *Agente Petra* 💜\n\n"
            "📍 {barrio}, {ciudad}\n\n"
            "Vamos a arrancar con tu capacitación paso a paso 🚀\n"
            "Tocá el botón de abajo para ver los próximos pasos 👇"
        ),
        nombre_corto=nombre_corto,
        barrio=meta.get("barrio") or "",
        ciudad=meta.get("ciudad") or "",
    )
    _flow_send_btns(telefono, felicitaciones,
        [{"id": "ONBAG_VER_JOURNEY",
          "title": _b("ONBAG_VER_JOURNEY", "Próximos pasos")}],
        estado_key="inscrito")

    # ── Notificar a Valentina (env VALENTINA_PHONE) ────────────────
    # Avisamos a Valentina cuando una agente completa su registro.
    # Best-effort: cualquier fallo acá no bloquea la inscripción.
    try:
        import os as _os
        vale_tel = (_os.environ.get("VALENTINA_PHONE") or "").strip()
        if vale_tel:
            from app import petra_wa as _pwa_vale
            panel_token = (_os.environ.get("PANEL_TOKEN") or "").strip()
            host = (_os.environ.get("PUBLIC_HOST")
                    or "https://petra-inmuebles.onrender.com")
            link_panel = f"{host}/panel/petra/agente/{cid}"
            if panel_token:
                link_panel += f"?token={panel_token}"
            aviso = (
                "🆕 *Nueva agente registrada en Petra:*\n\n"
                f"• Nombre: {meta.get('nombre') or '—'}\n"
                f"• Cédula: {meta.get('cedula') or '—'}\n"
                f"• Ciudad: {meta.get('ciudad') or '—'}\n"
                f"• Barrio: {meta.get('barrio') or '—'}\n"
                f"• Conjunto: {meta.get('conjunto') or '—'}\n"
                f"• Teléfono: {telefono}\n\n"
                f"Entra al panel: {link_panel}"
            )
            _pwa_vale.send_text(vale_tel, aviso)
    except Exception:
        log.exception("[v3] aviso Valentina fallo cid=%s", cid)


def _rechazar_conjunto_duplicado(conn, cid, telefono, meta, conjunto):
    """M2.B: El conjunto ya tiene un QR activo (o kit en camino) en manos
    de otra agente. Rechazamos la inscripción:
    - mensaje templateado (clave `inscripcion.conjunto_duplicado`),
    - sin mencionar que hay otra agente,
    - borramos el rol agente_petra (meta se resetea; el teléfono queda
      en petra_contactos para futuro),
    - cerramos sesión + intent para que el próximo "hola" arranque limpio.
    """
    try:
        conn.execute(
            "DELETE FROM petra_contacto_roles "
            "WHERE contacto_id=%s AND rol='agente_petra'",
            (cid,),
        )
    except Exception:
        log.exception("[v3] delete rol duplicado fallo cid=%s", cid)
    msg = _t(
        "inscripcion.conjunto_duplicado",
        fallback=(
            "🏢 Ya hay un QR instalado en ese edificio.\n\n"
            "Si creés que es un error, escribile a soporte."
        ),
        conjunto=(conjunto or "").strip(),
    )
    _send_buttons(telefono, msg,
        [{"id": "ONBAG_CANCELAR",
          "title": _b("ONBAG_CANCELAR", "Entiendo, cancelar")}],
        estado_key="recolectando")
    try:
        from app.petra_agents import sesion, intent_store
        intent_store.cerrar(telefono)
        sesion.cerrar(telefono, razon="conjunto_duplicado")
    except Exception:
        log.exception("[v3] cerrar sesion dup fallo tel=%s", telefono)
    log.info("[v3] tel=%s rechazado por conjunto duplicado=%s", telefono, conjunto)


def _cancelar(conn, cid, telefono):
    try:
        conn.execute(
            "DELETE FROM petra_contacto_roles "
            "WHERE contacto_id=%s AND rol='agente_petra'",
            (cid,),
        )
    except Exception:
        log.exception("[v3] delete rol fallo cid=%s", cid)
    _send_text(telefono,
        "Dale, cancelamos tu registro. Si cambiás de idea, "
        "volvenos a escribir cuando quieras. 👋")
    # Cerramos sesión + intent como hace el flow legacy.
    try:
        from app.petra_agents import sesion, intent_store
        intent_store.cerrar(telefono)
        sesion.cerrar(telefono, razon="cancelado_por_user")
    except Exception:
        log.exception("[v3] cerrar sesion fallo tel=%s", telefono)


# ─── Entry-point ────────────────────────────────────────────────────────

def handle(telefono, texto, cid, meta, conn, es_handoff=False):
    """Procesa UN turno de inscripción. Devuelve True si manejó."""
    texto = (texto or "").strip()
    texto_lc = texto.lower()

    # ── 1) Botón Confirmar / palabras equivalentes ──────────────────────
    if texto == "ONBAG_CONFIRM" or texto_lc in (
        "registrarme", "registrar", "confirmo", "confirmar",
        "si", "sí", "listo",
    ):
        faltantes = [c for c in CAMPOS if not meta.get(c)]
        if faltantes:
            resp = (
                "Antes de confirmar me faltan estos datos: "
                + ", ".join(NOMBRE_CAMPO[c] for c in faltantes) + "."
            )
            _send_text(telefono, resp)
            prox = faltantes[0]
            _enviar_pregunta(telefono, prox)
            return True
        # M2.B: defensive re-check al confirmar (en caso de race condition
        # entre captura de conjunto y pulsar Registrarme).
        if _conjunto_ya_tomado(conn, meta.get("conjunto") or "",
                               cid_actual=cid):
            _rechazar_conjunto_duplicado(conn, cid, telefono, meta,
                                         meta.get("conjunto") or "")
            return True
        _felicitar_y_marcar_inscrito(conn, cid, telefono, meta)
        return True

    # ── 2) Botón Corregir / palabras equivalentes ───────────────────────
    if texto == "ONBAG_CORREGIR" or texto_lc in ("corregir", "editar"):
        if any(meta.get(c) for c in CAMPOS):
            _enviar_lista_correccion(telefono, meta)
            return True

    # ── 3) Botón Cancelar / palabras equivalentes ───────────────────────
    if texto == "ONBAG_CANCELAR" or texto_lc in ("cancelar", "cancelo"):
        _cancelar(conn, cid, telefono)
        return True

    # ── 4) Botón FIX_<campo> del list de corrección ─────────────────────
    if texto.startswith("ONBAG_FIX_"):
        campo = texto.replace("ONBAG_FIX_", "")
        # Compat: si llega torre_apto (del flow viejo), lo removemos
        # silenciosamente y arrancamos a pedir el próximo faltante.
        if campo == "torre_apto":
            meta.pop("torre_apto", None)
            _save_meta(conn, cid, meta)
            _send_text(telefono,
                "Ese dato ya no se pide acá — lo arreglamos solos.")
            return True
        if campo in CAMPOS:
            meta[campo] = None
            _save_meta(conn, cid, meta)
            _send_text(telefono,
                f"Dale, corrijamos *{NOMBRE_CAMPO[campo]}*.")
            _enviar_pregunta(telefono, campo)
            return True

    # ── 5) Determinar si el input es un dato o algo "ruidoso" ────────────
    faltantes = [c for c in CAMPOS if not meta.get(c)]

    # Si es la PRIMERA entrada ABSOLUTA (meta vacía — ningún campo
    # guardado Y nunca mandamos el saludo), NO consumimos el texto como
    # dato. La agente recién arranca y suele mandar "Hola quiero ser
    # agente"; eso cae al saludo + pedir_nombre.
    # Una vez que mandamos el saludo (marcamos flag), las entradas
    # siguientes ya se procesan como datos aunque los campos sigan vacíos.
    es_primera_entrada = (
        len(faltantes) == len(CAMPOS)
        and not meta.get("inscripcion_prompted_at")
    )
    if es_primera_entrada:
        import datetime as _dtp
        meta["inscripcion_prompted_at"] = _dtp.datetime.utcnow().isoformat()
        _save_meta(conn, cid, meta)

    # Mensaje vacío / sospechoso / primera entrada → re-preguntar el
    # próximo pendiente, SIN guardar nada.
    if not texto or _es_sospechoso(texto) or es_primera_entrada:
        # M4.B escape hatch: si parece una pregunta de la agente, dejamos
        # que el FAQ la responda antes de re-pedir el dato pendiente.
        # Primera entrada (meta vacía, len(texto)==0) la salteamos.
        if texto and any(meta.get(c) for c in CAMPOS):
            try:
                from app.petra_agents._faq_hook import intentar_faq
                prox = faltantes[0] if faltantes else None
                if prox:
                    desc = f"darme tu {NOMBRE_CAMPO[prox]}"
                    boton = None
                else:
                    desc = "confirmar tus datos"
                    boton = {"id": "ONBAG_CONFIRM",
                             "title": _b("ONBAG_CONFIRM", "✅ Registrarme")}
                if intentar_faq(telefono, texto, meta, conn, cid,
                                ultimo_paso_descripcion=desc,
                                ultimo_boton=boton):
                    log.info("[v3:ins] FAQ respondió → sin re-pregunta")
                    return True
            except Exception:
                log.exception("[v3:ins] FAQ hook fallo tel=%s", telefono)
        if faltantes:
            _enviar_pregunta(telefono, faltantes[0])
        else:
            _enviar_resumen(telefono, meta)
        return True

    # ── 6) Intento de multi-captura (varios campos en un msg) ───────────
    # Solo aceptamos la multi-captura si detectó >=2 campos: así evitamos
    # que una respuesta simple tipo "Edificio Palermo" al pedir conjunto
    # pierda el prefijo "Edificio" (el regex habría extraído solo "Palermo").
    extraidos_bruto = _extraer_multiples(texto)
    extraidos = {c: v for c, v in extraidos_bruto.items() if not meta.get(c)}
    if len(extraidos) < 2:
        extraidos = {}

    # ── 7) Si no hubo multi-captura, tratamos el msg como respuesta
    #       al próximo campo faltante. ────────────────────────────────────
    if not extraidos:
        if not faltantes:
            # Todos los campos ya están — mostramos resumen.
            _enviar_resumen(telefono, meta)
            return True
        campo = faltantes[0]
        ok, valor, err = _validar(campo, texto)
        if not ok:
            # Para campos con validación dura (nombre, cédula) mandamos
            # el mensaje de rechazo específico.
            rechazo = RECHAZO_CAMPO.get(campo)
            if rechazo:
                _send_text(telefono, rechazo)
            else:
                # Validación suave falló (texto demasiado corto, etc).
                _enviar_pregunta(telefono, campo)
            return True
        extraidos = {campo: valor}

    # ── 7.5) M2.B: si entre los extraídos viene `conjunto`, chequeamos
    #       que no esté tomado por otra agente activa / con kit en camino.
    #       Si lo está, rechazamos y borramos la inscripción en curso. ──
    if "conjunto" in extraidos:
        if _conjunto_ya_tomado(conn, extraidos["conjunto"], cid_actual=cid):
            _rechazar_conjunto_duplicado(conn, cid, telefono, meta,
                                         extraidos["conjunto"])
            return True

    # ── 8) Guardar los campos extraídos ─────────────────────────────────
    for c, v in extraidos.items():
        meta[c] = v
    _save_meta(conn, cid, meta)
    log.info("[v3] tel=%s capturó %s", telefono, list(extraidos.keys()))

    # ── 9) Mensaje de confirmación + próxima pregunta / resumen ─────────
    confirmaciones = [_mensaje_ok(c, extraidos[c]) for c in CAMPOS if c in extraidos]
    nuevos_faltantes = [c for c in CAMPOS if not meta.get(c)]
    if nuevos_faltantes:
        prox = nuevos_faltantes[0]
        clave_q, fallback_q = PREGUNTA_CAMPO[prox]
        pregunta = _t(clave_q, fallback_q)
        _send_text(telefono, "\n\n".join(confirmaciones + [pregunta]))
    else:
        # Último campo completado: saltamos la confirmación y vamos directo
        # al resumen/felicitación — evitamos el "Anotado el ..." redundante.
        _enviar_resumen(telefono, meta)
    return True
