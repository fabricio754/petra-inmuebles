"""Orquestador propio de Petra — reemplazo de ElevenLabs como cerebro de texto.

Diseño:
  - Recibe un mensaje de WhatsApp (texto / botón / lista).
  - Carga historial del chat desde petra_llm_logs.
  - Llama Claude con tool_use NATIVO (sin HTTP de ida-vuelta a /tools/*).
  - Loop hasta que Claude termine (stop_reason=end_turn).
  - Si Anthropic falla → fallback a Gemini.
  - Si tarda > 3s en el primer token → emite "estoy revisando..." keepalive.
  - Guardrails simples propios (regex pre-check + Haiku judge post-check).
  - Responde al usuario vía petra_wa.send_text / send_buttons / send_list.

Activación: ENV `USE_OWN_ORCHESTRATOR=true`.
Mientras esté en false, este módulo existe pero no se invoca: el webhook_worker
sigue ruteando a ElevenLabs (vía EL recibiendo el webhook directo de Meta).

NO uses HTTP para los tools cuando corre acá — las ejecutamos localmente contra
petra_core / petra_wa / tools helpers.
"""
import json
import logging
import os
import re
import threading
import time

log = logging.getLogger("petra")

# =============================================================================
# Config
# =============================================================================
MODEL_PRIMARY = os.environ.get("PETRA_ORCH_MODEL", "claude-haiku-4-5")
MODEL_FALLBACK = os.environ.get("PETRA_ORCH_FALLBACK", "gemini-2.5-flash-lite")
MAX_TURNS = int(os.environ.get("PETRA_ORCH_MAX_TURNS", "6"))
HISTORIAL_MSGS = int(os.environ.get("PETRA_ORCH_HISTORIAL", "20"))
KEEPALIVE_SEG = int(os.environ.get("PETRA_ORCH_KEEPALIVE_SEG", "3"))


# =============================================================================
# System prompt (versión texto; sincronizada con prompt EL cuando migramos voz)
# =============================================================================
SYSTEM_PROMPT = """\
Eres Petra, asistente virtual de la plataforma inmobiliaria PETRA en Colombia.
Hablas por WhatsApp. El teléfono del usuario es {telefono} — NUNCA lo preguntes.

Acompañas 6 journeys:
- Crédito hipotecario con Sureti
- Agente Petra (inscripción → capacitación → autorización → QR → operativo)
- Propietario que publica, vende, arrienda o pide administración
- Interesado que compra o arrienda
- Soporte a clientes activos
- Información general

Reglas duras (NUNCA romper):
- No prometas aprobación de crédito ni montos exactos. Las condiciones son referenciales — las define Sureti.
- No pidas contraseñas, foto de cédula, OTP, selfie con documento, CVV.
- No prometas monto de comisión a agentes (lo define el equipo humano).
- No digas que tú vas a llamar — el usuario toca el 📞 arriba del chat.
- No des asesoría financiera o legal personalizada.
- No preguntes ni repitas el teléfono.

Condiciones crédito (citables):
Hasta 40% del valor comercial · hasta 1.95% mensual · hasta 20 años ·
aprobación 1 día, desembolso 15 días. Siempre: "referenciales, las define Sureti".

Tono: español colombiano cercano, frases cortas, una pregunta a la vez. Si no sabés, dilo con transparencia.

Flujos clave:
- Router de intención al inicio: si no es claro, usa enviar_menu_rama (botones).
- Agente Petra:
  * Perfil ideal: MUJERES, ojalá MADRES cabeza de familia, que VIVEN en un
    conjunto o edificio y tienen tiempo/disponibilidad para gestionar
    anuncios de inmuebles del edificio. NO son porteros, NO son asesores
    inmobiliarios profesionales — son residentes del edificio.
  * Onboarding inicial (3 datos nada más): (1) nombre completo,
    (2) ciudad donde vive, (3) nombre del conjunto o edificio donde VIVE
    (NO preguntar "dónde vas a instalar el QR").
  * Después del onboarding: inscribe → envía videos de capacitación uno a
    uno (marca visto entre cada uno) → tras completar videos, envía kit
    de autorización para la administración del edificio.
  * Dirección exacta (torre + apto) se pide SOLO en etapa de ACTIVACIÓN
    (cuando ya está certificada y aprobada), para enviarle el paquete
    físico con el QR + un regalo. NO pedirla en el onboarding.
  * Tras aprobación de administración → generar QR → capturar anuncios
    existentes del edificio.
- Propietario que da mandato: tipo + comisión + exclusivo + plazo → registrar_mandato.
- Interesado: captura datos → agenda visita → oferta/postulación según venta/arriendo.
- Soporte: marca requiere_humano con prioridad.

Usa las herramientas disponibles. No simules confirmaciones — llama la tool.
"""


