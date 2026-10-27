"""Motor DETERMINÍSTICO (sin LLM) para la fase de AUTORIZACIÓN.

M3.B/C: reemplaza a `tools_autorizacion.py` (tool-calling LLM) y la lógica
dentro del flow legacy para `estado=autorizacion_pendiente`. Patrón
idéntico a `inscripcion_v3.py` / `capacitacion_v3.py`:

    handle(telefono, texto, cid, meta, conn, es_handoff=False) -> bool

Activada con `PETRA_V3=1`. Maneja también el nuevo estado pausado
`autorizacion_rechazada` (M3.C).

Flujo:
  entrada (desde capacitación) → bot entra, manda material + CTA Paso 2
      "¿Te sientes preparada para solicitar la reunión…?" + botón
      "📅 Voy a agendar".
  pulsa Voy a agendar → mensaje "mucha suerte" + 3 botones Paso 3:
      ✅ Me dijo que SÍ    → `ONBAG_ADMIN_SI`   → kit_pendiente
      ❌ Me dijo que NO    → `ONBAG_ADMIN_NO`   → autorizacion_rechazada
      ❓ Tengo una duda    → `ONBAG_TENGO_DUDA` → prompt + hardcode mínima

Compat: ONBAG_YA_TENGO_AUTORIZACION / ONBAG_VOY_AGENDAR_CITA siguen
funcionando (agentes en estados viejos no quedan colgadas).

Estado `autorizacion_rechazada` (pausada):
  - Texto "ya me autorizó" / variantes → vuelve a autorizacion_pendiente
    y reinicia la fase (manda material + CTA).
  - Otros textos: recordatorio breve.
"""
import datetime as _dt
import json as _json
import logging
import time as _time

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


# ─── Palabras clave ─────────────────────────────────────────────────────

_PAL_RETOMAR = (
    "ya me autorizó", "ya me autorizo", "me autorizó", "me autorizo",
    "me dieron el ok", "me dio el ok", "ya tengo autorización",
    "ya tengo autorizacion", "ya puedo seguir", "listo me autorizaron",
    "ya me autorizaron",
)
_PAL_ADMIN_SI = (
    "autorizado", "autorizacion", "autorización", "tengo autorizacion",
    "tengo autorización", "ya tengo", "me dijo que si", "me dijo que sí",
    "dijo que si", "dijo que sí", "sí", "si",
)
_PAL_MATERIAL = (
    "material", "materiales", "reenvi", "reenví", "mándame", "mandame",
    "manda", "volver a enviar", "no me llego", "no me llegó", "no llego",
    "no llegó", "video", "videos", "demo", "pieza", "piezas", "gráfica",
    "grafica", "ejemplo", "ejemplos", "apoyo",
)
_PAL_SUERTE = ("suerte", "voy a agendar", "preparada", "lista", "estoy lista")


# ─── Material + CTAs ────────────────────────────────────────────────────

def _enviar_material_y_cta_paso2(conn, telefono):
    """M3.B: material SIEMPRE al entrar en la fase + CTA Paso 2.

    Envía recordatorio + material y después un mensaje con el botón
    '📅 Voy a agendar' (Paso 2: '¿Te sientes preparada para solicitar la
    reunión…?')."""
    from app.petra_agents.flow_onboarding_agente import (
        _enviar_recordatorio_capacitacion, _enviar_material_autorizacion,
    )
    from app import petra_wa as _pwa
    _pwa.send_text(telefono, _t(
        "autorizacion.msg_envio_material",
        fallback=(
            "Perfecto 💜 Acá te paso el material de apoyo para que lo "
            "lleves a la reunión con el administrador:"
        ),
    ))
    _time.sleep(1)
    try:
        _enviar_recordatorio_capacitacion(conn, telefono)
    except Exception:
        log.exception("[v3:auth] recordatorio fallo tel=%s", telefono)
    try:
        _enviar_material_autorizacion(conn, telefono)
    except Exception:
        log.exception("[v3:auth] material fallo tel=%s", telefono)
    _send_buttons(telefono, _t(
        "autorizacion.msg_preparada_paso2",
        fallback=(
            "¿Te sentís preparada para solicitar la reunión con el "
            "administrador de tu conjunto? 💜\n\n"
            "Cuando estés lista, apretá 👇"
        ),
    ), [
        {"id": "ONBAG_VOY_AGENDAR_CITA",
         "title": _b("ONBAG_VOY_AGENDAR_CITA", "📅 Voy a agendar")},
        {"id": "ONBAG_TENGO_DUDA",
         "title": _b("ONBAG_TENGO_DUDA", "❓ Tengo una duda")},
    ], estado_key="autorizacion_pendiente")


