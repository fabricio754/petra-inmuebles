"""Motor DETERMINÍSTICO (sin LLM) para la Fase 6 — Activación / Kit.

M4.A: reemplaza las ramas tool-calling (`tools_kit.py`) y legacy
(`_fase_kit_pendiente` / `_fase_kit_enviado` del flow) por una
implementación en Python puro. Patrón idéntico a `inscripcion_v3.py`,
`capacitacion_v3.py`, `autorizacion_v3.py`.

    handle(telefono, texto, cid, meta, conn, es_handoff=False) -> bool

Activada con `PETRA_V3=1`. Rutas viejas (sin flag) quedan intactas.

Flow (del Word firmado, 7 pasos post-autorización):

  1) Bot pide dirección en 1 solo mensaje libre.
  2) Al validar: 🎉 ¡Dirección confirmada! + botón ✨ Perfecto.
  3) Al pulsar Perfecto: 📸 Foto del libro de arriendos + ✨ Perfecto.
     → el usuario manda la foto, el humano la procesa off-band.
  4) Humano dispara /panel/petra/_admin/kit-qr-cargado?tel=…&token=…
     → bot manda "📸 Hemos recibido tus fotos y hemos cargado el QR" +
       botón ✨ Perfecto (ONBAG_QR_CARGADO).
  5) Al pulsar ONBAG_QR_CARGADO: "📦 Avísanos cuando llegue el kit.
     Tu número de guía es: [X]" + botón ✨ Perfecto (ONBAG_GUIA_PERFECTO).
     El número de guía viene de /panel/petra/_admin/kit-guia?tel=…&guia=…
     — si todavía no se cargó, el msg dice "te la compartimos cuando
     esté disponible".
  6) Al llegar el kit (botón ONBAG_KIT_LLEGO o texto "ya llegó"):
     descripción del contenido del kit + botón "✨ Dime los pasos"
     (ONBAG_DIME_PASOS).
  7) Al pulsar Dime los pasos: 7 pasos de instalación del QR + botón
     "✅ Ya lo instalé" (ONBAG_YA_INSTALE).

Después de "Ya lo instalé": bot pide la foto del QR instalado. La
transición final kit_enviado → activa la maneja `capturar_foto_qr`
(flow_onboarding_agente), fuera del scope de M4.A.

Estados usados:
  - `kit_pendiente` → esperando dirección.
  - `kit_enviado` → dirección capturada; sub-paso en `meta.kit_paso`.

Sub-pasos (`meta.kit_paso`):
  - "dir_confirmada": acaba de llegar la dirección, esperamos
    ONBAG_KIT_PERFECTO.
  - "foto_libro_pedida": pidió foto; esperamos foto / ack.
  - "qr_cargado": humano cargó el QR, esperamos ONBAG_QR_CARGADO.
  - "guia_enviada": agente recibió msg de guía, esperamos que llegue
    el kit (ONBAG_KIT_LLEGO) o "ya llegó".
  - "kit_recibido": agente avisó, mostramos contenido + Dime los pasos.
  - "pasos_mostrados": mostramos los 7 pasos; esperamos ONBAG_YA_INSTALE
    o foto directa.
  - "qr_foto_pendiente": pidió foto del QR instalado (fase 7).
"""
import datetime as _dt
import json as _json
import logging
import re as _re

log = logging.getLogger("petra")


# ─── Senders / helpers ──────────────────────────────────────────────────

def _t(clave, fallback, **vars):
    from app.petra_agents.textos import t as _tfn
    return _tfn(clave, fallback=fallback, **vars)


def _b(id_boton, titulo):
    from app.petra_agents.textos import boton as _bfn
    return _bfn(id_boton, titulo)


def _send_text(telefono, texto):
    from app import petra_wa
    petra_wa.send_text(telefono, texto)


def _send_buttons(telefono, texto, botones, estado_key=None):
    from app import petra_wa
    try:
        from app.petra_agents import botones_estacion as _bots
        final = _bots.aplicar_reglas(estado_key, botones) if estado_key else botones
    except Exception:
        final = botones
    if not final:
        petra_wa.send_text(telefono, texto)
        return
    petra_wa.send_buttons(telefono, texto, final)