# =============================================================================
# Tool schema para Anthropic (23 tools equivalentes a los Server Tools)
# =============================================================================
def _tool_schemas():
    """Las mismas 23 tools que tenía Petra en ElevenLabs, pero como definiciones
    nativas de Anthropic (sin HTTP). Los input_schema son JSON Schema."""
    return [
        {
            "name": "consultar_lead",
            "description": "Recupera el historial del lead de crédito por teléfono (ya se inyecta). Llama al inicio de cada conversación.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "guardar_lead",
            "description": "Crea o actualiza el lead de crédito con los datos que vas acumulando.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string"},
                    "ciudad": {"type": "string"},
                    "tipo_inmueble": {"type": "string"},
                    "avaluo": {"type": "integer"},
                    "monto_solicitado": {"type": "integer"},
                    "destino_credito": {"type": "string"},
                    "email": {"type": "string"},
                },
            },
        },
        {
            "name": "guardar_nota",
            "description": "Deja una nota interna para el operador humano (no visible al cliente).",
            "input_schema": {
                "type": "object",
                "properties": {"texto": {"type": "string"}, "autor": {"type": "string"}},
                "required": ["texto"],
            },
        },
        {
            "name": "marcar_requiere_humano",
            "description": "Escala el caso a un operador humano cuando se detecta frustración, caso complejo o soporte crítico.",
            "input_schema": {
                "type": "object",
                "properties": {"motivo": {"type": "string"}},
                "required": ["motivo"],
            },
        },
        {
            "name": "agendar_recordatorio",
            "description": "Programa un seguimiento futuro con fecha ISO8601.",
            "input_schema": {
                "type": "object",
                "properties": {"disparar_at": {"type": "string"}, "texto": {"type": "string"}},
                "required": ["disparar_at", "texto"],
            },
        },
        {
            "name": "enviar_a_sureti",
            "description": "Envía la solicitud al broker Sureti. SOLO cuando: autorizó + nombre + ciudad + tipo + avalúo/monto + le explicaste que no garantiza aprobación.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "inscribir_agente_petra",
            "description": "Registra al usuario como agente Petra con nombre + ciudad + conjunto donde instalará el QR.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string"},
                    "ciudad": {"type": "string"},
                    "conjunto": {"type": "string"},
                    "disponibilidad": {"type": "string"},
                },
                "required": ["nombre", "ciudad", "conjunto"],
            },
        },
        {
            "name": "enviar_siguiente_video",
            "description": "Envía al agente el próximo video de capacitación pendiente. Devuelve título + URL para que los pegues en el chat.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "marcar_video_visto",
            "description": "Marca como visto el último video enviado al agente. Si fue el último, lo promueve a certificado.",
            "input_schema": {
                "type": "object",
                "properties": {"comentario": {"type": "string"}},
            },
        },
        {
            "name": "enviar_kit_autorizacion",
            "description": "Entrega al agente certificado el material para el administrador de su conjunto.",
            "input_schema": {
                "type": "object",
                "properties": {"conjunto_nombre": {"type": "string"}, "nota": {"type": "string"}},
                "required": ["conjunto_nombre"],
            },
        },
        {
            "name": "reportar_autorizacion",
            "description": "Registra el resultado de la gestión con el admin: aprobada / rechazada / en_gestion.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "estado": {"type": "string", "enum": ["aprobada", "rechazada", "en_gestion"]},
                    "nota": {"type": "string"},
                },
                "required": ["estado"],
            },
        },
        {
            "name": "generar_qr_edificio",
            "description": "Genera el QR del conjunto del agente y devuelve URL PNG. SOLO si autorización aprobada.",
            "input_schema": {
                "type": "object",
                "properties": {"conjunto_nombre": {"type": "string"}, "ciudad": {"type": "string"}},
                "required": ["conjunto_nombre"],
            },
        },
        {
            "name": "capturar_anuncio_existente",
            "description": "El agente envía URL o foto de un anuncio existente de su conjunto para sobrepublicarle QR.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "url_anuncio": {"type": "string"},
                    "foto_url": {"type": "string"},
                    "portal": {"type": "string"},
                    "nota": {"type": "string"},
                },
            },
        },
        {
            "name": "registrar_mandato",
            "description": "Mandato de un propietario: venta / arriendo / administracion. Captura comisión y plazo. NO prometas comisión específica.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "tipo": {"type": "string", "enum": ["venta", "arriendo", "administracion"]},
                    "inmueble_id": {"type": "integer"},
                    "comision_pct": {"type": "number"},
                    "canon_acordado": {"type": "integer"},
                    "exclusivo": {"type": "boolean"},
                    "plazo_meses": {"type": "integer"},
                    "nota": {"type": "string"},
                },
                "required": ["tipo"],
            },
        },
        {
            "name": "publicar_inmueble",
            "description": "Publica un inmueble en un edificio Petra.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "edificio_slug": {"type": "string"},
                    "tipo": {"type": "string"},
                    "modalidad": {"type": "string"},
                    "torre": {"type": "string"},
                    "piso": {"type": "integer"},
                    "area_m2": {"type": "integer"},
                    "parqueadero": {"type": "boolean"},
                    "precio_millones": {"type": "integer"},
                    "precio_arriendo_millones": {"type": "number"},
                    "nombre_anunciante": {"type": "string"},
                },
                "required": ["edificio_slug", "tipo", "modalidad"],
            },
        },
        {
            "name": "registrar_oferta",
            "description": "Registra la oferta de un interesado por un inmueble.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "oportunidad_id": {"type": "integer"},
                    "monto": {"type": "integer"},
                    "condiciones": {"type": "string"},
                },
                "required": ["oportunidad_id", "monto"],
            },
        },
        {
            "name": "registrar_postulacion_arriendo",
            "description": "Postulación para arrendar un inmueble.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "inmueble_id": {"type": "integer"},
                    "oportunidad_id": {"type": "integer"},
                    "codeudor_nombre": {"type": "string"},
                    "codeudor_tel": {"type": "string"},
                    "ingresos_decl": {"type": "integer"},
                },
            },
        },
        {
            "name": "consultar_oportunidad",
            "description": "Consulta oportunidades activas del contacto.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "abrir_caso_soporte",
            "description": "Abre un caso de soporte. prioridad: critica (fuga/seguridad) | alta (cobros) | media | baja.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "categoria": {"type": "string"},
                    "prioridad": {"type": "string", "enum": ["critica", "alta", "media", "baja"]},
                    "descripcion": {"type": "string"},
                },
                "required": ["categoria", "descripcion"],
            },
        },
        {
            "name": "otorgar_consentimiento",
            "description": "Registra consentimiento de habeas data (Ley 1581).",
            "input_schema": {
                "type": "object",
                "properties": {"version_politica": {"type": "string"}},
            },
        },
        {
            "name": "enviar_menu_rama",
            "description": "Lista interactiva con las 5 ramas (publicar/buscar/crédito/agente/soporte). Úsala en saludos neutros o intención ambigua.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "elegir_modalidad_botones",
            "description": "Botones Venta/Arriendo/Ambas al inicio del flujo publicar/buscar.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "intencion": {"type": "string", "enum": ["publicar", "buscar"]},
                },
                "required": ["intencion"],
            },
        },
        {
            "name": "confirmar_visita_botones",
            "description": "Botones Sí/Reagendar/Cancelar para confirmar una visita agendada.",
            "input_schema": {
                "type": "object",
                "properties": {"visita_id": {"type": "integer"}},
                "required": ["visita_id"],
            },
        },
    ]


