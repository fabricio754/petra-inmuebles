"""PETRA router + state machine del chat portería (Sprint 2).

Flujos conversacionales determinísticos (no ElevenLabs). State machine en
Postgres (petra_conversacion_estado). Diseñado para que el webhook de
WhatsApp lo invoque con el mensaje entrante y devuelva mensajes para enviar.

Flujos implementados:
  - menu_inicial        : bienvenida con botones/lista según rol del contacto
  - publicar            : 8 preguntas secuenciales → crea inmueble
  - ver_inventario      : lista inmuebles del edificio por modalidad
  - editar_anuncio      : selecciona anuncio + modifica un campo

Endpoint de prueba:
  POST /petra/inbound  — simula un mensaje de WhatsApp y devuelve lo que
                         el router respondería (sin enviar al API de Meta).
                         Al reconectar el número, el webhook real llama
                         handle_inbound() directamente.
"""
import json
import logging
import os
import re
from functools import wraps

from flask import Blueprint, jsonify, request

from app import db as _db

log = logging.getLogger("petra")

petra_router_bp = Blueprint("petra_router", __name__, url_prefix="/petra")

# --- auth idéntico a /petra y /tools ----------------------------------------

def _require_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = os.environ.get("TOOLS_API_TOKEN", "")
        if not token or request.headers.get("X-Tools-Token") != token:
            return jsonify({"error": "no_autorizado"}), 401
        return fn(*args, **kwargs)
    return wrapper


# =============================================================================
# TIPOS DE MENSAJE DE RESPUESTA (DTOs)
# =============================================================================

def msg_text(text):
    return {"type": "text", "text": text}


def msg_buttons(text, buttons):
    """buttons: lista de dicts {id, title} (title max 20 chars)."""
    return {"type": "buttons", "text": text, "buttons": buttons}


def msg_list(text, button_label, sections):
    """sections: lista de {title, rows:[{id, title, description?}]}."""
    return {"type": "list", "text": text, "button_label": button_label, "sections": sections}


# =============================================================================
# CATÁLOGOS
# =============================================================================

TIPOS = [
    ("apartamento", "Apartamento"),
    ("casa", "Casa"),
    ("local", "Local"),
    ("oficina", "Oficina"),
    ("habitacion", "Habitación"),
    ("parqueadero", "Parqueadero"),
]
MODALIDADES = [
    ("venta", "Venta"),
    ("arriendo", "Arriendo"),
    ("ambas", "Ambas"),
]
SI_NO = [("si", "Sí"), ("no", "No")]

PUBLICAR_TOTAL_PASOS = 8


# =============================================================================
# PARSEOS
# =============================================================================

_TEL_RE = re.compile(r"^\+?\d{10,15}$")
_TEL_STRIP = re.compile(r"[\s\-\.]+")


def parse_telefono(raw):
    if not raw:
        return None
    s = _TEL_STRIP.sub("", str(raw))
    return s if _TEL_RE.match(s) else None


def parse_num(raw):
    if raw is None:
        return None
    s = str(raw).strip().lower()
    s = re.sub(r"[^\d\.\,]", "", s).replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_torre_piso(raw):
    """Devuelve (torre, piso) tratando de extraer del texto.
    Acepta 'Torre 3 piso 8', 'T3 P8', 'torre 3, piso 8', '3-8', etc."""
    if not raw:
        return (None, None)
    s = str(raw).strip().lower()
    torre = None
    piso = None
    m_t = re.search(r"torre\s*([a-z0-9]+)|t\s*([0-9]+)", s)
    m_p = re.search(r"piso\s*([a-z0-9]+)|p\s*([0-9]+)", s)
    if m_t:
        torre = next(g for g in m_t.groups() if g)
    if m_p:
        piso = next(g for g in m_p.groups() if g)
    if torre is None and piso is None:
        # Posible "3-8" o "3 8"
        nums = re.findall(r"\d+", s)
        if len(nums) == 2:
            torre, piso = nums
        elif len(nums) == 1:
            piso = nums[0]
    return (torre, piso)


def parse_boton_si_no(raw):
    s = (str(raw) or "").strip().lower()
    if s in ("si", "sí", "s", "yes", "y", "1", "true"):
        return True
    if s in ("no", "n", "0", "false"):
        return False
    return None


# =============================================================================
# ESTADO DE CONVERSACIÓN (persistencia)
# =============================================================================

def _get_contacto_id(conn, telefono, nombre=None):
    row = conn.execute(
        "INSERT INTO petra_contactos (telefono, nombre) VALUES (%s, %s) "
        "ON CONFLICT (telefono) DO UPDATE SET "
        "  nombre = COALESCE(EXCLUDED.nombre, petra_contactos.nombre), "
        "  updated_at = NOW() "
        "RETURNING id",
        (telefono, nombre),
    ).fetchone()
    return row[0]


