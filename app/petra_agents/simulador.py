"""Simulador de flujo del bot — para probar sin enviar WhatsApp real.

Permite al humano:
- Elegir un estado inicial (dropdown)
- Resetear el contacto SIM a ese estado
- Escribir texto o apretar botones como si fuera la agente
- Ver qué respondería el bot (mensajes OUT capturados, no enviados a Meta)

Reutiliza TODO el pipeline real (router → flow → LLM). Lo único que cambia:
petra_wa._post() detecta que estamos capturando y guarda en memoria en
vez de enviar a WhatsApp.

Contacto SIM = telefono fijo "SIM_PETRA_01". Es un contacto real en DB,
podés reiniciarlo tantas veces como quieras.
"""
import datetime as _dt
import json as _json
import logging

log = logging.getLogger("petra")

SIM_TELEFONO = "SIM_PETRA_01"


# =============================================================================
# Presets de estado inicial — cada uno define qué datos quedan en metadata
# para colocar al contacto SIM en el punto deseado del journey.
# =============================================================================

PRESETS = {
    "nuevo": {
        "grupo": "inscripcion", "categoria": "captacion",
        "label": "🆕 Nuevo (recién llegó, pide ser agente)",
        "rol": False,          # borra el rol
        "contexto": "El contacto NO tiene rol todavía. Al escribir cualquier cosa el router lo clasifica y arranca el flow de inscripción.",
        "textos_claves": [],  # el bot todavía no habla
        "botones": [],
        "transiciones": [
            "escribe 'quiero ser agente' → recolectando_vacio",
        ],
    },
    "recolectando_vacio": {
        "grupo": "inscripcion", "categoria": "registro",
        "label": "📝 Inscripción empezada (0 datos)",
        "rol": True,
        "metadata": {"estado_agente": "recolectando"},
        "contexto": "Rol creado, estado=recolectando. Falta pedir los 5 datos (nombre, cédula, ciudad, barrio, conjunto).",
        "textos_claves": [
            "inscripcion.pregunta_nombre",
            "inscripcion.pregunta_cedula",
            "inscripcion.pregunta_ciudad",
            "inscripcion.pregunta_barrio",
            "inscripcion.pregunta_conjunto",
        ],
        "transiciones": [
            "la agente responde cada pregunta → avanza al próximo campo",
            "cuando tiene los 5 datos + 'Registrarme' → inscrito",
        ],
    },
    "recolectando_mitad": {
        "grupo": "inscripcion", "categoria": "registro",
        "label": "📝 Inscripción a mitad (nombre + cédula + ciudad)",
        "rol": True,
        "metadata": {
            "estado_agente": "recolectando",
            "nombre": "Juana Pérez",
            "cedula": "1020800837",
            "ciudad": "Bogotá",
        },
        "contexto": "Rol creado, tiene nombre, cédula y ciudad. Faltan barrio y conjunto.",
        "textos_claves": [
            "inscripcion.pregunta_barrio",
            "inscripcion.pregunta_conjunto",
        ],
        "transiciones": [
            "responde barrio → pide conjunto",
            "responde conjunto → muestra resumen → inscrito",
        ],
    },
    "inscrito": {
        "grupo": "inscripcion", "categoria": "registro",
        "label": "✅ Inscrita (datos completos, botón Ver próximos pasos)",
        "rol": True,
        "metadata": {
            "estado_agente": "inscrito",
            "nombre": "Juana Pérez",
            "cedula": "1020800837",
            "ciudad": "Bogotá",
            "barrio": "Chapinero",
            "conjunto": "Edificio Prueba",
            "inscrito_at": "SIM_NOW",
        },
        "contexto": "Datos completos, inscripción confirmada. Esperando que pulse 'Ver próximos pasos'.",
        "textos_claves": ["inscripcion.msg_felicitaciones"],
        "transiciones": [
            "botón 'Ver próximos pasos' → envía videos bienvenida → bienvenida_enviada",
        ],
    },
    "bienvenida_enviada": {
        "grupo": "inscripcion", "categoria": "capacitacion",
        "label": "🎬 Vio bienvenida, esperando botón Empezar ahora",
        "rol": True,
        "metadata": {
            "estado_agente": "bienvenida_enviada",
            "nombre": "Juana Pérez", "cedula": "1020800837",
            "ciudad": "Bogotá", "barrio": "Chapinero",
            "conjunto": "Edificio Prueba",
            "cap_version": 2,
            "bienvenida_vista_at": "SIM_NOW",
        },
        "contexto": "Recibió los videos de bienvenida. Esperando botón 'Empezar ahora' para arrancar día 1 de capacitación.",
        "textos_claves": [
            "capacitacion.msg_recordatorio_empezar",
            "capacitacion.msg_arranca_manana",
        ],
        "transiciones": [
            "botón 'Empezar ahora' → envía día 1 → en_capacitacion",
        ],
    },
    "en_capacitacion_dia3": {
        "grupo": "inscripcion", "categoria": "capacitacion",
        "label": "📚 En capacitación, día 3 (4 videos vistos)",
        "rol": True,
        "metadata": {
            "estado_agente": "en_capacitacion",
            "nombre": "Juana Pérez", "cedula": "1020800837",
            "ciudad": "Bogotá", "barrio": "Chapinero",
            "conjunto": "Edificio Prueba",
            "cap_version": 2,
            "cap_dia_enviado": 2,
            "videos_vistos": 4,
        },
        "contexto": "A mitad de capacitación. Ya cerró el día 2 (4 videos). Al pulsar 'Listo' avanza al día 3.",
        "textos_claves": [
            "capacitacion.msg_cron_diario",
            "capacitacion.msg_dia_completo",
            "capacitacion.msg_reenvio_videos",
            "capacitacion.msg_por_hoy_ya",
            "capacitacion.msg_siguiente_dia_fallo",
        ],
        "transiciones": [
            "botón ✅ Listo → avanza 2 videos (día siguiente)",
            "botón ⏭ Ver día siguiente → dispara cron inmediato",
            "7× Listo → autorizacion_pendiente",
        ],
    },
    "capacitacion_casi_lista": {
        "grupo": "inscripcion", "categoria": "capacitacion",
        "label": "🏁 Capacitación casi lista (día 6, 12/14 videos)",
        "rol": True,
        "metadata": {
            "estado_agente": "en_capacitacion",
            "nombre": "Juana Pérez", "cedula": "1020800837",
            "ciudad": "Bogotá", "barrio": "Chapinero",
            "conjunto": "Edificio Prueba",
            "cap_version": 2,
            "cap_dia_enviado": 6,
            "videos_vistos": 12,
        },
        "contexto": "Último día pendiente. Al pulsar 'Listo' una vez más → capacitación completa + pasa a autorización.",
        "textos_claves": [
            "capacitacion.msg_cron_diario",
            "capacitacion.msg_completada_header",
        ],
        "transiciones": [
            "último Listo → autorizacion_pendiente",
        ],
    },
    "autorizacion_pendiente": {
        "grupo": "inscripcion", "categoria": "autorizacion",
        "label": "📍 Autorización pendiente (acaba de terminar capacitación)",
        "rol": True,
        "metadata": {
            "estado_agente": "autorizacion_pendiente",
            "nombre": "Juana Pérez", "cedula": "1020800837",
            "ciudad": "Bogotá", "barrio": "Chapinero",
            "conjunto": "Edificio Prueba",
            "cap_version": 2,
            "videos_vistos": 14,
        },
        "contexto": "Terminó capacitación. Esperando botón 'Iniciar' → luego Ver material / Tengo duda / Ya tengo autorización.",
        "textos_claves": [
            "capacitacion.msg_completada_header",
            "autorizacion.cta_iniciar",
            "autorizacion.cta_botones",
            "autorizacion.msg_envio_material",
            "autorizacion.msg_recordatorio_video",
            "autorizacion.msg_post_material",
            "autorizacion.msg_prompt_duda",
            "autorizacion.msg_confirmada",
        ],
        "transiciones": [
            "botón 🚀 Iniciar → muestra 3 botones",
            "botón 📎 Ver material → envía video + demo + piezas",
            "botón ❓ Tengo una duda → prompt + LLM responde",
            "botón ✅ Ya tengo autorización → kit_pendiente (pide dirección)",
        ],
    },
    "kit_pendiente_direccion": {
        "grupo": "inscripcion", "categoria": "activacion",
        "label": "📦 Confirmó autorización (pidiendo dirección — torre)",
        "rol": True,
        "metadata": {
            "estado_agente": "kit_pendiente",
            "direccion_paso": "torre",
            "nombre": "Juana Pérez", "cedula": "1020800837",
            "ciudad": "Bogotá", "barrio": "Chapinero",
            "conjunto": "Edificio Prueba",
            "cap_version": 2,
            "videos_vistos": 14,
        },
        "contexto": "Confirmó autorización. Bot está en medio de captura de dirección (torre, apto, barrio, ciudad).",
        "textos_claves": [
            "activacion.msg_pedir_direccion_inicio",
            "activacion.pregunta_torre",
            "activacion.pregunta_apto",
            "activacion.pregunta_barrio",
            "activacion.pregunta_ciudad",
            "activacion.msg_direccion_completa",
        ],
        "transiciones": [
            "responde torre → pide apto → barrio → ciudad",
            "ciudad respondida → kit_pendiente_direccion_lista",
        ],
    },
    "kit_pendiente_direccion_lista": {
        "grupo": "inscripcion", "categoria": "activacion",
        "label": "📬 Dirección completa, esperando que humano marque kit enviado",
        "rol": True,
        "metadata": {
            "estado_agente": "kit_pendiente",
            "nombre": "Juana Pérez", "cedula": "1020800837",
            "ciudad": "Bogotá", "barrio": "Chapinero",
            "conjunto": "Edificio Prueba",
            "cap_version": 2,
            "videos_vistos": 14,
            "direccion": {
                "torre": "Torre 3", "apto": "702",
                "barrio": "Chapinero", "ciudad": "Bogotá",
            },
            "direccion_capturada_at": "SIM_NOW",
        },
        "contexto": "Dirección capturada. Aparece en tab 'Kits a enviar' del panel esperando que un humano marque 'Enviado'.",
        "textos_claves": ["activacion.msg_direccion_completa"],
        "transiciones": [
            "humano marca 'Enviado' en panel → kit_enviado",
        ],
    },
    "kit_enviado": {
        "grupo": "inscripcion", "categoria": "activacion",
        "label": "🚚 Kit enviado (esperando botón Ya instalé el QR)",
        "rol": True,
        "metadata": {
            "estado_agente": "kit_enviado",
            "nombre": "Juana Pérez", "cedula": "1020800837",
            "ciudad": "Bogotá", "barrio": "Chapinero",
            "conjunto": "Edificio Prueba",
            "cap_version": 2,
            "videos_vistos": 14,
            "kit_enviado_at": "SIM_NOW",
        },
        "contexto": "Humano marcó kit como enviado. Agente debería haber recibido QR + video. Esperando 'Ya instalé el QR'.",
        "textos_claves": [
            "activacion.msg_kit_enviado",
            "activacion.msg_qr_boton",
            "activacion.msg_qr_instalado",
        ],
        "transiciones": [
            "botón '✅ Ya instalé el QR' → activa",
        ],
    },
    "activa": {
        "grupo": "dia_a_dia", "categoria": "activa",
        "label": "🚀 Activa (fase operativa, puede manejar citas e inmuebles)",
        "rol": True,
        "metadata": {
            "estado_agente": "activa",
            "nombre": "Juana Pérez", "cedula": "1020800837",
            "ciudad": "Bogotá", "barrio": "Chapinero",
            "conjunto": "Edificio Prueba",
            "cap_version": 2,
            "videos_vistos": 14,
            "activada_at": "SIM_NOW",
            "video_citas_enviado": True,
        },
        "contexto": "QR instalado. Fase operativa: puede recibir citas desde el panel, cargar inmuebles con 'registrar inmueble', hacer preguntas al LLM.",
        "textos_claves": [
            "activacion.msg_qr_instalado",
            "citas.msg_intro_video",
            "citas.msg_nueva_cita",
            "citas.msg_confirmada",
            "citas.msg_cancelada",
            "citas.msg_pedir_contrapropuesta",
            "inventario.msg_arranque",
            "inventario.msg_completado",
        ],
        "transiciones": [
            "humano crea cita → recibe 3 botones (Puedo/Proponer/No)",
            "escribe 'registrar inmueble' → arranca captura step-by-step",
            "pregunta libre → LLM responde",
        ],
    },
}