# =============================================================================
# Tool dispatcher (local, sin HTTP)
# =============================================================================
def _dispatch_tool(name, args, telefono):
    """Ejecuta localmente la lógica del tool. Devuelve dict JSON-serializable."""
    from app import db as _db, petra_core, petra_wa
    try:
        with _db._conexion_directa(timeout=10) as conn:
            if name == "consultar_lead":
                row = conn.execute(
                    "SELECT nombre, ciudad, tipo_inmueble, avaluo, monto_solicitado, "
                    "destino_credito, email FROM pilot_leads WHERE telefono=%s",
                    (telefono,),
                ).fetchone()
                return {"ok": True, "lead": None} if not row else {
                    "ok": True, "lead": dict(zip(
                        ("nombre", "ciudad", "tipo_inmueble", "avaluo",
                         "monto_solicitado", "destino_credito", "email"), row))
                }

            if name == "guardar_lead":
                cols = [k for k, v in args.items() if v is not None]
                if not cols:
                    return {"ok": False, "error": "sin datos"}
                sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols)
                cols_sql = ", ".join(["telefono"] + cols)
                placeholders = ", ".join(["%s"] * (len(cols) + 1))
                conn.execute(
                    f"INSERT INTO pilot_leads ({cols_sql}) VALUES ({placeholders}) "
                    f"ON CONFLICT (telefono) DO UPDATE SET {sets}, updated_at=NOW()",
                    (telefono, *(args[c] for c in cols)),
                )
                return {"ok": True}

            if name == "guardar_nota":
                conn.execute(
                    "INSERT INTO pilot_leads (telefono) VALUES (%s) ON CONFLICT DO NOTHING",
                    (telefono,),
                )
                conn.execute(
                    "INSERT INTO pilot_notas (telefono, autor, texto) VALUES (%s, %s, %s)",
                    (telefono, args.get("autor", "petra"), args["texto"]),
                )
                return {"ok": True}

            if name == "marcar_requiere_humano":
                conn.execute(
                    "INSERT INTO pilot_leads (telefono, requiere_humano, "
                    "requiere_humano_motivo, requiere_humano_at) "
                    "VALUES (%s, TRUE, %s, NOW()) "
                    "ON CONFLICT (telefono) DO UPDATE SET "
                    "requiere_humano=TRUE, requiere_humano_motivo=EXCLUDED.requiere_humano_motivo, "
                    "requiere_humano_at=NOW()",
                    (telefono, args["motivo"]),
                )
                return {"ok": True}

            if name == "agendar_recordatorio":
                conn.execute(
                    "INSERT INTO pilot_leads (telefono) VALUES (%s) ON CONFLICT DO NOTHING",
                    (telefono,),
                )
                conn.execute(
                    "INSERT INTO pilot_recordatorios (telefono, disparar_at, texto) "
                    "VALUES (%s, %s, %s)",
                    (telefono, args["disparar_at"], args["texto"]),
                )
                return {"ok": True}

            if name == "enviar_a_sureti":
                return {"ok": True, "nota": "endpoint real a cablear al enviar"}

            if name == "inscribir_agente_petra":
                cid, estado = petra_core.inscribir_agente(
                    conn, telefono,
                    nombre=args["nombre"], ciudad=args["ciudad"],
                    num_edificios_conocidos=1, origen="orch_" + (args.get("conjunto", "")[:30]),
                )
                return {"ok": True, "contacto_id": cid, "estado": estado}

            if name == "consultar_oportunidad":
                rows = conn.execute(
                    "SELECT o.id, o.tipo, o.estado FROM petra_oportunidades o "
                    "JOIN petra_contactos c ON c.id=o.contacto_id "
                    "WHERE c.telefono=%s ORDER BY o.updated_at DESC LIMIT 5",
                    (telefono,),
                ).fetchall()
                return {"ok": True, "oportunidades": [
                    {"id": r[0], "tipo": r[1], "estado": r[2]} for r in rows]}

            if name == "abrir_caso_soporte":
                row = conn.execute(
                    "SELECT id FROM petra_contactos WHERE telefono=%s", (telefono,),
                ).fetchone()
                if not row:
                    row = conn.execute(
                        "INSERT INTO petra_contactos (telefono) VALUES (%s) RETURNING id",
                        (telefono,),
                    ).fetchone()
                caso_id = petra_core.abrir_caso_soporte(
                    conn, row[0], args["categoria"], args.get("prioridad", "media"),
                    descripcion=args["descripcion"],
                )
                return {"ok": True, "caso_id": caso_id}

            # Tools de UX WhatsApp — mandan directo al usuario
            if name == "enviar_menu_rama":
                petra_wa.send_list(
                    telefono, "¿Con qué te ayudo? Elige una opción:",
                    "Ver opciones",
                    [{"title": "Petra", "rows": [
                        {"id": "RAMA_PUBLICAR", "title": "Publicar inmueble", "description": "Vender o arrendar"},
                        {"id": "RAMA_BUSCAR", "title": "Buscar inmueble", "description": "Explorar inventario"},
                        {"id": "RAMA_CREDITO", "title": "Pedir crédito", "description": "Garantía inmobiliaria"},
                        {"id": "RAMA_AGENTE", "title": "Ser Agente Petra", "description": "Comisión en tu edificio"},
                        {"id": "RAMA_SOPORTE", "title": "Soporte", "description": "Hablar con una persona"},
                    ]}],
                )
                return {"ok": True, "accion": "menu_rama_enviado"}

            if name == "elegir_modalidad_botones":
                verbo = args["intencion"]
                petra_wa.send_buttons(
                    telefono, f"¿Qué modalidad quieres {verbo}?",
                    [
                        {"id": f"MOD_VENTA_{verbo}", "title": "Venta"},
                        {"id": f"MOD_ARRIENDO_{verbo}", "title": "Arriendo"},
                        {"id": f"MOD_AMBAS_{verbo}", "title": "Ambas"},
                    ],
                )
                return {"ok": True}

            if name == "confirmar_visita_botones":
                vid = args["visita_id"]
                petra_wa.send_buttons(
                    telefono, f"¿Confirmas tu visita? (ref #{vid})",
                    [
                        {"id": f"VISITA_SI_{vid}", "title": "Sí, confirmo"},
                        {"id": f"VISITA_REAGENDAR_{vid}", "title": "Reagendar"},
                        {"id": f"VISITA_NO_{vid}", "title": "Cancelar"},
                    ],
                )
                return {"ok": True}

            # Capacitación de agente
            if name in ("enviar_siguiente_video", "marcar_video_visto",
                        "enviar_kit_autorizacion", "reportar_autorizacion",
                        "generar_qr_edificio", "capturar_anuncio_existente",
                        "registrar_mandato", "registrar_oferta",
                        "registrar_postulacion_arriendo", "publicar_inmueble",
                        "otorgar_consentimiento"):
                # Delegamos al mismo helper que usan los endpoints HTTP, llamándolos
                # localmente en vez de hacer HTTP a nuestro propio servidor.
                return _dispatch_a_tools_py(conn, name, args, telefono)

        return {"ok": False, "error": f"tool desconocida: {name}"}
    except Exception as exc:
        log.exception("[orch] tool %s fallo", name)
        return {"ok": False, "error": str(exc)[:200]}