def _save_meta(conn, cid, meta):
    conn.execute(
        "UPDATE petra_contacto_roles SET metadata=%s::jsonb, "
        "updated_at=NOW() WHERE contacto_id=%s AND rol='agente_petra'",
        (_json.dumps(meta), cid),
    )


def _log(telefono, u, a, nota=""):
    try:
        from app.petra_agents.flow_onboarding_agente import _log as _flog
        _flog(telefono, u, a, nota=nota)
    except Exception:
        pass


def _set_estado(meta, nuevo):
    from app.petra_agents.flow_onboarding_agente import _set_estado as _fse
    _fse(meta, nuevo)


# ─── Textos / fallbacks ─────────────────────────────────────────────────

FB_DIRECCION_INVALIDA = (
    "📍 Esa dirección no me quedó clara. Mandámela *completa en un solo "
    "mensaje*, con todo lo que tengas: torre o casa, apto, barrio y "
    "ciudad.\n\n"
    "_Ejemplos:_\n"
    "• _Torre 3 apto 702, Chapinero, Bogotá_\n"
    "• _Casa 49, Portal del Norte, Zipaquirá_"
)

FB_DIRECCION_CONFIRMADA = (
    "🎉 *¡Dirección confirmada!* 📍\n\n"
    "Ya pedimos tu kit de activación 📦\n"
    "En *2 a 3 días hábiles* te llega a esa dirección.\n\n"
    "💜 Mientras tanto, podés seguir con tu capacitación."
)

FB_PEDIR_FOTO_LIBRO = (
    "📚 *¿Ya cargaste las fotos del libro de arriendos y ventas "
    "de tu conjunto?*\n\n"
    "Pasá por la *portería o el lobby* y pedí ver el libro — tomale "
    "una foto bien clara de cada hoja y mandámela por acá 💜\n\n"
    "Cuando las tengas subidas, apretá *Sí* y cargamos tu QR 🚀"
)

FB_FOTO_LIBRO_RECIBIDA = (
    "📸 *Acabas de enviar la foto. Decime:*\n\n"
    "¿Ya cargaste *todas* las fotos del libro?\n\n"
    "Apretá *Sí* y cargamos tu QR 🚀\n"
    "Apretá *No* si todavía te faltan 💜"
)

FB_LIBRO_NO = (
    "Dale, mientras tanto 💜\n\n"
    "📸 Pasá por la *portería o el lobby*, pedí ver el *libro de "
    "arriendos y ventas*, y mandame una foto bien clara de las hojas "
    "que necesitamos.\n\n"
    "Cuando ya las tengas acá, apretá *Sí* y seguimos ✨"
)

FB_FOTO_LIBRO_ACK = (
    "✅ ¡Perfecto! Apenas nos mandes la foto del libro, cargamos el QR "
    "y te aviso por acá 💜"
)

FB_QR_CARGADO = (
    "📸 *¡Hemos recibido tus fotos y hemos cargado el QR!* ✨\n\n"
    "Tu QR ya está listo para funcionar con la info de tu conjunto 🚀\n\n"
    "Apretá para seguir 👇"
)

FB_GUIA_ENVIO = (
    "📦 *¡Tu kit ya está en camino!*\n\n"
    "Avísanos cuando te llegue. *Tu número de guía es:* `{guia}` 🚚\n\n"
    "Apretá para seguir 👇"
)

FB_GUIA_ENVIO_SIN_GUIA = (
    "📦 *¡Tu kit ya está en camino!*\n\n"
    "Avísanos cuando te llegue. 🚚\n\n"
    "_Tu número de guía te lo compartimos apenas esté disponible._\n\n"
    "Apretá para seguir 👇"
)

FB_KIT_LLEGADO_CONTENIDO = (
    "📦 *¡Genial! Destapá tu kit* 🎁\n\n"
    "Adentro vas a encontrar:\n"
    "• 👜 Tu *bolsa Petra*\n"
    "• 🔘 Tu *botón ✨ Petra*\n"
    "• 🖊️ Un *esfero* de Petra\n"
    "• ☂️ Una *sombrilla* Petra\n"
    "• 🏷️ Tu *QR sticker*\n"
    "• 🪧 Tu *QR con base* para pegar\n\n"
    "Cuando lo tengas todo a la mano, apretá y te paso los pasos para "
    "instalar el QR 👇"
)