def estado_real_del_preset(preset_key):
    """Devuelve el estado_agente real que vive en metadata para este preset.
    Útil para mapear la UI del simulador con tablas que indexan por estado
    real del flow (ej. petra_media_estacion)."""
    preset = PRESETS.get(preset_key) or {}
    meta = preset.get("metadata") or {}
    return meta.get("estado_agente")


def _reemplazar_fechas(meta):
    """Reemplaza 'SIM_NOW' con el timestamp actual."""
    now_iso = _dt.datetime.utcnow().isoformat()
    out = {}
    for k, v in meta.items():
        if v == "SIM_NOW":
            out[k] = now_iso
        elif isinstance(v, dict):
            out[k] = _reemplazar_fechas(v)
        else:
            out[k] = v
    return out


def _asegurar_contacto_sim(conn):
    """Crea el contacto SIM si no existe. Devuelve su id."""
    row = conn.execute(
        "SELECT id FROM petra_contactos WHERE telefono=%s", (SIM_TELEFONO,)
    ).fetchone()
    if row:
        return row[0]
    r = conn.execute(
        "INSERT INTO petra_contactos (telefono, nombre) VALUES (%s, %s) RETURNING id",
        (SIM_TELEFONO, "🧪 Simulador"),
    ).fetchone()
    return r[0]