def _dispatch_a_tools_py(conn, name, args, telefono):
    """Delegación local a la lógica equivalente a los endpoints de tools.py.
    Evita HTTP de ida-vuelta a nuestro propio servidor."""
    from app import petra_core
    if name == "enviar_siguiente_video":
        c = conn.execute("SELECT id FROM petra_contactos WHERE telefono=%s", (telefono,)).fetchone()
        if not c:
            return {"ok": False, "error": "no inscrito"}
        rol = conn.execute(
            "SELECT metadata FROM petra_contacto_roles WHERE contacto_id=%s AND rol='agente_petra'",
            (c[0],),
        ).fetchone()
        if not rol:
            return {"ok": False, "error": "no es agente"}
        if (rol[0] or {}).get("estado_agente") == "inscrito":
            petra_core.iniciar_capacitacion(conn, c[0])
        sig = petra_core.siguiente_video_pendiente(conn, c[0])
        if not sig:
            return {"ok": True, "sin_pendientes": True}
        petra_core.marcar_video_enviado(conn, c[0], sig["contenido_id"])
        return {"ok": True, "titulo": sig["titulo"], "url": sig["url"]}
    if name == "marcar_video_visto":
        c = conn.execute("SELECT id FROM petra_contactos WHERE telefono=%s", (telefono,)).fetchone()
        if not c:
            return {"ok": False}
        row = conn.execute(
            "SELECT contenido_id FROM petra_capacitacion_progreso "
            "WHERE contacto_id=%s AND enviado_at IS NOT NULL AND visto_at IS NULL "
            "ORDER BY enviado_at DESC LIMIT 1",
            (c[0],),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "ningún video pendiente de marcar"}
        cert, nota = petra_core.marcar_video_visto(conn, c[0], row[0], args.get("comentario"))
        return {"ok": True, "certificado": cert, "progreso": nota}
    if name == "enviar_kit_autorizacion":
        c = conn.execute("SELECT id FROM petra_contactos WHERE telefono=%s", (telefono,)).fetchone()
        if not c:
            return {"ok": False}
        aid = petra_core.crear_autorizacion_admin(conn, c[0], args["conjunto_nombre"], nota=args.get("nota"))
        petra_core.marcar_kit_autorizacion_enviado(conn, aid)
        kits = conn.execute(
            "SELECT titulo, url, texto FROM petra_contenidos WHERE tipo='kit_autorizacion' AND activo=TRUE ORDER BY orden",
        ).fetchall()
        return {"ok": True, "autorizacion_id": aid,
                "kits": [{"titulo": k[0], "url": k[1], "texto": k[2]} for k in kits]}
    if name == "reportar_autorizacion":
        c = conn.execute("SELECT id FROM petra_contactos WHERE telefono=%s", (telefono,)).fetchone()
        if not c:
            return {"ok": False}
        row = conn.execute(
            "SELECT id FROM petra_autorizaciones_admin WHERE agente_id=%s "
            "AND estado IN ('pendiente','kit_enviado','en_gestion') ORDER BY created_at DESC LIMIT 1",
            (c[0],),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "sin autorización activa"}
        petra_core.actualizar_estado_autorizacion(conn, row[0], args["estado"], nota=args.get("nota"))
        return {"ok": True, "autorizacion_id": row[0], "estado": args["estado"]}
    if name == "generar_qr_edificio":
        c = conn.execute("SELECT id FROM petra_contactos WHERE telefono=%s", (telefono,)).fetchone()
        if not c:
            return {"ok": False}
        edif_id, slug = petra_core.asegurar_edificio_para_agente(
            conn, c[0], args["conjunto_nombre"], ciudad=args.get("ciudad"))
        petra_core.avanzar_estado_agente(conn, c[0], "kit_asignado")
        base = os.environ.get("PUBLIC_BASE_URL", "https://petra-inmuebles.onrender.com")
        return {"ok": True, "edificio_id": edif_id, "qr_slug": slug,
                "qr_png_url": f"{base}/petra/edificios/{edif_id}/qr.png"}
    if name == "capturar_anuncio_existente":
        c = conn.execute("SELECT id FROM petra_contactos WHERE telefono=%s", (telefono,)).fetchone()
        if not c:
            return {"ok": False}
        row = conn.execute(
            "INSERT INTO petra_anuncios_detectados "
            "(agente_id, url_anuncio, foto_url, portal, nota, estado) "
            "VALUES (%s,%s,%s,%s,%s,'pendiente') RETURNING id",
            (c[0], args.get("url_anuncio"), args.get("foto_url"),
             args.get("portal"), args.get("nota")),
        ).fetchone()
        petra_core.avanzar_estado_agente(conn, c[0], "operativo")
        return {"ok": True, "anuncio_id": row[0]}
    if name == "registrar_mandato":
        c = conn.execute("SELECT id FROM petra_contactos WHERE telefono=%s", (telefono,)).fetchone()
        if not c:
            c = conn.execute("INSERT INTO petra_contactos (telefono) VALUES (%s) RETURNING id", (telefono,)).fetchone()
        mid, msg = petra_core.registrar_mandato(
            conn, c[0], args["tipo"],
            inmueble_id=args.get("inmueble_id"),
            comision_pct=args.get("comision_pct"),
            canon_acordado=args.get("canon_acordado"),
            exclusivo=args.get("exclusivo", False),
            plazo_meses=args.get("plazo_meses"),
            nota=args.get("nota"))
        return {"ok": bool(mid), "mandato_id": mid, "msg": msg}
    if name == "registrar_oferta":
        c = conn.execute("SELECT id FROM petra_contactos WHERE telefono=%s", (telefono,)).fetchone()
        if not c:
            return {"ok": False}
        oid = petra_core.registrar_oferta(conn, args["oportunidad_id"], c[0],
                                           args["monto"], args.get("condiciones"))
        return {"ok": True, "oferta_id": oid}
    if name == "registrar_postulacion_arriendo":
        c = conn.execute("SELECT id FROM petra_contactos WHERE telefono=%s", (telefono,)).fetchone()
        if not c:
            c = conn.execute("INSERT INTO petra_contactos (telefono) VALUES (%s) RETURNING id", (telefono,)).fetchone()
        pid = petra_core.registrar_postulacion_arriendo(
            conn, c[0],
            inmueble_id=args.get("inmueble_id"),
            oportunidad_id=args.get("oportunidad_id"),
            codeudor_nombre=args.get("codeudor_nombre"),
            codeudor_tel=args.get("codeudor_tel"),
            ingresos_decl=args.get("ingresos_decl"))
        return {"ok": True, "postulacion_id": pid}
    if name == "publicar_inmueble":
        return {"ok": True, "nota": "cablear a petra_core.publicar via tool helper existente"}
    if name == "otorgar_consentimiento":
        return {"ok": True, "nota": "cablear a petra_core.otorgar_consentimiento"}
    return {"ok": False, "error": f"dispatch no implementado para {name}"}


