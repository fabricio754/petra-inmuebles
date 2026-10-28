"""Webhook de WhatsApp Cloud API. Este es el único servidor del
prototipo — reemplaza lo que en la arquitectura final haría WATI.
Se ejecuta local y se expone con un túnel (ver README) mientras no
haya hosting propio."""
import os
import json
import logging
import socket
from dotenv import load_dotenv

# Python-level socket timeout so DB/network connections time out in worker threads.
# libpq's connect_timeout uses alarm(2) which only fires in the main thread;
# setdefaulttimeout() uses select(2) which works in all threads including gunicorn's.
socket.setdefaulttimeout(20)

load_dotenv()  # debe cargar antes de importar módulos que leen env al importar

from flask import Flask, request, jsonify, render_template

from app import scheduler, webhook_worker

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("petra")

VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "petra-verify-token")

# MIMEs soportados para documentos enviados por el vendedor
MIME_SOPORTADOS = {"image/jpeg", "image/png", "application/pdf"}

app = Flask(__name__)
from app.tools import tools_bp
app.register_blueprint(tools_bp)
from app.petra import petra_bp
app.register_blueprint(petra_bp)
from app.petra_router import petra_router_bp
app.register_blueprint(petra_router_bp)
from app.petra_match import petra_match_bp
app.register_blueprint(petra_match_bp)
from app.petra_llm import petra_llm_bp
app.register_blueprint(petra_llm_bp)
from app.petra_core import petra_core_bp
app.register_blueprint(petra_core_bp)
from app.petra_panel import petra_panel_bp
app.register_blueprint(petra_panel_bp)
from app.petra_el_bridge import petra_el_bp
app.register_blueprint(petra_el_bp)
scheduler.iniciar()
webhook_worker.iniciar()


@app.get("/")
def health():
    return {"status": "ok", "service": "petra-inmuebles-webhook"}


@app.get("/petra/health_full")
def health_full():
    """Diagnóstico end-to-end: valida DB, env tokens críticos, scheduler.
    Protegido por PETRA_LLM_TOKEN (query ?token=... o Authorization Bearer)."""
    import os as _os
    required = (_os.environ.get("PETRA_LLM_TOKEN") or "").strip()
    if required:
        header = request.headers.get("Authorization", "")
        tok = header[7:].strip() if header.startswith("Bearer ") else ""
        tok = tok or (request.args.get("token") or "").strip()
        if tok != required:
            return {"error": "unauthorized"}, 401

    checks = {}
    # DB
    try:
        from app import db as _db
        with _db._conexion_directa(timeout=5) as conn:
            conn.execute("SELECT 1").fetchone()
            # Conteos rápidos para sanity
            counts = {}
            for t in ("petra_contactos", "petra_oportunidades",
                      "petra_inmuebles", "petra_edificios",
                      "petra_integracion_errores", "petra_llm_logs"):
                try:
                    counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except Exception:
                    counts[t] = "n/a"
            # Errores sin resolver
            err_pend = conn.execute(
                """SELECT estado, COUNT(*) FROM petra_integracion_errores
                   WHERE estado IN ('pendiente_retry','reintentando','dead_letter')
                   GROUP BY estado"""
            ).fetchall()
            checks["db"] = {"ok": True, "counts": counts,
                            "integracion_errores_abiertos": dict(err_pend)}
    except Exception as exc:
        checks["db"] = {"ok": False, "error": str(exc)[:200]}

    # Env críticos (no revela valores, solo presencia)
    env_keys = ["PETRA_LLM_TOKEN", "TOOLS_API_TOKEN", "ANTHROPIC_API_KEY",
                "ANTHROPIC_WORKSPACE_ID", "META_ACCESS_TOKEN",
                "META_PHONE_NUMBER_ID", "WA_PILOT_PHONE_ID",
                "WA_PILOT_TOKEN", "ELEVENLABS_WEBHOOK_SECRET"]
    checks["env"] = {k: bool((_os.environ.get(k) or "").strip())
                     for k in env_keys}

    # Scheduler
    try:
        from app import scheduler as _sched
        checks["scheduler"] = {
            "ok": True,
            "running": bool(getattr(_sched, "_scheduler", None)
                            and _sched._scheduler.running),
        }
    except Exception as exc:
        checks["scheduler"] = {"ok": False, "error": str(exc)[:200]}

    overall_ok = (checks.get("db", {}).get("ok")
                  and checks.get("scheduler", {}).get("ok"))
    return {"ok": bool(overall_ok), "checks": checks}


@app.get("/petra/admin/reset_contacto")
def admin_reset_contacto():
    """DEV: resetea un contacto de testing — borra rol agente, progreso de
    capacitación y últimos N turnos del chat para probar flujos desde cero.

    GET /petra/admin/reset_contacto?token=<PETRA_LLM_TOKEN>&tel=<E164_sin_+>

    Idempotente. No borra el contacto en sí, solo flags y memoria."""
    tok = (request.args.get("token") or "").strip()
    acepta = [
        (os.environ.get("PETRA_LLM_TOKEN") or "").strip(),
        (os.environ.get("PANEL_TOKEN") or "").strip(),
    ]
    acepta = [t for t in acepta if t]
    if acepta and tok not in acepta:
        return {"error": "unauthorized"}, 401
    tel = (request.args.get("tel") or "").strip()
    if not tel:
        return {"error": "tel requerido (E.164 sin +)"}, 400

    out = {"tel": tel, "deleted": {}}
    try:
        from app import db as _db
        with _db._conexion_directa(timeout=10) as conn:
            row = conn.execute(
                "SELECT id FROM petra_contactos WHERE telefono=%s", (tel,)
            ).fetchone()
            cid = row[0] if row else None
            if cid:
                r = conn.execute(
                    "DELETE FROM petra_contacto_roles WHERE contacto_id=%s",
                    (cid,),
                )
                out["deleted"]["roles"] = r.rowcount
                try:
                    r2 = conn.execute(
                        "DELETE FROM petra_capacitacion_progreso WHERE contacto_id=%s",
                        (cid,),
                    )
                    out["deleted"]["capacitacion"] = r2.rowcount
                except Exception:
                    out["deleted"]["capacitacion"] = "n/a"
            else:
                out["deleted"]["roles"] = 0
            r3 = conn.execute(
                "DELETE FROM petra_llm_logs WHERE telefono=%s", (tel,)
            )
            out["deleted"]["llm_logs"] = r3.rowcount
        return out
    except Exception as exc:
        return {"error": str(exc)[:300], "partial": out}, 500