FB_PASOS_INSTALACION = (
    "📋 *Pasos para instalar tu QR* 📱\n\n"
    "1️⃣ Hablá con el *administrador* o la *portería* y pedí autorización "
    "para pegar el QR en un lugar visible.\n"
    "2️⃣ Elegí el mejor punto: *portería*, *lobby*, cartelera común o "
    "ascensor — donde pase la mayor cantidad de gente del conjunto.\n"
    "3️⃣ Limpiá la superficie para que el *sticker* o la base se pegue "
    "bien y no se despegue.\n"
    "4️⃣ Pegá el *QR* (sticker o base rígida) a la altura de los ojos, "
    "sin reflejos ni obstáculos.\n"
    "5️⃣ Pedile al *vigilante* o al admin que te ayude a verificar que "
    "quedó derecho y visible.\n"
    "6️⃣ Escanealo con tu celular para confirmar que *abre bien* el chat "
    "de Petra.\n"
    "7️⃣ Tomale una *foto clara* al QR instalado y mandámela por acá "
    "📸 — con eso te activo oficialmente.\n\n"
    "Cuando lo tengas pegado, apretá 👇"
)

FB_PEDIR_FOTO_QR = (
    "¡Casi listo! 💜\n\n"
    "Para activarte oficialmente necesito *una foto del QR instalado*. "
    "Mandámela por acá 📸 y te activo al toque 🚀"
)


# ─── Validadores ────────────────────────────────────────────────────────

_PATRONES_DIR_SOSPECHOSOS = (
    "?", "ya te", "ya les", "cuanto", "cuándo", "cómo", "dónde",
    "disculp", "olvid", "no sé", "no se", "mandame",
)


def _es_direccion_valida(texto):
    """True si el texto parece una dirección razonable."""
    if not texto:
        return False
    v = texto.strip()
    utiles = _re.sub(r"[^a-záéíóúñA-ZÁÉÍÓÚÑ0-9]", "", v)
    t_lc = v.lower()
    sospechoso = any(s in t_lc for s in _PATRONES_DIR_SOSPECHOSOS)
    if len(v) < 6 or len(utiles) < 8 or sospechoso:
        return False
    # Payload de botón: no es dirección.
    if t_lc.startswith(("onbag_", "custom_")):
        return False
    return True


# ─── Palabras clave ─────────────────────────────────────────────────────

PALABRAS_KIT = (
    "kit", "mi kit", "donde está mi kit", "donde esta mi kit",
    "info kit", "estado kit", "que paso con mi kit", "qué pasó con mi kit",
)

_PAL_KIT_LLEGO = (
    "ya llego", "ya llegó", "me llego", "me llegó", "ya me llego",
    "ya me llegó", "llego el kit", "llegó el kit", "ya tengo el kit",
    "tengo el kit", "ya está el kit", "ya esta el kit",
)

_PAL_YA_INSTALE = (
    "ya instale", "ya instalé", "ya lo instale", "ya lo instalé",
    "qr instalado", "pegado", "ya quedó", "ya quedo", "listo instalado",
)


# ─── Pasos del flow (helpers) ───────────────────────────────────────────

def _enviar_dir_confirmada(telefono):
    _send_buttons(telefono, _t(
        "activacion.msg_direccion_completa",
        fallback=FB_DIRECCION_CONFIRMADA,
    ), [{"id": "ONBAG_KIT_PERFECTO",
         "title": _b("ONBAG_KIT_PERFECTO", "✨ Perfecto")}],
       estado_key="kit_enviado")


def _enviar_pedir_foto_libro(telefono):
    _send_text(telefono, _t(
        "activacion.msg_libro_intro",
        fallback=(
            "📸 *Ahora necesitamos una foto del libro de arriendos y ventas* "
            "de tu conjunto.\n\n"
            "Pasá por la *portería o el lobby* y pedí ver el libro. Tomale una "
            "foto bien clara y mandámela por acá 💜\n\n"
            "✨ Con eso vamos a *cargar tu QR con la info de tu conjunto*, para "
            "que cuando lo instales ya funcione completo 🚀"
        ),
    ))
    _send_buttons(telefono, _t(
        "activacion.msg_libro_intro",
        fallback=(
            "📸 *Ahora necesitamos una foto del libro de arriendos y ventas* "
            "de tu conjunto.\n\n"
            "Pasá por la *portería o el lobby* y pedí ver el libro. Tomale una "
            "foto bien clara y mandámela por acá 💜\n\n"
            "✨ Con eso vamos a *cargar tu QR con la info de tu conjunto*, para "
            "que cuando lo instales ya funcione completo 🚀"
        ),
    ), [
        {"id": "ONBAG_LIBRO_SI",
         "title": _b("ONBAG_LIBRO_SI", "✅ Sí")},
        {"id": "ONBAG_LIBRO_NO",
         "title": _b("ONBAG_LIBRO_NO", "❌ No")},
    ], estado_key="kit_enviado")