def _enviar_paso3_3botones(telefono):
    """M3.B: mensaje de suerte + 3 botones (SÍ / NO / Duda).
    Se envía al pulsar 'Voy a agendar' (Paso 3)."""
    _send_buttons(telefono, _t(
        "autorizacion.msg_voy_agendar",
        fallback=(
            "¡Perfecto! 🙌✨\n\n"
            "Te deseamos *mucha suerte* con la reunión 🤝 Acordate de "
            "llevar el material que te pasé — vas a estar genial.\n\n"
            "Estamos acá si necesitás ayuda 💜 Apenas termines, elegí "
            "abajo según cómo te fue:"
        ),
    ), [
        {"id": "ONBAG_ADMIN_SI",
         "title": _b("ONBAG_ADMIN_SI", "✅ Me dijo que SÍ")},
        {"id": "ONBAG_ADMIN_NO",
         "title": _b("ONBAG_ADMIN_NO", "❌ Me dijo que NO")},
        {"id": "ONBAG_TENGO_DUDA",
         "title": _b("ONBAG_TENGO_DUDA", "❓ Tengo una duda")},
    ], estado_key="autorizacion_pendiente")


def _transicionar_a_kit(conn, cid, telefono, meta):
    """Admin dijo SÍ → kit_pendiente + pedir dirección (arranca M6)."""
    _set_estado(meta, "kit_pendiente")
    meta.pop("direccion_paso", None)
    meta["autorizacion_confirmada_at"] = _dt.datetime.utcnow().isoformat()
    _save_meta(conn, cid, meta)
    _send_text(telefono, _t(
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


def _transicionar_a_rechazada(conn, cid, telefono, meta):
    """Admin dijo NO → estado pausada autorizacion_rechazada."""
    _set_estado(meta, "autorizacion_rechazada")
    meta["autorizacion_rechazada_at"] = _dt.datetime.utcnow().isoformat()
    _save_meta(conn, cid, meta)
    _send_text(telefono, _t(
        "autorizacion.msg_rechazada",
        fallback=(
            "😔 *Lo lamentamos.*\n\n"
            "Si lo ves necesario, podés hablar con un *asesor Petra* "
            "que te acompañe a replantear la conversación con el admin.\n\n"
            "Cuando el administrador te autorice (o si cambia de idea), "
            "escribinos *\"ya me autorizó\"* y seguimos con el kit 💜"
        ),
    ))


def _responder_duda(telefono):
    """ONBAG_TENGO_DUDA: prompt para que escriba la duda.
    (En M4 se enganchará con el FAQ)."""
    _send_text(telefono, _t(
        "autorizacion.msg_prompt_duda",
        fallback=(
            "¡Claro! 💜 Escribime tu pregunta y te respondo acá mismo. "
            "Si no logro resolverlo, te paso con un asesor Petra."
        ),
    ))


# ─── Entry-point ────────────────────────────────────────────────────────

def handle(telefono, texto, cid, meta, conn, es_handoff=False):
    """Procesa UN turno de autorización. Devuelve True si manejó."""
    texto = (texto or "").strip()
    t_lc = texto.lower()
    estado = meta.get("estado_agente", "autorizacion_pendiente")

    # ── Rama A: estado pausado `autorizacion_rechazada` ──────────────────
    if estado == "autorizacion_rechazada":
        # Palabras / botón para retomar.
        if texto in ("ONBAG_ADMIN_SI", "ONBAG_YA_TENGO_AUTORIZACION"):
            _transicionar_a_kit(conn, cid, telefono, meta)
            _log(telefono, texto, "[v3:auth rechazada → kit_pendiente]")
            return True
        if any(k in t_lc for k in _PAL_RETOMAR):
            _set_estado(meta, "autorizacion_pendiente")
            _save_meta(conn, cid, meta)
            _send_text(telefono,
                "¡Buenísimo! Retomamos la autorización 💜")
            _time.sleep(1)
            _enviar_material_y_cta_paso2(conn, telefono)
            _log(telefono, texto, "[v3:auth retoma desde rechazada]")
            return True
        # Cualquier otra cosa: recordatorio breve con el gancho de retomar.
        _send_text(telefono, _t(
            "autorizacion.msg_rechazada_recordatorio",
            fallback=(
                "Seguimos acá cuando el administrador te autorice 💜 "
                "Escribime *\"ya me autorizó\"* y arrancamos con el kit."
            ),
        ))
        _log(telefono, texto, "[v3:auth rechazada: recordatorio]")
        return True

    # ── Rama B: capacitacion_completa (estado legacy) ──────────────────
    # Ya no debería llegar nadie acá: tanto capacitacion_v3 como el flow
    # legacy ahora pasan directo a autorizacion_pendiente al terminar los
    # videos. flow_onboarding_agente._handle_core también intercepta este
    # estado y migra silenciosamente al panel. Mantenemos este bloque
    # como seguro: si una agente queda atrapada acá, la movemos y le
    # mostramos el panel.
    if estado == "capacitacion_completa":
        _set_estado(meta, "autorizacion_pendiente")
        _save_meta(conn, cid, meta)
        from app.petra_agents.flow_onboarding_agente import (
            MSG_FIN_CAPACITACION as _MSG_FIN_CAP,
        )
        _send_buttons(telefono, _t(
            "capacitacion.msg_completada_header",
            fallback=_MSG_FIN_CAP,
        ), [
            {"id": "ONBAG_IR_PANEL",
             "title": _b("ONBAG_IR_PANEL", "Panel de control")},
        ], estado_key="autorizacion_pendiente")
        _log(telefono, texto,
             "[v3:auth capacitacion_completa legacy → panel]")
        return True

    # ── Rama C: estado `autorizacion_pendiente` ─────────────────────────

    # Botón de INICIAR (desde capacitación o re-click): manda material + CTA.
    if texto in ("ONBAG_INICIAR", "ONBAG_INICIAR_AUTORIZACION"):
        _enviar_material_y_cta_paso2(conn, telefono)
        _log(telefono, texto, "[v3:auth iniciar → material + CTA paso2]")
        return True

    # Paso 2 → Paso 3: "Voy a agendar" / "Hablar con admin" / "suerte" /
    # palabras de preparada. Todos disparan los 3 botones SÍ/NO/Duda.
    if texto in ("ONBAG_VOY_AGENDAR_CITA", "ONBAG_HABLAR_ADMIN") \
            or t_lc in _PAL_SUERTE:
        _enviar_paso3_3botones(telefono)
        _log(telefono, texto, "[v3:auth paso3: 3 botones]")
        return True

    # Admin dijo SÍ (nuevo botón + compat con el viejo).
    if texto in ("ONBAG_ADMIN_SI", "ONBAG_YA_TENGO_AUTORIZACION") or \
            t_lc in _PAL_ADMIN_SI:
        _transicionar_a_kit(conn, cid, telefono, meta)
        _log(telefono, texto, "[v3:auth SÍ → kit_pendiente]")
        return True

    # Admin dijo NO (nuevo botón).
    if texto == "ONBAG_ADMIN_NO" or t_lc in (
        "me dijo que no", "dijo que no", "no me autorizó", "no me autorizo",
        "me rechazó", "me rechazo", "rechazó", "rechazo",
    ):
        _transicionar_a_rechazada(conn, cid, telefono, meta)
        _log(telefono, texto, "[v3:auth NO → autorizacion_rechazada]")
        return True

    # Botón de duda.
    if texto == "ONBAG_TENGO_DUDA":
        _responder_duda(telefono)
        _log(telefono, texto, "[v3:auth prompt duda]")
        return True

    # Pedir material (botón viejo o palabra).
    if texto == "ONBAG_VER_MATERIAL" or any(p in t_lc for p in _PAL_MATERIAL):
        _enviar_material_y_cta_paso2(conn, telefono)
        _log(telefono, texto, "[v3:auth re-envío material]")
        return True

    # Pregunta libre → delegamos al LLM agente_petra (en M4 va a FAQ).
    # Anti-spam 30s.
    now = _dt.datetime.utcnow()
    ultimo = meta.get("ultimo_llm_at")
    if ultimo:
        try:
            dt_last = _dt.datetime.fromisoformat(ultimo)
            if (now - dt_last).total_seconds() < 30:
                _log(telefono, texto, "[v3:auth silencio LLM <30s]")
                return True
        except Exception:
            pass
    meta["ultimo_llm_at"] = now.isoformat()
    _save_meta(conn, cid, meta)
    # M4.B escape hatch: primero probamos el mini-agente FAQ.
    try:
        from app.petra_agents._faq_hook import intentar_faq
        if intentar_faq(telefono, texto, meta, conn, cid,
                        ultimo_paso_descripcion=(
                            "hablar con el administrador de tu conjunto"),
                        ultimo_boton={
                            "id": "ONBAG_VOY_AGENDAR_CITA",
                            "title": _b("ONBAG_VOY_AGENDAR_CITA",
                                        "📅 Voy a agendar")}):
            _log(telefono, texto, "[v3:auth FAQ respondió]")
            return True
    except Exception:
        log.exception("[v3:auth] FAQ hook fallo tel=%s", telefono)
    try:
        from app.petra_agents.flow_onboarding_agente import _responder_con_llm
        _log(telefono, texto, "[v3:auth delegado a LLM/FAQ]")
        return _responder_con_llm(telefono, texto)
    except Exception:
        log.exception("[v3:auth] fallback LLM falló tel=%s", telefono)
        _send_text(telefono,
            "Perdón, no te pude responder bien ahora. "
            "Un asesor te contacta pronto 💜")
        return True