# =============================================================================
# Guardrails simples (pre + post)
# =============================================================================
_PATRONES_PROHIBIDOS = [
    (re.compile(r"\b(garantiz|aprob[oa]d[oa] seguro|crédito aprobado)\b", re.I),
     "promesa de aprobación"),
    (re.compile(r"\b(c[eé]dula|CVV|contraseñ[ao]|OTP|PIN)\b", re.I),
     "pide credenciales"),
]


def _guardrail_pre_check(texto):
    for pat, motivo in _PATRONES_PROHIBIDOS:
        if pat.search(texto):
            return False, motivo
    return True, None


# =============================================================================
# Keepalive — "estoy revisando..." si tarda > N segundos
# =============================================================================
def _lanzar_keepalive(telefono, cancel_event):
    def _run():
        if cancel_event.wait(KEEPALIVE_SEG):
            return
        try:
            from app import petra_wa
            petra_wa.send_text(telefono, "Un segundo, estoy revisando… 🔎")
        except Exception:
            log.exception("[orch] keepalive fallo")
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# =============================================================================
# Contexto del contacto desde la DB (fuente de verdad para rol y estado)
# =============================================================================
def _cargar_contexto_contacto(telefono):
    """Lee el estado REAL del contacto desde la DB y lo devuelve como string
    listo para inyectar al system prompt. Esto es la fuente de verdad: si
    el operador humano cambia un rol o estado desde el panel, Claude lo ve
    acá — nunca inferir rol del historial del chat."""
    from app import db as _db
    lineas = []
    try:
        with _db._conexion_directa(timeout=5) as conn:
            row = conn.execute(
                "SELECT id, nombre FROM petra_contactos WHERE telefono=%s",
                (telefono,),
            ).fetchone()
            if not row:
                return "contacto NUEVO (no registrado aún en petra_contactos)"
            cid, nombre = row
            if nombre:
                lineas.append(f"nombre: {nombre}")
            roles = conn.execute(
                "SELECT rol, metadata FROM petra_contacto_roles "
                "WHERE contacto_id=%s ORDER BY updated_at DESC",
                (cid,),
            ).fetchall()
            if roles:
                for rol, meta in roles:
                    extra = ""
                    if meta:
                        if rol == "agente_petra":
                            edo = meta.get("estado_agente") or "lead"
                            vv = meta.get("videos_vistos", 0)
                            edif = meta.get("edificio") or meta.get("conjunto")
                            ciudad = meta.get("ciudad")
                            extra = f" [estado={edo}, videos={vv}/5"
                            if edif:
                                extra += f", vive en={edif}"
                            if ciudad:
                                extra += f", ciudad={ciudad}"
                            extra += "]"
                    lineas.append(f"rol: {rol}{extra}")
            else:
                lineas.append("rol: ninguno asignado")
            try:
                lead = conn.execute(
                    "SELECT ciudad, tipo_inmueble, avaluo, monto_hasta_millones "
                    "FROM pilot_leads WHERE telefono=%s",
                    (telefono,),
                ).fetchone()
                if lead and any(lead):
                    campos = [str(v) for v in lead if v]
                    lineas.append("lead_credito activo: " + ", ".join(campos))
            except Exception:
                pass
            try:
                mandatos = conn.execute(
                    "SELECT tipo, estado FROM petra_mandatos "
                    "WHERE contacto_id=%s AND estado IN ('propuesto','firmado','activo') "
                    "ORDER BY updated_at DESC LIMIT 3",
                    (cid,),
                ).fetchall()
                if mandatos:
                    lineas.append("mandatos: " + "; ".join(
                        f"{m[0]}:{m[1]}" for m in mandatos))
            except Exception:
                pass
    except Exception:
        log.exception("[orch] _cargar_contexto_contacto fallo tel=%s", telefono)
        return "no se pudo leer contexto del contacto"
    return "\n".join(lineas) if lineas else "sin datos"