def _enviar_ack_foto_libro(telefono):
    _send_text(telefono, _t(
        "activacion.msg_foto_libro_ack", fallback=FB_FOTO_LIBRO_ACK,
    ))


def _enviar_qr_cargado(telefono):
    _send_buttons(telefono, _t(
        "activacion.msg_qr_cargado", fallback=FB_QR_CARGADO,
    ), [{"id": "ONBAG_QR_CARGADO",
         "title": _b("ONBAG_QR_CARGADO", "✨ Perfecto")}],
       estado_key="kit_enviado")


def _enviar_guia(telefono, guia):
    guia = (guia or "").strip()
    if guia:
        body = _t(
            "activacion.msg_guia_envio",
            fallback=FB_GUIA_ENVIO,
            guia=guia,
        )
    else:
        body = _t(
            "activacion.msg_guia_envio_sin_guia",
            fallback=FB_GUIA_ENVIO_SIN_GUIA,
        )
    _send_buttons(telefono, body,
        [{"id": "ONBAG_GUIA_PERFECTO",
          "title": _b("ONBAG_GUIA_PERFECTO", "✨ Perfecto")}],
        estado_key="kit_enviado")


def _enviar_kit_llegado(telefono):
    _send_buttons(telefono, _t(
        "activacion.msg_kit_llegado_contenido",
        fallback=FB_KIT_LLEGADO_CONTENIDO,
    ), [{"id": "ONBAG_DIME_PASOS",
         "title": _b("ONBAG_DIME_PASOS", "✨ Dime los pasos")}],
       estado_key="kit_enviado")


def _enviar_pasos(telefono):
    _send_buttons(telefono, _t(
        "activacion.msg_pasos_instalacion",
        fallback=FB_PASOS_INSTALACION,
    ), [{"id": "ONBAG_YA_INSTALE",
         "title": _b("ONBAG_YA_INSTALE", "✅ Ya lo instalé")}],
       estado_key="kit_enviado")


def _enviar_pedir_foto_qr(telefono):
    _send_text(telefono, _t(
        "activacion.msg_pedir_foto_qr", fallback=FB_PEDIR_FOTO_QR,
    ))


# ─── Transiciones ───────────────────────────────────────────────────────

def _guardar_direccion_y_avanzar(conn, cid, telefono, meta, direccion):
    """Guarda la dirección y salta a `kit_enviado` + sub-paso
    `dir_confirmada`."""
    limpio = direccion.strip()[:280]
    meta["direccion"] = {"completa": limpio}
    meta["direccion_texto"] = limpio
    meta["direccion_capturada_at"] = _dt.datetime.utcnow().isoformat()
    meta.pop("direccion_paso", None)
    _set_estado(meta, "kit_enviado")
    meta["kit_enviado_at"] = _dt.datetime.utcnow().isoformat()
    meta["kit_enviado_tras_direccion"] = True
    meta["kit_paso"] = "dir_confirmada"
    _save_meta(conn, cid, meta)
    _enviar_dir_confirmada(telefono)


def _marcar_paso(conn, cid, meta, nuevo_paso):
    meta["kit_paso"] = nuevo_paso
    meta[f"kit_paso_{nuevo_paso}_at"] = _dt.datetime.utcnow().isoformat()
    _save_meta(conn, cid, meta)


# ─── Entry-point ────────────────────────────────────────────────────────