def _load_estado(conn, contacto_id):
    row = conn.execute(
        "SELECT id, flow_name, paso_actual, datos_parciales, contexto "
        "FROM petra_conversacion_estado "
        "WHERE contacto_id = %s AND activa = TRUE "
        "ORDER BY updated_at DESC LIMIT 1",
        (contacto_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "flow_name": row[1],
        "paso_actual": row[2],
        "datos_parciales": row[3] or {},
        "contexto": row[4] or {},
    }


def _close_estado(conn, estado_id):
    conn.execute(
        "UPDATE petra_conversacion_estado SET activa = FALSE, updated_at = NOW() "
        "WHERE id = %s", (estado_id,),
    )


def _save_estado(conn, contacto_id, flow_name, paso, datos, contexto, previo_id=None):
    if previo_id:
        _close_estado(conn, previo_id)
    row = conn.execute(
        "INSERT INTO petra_conversacion_estado "
        "(contacto_id, flow_name, paso_actual, datos_parciales, contexto, activa) "
        "VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, TRUE) "
        "RETURNING id",
        (contacto_id, flow_name, paso, json.dumps(datos), json.dumps(contexto)),
    ).fetchone()
    return row[0]


def _update_estado(conn, estado_id, paso, datos, contexto):
    conn.execute(
        "UPDATE petra_conversacion_estado "
        "SET paso_actual = %s, datos_parciales = %s::jsonb, contexto = %s::jsonb, "
        "    ultimo_mensaje_at = NOW(), updated_at = NOW() "
        "WHERE id = %s",
        (paso, json.dumps(datos), json.dumps(contexto), estado_id),
    )


# =============================================================================
# FLUJO: MENÚ INICIAL (bienvenida al escanear QR o escribir)
# =============================================================================

def flow_menu_inicial(conn, contacto_id, edificio, es_contacto_nuevo):
    """Devuelve mensajes de bienvenida con opciones. No requiere input previo."""
    nombre_edif = edificio["nombre"] if edificio else "PETRA"
    # ¿tiene anuncios activos en este edificio?
    tiene_anuncios = False
    peek = []
    if edificio:
        rows = conn.execute(
            "SELECT id, tipo, modalidad, torre, piso, precio_millones, precio_arriendo_mensual_millones "
            "FROM petra_inmuebles "
            "WHERE contacto_id = %s AND edificio_id = %s AND estado = 'activo' "
            "ORDER BY publicado_at DESC LIMIT 3",
            (contacto_id, edificio["id"]),
        ).fetchall()
        tiene_anuncios = len(rows) > 0
        for r in rows:
            modal = r[2]
            precio = r[5] if modal in ("venta", "ambas") else r[6]
            precio_txt = f"${precio:.0f}M" if precio else "a convenir"
            if modal == "arriendo":
                precio_txt += "/mes"
            peek.append(f"• {r[1].capitalize()} {modal} T{r[3] or '?'} P{r[4] or '?'} — {precio_txt}")

    saludo = f"¡Hola! 👋 Bienvenida al mercado inmobiliario de {nombre_edif}."
    if not es_contacto_nuevo:
        saludo = f"¡Hola de nuevo! 👋"

    if tiene_anuncios:
        texto = saludo + "\n\n" + "Tus anuncios activos:\n" + "\n".join(peek) + "\n\n¿Qué te interesa hoy?"
        msg = msg_list(
            texto,
            "Elegir",
            [{
                "title": "Opciones",
                "rows": [
                    {"id": "publicar", "title": "Publicar nuevo"},
                    {"id": "editar", "title": "Editar mis anuncios"},
                    {"id": "ver_ventas", "title": "Ver ventas"},
                    {"id": "ver_arriendos", "title": "Ver arriendos"},
                ],
            }],
        )
    else:
        texto = saludo + "\n\n¿Qué te interesa?"
        msg = msg_buttons(texto, [
            {"id": "publicar", "title": "Publicar inmueble"},
            {"id": "ver_ventas", "title": "Ver ventas"},
            {"id": "ver_arriendos", "title": "Ver arriendos"},
        ])
    return [msg]


# =============================================================================
# FLUJO: PUBLICAR (8 preguntas)
# =============================================================================

PUBLICAR_PASOS = [
    "tipo", "modalidad", "nombre", "telefono",
    "torre_piso", "area", "parqueadero", "precio", "confirmar",
]


def _publicar_pregunta(paso, datos):
    idx = PUBLICAR_PASOS.index(paso) + 1 if paso != "confirmar" else PUBLICAR_TOTAL_PASOS
    header = f"Pregunta {idx}/{PUBLICAR_TOTAL_PASOS}"
    if paso == "tipo":
        return msg_buttons(
            f"{header}\n¿Qué tipo de inmueble?",
            [{"id": k, "title": v} for k, v in TIPOS[:3]],
        ), msg_buttons(
            "Más opciones:",
            [{"id": k, "title": v} for k, v in TIPOS[3:]],
        )
    if paso == "modalidad":
        return msg_buttons(
            f"{header}\n¿Venta o arriendo?",
            [{"id": k, "title": v} for k, v in MODALIDADES],
        ), None
    if paso == "nombre":
        return msg_text(f"{header}\n¿Cuál es tu nombre?"), None
    if paso == "telefono":
        default = datos.get("telefono_canal", "este número")
        return msg_text(
            f"{header}\n¿Teléfono para que te contacten?\n"
            f"Puedes escribir otro, o responde \"si\" para usar {default}."
        ), None
    if paso == "torre_piso":
        return msg_text(f"{header}\n¿Torre y piso? Ej: \"Torre 3, piso 8\" o solo \"piso 8\"."), None
    if paso == "area":
        return msg_text(f"{header}\n¿Área aproximada en m²? Ej: 80"), None
    if paso == "parqueadero":
        return msg_buttons(
            f"{header}\n¿Tiene parqueadero?",
            [{"id": k, "title": v} for k, v in SI_NO],
        ), None
    if paso == "precio":
        modalidad = datos.get("modalidad")
        if modalidad == "arriendo":
            ej = "Ej: \"2.5\" para $2.5 millones mensuales"
        elif modalidad == "venta":
            ej = "Ej: \"480\" para $480 millones"
        else:
            ej = "Ej: \"480\" venta en millones (del arriendo pregunto después)"
        return msg_text(f"{header}\n¿Precio? {ej}"), None
    if paso == "confirmar":
        res = _resumen_publicar(datos)
        return msg_buttons(
            f"✅ Resumen:\n\n{res}\n\n¿Publicamos?",
            [{"id": "publicar_ok", "title": "Publicar"},
             {"id": "publicar_corregir", "title": "Corregir algo"}],
        ), None


def _resumen_publicar(datos):
    tipo = dict(TIPOS).get(datos.get("tipo"), datos.get("tipo", "?"))
    modalidad = dict(MODALIDADES).get(datos.get("modalidad"), datos.get("modalidad", "?"))
    torre = datos.get("torre") or "?"
    piso = datos.get("piso") or "?"
    area = datos.get("area_m2")
    area_txt = f"{area:.0f}m²" if area else "? m²"
    parq = datos.get("parqueadero")
    parq_txt = "con parqueadero" if parq else ("sin parqueadero" if parq is False else "parqueadero ?")
    nombre = datos.get("nombre_anunciante") or "?"
    tel = datos.get("telefono_contacto") or "?"
    partes = [
        f"{tipo} · {modalidad}",
        f"Torre {torre}, piso {piso}, {area_txt}, {parq_txt}",
    ]
    precio_v = datos.get("precio_millones")
    precio_a = datos.get("precio_arriendo_mensual_millones")
    if precio_v:
        partes.append(f"Venta: ${precio_v:.0f}M")
    if precio_a:
        partes.append(f"Arriendo: ${precio_a:.1f}M/mes")
    partes.append(f"Contacto: {nombre} — {tel}")
    return "\n".join(partes)


def _publicar_procesar_input(paso, texto, button_id, list_id, datos):
    """Devuelve (nuevo_paso, datos_actualizados, error_opt)."""
    val = button_id or list_id or texto
    val_s = (val or "").strip()

    if paso == "tipo":
        key = val_s.lower() if val_s else ""
        if key not in dict(TIPOS):
            return paso, datos, "Elige una opción: " + ", ".join(v for _, v in TIPOS)
        datos["tipo"] = key
        return "modalidad", datos, None

    if paso == "modalidad":
        key = val_s.lower() if val_s else ""
        if key not in dict(MODALIDADES):
            return paso, datos, "Elige: Venta, Arriendo o Ambas"
        datos["modalidad"] = key
        return "nombre", datos, None

    if paso == "nombre":
        if len(val_s) < 2:
            return paso, datos, "Dame tu nombre (mínimo 2 letras)."
        datos["nombre_anunciante"] = val_s[:120]
        return "telefono", datos, None

    if paso == "telefono":
        if val_s.lower() in ("si", "sí", "s", "yes"):
            tel = datos.get("telefono_canal")
            if not tel:
                return paso, datos, "No tengo tu número del canal. Escríbelo tú, por favor."
        else:
            tel = parse_telefono(val_s)
            if not tel:
                return paso, datos, "Formato de teléfono inválido. Ej: +573001234567"
        datos["telefono_contacto"] = tel
        return "torre_piso", datos, None

    if paso == "torre_piso":
        torre, piso = parse_torre_piso(val_s)
        if not torre and not piso:
            return paso, datos, "No pude entender. Escribe algo como: Torre 3 piso 8"
        datos["torre"] = torre
        datos["piso"] = piso
        return "area", datos, None

    if paso == "area":
        area = parse_num(val_s)
        if area is None or area < 5 or area > 10000:
            return paso, datos, "Área inválida. Dame un número entre 5 y 10000, ej: 80"
        datos["area_m2"] = area
        return "parqueadero", datos, None

    if paso == "parqueadero":
        key = val_s.lower() if val_s else ""
        b = parse_boton_si_no(key)
        if b is None:
            return paso, datos, "Responde: Sí o No"
        datos["parqueadero"] = b
        return "precio", datos, None

    if paso == "precio":
        n = parse_num(val_s)
        if n is None or n <= 0:
            return paso, datos, "Precio inválido. Ej: 480 (venta) o 2.5 (arriendo)"
        modalidad = datos.get("modalidad")
        if modalidad == "venta":
            datos["precio_millones"] = n
        elif modalidad == "arriendo":
            datos["precio_arriendo_mensual_millones"] = n
        else:
            # ambas: tomamos el valor como venta; el arriendo lo preguntaríamos
            # con un paso extra (futuro); por ahora dejamos ambos iguales si es ambas.
            datos["precio_millones"] = n
        return "confirmar", datos, None

    if paso == "confirmar":
        if val_s.lower() in ("publicar_ok", "publicar", "si", "sí", "ok"):
            return "__finalizar__", datos, None
        if val_s.lower() in ("publicar_corregir", "corregir"):
            return "__corregir__", datos, None
        return paso, datos, "Elige: Publicar o Corregir algo"

    return paso, datos, "Paso no reconocido"


def _publicar_crear_inmueble(conn, contacto_id, edificio_id, datos):
    """Crea el inmueble en estado activo y devuelve su id. Si existe otro
    anuncio activo con el mismo hash (torre/piso/tipo/modalidad) → devuelve
    tupla (None, inm_id_existente) para que el flujo informe duplicado."""
    from app import petra_core

    dedup = petra_core.dedup_hash_inmueble(
        edificio_id, datos.get("torre"), datos.get("piso"),
        datos.get("apto"), datos.get("tipo"), datos.get("modalidad"),
    )
    existente = conn.execute(
        """SELECT id FROM petra_inmuebles
           WHERE dedup_hash = %s
             AND estado IN ('borrador','activo','reservado','pausado')
           LIMIT 1""",
        (dedup,),
    ).fetchone()
    if existente:
        return None, existente[0]

    row = conn.execute(
        "INSERT INTO petra_inmuebles "
        "(edificio_id, contacto_id, tipo, modalidad, torre, piso, area_m2, "
        " parqueadero, precio_millones, precio_arriendo_mensual_millones, "
        " nombre_anunciante, telefono_contacto, dedup_hash, estado, publicado_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'activo', NOW()) "
        "RETURNING id",
        (edificio_id, contacto_id, datos.get("tipo"), datos.get("modalidad"),
         datos.get("torre"), datos.get("piso"), datos.get("area_m2"),
         datos.get("parqueadero"), datos.get("precio_millones"),
         datos.get("precio_arriendo_mensual_millones"),
         datos.get("nombre_anunciante"), datos.get("telefono_contacto"), dedup),
    ).fetchone()
    conn.execute(
        "INSERT INTO petra_contacto_roles "
        "(contacto_id, rol, estado, edificio_id, metadata) "
        "VALUES (%s, 'anunciante', 'activo', %s, %s::jsonb) "
        "ON CONFLICT (contacto_id, rol, edificio_id) DO UPDATE SET "
        "  estado = 'activo', updated_at = NOW()",
        (contacto_id, edificio_id, json.dumps({"ultimo_inmueble_id": row[0]})),
    )
    return row[0], None


# =============================================================================
# FLUJO: VER INVENTARIO
# =============================================================================

def flow_ver_inventario(conn, edificio_id, modalidad_filtro):
    where = "edificio_id = %s AND estado = 'activo'"
    params = [edificio_id]
    if modalidad_filtro in ("venta", "arriendo"):
        where += " AND (modalidad = %s OR modalidad = 'ambas')"
        params.append(modalidad_filtro)
    rows = conn.execute(
        "SELECT id, tipo, modalidad, torre, piso, area_m2, parqueadero, "
        "       precio_millones, precio_arriendo_mensual_millones, nombre_anunciante "
        f"FROM petra_inmuebles WHERE {where} "
        "ORDER BY publicado_at DESC LIMIT 10",
        params,
    ).fetchall()

    if not rows:
        label = "en venta" if modalidad_filtro == "venta" else "en arriendo"
        return [
            msg_text(f"Por ahora no hay inmuebles {label} publicados aquí."),
            msg_buttons(
                "¿Quieres que te avisemos cuando aparezcan?",
                [{"id": "notif_si", "title": "Sí, avísame"},
                 {"id": "notif_no", "title": "No, gracias"}],
            ),
        ]

    label = "Ventas" if modalidad_filtro == "venta" else "Arriendos"
    sections = [{"title": label, "rows": []}]
    for r in rows[:10]:
        rid, tipo, modal, torre, piso, area, parq, pv, pa, anunciante = r
        area_t = f"{area:.0f}m²" if area else "?"
        if modalidad_filtro == "arriendo" and pa:
            precio_t = f"${pa:.1f}M/mes"
        elif modalidad_filtro == "venta" and pv:
            precio_t = f"${pv:.0f}M"
        else:
            precio_t = "a convenir"
        titulo = f"{tipo.capitalize()} T{torre or '?'} P{piso or '?'}"[:24]
        desc = f"{area_t} · {'con parq' if parq else 'sin parq'} · {precio_t}"[:72]
        sections[0]["rows"].append({"id": f"ver_inm_{rid}", "title": titulo, "description": desc})

    return [msg_list(f"{label} disponibles:", "Ver", sections)]


def flow_ver_detalle_inmueble(conn, inm_id):
    row = conn.execute(
        "SELECT tipo, modalidad, torre, piso, area_m2, parqueadero, "
        "       precio_millones, precio_arriendo_mensual_millones, "
        "       nombre_anunciante "
        "FROM petra_inmuebles WHERE id = %s AND estado IN ('activo','reservado')",
        (inm_id,),
    ).fetchone()
    if not row:
        return [msg_text("Ese inmueble ya no está disponible.")]
    tipo, modal, torre, piso, area, parq, pv, pa, anun = row
    area_t = f"{area:.0f}m²" if area else "?"
    partes = [
        f"{tipo.capitalize()} en {modal}",
        f"Torre {torre or '?'}, piso {piso or '?'}",
        f"Área: {area_t}",
        f"Parqueadero: {'Sí' if parq else ('No' if parq is False else '?')}",
    ]
    if pv:
        partes.append(f"Venta: ${pv:.0f}M")
    if pa:
        partes.append(f"Arriendo: ${pa:.1f}M/mes")
    partes.append(f"\nAnunciante: {anun or '-'}")
    partes.append("Toca 'Me interesa' y te paso el contacto directo.")
    return [msg_buttons("\n".join(partes), [
        {"id": f"interesa_inm_{inm_id}", "title": "Me interesa"},
        {"id": "ver_otro", "title": "Ver otro"},
        {"id": "menu_atras", "title": "Volver"},
    ])]


# =============================================================================
# FLUJO: EDITAR
# =============================================================================

def flow_editar_lista(conn, contacto_id, edificio_id):
    rows = conn.execute(
        "SELECT id, tipo, modalidad, torre, piso FROM petra_inmuebles "
        "WHERE contacto_id = %s AND edificio_id = %s AND estado IN ('activo', 'pausado') "
        "ORDER BY publicado_at DESC",
        (contacto_id, edificio_id),
    ).fetchall()
    if not rows:
        return [msg_text("No tienes anuncios activos en este edificio.")]
    sections = [{"title": "Mis anuncios", "rows": [
        {"id": f"editar_inm_{r[0]}",
         "title": f"{r[1].capitalize()} T{r[3] or '?'} P{r[4] or '?'}"[:24],
         "description": r[2].capitalize()[:72]}
        for r in rows[:10]
    ]}]
    return [msg_list("¿Qué anuncio quieres editar?", "Elegir", sections)]


def flow_editar_opciones(inm_id):
    return [msg_list(
        "¿Qué quieres hacer con este anuncio?",
        "Elegir",
        [{"title": "Acciones", "rows": [
            {"id": f"edit_precio_{inm_id}", "title": "Cambiar precio"},
            {"id": f"edit_tel_{inm_id}", "title": "Cambiar teléfono"},
            {"id": f"edit_area_{inm_id}", "title": "Cambiar área"},
            {"id": f"edit_parq_{inm_id}", "title": "Cambiar parqueadero"},
            {"id": f"edit_pausar_{inm_id}", "title": "Pausar anuncio"},
            {"id": f"edit_vendido_{inm_id}", "title": "Marcar vendido"},
            {"id": f"edit_arrendado_{inm_id}", "title": "Marcar arrendado"},
            {"id": f"edit_eliminar_{inm_id}", "title": "Eliminar"},
        ]}],
    )]


# =============================================================================
# ROUTER PRINCIPAL
# =============================================================================

def handle_inbound(telefono, texto="", button_id=None, list_id=None,
                   edificio_slug=None, contacto_nombre=None):
    """Punto de entrada: toma un mensaje de WhatsApp y devuelve una lista
    de mensajes a enviar al usuario, más el nuevo estado.

    telefono       : E.164 CO del remitente
    texto          : texto libre del mensaje (si no es button/list)
    button_id      : id del quick-reply button si lo tocó
    list_id        : id del list reply si lo tocó
    edificio_slug  : slug detectado del mensaje pre-llenado del QR
    contacto_nombre: nombre del perfil WhatsApp (si disponible)

    Devuelve: {messages: [...], flow: str, paso: str, cerro_flow: bool}
    """
    tel = parse_telefono(telefono)
    if not tel:
        return {"messages": [msg_text("Teléfono inválido.")], "error": "telefono"}

    with _db._conexion_directa(timeout=10) as conn:
        contacto_id = _get_contacto_id(conn, tel, contacto_nombre)
        estado = _load_estado(conn, contacto_id)

        edificio = _resolver_edificio(conn, edificio_slug, estado)

        # Comandos globales
        texto_norm = (texto or "").strip().lower()
        if texto_norm in ("cancelar", "salir", "cancel"):
            if estado:
                _close_estado(conn, estado["id"])
            return {"messages": [msg_text("Listo, cancelé. Si necesitas algo escríbeme otra vez. 👋")],
                    "flow": None, "paso": None, "cerro_flow": True}

        # Botones globales que sobreviven cierre de flujo (Sprint 8)
        boton = (button_id or list_id or "").strip().lower()
        if boton.startswith("servicio_si_"):
            try:
                inm_id = int(boton.replace("servicio_si_", ""))
            except ValueError:
                inm_id = None
            if inm_id:
                return _ofrecer_servicio_petra(conn, contacto_id, inm_id)
        if boton == "servicio_no":
            return {"messages": [msg_text(
                "Perfecto, lo dejo publicado así. Si cambias de opinión, escríbeme. 👍"
            )], "flow": None, "paso": None, "cerro_flow": True}

        # Sprint 12 — Cross-sell (Flujo 6): si el contacto es cliente activo
        # y expresa una nueva intención, atendemos como VIP (saltamos el menú
        # genérico y le damos acogida con contexto).
        if texto and not estado:
            cross = _maybe_cross_sell(conn, contacto_id, texto)
            if cross:
                return cross

        # Sprint 9 — Detector de soporte: si el contacto ya tiene contrato
        # activo y escribe algo con palabras clave de incidencia, abrimos un
        # caso con SLA automático.
        if texto and not estado:
            caso_resp = _maybe_abrir_caso_soporte(conn, contacto_id, texto)
            if caso_resp:
                return caso_resp

        # Si no hay flujo activo y hay edificio nuevo: menú inicial
        if not estado:
            if not edificio:
                return {"messages": [msg_text(
                    "Hola 👋 Para empezar escanea el QR de tu edificio y vuelve a escribirme."
                )], "flow": None, "paso": None, "cerro_flow": False}
            return _iniciar_menu(conn, contacto_id, edificio, es_nuevo=True)

        flow = estado["flow_name"]
        paso = estado["paso_actual"]
        datos = estado["datos_parciales"]
        contexto = estado["contexto"]

        # Comando "atrás" dentro de publicar
        if texto_norm in ("atras", "atrás", "back") and flow == "publicar":
            return _publicar_atras(conn, estado, datos, contexto)

        # Menú → dispatch de botones iniciales
        if flow == "menu_inicial":
            return _menu_dispatch(conn, estado, contacto_id, edificio, button_id or list_id or texto_norm)

        if flow == "publicar":
            return _publicar_step(conn, estado, contacto_id, edificio, texto, button_id, list_id)

        if flow == "ver_inventario":
            return _ver_dispatch(conn, estado, button_id or list_id or texto_norm)

        if flow == "editar":
            return _editar_dispatch(conn, estado, contacto_id, edificio, button_id or list_id or texto_norm, texto)

        # Flujo desconocido → menú
        _close_estado(conn, estado["id"])
        return _iniciar_menu(conn, contacto_id, edificio, es_nuevo=False)


def _resolver_edificio(conn, slug, estado):
    """Resuelve el edificio por slug nuevo, o por contexto previo si no viene slug."""
    if slug:
        row = conn.execute(
            "SELECT id, nombre, ciudad FROM petra_edificios WHERE qr_slug = %s AND activo = TRUE",
            (slug,),
        ).fetchone()
        if row:
            return {"id": row[0], "nombre": row[1], "ciudad": row[2]}
    if estado and estado.get("contexto", {}).get("edificio_id"):
        eid = estado["contexto"]["edificio_id"]
        row = conn.execute(
            "SELECT id, nombre, ciudad FROM petra_edificios WHERE id = %s",
            (eid,),
        ).fetchone()
        if row:
            return {"id": row[0], "nombre": row[1], "ciudad": row[2]}
    return None


def _iniciar_menu(conn, contacto_id, edificio, es_nuevo):
    msgs = flow_menu_inicial(conn, contacto_id, edificio, es_nuevo)
    ctx = {"edificio_id": edificio["id"]} if edificio else {}
    new_id = _save_estado(conn, contacto_id, "menu_inicial", "esperando_opcion", {}, ctx)
    return {"messages": msgs, "flow": "menu_inicial", "paso": "esperando_opcion",
            "estado_id": new_id, "cerro_flow": False}


def _menu_dispatch(conn, estado, contacto_id, edificio, opcion):
    opc = (opcion or "").strip().lower()
    if not edificio:
        return {"messages": [msg_text("Falta contexto del edificio. Escanea el QR de nuevo.")],
                "error": "no_edificio"}

    if opc == "publicar":
        ctx = {"edificio_id": edificio["id"]}
        datos = {"telefono_canal": None}
        new_id = _save_estado(conn, contacto_id, "publicar", "tipo", datos, ctx, previo_id=estado["id"])
        msg1, msg2 = _publicar_pregunta("tipo", datos)
        intro = msg_text(
            f"Perfecto. Te voy a hacer {PUBLICAR_TOTAL_PASOS} preguntas rápidas para publicar.\n"
            "Puedes escribir \"atrás\" para corregir o \"cancelar\" para salir."
        )
        msgs = [intro, msg1] + ([msg2] if msg2 else [])
        return {"messages": msgs, "flow": "publicar", "paso": "tipo",
                "estado_id": new_id, "cerro_flow": False}

    if opc == "editar":
        ctx = {"edificio_id": edificio["id"]}
        new_id = _save_estado(conn, contacto_id, "editar", "lista", {}, ctx, previo_id=estado["id"])
        return {"messages": flow_editar_lista(conn, contacto_id, edificio["id"]),
                "flow": "editar", "paso": "lista", "estado_id": new_id, "cerro_flow": False}

    if opc in ("ver_ventas", "ver_arriendos"):
        modalidad = "venta" if opc == "ver_ventas" else "arriendo"
        ctx = {"edificio_id": edificio["id"], "modalidad": modalidad}
        new_id = _save_estado(conn, contacto_id, "ver_inventario", "lista", {}, ctx, previo_id=estado["id"])
        return {"messages": flow_ver_inventario(conn, edificio["id"], modalidad),
                "flow": "ver_inventario", "paso": "lista", "estado_id": new_id, "cerro_flow": False}

    return {"messages": [msg_text("No entendí. Elige una opción del menú.")],
            "flow": "menu_inicial", "paso": "esperando_opcion"}


def _publicar_atras(conn, estado, datos, contexto):
    paso = estado["paso_actual"]
    try:
        idx = PUBLICAR_PASOS.index(paso)
    except ValueError:
        return {"messages": [msg_text("No puedo volver desde aquí.")]}
    if idx == 0:
        return {"messages": [msg_text("Ya estás en la primera pregunta.")]}
    nuevo = PUBLICAR_PASOS[idx - 1]
    _update_estado(conn, estado["id"], nuevo, datos, contexto)
    msg1, msg2 = _publicar_pregunta(nuevo, datos)
    msgs = [msg_text("↩️ Volvamos atrás."), msg1] + ([msg2] if msg2 else [])
    return {"messages": msgs, "flow": "publicar", "paso": nuevo,
            "estado_id": estado["id"], "cerro_flow": False}


def _publicar_step(conn, estado, contacto_id, edificio, texto, button_id, list_id):
    datos = dict(estado["datos_parciales"])
    datos.setdefault("telefono_canal", _tel_del_contacto(conn, contacto_id))
    contexto = dict(estado["contexto"])
    nuevo_paso, datos, err = _publicar_procesar_input(
        estado["paso_actual"], texto, button_id, list_id, datos
    )
    if err:
        return {"messages": [msg_text(err)], "flow": "publicar",
                "paso": estado["paso_actual"], "estado_id": estado["id"], "cerro_flow": False}

    if nuevo_paso == "__finalizar__":
        if not edificio:
            return {"messages": [msg_text("Falta contexto del edificio. Vuelve a escanear el QR.")]}
        inm_id, duplicado_id = _publicar_crear_inmueble(
            conn, contacto_id, edificio["id"], datos)
        if duplicado_id:
            _close_estado(conn, estado["id"])
            return {"messages": [msg_text(
                "⚠️ Ya existe un anuncio activo para esa torre/piso/tipo en este edificio. "
                "Si es tuyo y necesitas actualizarlo, escríbeme 'editar'."
            )], "flow": None, "paso": None, "cerro_flow": True,
                "duplicado_id": duplicado_id}
        _close_estado(conn, estado["id"])
        # Trigger matching inmediato — notifica interesados que cumplen criterios.
        try:
            from app.petra_match import notificar_matches_al_publicar
            n_matches = notificar_matches_al_publicar(conn, inm_id)
        except Exception:
            log.exception("[petra_router] matching trigger fallo inm=%s", inm_id)
            n_matches = 0
        msgs = [
            msg_text(f"🎉 ¡Publicado! Tu anuncio ya está en el inventario de {edificio['nombre']}."),
        ]
        if n_matches > 0:
            msgs.append(msg_text(
                f"🔔 Avisamos a {n_matches} interesado{'s' if n_matches != 1 else ''} "
                f"que coinciden con tu publicación."
            ))
        else:
            msgs.append(msg_text("Te avisaremos si alguien se interesa."))
        # Ofrecer servicio Petra (separado de publicar en inventario)
        msgs.append(msg_buttons(
            "¿Quieres que Petra te ayude con la venta/arriendo? "
            "Nos encargamos de agendar visitas, filtrar interesados y acompañar el cierre.",
            [
                {"id": f"servicio_si_{inm_id}", "title": "Sí, cuéntame"},
                {"id": "servicio_no", "title": "No por ahora"},
            ],
        ))
        return {"messages": msgs, "flow": None, "paso": None, "estado_id": None,
                "cerro_flow": True, "inmueble_id": inm_id, "matches_notificados": n_matches}

    if nuevo_paso == "__corregir__":
        # Volvemos a la primera pregunta — simple y entendible.
        datos = {k: v for k, v in datos.items() if k == "telefono_canal"}
        _update_estado(conn, estado["id"], "tipo", datos, contexto)
        msg1, msg2 = _publicar_pregunta("tipo", datos)
        return {"messages": [msg_text("Dale, empecemos de nuevo:"), msg1] + ([msg2] if msg2 else []),
                "flow": "publicar", "paso": "tipo", "estado_id": estado["id"], "cerro_flow": False}

    _update_estado(conn, estado["id"], nuevo_paso, datos, contexto)
    msg1, msg2 = _publicar_pregunta(nuevo_paso, datos)
    msgs = [msg1] + ([msg2] if msg2 else [])
    return {"messages": msgs, "flow": "publicar", "paso": nuevo_paso,
            "estado_id": estado["id"], "cerro_flow": False}


def _tel_del_contacto(conn, contacto_id):
    row = conn.execute(
        "SELECT telefono FROM petra_contactos WHERE id = %s", (contacto_id,),
    ).fetchone()
    return row[0] if row else None


def _ver_dispatch(conn, estado, opcion):
    opc = (opcion or "").strip().lower()
    if opc.startswith("ver_inm_"):
        try:
            inm_id = int(opc.replace("ver_inm_", ""))
            return {"messages": flow_ver_detalle_inmueble(conn, inm_id),
                    "flow": "ver_inventario", "paso": "detalle",
                    "estado_id": estado["id"], "cerro_flow": False}
        except ValueError:
            pass
    if opc.startswith("interesa_inm_"):
        try:
            inm_id = int(opc.replace("interesa_inm_", ""))
        except ValueError:
            inm_id = None
        if inm_id:
            interesado_id = _contacto_id_de_estado(conn, estado)
            msgs_interesado, msgs_propietario = _flow_me_interesa(conn, inm_id, interesado_id)
            # Encolar aviso al propietario (outbound queue, se envía por scheduler)
            if msgs_propietario:
                _encolar_aviso_propietario(conn, inm_id, msgs_propietario)
            return {"messages": msgs_interesado,
                    "flow": "ver_inventario", "paso": "post_interes",
                    "estado_id": estado["id"], "cerro_flow": False}
    if opc == "notif_si":
        # Capturamos una búsqueda ancla al edificio con la modalidad del
        # contexto. Es ligera, sin rangos de precio; el interesado puede
        # refinarla luego.
        ctx = estado.get("contexto") or {}
        edif_id = ctx.get("edificio_id")
        modalidad = ctx.get("modalidad") or "ambas"
        if not edif_id:
            return {"messages": [msg_text("Falta contexto, intenta de nuevo.")]}
        row = conn.execute(
            "SELECT id FROM petra_busquedas "
            "WHERE contacto_id = %s AND edificio_id = %s AND modalidad = %s AND activa = TRUE "
            "LIMIT 1",
            (estado.get("contacto_id") or _contacto_id_de_estado(conn, estado),
             edif_id, modalidad),
        ).fetchone()
        if not row:
            cid = _contacto_id_de_estado(conn, estado)
            conn.execute(
                "INSERT INTO petra_busquedas "
                "(contacto_id, edificio_id, modalidad) VALUES (%s, %s, %s)",
                (cid, edif_id, modalidad),
            )
            conn.execute(
                "INSERT INTO petra_contacto_roles "
                "(contacto_id, rol, estado, edificio_id, metadata) "
                "VALUES (%s, 'interesado', 'activo', %s, '{}'::jsonb) "
                "ON CONFLICT (contacto_id, rol, edificio_id) DO UPDATE SET "
                "  estado = 'activo', updated_at = NOW()",
                (cid, edif_id),
            )
        _close_estado(conn, estado["id"])
        return {"messages": [msg_text(
            "👍 Listo. Te avisaré apenas aparezca un inmueble que te sirva. "
            "Si quieres afinar tu búsqueda (precio, tamaño, etc.) escríbeme y la ajustamos."
        )], "flow": None, "paso": None, "cerro_flow": True}
    if opc == "notif_no":
        _close_estado(conn, estado["id"])
        return {"messages": [msg_text("Perfecto. Si cambias de opinión, aquí estaré. 👋")],
                "flow": None, "paso": None, "cerro_flow": True}
    return {"messages": [msg_text("Escríbeme para volver al menú.")],
            "flow": "ver_inventario", "paso": estado["paso_actual"]}


def _contacto_id_de_estado(conn, estado):
    """Resuelve contacto_id del estado si no viene en el dict (compat)."""
    row = conn.execute(
        "SELECT contacto_id FROM petra_conversacion_estado WHERE id = %s",
        (estado["id"],),
    ).fetchone()
    return row[0] if row else None


def _editar_dispatch(conn, estado, contacto_id, edificio, opcion, texto):
    opc = (opcion or "").strip().lower()
    datos = dict(estado["datos_parciales"])
    contexto = dict(estado["contexto"])

    if opc.startswith("editar_inm_"):
        try:
            inm_id = int(opc.replace("editar_inm_", ""))
            datos["inm_id"] = inm_id
            _update_estado(conn, estado["id"], "opciones", datos, contexto)
            return {"messages": flow_editar_opciones(inm_id),
                    "flow": "editar", "paso": "opciones", "estado_id": estado["id"]}
        except ValueError:
            pass

    for action, campo in (
        ("edit_precio_", "precio"),
        ("edit_tel_", "telefono_contacto"),
        ("edit_area_", "area_m2"),
    ):
        if opc.startswith(action):
            try:
                inm_id = int(opc.replace(action, ""))
                datos["inm_id"] = inm_id
                datos["campo"] = campo
                _update_estado(conn, estado["id"], f"esperando_{campo}", datos, contexto)
                return {"messages": [msg_text(f"Dime el nuevo valor para {campo}:")],
                        "flow": "editar", "paso": f"esperando_{campo}", "estado_id": estado["id"]}
            except ValueError:
                pass

    for action, nuevo_estado in (
        ("edit_parq_", "toggle_parqueadero"),
        ("edit_pausar_", "pausar"),
        ("edit_vendido_", "vendido"),
        ("edit_arrendado_", "arrendado"),
        ("edit_eliminar_", "eliminado"),
    ):
        if opc.startswith(action):
            try:
                inm_id = int(opc.replace(action, ""))
                return _editar_aplicar_estado(conn, estado, inm_id, nuevo_estado)
            except ValueError:
                pass

    # Si estamos esperando un valor libre
    paso = estado["paso_actual"]
    if paso.startswith("esperando_"):
        campo = datos.get("campo")
        inm_id = datos.get("inm_id")
        if not inm_id or not campo:
            return {"messages": [msg_text("Error de contexto, intenta de nuevo.")]}
        val = (texto or "").strip()
        if campo == "precio":
            n = parse_num(val)
            if n is None or n <= 0:
                return {"messages": [msg_text("Precio inválido.")]}
            modal = conn.execute(
                "SELECT modalidad FROM petra_inmuebles WHERE id = %s", (inm_id,)
            ).fetchone()
            col = "precio_arriendo_mensual_millones" if (modal and modal[0] == "arriendo") else "precio_millones"
            conn.execute(f"UPDATE petra_inmuebles SET {col} = %s, updated_at = NOW() WHERE id = %s", (n, inm_id))
        elif campo == "telefono_contacto":
            t = parse_telefono(val)
            if not t:
                return {"messages": [msg_text("Teléfono inválido.")]}
            conn.execute("UPDATE petra_inmuebles SET telefono_contacto = %s, updated_at = NOW() WHERE id = %s", (t, inm_id))
        elif campo == "area_m2":
            a = parse_num(val)
            if a is None or a < 5 or a > 10000:
                return {"messages": [msg_text("Área inválida.")]}
            conn.execute("UPDATE petra_inmuebles SET area_m2 = %s, updated_at = NOW() WHERE id = %s", (a, inm_id))
        _close_estado(conn, estado["id"])
        return {"messages": [msg_text("✅ Actualizado.")],
                "flow": None, "paso": None, "cerro_flow": True}

    return {"messages": [msg_text("No entendí. Elige una opción.")],
            "flow": "editar", "paso": estado["paso_actual"]}


def _editar_aplicar_estado(conn, estado, inm_id, accion):
    if accion == "toggle_parqueadero":
        row = conn.execute(
            "SELECT parqueadero FROM petra_inmuebles WHERE id = %s", (inm_id,)
        ).fetchone()
        if not row:
            return {"messages": [msg_text("No encontré el anuncio.")]}
        nuevo = not bool(row[0])
        conn.execute(
            "UPDATE petra_inmuebles SET parqueadero = %s, updated_at = NOW() WHERE id = %s",
            (nuevo, inm_id),
        )
        _close_estado(conn, estado["id"])
        return {"messages": [msg_text(f"✅ Parqueadero actualizado a: {'Sí' if nuevo else 'No'}")],
                "flow": None, "paso": None, "cerro_flow": True}

    mapping = {
        "pausar": ("pausado", "⏸️ Anuncio pausado."),
        "vendido": ("vendido", "🏁 Marcado como vendido. ¡Felicitaciones!"),
        "arrendado": ("arrendado", "🏁 Marcado como arrendado. ¡Felicitaciones!"),
        "eliminado": ("eliminado", "🗑️ Anuncio eliminado."),
    }
    if accion not in mapping:
        return {"messages": [msg_text("Acción no soportada.")]}
    nuevo_estado, msg_ok = mapping[accion]
    if nuevo_estado in ("vendido", "arrendado", "eliminado"):
        conn.execute(
            "UPDATE petra_inmuebles SET estado = %s, cerrado_at = NOW(), cerrado_motivo = %s, "
            "updated_at = NOW() WHERE id = %s AND estado NOT IN ('vendido', 'arrendado', 'eliminado')",
            (nuevo_estado, "edicion_usuario", inm_id),
        )
    else:
        conn.execute(
            "UPDATE petra_inmuebles SET estado = %s, updated_at = NOW() WHERE id = %s",
            (nuevo_estado, inm_id),
        )
    _close_estado(conn, estado["id"])
    return {"messages": [msg_text(msg_ok)], "flow": None, "paso": None, "cerro_flow": True}


# =============================================================================
# ENDPOINT DE PRUEBA (simula WhatsApp)
# =============================================================================

@petra_router_bp.post("/inbound")
@_require_token
def test_inbound():
    """Simula un mensaje entrante de WhatsApp y devuelve lo que respondería.

    Body:
      telefono        : requerido, E.164
      texto           : texto libre (opcional)
      button_id       : id del quick-reply (opcional)
      list_id         : id del list reply (opcional)
      edificio_slug   : slug del QR, para primera conversación (opcional)
      contacto_nombre : nombre del perfil WA (opcional)
    """
    body = request.get_json(silent=True) or {}
    out = handle_inbound(
        telefono=body.get("telefono", ""),
        texto=body.get("texto", "") or "",
        button_id=body.get("button_id"),
        list_id=body.get("list_id"),
        edificio_slug=body.get("edificio_slug"),
        contacto_nombre=body.get("contacto_nombre"),
    )
    return jsonify(out), 200


# =============================================================================
# Sprint 8 — "Me interesa": conectar interesado con propietario + oportunidad
# =============================================================================

def _flow_me_interesa(conn, inm_id, interesado_id):
    """Lee datos del inmueble, crea oportunidad y arma mensajes al interesado
    y al propietario. Devuelve (msgs_interesado, msgs_propietario)."""
    from app import petra_core

    row = conn.execute(
        """SELECT i.contacto_id, i.tipo, i.modalidad, i.torre, i.piso,
                  i.nombre_anunciante, i.telefono_contacto,
                  i.precio_millones, i.precio_arriendo_mensual_millones,
                  e.nombre
             FROM petra_inmuebles i
             JOIN petra_edificios e ON e.id = i.edificio_id
            WHERE i.id = %s AND i.estado IN ('activo','reservado')""",
        (inm_id,),
    ).fetchone()
    if not row:
        return [msg_text("Ese inmueble ya no está disponible.")], None

    (prop_id, tipo, modal, torre, piso, anun, tel_prop,
     pv, pa, edif_nombre) = row

    if not interesado_id:
        return [msg_text("No pude registrarte. Intenta de nuevo.")], None

    if interesado_id == prop_id:
        return [msg_text("Este anuncio es tuyo. 🙂")], None

    # Tipo de oportunidad según modalidad
    tipo_op = "venta" if modal == "venta" else "arriendo"
    valor = (pv * 1_000_000) if (pv and tipo_op == "venta") else (
        (pa * 1_000_000) if pa else None)

    # Oportunidad lado interesado
    petra_core.crear_oportunidad(
        conn, contacto_id=interesado_id, tipo=tipo_op,
        inmueble_id=inm_id, origen="qr_interesado",
        origen_metadata={"inmueble_id": inm_id, "edificio": edif_nombre},
        valor_cop=valor,
    )
    # Oportunidad lado propietario (lado vendedor del match)
    petra_core.crear_oportunidad(
        conn, contacto_id=prop_id, tipo=tipo_op,
        inmueble_id=inm_id, origen="match_interesado",
        origen_metadata={"interesado_id": interesado_id, "edificio": edif_nombre},
        valor_cop=valor,
    )

    # Mensaje al interesado — contacto directo
    partes_i = [f"🎯 {tipo.capitalize()} T{torre or '?'} P{piso or '?'} en {edif_nombre}",
                f"Contacto: {anun or 'Anunciante'} — {tel_prop or '-'}"]
    if tel_prop:
        tel_link = (tel_prop or "").lstrip("+")
        partes_i.append(f"👉 wa.me/{tel_link}")
    partes_i.append("\nTambién le avisé que estás interesado. 👍")

    # Mensaje al propietario (encolado)
    partes_p = [f"👋 Alguien está interesado en tu {tipo} T{torre or '?'} P{piso or '?'} en {edif_nombre}.",
                "Te escribirá directamente. Si prefieres, respóndeme para coordinar."]

    return [msg_text("\n".join(partes_i))], [msg_text("\n".join(partes_p))]


_CROSS_SELL_INTENTS = {
    "venta":    ["vender", "poner en venta", "vender mi"],
    "arriendo": ["arrendar", "alquilar", "poner en arriendo", "rentar"],
    "credito":  ["crédito", "credito", "necesito plata", "financiación", "financiacion", "préstamo", "prestamo"],
    "admin":    ["administración", "administracion", "que me administren", "admin arriendo"],
}


def _maybe_cross_sell(conn, contacto_id, texto):
    """Si el contacto ya es cliente activo y expresa nueva intención,
    responde con bienvenida personalizada y crea oportunidad con origen
    cross_sell. No lo mete a un flujo determinista: el contexto rico lo
    maneja mejor Claude con la info enriquecida del Sprint 5.1."""
    if not contacto_id:
        return None
    row = conn.execute(
        """SELECT c.nombre
           FROM petra_contactos c
           JOIN petra_contacto_roles r ON r.contacto_id = c.id
          WHERE c.id = %s
            AND r.rol IN ('cliente_credito','cliente_servicios','anunciante')
            AND r.estado = 'activo'
          LIMIT 1""",
        (contacto_id,),
    ).fetchone()
    if not row:
        return None
    nombre = row[0] or "cliente"

    t = texto.lower()
    tipo_op = None
    for tipo, keys in _CROSS_SELL_INTENTS.items():
        if any(k in t for k in keys):
            tipo_op = tipo
            break
    if not tipo_op:
        return None

    from app import petra_core
    petra_core.crear_oportunidad(
        conn, contacto_id=contacto_id,
        tipo="venta" if tipo_op == "venta" else (
            "arriendo" if tipo_op == "arriendo" else (
                "credito" if tipo_op == "credito" else "admin_arriendo")),
        origen="cross_sell",
        origen_metadata={"intencion_texto": texto[:200]},
    )
    return {"messages": [msg_text(
        f"¡Hola {nombre}! Qué bueno saber de ti. Veo que te interesa "
        f"{tipo_op}; dado que ya eres cliente, un asesor Petra se contactará "
        f"contigo hoy mismo con condiciones preferentes. ¿Me cuentas más "
        f"detalles mientras tanto?"
    )], "flow": None, "paso": None, "cerro_flow": True}


_SOPORTE_KEYWORDS = {
    "cobro":     ("cobro", ["cobro", "pago", "no ha pagado", "no me ha pagado", "deuda", "factura"]),
    "mantenimiento": ("mantenimiento", ["daño", "danado", "roto", "goteo", "fuga", "no funciona", "reparar", "arreglar", "mantenimiento"]),
    "reclamo":   ("reclamo", ["reclamo", "queja", "molesto", "molesta", "pésimo", "pesimo", "mal servicio", "indignado"]),
    "cancelacion": ("cancelacion", ["cancelar", "retirar servicio", "dar de baja", "terminar contrato"]),
    "consulta":  ("consulta", ["pregunta", "duda", "consulta", "cómo hago", "como hago"]),
}

_PRIORIDAD_POR_CATEGORIA = {
    "cobro": "alta",
    "mantenimiento": "media",
    "reclamo": "alta",
    "cancelacion": "alta",
    "consulta": "baja",
}


def _maybe_abrir_caso_soporte(conn, contacto_id, texto):
    """Si el contacto tiene contrato activo y el texto tiene palabras clave
    de soporte, abre un caso con prioridad por categoría y responde con
    acuse de recibo. Si no aplica, devuelve None."""
    if not contacto_id:
        return None
    t = texto.lower()
    categoria = None
    for _, (cat, keys) in _SOPORTE_KEYWORDS.items():
        if any(k in t for k in keys):
            categoria = cat
            break
    if not categoria:
        return None

    row = conn.execute(
        """SELECT id FROM petra_contratos
           WHERE contacto_id = %s AND estado IN ('activo','firmado')
           ORDER BY updated_at DESC LIMIT 1""",
        (contacto_id,),
    ).fetchone()
    if not row:
        # Sin contrato activo no abrimos caso; dejamos que lo tome Claude.
        return None
    contrato_id = row[0]

    from app import petra_core
    prioridad = _PRIORIDAD_POR_CATEGORIA.get(categoria, "media")
    caso_id = petra_core.crear_caso_soporte(
        conn, contacto_id=contacto_id, categoria=categoria,
        descripcion=texto[:1000], prioridad=prioridad,
        contrato_id=contrato_id,
    )
    sla_horas = petra_core.SLA_HORAS.get(prioridad, 8)
    return {"messages": [msg_text(
        f"Gracias por avisar. Abrí tu caso #{caso_id} ({categoria}, "
        f"prioridad {prioridad}). Un humano te responderá en máximo "
        f"{sla_horas}h. Mientras tanto, si tienes más detalles, escríbelos aquí."
    )], "flow": None, "paso": None, "cerro_flow": True, "caso_id": caso_id}


def _ofrecer_servicio_petra(conn, contacto_id, inm_id):
    """El propietario acepta hablar de contratar servicio Petra: creamos una
    oportunidad separada (intermediacion_venta o admin_arriendo) y mandamos
    datos de contacto para que un humano haga el seguimiento."""
    from app import petra_core

    row = conn.execute(
        "SELECT modalidad FROM petra_inmuebles WHERE id = %s", (inm_id,),
    ).fetchone()
    modalidad = (row[0] if row else "venta") or "venta"
    tipo_op = "intermediacion" if modalidad == "venta" else "admin_arriendo"

    petra_core.crear_oportunidad(
        conn, contacto_id=contacto_id, tipo=tipo_op,
        inmueble_id=inm_id, origen="post_publicacion",
    )
    return {"messages": [msg_text(
        "👍 Perfecto. Un asesor Petra te contactará hoy mismo para explicarte "
        "las condiciones (comisión, tiempos, acompañamiento). Mientras tanto, "
        "tu anuncio sigue publicado."
    )], "flow": None, "paso": None, "cerro_flow": True}


def _encolar_aviso_propietario(conn, inm_id, mensajes):
    """Encola mensajes para el propietario del inmueble en petra_outbound_queue."""
    row = conn.execute(
        "SELECT contacto_id FROM petra_inmuebles WHERE id = %s", (inm_id,),
    ).fetchone()
    if not row:
        return
    prop_id = row[0]
    tel_row = conn.execute(
        "SELECT telefono FROM petra_contactos WHERE id = %s", (prop_id,),
    ).fetchone()
    if not tel_row or not tel_row[0]:
        return
    import json as _json
    for m in mensajes:
        conn.execute(
            """INSERT INTO petra_outbound_queue
               (contacto_id, telefono, tipo, payload, origen, estado)
               VALUES (%s, %s, 'text', %s::jsonb, 'aviso_interesado', 'pendiente')""",
            (prop_id, tel_row[0], _json.dumps(m, ensure_ascii=False)),
        )