def reset(preset_key):
    """Resetea el contacto SIM al estado del preset. Devuelve el preset."""
    from app import db as _db
    preset = PRESETS.get(preset_key) or PRESETS["nuevo"]
    with _db._conexion_directa(timeout=10) as conn:
        cid = _asegurar_contacto_sim(conn)
        # Borra sesiones y rol anterior
        conn.execute(
            "DELETE FROM petra_sesion_chat WHERE telefono=%s", (SIM_TELEFONO,),
        )
        conn.execute(
            "DELETE FROM petra_contacto_roles "
            "WHERE contacto_id=%s AND rol='agente_petra'", (cid,),
        )
        # Limpia historial de mensajes del simulador
        conn.execute("DELETE FROM mensajes WHERE telefono=%s", (SIM_TELEFONO,))
        # Limpia logs del LLM del simulador (sin esto, el LLM lee
        # conversaciones previas y aluciona datos viejos)
        try:
            conn.execute(
                "DELETE FROM petra_llm_logs WHERE telefono=%s",
                (SIM_TELEFONO,),
            )
        except Exception:
            pass
        # Limpia también cualquier intent store activo
        try:
            conn.execute(
                "DELETE FROM petra_intent_activo WHERE telefono=%s",
                (SIM_TELEFONO,),
            )
        except Exception:
            pass
        # Si el preset tiene rol, lo inserta con la metadata correspondiente
        if preset.get("rol"):
            meta = _reemplazar_fechas(preset.get("metadata") or {})
            meta.setdefault("estado_agente", "recolectando")
            meta.setdefault("estado_desde", _dt.datetime.utcnow().isoformat())
            conn.execute(
                "INSERT INTO petra_contacto_roles "
                "(contacto_id, rol, metadata) VALUES (%s, 'agente_petra', %s::jsonb)",
                (cid, _json.dumps(meta)),
            )
    log.info("[sim] reseteado al preset %s", preset_key)
    return preset