def handle(telefono, texto, cid, meta, conn, es_handoff=False):
    """Procesa UN turno del flow de activación/kit. Devuelve True."""
    texto = (texto or "").strip()
    t_lc = texto.lower()
    estado = meta.get("estado_agente", "kit_pendiente")
    paso = meta.get("kit_paso") or ""

    # ─── Rama A: kit_pendiente — esperamos dirección ────────────────────
    if estado == "kit_pendiente":
        # Shortcut: pregunta por el kit sin haber dado dirección.
        if t_lc in PALABRAS_KIT:
            _send_text(telefono,
                "Estamos capturando tu dirección. Mandámela en un solo "
                "mensaje con torre/casa + apto + barrio + ciudad 👆")
            _log(telefono, texto, "[v3:kit kit_pendiente: shortcut kit sin dir]")
            return True
        # Validamos la dirección libre.
        if not texto:
            _send_text(telefono, _t(
                "activacion.msg_pedir_direccion_simple",
                fallback=(
                    "📍 Mandame tu dirección completa en un solo mensaje "
                    "(torre o casa, apto, barrio, ciudad)."
                ),
            ))
            return True
        if not _es_direccion_valida(texto):
            _send_text(telefono, _t(
                "activacion.msg_direccion_invalida",
                fallback=FB_DIRECCION_INVALIDA,
            ))
            _log(telefono, texto, "[v3:kit dir inválida]")
            return True
        _guardar_direccion_y_avanzar(conn, cid, telefono, meta, texto)
        _log(telefono, texto, "[v3:kit dir captured → kit_enviado]")
        return True

    # ─── Rama B: kit_enviado — flujo paso a paso ────────────────────────
    if estado != "kit_enviado":
        # Estado no es nuestro.
        return False

    # Shortcut: pregunta por el kit — re-enviamos el msg actual.
    if t_lc in PALABRAS_KIT:
        # Mandamos el msg del paso en el que estamos.
        _reenviar_paso_actual(telefono, meta)
        _log(telefono, texto, f"[v3:kit shortcut kit paso={paso}]")
        return True

    # ── Paso 2 → Paso 3: pulsa ONBAG_KIT_PERFECTO ──────────────────────
    if texto == "ONBAG_KIT_PERFECTO":
        _marcar_paso(conn, cid, meta, "foto_libro_pedida")
        _enviar_pedir_foto_libro(telefono)
        _log(telefono, texto, "[v3:kit → pide foto libro]")
        return True

    # ── Paso 3: pulsa ONBAG_LIBRO_SI → avanzamos a qr_cargado ──────────
    # La agente confirma que ya mandó las fotos del libro. Avanzamos sin
    # esperar que humano cargue el QR — el flow queda autónomo. Humano
    # puede revisar las fotos en el panel.
    # Compat: ONBAG_FOTO_LIBRO (botón viejo) también cuenta como Sí.
    if texto in ("ONBAG_LIBRO_SI", "ONBAG_FOTO_LIBRO"):
        import datetime as _dtl
        meta["qr_cargado_at"] = meta.get("qr_cargado_at") or _dtl.datetime.utcnow().isoformat()
        _marcar_paso(conn, cid, meta, "qr_cargado")
        _enviar_qr_cargado(telefono)
        _log(telefono, texto, "[v3:kit ONBAG_LIBRO_SI → qr_cargado]")
        return True

    # ── Paso 3 alterno: pulsa ONBAG_LIBRO_NO → recordamos cómo subirlas
    if texto == "ONBAG_LIBRO_NO":
        _send_buttons(telefono, _t(
            "activacion.msg_libro_no",
            fallback=FB_LIBRO_NO,
        ), [
            {"id": "ONBAG_LIBRO_SI",
             "title": _b("ONBAG_LIBRO_SI", "✅ Sí")},
            {"id": "ONBAG_LIBRO_NO",
             "title": _b("ONBAG_LIBRO_NO", "❌ No")},
        ], estado_key="kit_enviado")
        _log(telefono, texto, "[v3:kit ONBAG_LIBRO_NO → re-prompt]")
        return True

    # ── Paso 4: pulsa ONBAG_QR_CARGADO (tras admin endpoint) ───────────
    if texto == "ONBAG_QR_CARGADO":
        _marcar_paso(conn, cid, meta, "guia_enviada")
        _enviar_guia(telefono, (meta.get("kit_guia") or "").strip())
        _log(telefono, texto, "[v3:kit → envía guía]")
        return True

    # ── Paso 5 ack: ONBAG_GUIA_PERFECTO (ack de la guía) ───────────────
    if texto == "ONBAG_GUIA_PERFECTO":
        _send_buttons(telefono, _t(
            "activacion.msg_guia_ack",
            fallback=(
                "💜 Dale, apenas te llegue el kit, apretá el botón o "
                "escribime *\"ya llegó\"* y seguimos."
            ),
        ), [{"id": "ONBAG_KIT_LLEGO",
             "title": _b("ONBAG_KIT_LLEGO", "📦 Ya llegó")}],
           estado_key="kit_enviado")
        _log(telefono, texto, "[v3:kit ack guía + botón ya llegó]")
        return True

    # ── Paso 6: kit llegado — botón o texto ─────────────────────────────
    if texto == "ONBAG_KIT_LLEGO" or any(k in t_lc for k in _PAL_KIT_LLEGO):
        _marcar_paso(conn, cid, meta, "kit_recibido")
        _enviar_kit_llegado(telefono)
        _log(telefono, texto, "[v3:kit → kit_recibido / describe contenido]")
        return True

    # ── Paso 7: dime los pasos ──────────────────────────────────────────
    if texto == "ONBAG_DIME_PASOS":
        _marcar_paso(conn, cid, meta, "pasos_mostrados")
        _enviar_pasos(telefono)
        _log(telefono, texto, "[v3:kit → pasos instalación]")
        return True

    # ── Paso final: ya instalé → pedí foto del QR (fase 7 toma de ahí) ──
    if texto == "ONBAG_YA_INSTALE" or any(k in t_lc for k in _PAL_YA_INSTALE):
        _marcar_paso(conn, cid, meta, "qr_foto_pendiente")
        _enviar_pedir_foto_qr(telefono)
        _log(telefono, texto, "[v3:kit → pide foto QR]")
        return True

    # Compat con payloads viejos del flow legacy (ONBAG_KIT_AVISO, etc.):
    # los mandamos al paso correspondiente.
    if texto == "ONBAG_KIT_AVISO":
        # El flow viejo pedía "avisanos cuando llegue" después de la foto
        # del libro — mapeamos a nuestro paso de guía/kit en camino.
        _marcar_paso(conn, cid, meta, "guia_enviada")
        _enviar_guia(telefono, (meta.get("kit_guia") or "").strip())
        _log(telefono, texto, "[v3:kit compat ONBAG_KIT_AVISO]")
        return True
    if texto == "ONBAG_QR_INSTALADO":
        _marcar_paso(conn, cid, meta, "qr_foto_pendiente")
        _enviar_pedir_foto_qr(telefono)
        _log(telefono, texto, "[v3:kit compat ONBAG_QR_INSTALADO]")
        return True

    # Safety-net: si estamos en foto_libro_pedida y la agente responde
    # cualquier cosa afirmativa ("perfecto", "ok", "listo"), avanzamos a
    # qr_cargado para no quedar en loop pidiendo la foto.
    if paso == "foto_libro_pedida":
        PAL_OK = ("perfecto", "ok", "okay", "listo", "dale", "sí", "si",
                  "vale", "ya", "✨ perfecto")
        if any(p == t_lc or p in t_lc for p in PAL_OK):
            import datetime as _dts
            meta["qr_cargado_at"] = meta.get("qr_cargado_at") or _dts.datetime.utcnow().isoformat()
            _marcar_paso(conn, cid, meta, "qr_cargado")
            _enviar_qr_cargado(telefono)
            _log(telefono, texto,
                 "[v3:kit safety-net foto_libro+fotos → qr_cargado]")
            return True

    # Texto libre no reconocido: primero intento FAQ (M4.B).
    # Si el agente FAQ responde la duda + reofrece el botón, listo.
    # Si no es pregunta, cae al recordatorio del paso actual.
    try:
        from app.petra_agents._faq_hook import intentar_faq
        descr, boton_actual = _descripcion_y_boton_del_paso(meta)
        if intentar_faq(telefono, texto, meta, conn, cid,
                        ultimo_paso_descripcion=descr,
                        ultimo_boton=boton_actual):
            _log(telefono, texto, f"[v3:kit FAQ respondió paso={paso}]")
            return True
    except Exception:
        log.exception("[v3:kit] FAQ hook fallo tel=%s", telefono)
    _reenviar_paso_actual(telefono, meta)
    _log(telefono, texto, f"[v3:kit sin match — recordatorio paso={paso}]")
    return True