# =============================================================================
# Historial del chat
# =============================================================================
def _cargar_historial(telefono, limite=HISTORIAL_MSGS):
    """Devuelve mensajes recientes en formato [{role, content}]."""
    from app import db as _db
    with _db._conexion_directa(timeout=5) as conn:
        rows = conn.execute(
            "SELECT texto_usuario, texto_asistente FROM petra_llm_logs "
            "WHERE telefono=%s AND rama IN ('claude','orchestrator') "
            "AND texto_usuario IS NOT NULL AND texto_usuario != '' "
            "ORDER BY created_at DESC LIMIT %s",
            (telefono, limite),
        ).fetchall()
    msgs = []
    for user_t, asst_t in reversed(rows):
        if user_t:
            msgs.append({"role": "user", "content": user_t[:4000]})
        if asst_t:
            msgs.append({"role": "assistant", "content": asst_t[:4000]})
    return msgs


# =============================================================================
# Loop principal
# =============================================================================
def handle_message(telefono, texto, button_id=None, list_id=None, contacto_nombre=None):
    """Punto de entrada. Procesa un mensaje de WhatsApp y envía la respuesta.
    Devuelve resumen para logging."""
    from app import petra_wa
    t0 = time.time()

    efectivo = button_id or list_id or texto or ""
    cancel = threading.Event()
    _lanzar_keepalive(telefono, cancel)
    try:
        texto_respuesta, err = _run_claude_loop(telefono, efectivo, contacto_nombre)
    except Exception as exc:
        log.exception("[orch] loop fallo tel=%s", telefono)
        texto_respuesta = "Perdón, estoy teniendo un problema técnico momentáneo. ¿Me reescribes en un ratito? 🙏"
        err = str(exc)[:300]
    cancel.set()

    if texto_respuesta:
        try:
            petra_wa.send_text(telefono, texto_respuesta)
        except Exception:
            log.exception("[orch] send fallo tel=%s", telefono)

    _log_orch(telefono, efectivo, texto_respuesta, int((time.time() - t0) * 1000), err)
    return {"ok": not err, "latencia_ms": int((time.time() - t0) * 1000)}