def tick(texto=None, button_id=None):
    """Procesa UN input del simulador: envía al router/flow y captura
    los mensajes que el bot HABRÍA enviado (sin tocar Meta).
    Persiste IN y OUT en la tabla `mensajes` para que el panel los vea.
    Devuelve lista de mensajes OUT capturados."""
    from app import db as _db
    from app import petra_wa
    from app.petra_agents import router as _router
    # 1. Loguea el IN (lo que la agente simulada escribió)
    try:
        resumen_in = texto or (button_id or "[sin texto]")
        tipo_in = "interactive" if button_id else "text"
        _db.log_mensaje(
            SIM_TELEFONO, "in", tipo_in, resumen_in[:500],
            {"button_id": button_id, "simulado": True},
        )
    except Exception:
        log.exception("[sim] log IN fallo")
    # 2. Ejecuta el flow con captura activada
    petra_wa.iniciar_captura()
    try:
        _router.handle_message(
            telefono=SIM_TELEFONO,
            texto=texto or "",
            button_id=button_id,
            list_id=None,
            contacto_nombre="🧪 Simulador",
            wa_msg_id=None,
        )
    except Exception:
        log.exception("[sim] tick fallo")
    capturados = petra_wa.detener_captura()
    # 3. Persiste los OUT capturados (payload en forma compatible con panel)
    for msg in capturados:
        try:
            _db.log_mensaje(
                SIM_TELEFONO, "out", msg.get("type") or "text",
                (msg.get("resumen") or "")[:500],
                {"status": 200, "payload": msg.get("payload") or {},
                 "simulado": True},
            )
        except Exception:
            log.exception("[sim] log OUT fallo")
    return capturados


def estado_actual():
    """Devuelve metadata y últimos N mensajes OUT del SIM."""
    from app import db as _db
    with _db._conexion_directa(timeout=10) as conn:
        row = conn.execute(
            "SELECT r.metadata FROM petra_contacto_roles r "
            "JOIN petra_contactos c ON c.id = r.contacto_id "
            "WHERE c.telefono=%s AND r.rol='agente_petra'",
            (SIM_TELEFONO,),
        ).fetchone()
        meta = dict(row[0]) if row and row[0] else None
        # Lee los mensajes de este simulador (OUT = del bot, IN = user simulado)
        mensajes = conn.execute(
            "SELECT fecha, direccion, tipo, resumen, payload "
            "FROM mensajes WHERE telefono=%s "
            "ORDER BY fecha ASC LIMIT 100",
            (SIM_TELEFONO,),
        ).fetchall()
    return {
        "metadata": meta,
        "mensajes": [
            {
                "fecha": r[0], "direccion": r[1], "tipo": r[2],
                "resumen": r[3], "payload": r[4],
            } for r in mensajes
        ],
    }
