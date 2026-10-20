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
        "label": "🆕 Nuevo (recién llegó, pide ser agente)",
        "rol": False,          # borra el rol
        "contexto": "El contacto NO tiene rol todavía. Al escribir cualquier cosa el router lo clasifica y arranca el flow de inscripción.",
    },
    "recolectando_vacio": {
        "label": "📝 Inscripción empezada (0 datos)",
        "rol": True,
        "metadata": {"estado_agente": "recolectando"},
        "contexto": "Rol creado, estado=recolectando. Falta pedir los 5 datos (nombre, cédula, ciudad, barrio, conjunto).",
    },
    "recolectando_mitad": {
        "label": "📝 Inscripción a mitad (nombre + cédula + ciudad)",
        "rol": True,
        "metadata": {
            "estado_agente": "recolectando",
            "nombre": "Juana Pérez",
            "cedula": "1020800837",
            "ciudad": "Bogotá",
        },
        "contexto": "Rol creado, tiene nombre, cédula y ciudad. Faltan barrio y conjunto.",
    },
    "inscrito": {
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
    },
    "bienvenida_enviada": {
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
    },
    "en_capacitacion_dia3": {
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
    },
    "capacitacion_casi_lista": {
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
    },
    "autorizacion_pendiente": {
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
    },
    "kit_pendiente_direccion": {
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
    },
    "kit_pendiente_direccion_lista": {
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
    },
    "kit_enviado": {
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
    },
    "activa": {
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
    },
}


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
    Devuelve lista de mensajes OUT capturados."""
    from app import petra_wa
    from app.petra_agents import router as _router
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
    return petra_wa.detener_captura()


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