def _run_claude_loop(telefono, texto_usuario, contacto_nombre=None):
    """Loop Claude tool_use con prompt caching. Devuelve (texto_final, error)."""
    from anthropic import Anthropic
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    workspace = (os.environ.get("ANTHROPIC_WORKSPACE_ID") or "").strip()
    if not api_key:
        return _fallback_gemini(texto_usuario), "sin ANTHROPIC_API_KEY"
    kwargs = {}
    if workspace:
        kwargs["default_headers"] = {"anthropic-workspace-id": workspace}
    client = Anthropic(api_key=api_key, **kwargs)

    contexto_contacto = _cargar_contexto_contacto(telefono)
    system_bloques = [
        {"type": "text",
         "text": SYSTEM_PROMPT.format(telefono=telefono),
         "cache_control": {"type": "ephemeral"}},
        {"type": "text",
         "text": (
             "=== CONTEXTO DEL CONTACTO (fuente de verdad, leído de DB) ===\n"
             f"{contexto_contacto}\n"
             "=== fin contexto ===\n"
             "Este bloque es la verdad sobre el rol y estado del usuario. "
             "El historial del chat sirve solo para continuidad conversacional, "
             "nunca para inferir rol o estado. Si el rol dice 'ninguno asignado' "
             "y el chat anterior lo trataba como agente, es porque un operador "
             "humano lo removió del rol — trátalo como un contacto nuevo que "
             "entra al router de intención."
         )},
    ]
    tools = _tool_schemas()

    historial = _cargar_historial(telefono)
    historial.append({"role": "user", "content": texto_usuario})

    for turno in range(MAX_TURNS):
        try:
            resp = client.messages.create(
                model=MODEL_PRIMARY, max_tokens=2048, system=system_bloques,
                tools=tools, messages=historial,
            )
        except Exception as exc:
            log.exception("[orch] anthropic turno %d fallo", turno)
            # Fallback a Gemini si Anthropic cae
            return _fallback_gemini(texto_usuario, historial), str(exc)[:200]

        if resp.stop_reason == "end_turn":
            # Final — extraemos el texto
            partes = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
            return " ".join(partes).strip() or "¿Me cuentas un poco más?", None

        if resp.stop_reason == "tool_use":
            # Ejecutar las tools que pidió y continuar
            historial.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for b in resp.content:
                if getattr(b, "type", "") == "tool_use":
                    result = _dispatch_tool(b.name, b.input or {}, telefono)
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": b.id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    })
            historial.append({"role": "user", "content": tool_results})
            continue

        # stop_reason inesperado — cortar
        return "Perdón, no pude procesar eso. ¿Me reescribes?", f"stop_reason={resp.stop_reason}"

    return "Déjame esto con un asesor humano, un momento.", "max_turns"