def _descripcion_y_boton_del_paso(meta):
    """Devuelve (descripcion_corta, botón_actual) para pasar al FAQ."""
    paso = meta.get("kit_paso") or "dir_confirmada"
    estado = meta.get("estado_agente") or "kit_enviado"
    if estado == "kit_pendiente":
        return ("mandar la dirección de donde vivís", None)
    mapping = {
        "dir_confirmada": ("confirmar que recibiste la info del kit",
                           {"id": "ONBAG_KIT_PERFECTO", "title": "✨ Perfecto"}),
        "foto_libro_pedida": ("mandar la foto del libro de arriendos",
                              None),
        "qr_cargado": ("confirmar que viste la carga del QR",
                       {"id": "ONBAG_GUIA_PERFECTO", "title": "✨ Perfecto"}),
        "guia_enviada": ("avisar cuando llegue el kit",
                         {"id": "ONBAG_KIT_LLEGO", "title": "📦 Ya llegó"}),
        "kit_recibido": ("ver los pasos para instalar el QR",
                         {"id": "ONBAG_DIME_PASOS", "title": "✨ Dime los pasos"}),
        "pasos_mostrados": ("instalar el QR y avisarme",
                            {"id": "ONBAG_YA_INSTALE", "title": "✅ Ya lo instalé"}),
        "qr_foto_pendiente": ("mandar la foto del QR instalado", None),
    }
    return mapping.get(paso, ("seguir con el flujo", None))