@app.get("/privacidad")
def privacidad():
    """Política de tratamiento de datos (Ley 1581), enlazada desde el bot."""
    return render_template(
        "privacidad.html",
        responsable=os.environ.get(
            "POLITICA_RESPONSABLE",
            "ALMOND CORP S.A.S. (marca Massi), NIT 901.931.289-4, domicilio principal en Bogotá D.C.",
        ),
        contacto=os.environ.get(
            "POLITICA_CONTACTO", "fabricio@petrasecondaries.com o WhatsApp +57 320 2813268"
        ),
        fecha="2 de octubre de 2026",
    )


@app.get("/webhook")
def verify_webhook():
    """Meta llama esto una sola vez, al configurar la URL del webhook."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("Webhook verificado correctamente por Meta.")
        return challenge, 200
    log.warning("Verificación de webhook fallida (token no coincide).")
    return "Forbidden", 403


@app.post("/webhook")
def receive_webhook():
    """Encola el webhook en Postgres y responde 200 instantáneo.

    Antes el handler despachaba el procesamiento a un ThreadPoolExecutor en
    memoria; si el proceso moría (SIGTERM, OOM) los mensajes encolados se
    perdían. Ahora el handler solo INSERTa el payload crudo en
    `webhook_queue` y un worker (app/webhook_worker.py) lo drena. Si el
    proceso muere a mitad de camino, lo pendiente queda en la DB y se
    reprocesa al reiniciar — 0 pérdidas.
    """
    payload = request.get_json(silent=True) or {}
    try:
        # Para que los logs sigan igual que antes, extraemos phone+event
        # (si existen) solo para loggear "Mensaje entrante".
        try:
            entry = payload["entry"][0]
            change = entry["changes"][0]["value"]
            messages = change.get("messages") or []
            if messages:
                message = messages[0]
                phone = message.get("from") or message.get("from_user_id") or "?"
                event = _to_event(message)
            else:
                phone, event = None, None
        except (KeyError, IndexError, TypeError):
            phone, event = None, None

        webhook_worker.encolar(payload)

        if phone is not None:
            log.info("Mensaje entrante de %s: %s", phone, event)
    except Exception:
        log.exception("Error encolando webhook. Payload: %s", payload)
    return jsonify({"status": "received"}), 200


def _to_event(message):
    msg_type = message.get("type")
    if msg_type == "button":
        # Botón de respuesta rápida de una plantilla (ej. "Quiero saber más").
        boton = message.get("button", {})
        return {"type": "template_button", "text": boton.get("text") or boton.get("payload") or ""}
    if msg_type == "text":
        return {"type": "text", "text": message["text"]["body"]}
    if msg_type == "interactive":
        interactive = message["interactive"]
        if interactive["type"] == "list_reply":
            return {"type": "list_reply", "id": interactive["list_reply"]["id"]}
        if interactive["type"] == "button_reply":
            return {"type": "button_reply", "id": interactive["button_reply"]["id"]}
        if interactive["type"] == "nfm_reply":
            # WHATSAPP FLOW -- respuesta del formulario nativo
            return {
                "type": "flow_reply",
                "response": json.loads(interactive["nfm_reply"]["response_json"]),
            }
    if msg_type == "image":
        img = message.get("image", {})
        return {
            "type": "media",
            "media_subtipo": "image",
            "media_id": img.get("id", ""),
            "mime_type": img.get("mime_type", "image/jpeg"),
            "caption": img.get("caption", "") or "",
            "filename": "",
        }
    if msg_type == "document":
        doc = message.get("document", {})
        return {
            "type": "media",
            "media_subtipo": "document",
            "media_id": doc.get("id", ""),
            "mime_type": doc.get("mime_type", "application/pdf"),
            "caption": doc.get("caption", "") or "",
            "filename": doc.get("filename", "") or "",
        }
    if msg_type == "audio":
        au = message.get("audio", {})
        return {
            "type": "media",
            "media_subtipo": "audio",
            "media_id": au.get("id", ""),
            "mime_type": au.get("mime_type", "audio/ogg"),
            "voice": bool(au.get("voice", False)),
            "caption": "",
            "filename": "",
        }
    if msg_type == "video":
        vid = message.get("video", {})
        return {
            "type": "media",
            "media_subtipo": "video",
            "media_id": vid.get("id", ""),
            "mime_type": vid.get("mime_type", "video/mp4"),
            "caption": vid.get("caption", "") or "",
            "filename": "",
        }
    if msg_type == "sticker":
        st = message.get("sticker", {})
        return {
            "type": "media",
            "media_subtipo": "sticker",
            "media_id": st.get("id", ""),
            "mime_type": st.get("mime_type", "image/webp"),
            "caption": "",
            "filename": "",
        }
    # Tipo no manejado (ubicación, contactos...) → texto vacío.
    return {"type": "text", "text": ""}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