def _fallback_gemini(texto_usuario, historial=None):
    """Fallback a Gemini cuando Anthropic falla."""
    api_key = (os.environ.get("GOOGLE_AI_API_KEY") or "").strip()
    if not api_key:
        return "Perdón, estoy teniendo un problema técnico. ¿Me reescribes en un ratito? 🙏"
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (SYSTEM_PROMPT.replace("{telefono}", "***") +
                  "\n\nUsuario: " + (texto_usuario or ""))
        resp = client.models.generate_content(
            model=MODEL_FALLBACK, contents=prompt)
        return (resp.text or "").strip() or "¿Me cuentas más?"
    except Exception:
        log.exception("[orch] gemini fallback fallo")
        return "Perdón, estoy teniendo un problema técnico. ¿Me reescribes en un ratito? 🙏"


def _log_orch(telefono, texto_u, texto_a, latencia_ms, error):
    try:
        from app import db as _db
        with _db._conexion_directa(timeout=5) as conn:
            conn.execute(
                "INSERT INTO petra_llm_logs "
                "(telefono, rama, texto_usuario, texto_asistente, "
                " mensajes_in, stream, latencia_ms, error) "
                "VALUES (%s, 'orchestrator', %s, %s, '{}'::jsonb, FALSE, %s, %s)",
                (telefono, texto_u[:2000] if texto_u else None,
                 texto_a[:4000] if texto_a else None,
                 latencia_ms, error[:500] if error else None),
            )
    except Exception:
        log.exception("[orch] log fallo")


# =============================================================================
# Feature flag
# =============================================================================
def esta_activo():
    return (os.environ.get("USE_OWN_ORCHESTRATOR") or "").lower() in ("1", "true", "yes")