# ─── Recordatorio del paso actual ───────────────────────────────────────

def _reenviar_paso_actual(telefono, meta):
    """Reemite el mensaje del paso en el que la agente está."""
    paso = meta.get("kit_paso") or "dir_confirmada"
    if paso == "dir_confirmada":
        _enviar_dir_confirmada(telefono)
    elif paso == "foto_libro_pedida":
        _enviar_pedir_foto_libro(telefono)
    elif paso == "qr_cargado":
        _enviar_qr_cargado(telefono)
    elif paso == "guia_enviada":
        _enviar_guia(telefono, (meta.get("kit_guia") or "").strip())
    elif paso == "kit_recibido":
        _enviar_kit_llegado(telefono)
    elif paso == "pasos_mostrados":
        _enviar_pasos(telefono)
    elif paso == "qr_foto_pendiente":
        _enviar_pedir_foto_qr(telefono)
    else:
        _enviar_dir_confirmada(telefono)


# ─── Entry-points usados por los endpoints admin (humano-in-the-loop) ──

def disparar_qr_cargado(conn, cid, telefono, meta):
    """Humano confirma que las fotos del libro están OK y cargó el QR.
    Transiciona a sub-paso `qr_cargado` y manda el mensaje con el botón
    ONBAG_QR_CARGADO."""
    if meta.get("estado_agente") not in ("kit_pendiente", "kit_enviado"):
        return False
    # Si estaba en kit_pendiente aún, forzamos a kit_enviado (defensivo).
    if meta.get("estado_agente") != "kit_enviado":
        _set_estado(meta, "kit_enviado")
        meta["kit_enviado_at"] = _dt.datetime.utcnow().isoformat()
    _marcar_paso(conn, cid, meta, "qr_cargado")
    _enviar_qr_cargado(telefono)
    _log(telefono, "[admin kit-qr-cargado]", "[v3:kit → qr_cargado]")
    return True


def disparar_guia(conn, cid, telefono, meta, guia):
    """Humano setea el número de guía. Guarda `meta.kit_guia` y manda
    el mensaje de la guía con botón ONBAG_GUIA_PERFECTO. Admite
    disparar aun sin que la agente haya pulsado ONBAG_QR_CARGADO: en
    ese caso simplemente avanzamos el paso."""
    guia = (guia or "").strip()
    if meta.get("estado_agente") not in ("kit_pendiente", "kit_enviado"):
        return False
    if meta.get("estado_agente") != "kit_enviado":
        _set_estado(meta, "kit_enviado")
        meta["kit_enviado_at"] = _dt.datetime.utcnow().isoformat()
    meta["kit_guia"] = guia
    _marcar_paso(conn, cid, meta, "guia_enviada")
    _enviar_guia(telefono, guia)
    _log(telefono, f"[admin kit-guia guia={guia}]", "[v3:kit → guía enviada]")
    return True
