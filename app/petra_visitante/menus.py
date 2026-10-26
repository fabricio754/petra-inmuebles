"""Builders de los interactive messages (dicts para `petra_wa.send_message`).

Devuelven dicts con el formato que entiende `app.petra_wa.send_message`:
    {"type": "text",    "text": "..."}
    {"type": "buttons", "text": "...", "buttons": [{"id","title"}, ...]}
    {"type": "list",    "text": "...", "button_label": "...",
                         "sections": [{"title", "rows":[{"id","title","description"}]}]}

Mantener este archivo DELGADO: nada de lógica de negocio, solo armado
de los payloads. El router llama estos helpers y le pasa el resultado
a `petra_wa.send_message`.
"""
from app.petra_visitante import textos as T


# -------- Menu principal (max 3 botones por WhatsApp) ----------------------
def menu_principal(edificio):
    """Bienvenida + 3 botones (arriendo / comprar / publicar)."""
    body = T.BIENVENIDA.format(edificio=edificio or "tu edificio")
    body += "\n\n" + T.MENU_PROMPT
    return {
        "type": "buttons",
        "text": body,
        "buttons": [
            {"id": "VIS_ARRIENDO", "title": T.BTN_ARRIENDO},
            {"id": "VIS_COMPRAR", "title": T.BTN_COMPRAR},
            {"id": "VIS_PUBLICAR", "title": T.BTN_PUBLICAR},
        ],
    }


def solo_menu_boton(texto):
    """Un botón solo que devuelve al menú. Para pantallas de 'nada que
    mostrar' (ej. lista vacía) donde queremos re-dar la opción de
    publicar / volver."""
    return {
        "type": "buttons",
        "text": texto,
        "buttons": [
            {"id": "VIS_PUBLICAR", "title": T.BTN_PUBLICAR},
            {"id": "VIS_MENU", "title": T.BTN_MENU},
        ],
    }


# -------- Lista de inmuebles -----------------------------------------------
def lista_inmuebles(edificio, operacion, inmuebles):
    """Interactive list con hasta 10 inmuebles. `inmuebles` es una lista
    de dicts con las claves: id, nro, hab, banos, m2, precio, tipo.

    operacion ∈ {'arriendo', 'venta'} — cambia el header y el prefijo
    de los `id` que vuelven como `list_id`."""
    if operacion == "arriendo":
        header = T.LISTA_ARRIENDO_HEADER.format(edificio=edificio)
        prefix = "VIS_ARR_DET"
    else:
        header = T.LISTA_VENTA_HEADER.format(edificio=edificio)
        prefix = "VIS_VEN_DET"

    rows = []
    for inm in inmuebles[:10]:
        precio_fmt = T.formatear_precio(inm.get("precio"))
        desc = T.format_row_desc(
            hab=inm.get("hab"),
            banos=inm.get("banos"),
            m2=inm.get("m2"),
            precio_fmt=precio_fmt,
        )
        rows.append({
            "id": f"{prefix}:{inm['id']}",
            "title": T.row_title(inm.get("tipo"),
                                 inm.get("nro") or inm["id"]),
            "description": desc,
        })

    return {
        "type": "list",
        "text": header,
        "button_label": T.LISTA_BTN_LABEL,
        "sections": [{"title": T.LISTA_SECCION_TITULO, "rows": rows}],
    }


# -------- Detalle de inmueble ----------------------------------------------
def detalle_inmueble(edificio, operacion, inm):
    """Texto largo + botones Contactar / Volver."""
    tpl = T.DETALLE_ARRIENDO if operacion == "arriendo" else T.DETALLE_VENTA
    gestor = (
        T.GESTOR_PROPIETARIO if (inm.get("fuente") == "propietario")
        else T.GESTOR_PETRA
    )
    titulo = T.row_title(inm.get("tipo"), inm.get("nro") or inm["id"])
    body = tpl.format(
        titulo=titulo,
        edificio=edificio,
        atributos=T.detalle_atributos(
            hab=inm.get("hab"),
            banos=inm.get("banos"),
            m2=inm.get("m2"),
        ),
        precio_fmt=T.formatear_precio(inm.get("precio")),
        gestor=gestor,
    )
    if operacion == "arriendo":
        contact_id = f"VIS_ARR_CT:{inm['id']}"
    else:
        contact_id = f"VIS_VEN_CT:{inm['id']}"
    return {
        "type": "buttons",
        "text": body,
        "buttons": [
            {"id": contact_id, "title": T.BTN_CONTACTAR},
            {"id": "VIS_MENU", "title": T.BTN_MENU},
        ],
    }


# -------- Confirmación de contactar ----------------------------------------
def confirmar_contactar(nro, inmueble_id, operacion):
    prefix = "VIS_ARR_CT_OK" if operacion == "arriendo" else "VIS_VEN_CT_OK"
    return {
        "type": "buttons",
        "text": T.CONTACTAR_PROMPT.format(nro=nro),
        "buttons": [
            {"id": f"{prefix}:{inmueble_id}", "title": T.BTN_CONFIRM_SI},
            {"id": "VIS_MENU", "title": T.BTN_CONFIRM_NO},
        ],
    }


# -------- Publicar — pregunta operacion ------------------------------------
def pub_operacion(edificio):
    return {
        "type": "buttons",
        "text": T.PUB_OPERACION.format(edificio=edificio),
        "buttons": [
            {"id": "VIS_PUB_OP:arriendo", "title": T.BTN_OP_ARRIENDO},
            {"id": "VIS_PUB_OP:venta", "title": T.BTN_OP_VENTA},
            {"id": "VIS_MENU", "title": T.BTN_MENU},
        ],
    }


# -------- Publicar — tipo de inmueble (interactive list) -------------------
def lista_tipos_publicar(token_vuelta_menu=False):
    """Interactive list con los 5 tipos válidos. Cada row.id es
    'VIS_PUB_TIPO:<key>' donde key es lowercase y sin acento."""
    rows = []
    for key in T.TIPOS_VALIDOS:
        rows.append({
            "id": f"VIS_PUB_TIPO:{key}",
            "title": T.TIPO_LABELS.get(key, key.capitalize()),
            "description": "",
        })
    return {
        "type": "list",
        "text": T.PUB_PEDIR_TIPO,
        "button_label": T.LISTA_TIPO_BTN_LABEL,
        "sections": [{"title": T.LISTA_TIPO_SECCION_TITULO, "rows": rows}],
    }


# -------- Publicar — resumen + autorización --------------------------------
def pub_resumen(edificio, operacion, tipo, nro, m2, precio):
    precio_label = "Canon mensual" if operacion == "arriendo" else "Precio"
    body = T.PUB_RESUMEN.format(
        edificio=edificio,
        operacion="Arriendo" if operacion == "arriendo" else "Venta",
        tipo=T.tipo_label(tipo),
        nro=nro,
        m2=m2,
        precio_label=precio_label,
        precio_fmt=T.formatear_precio(precio),
        autorizacion=T.TEXTO_AUTORIZACION,
    )
    return {
        "type": "buttons",
        "text": body,
        "buttons": [
            {"id": "VIS_PUB_AUTH:si", "title": T.BTN_AUTORIZO},
            {"id": "VIS_PUB_AUTH:no", "title": T.BTN_NO_AUTORIZO},
        ],
    }


# -------- Home menu (sin QR / sin conjunto) --------------------------------
def home_menu():
    """Menú de bienvenida sin conjunto — 3 botones (al límite de WhatsApp)."""
    return {
        "type": "buttons",
        "text": T.HOME_WELCOME,
        "buttons": [
            {"id": "HOME_MIS_INMUEBLES", "title": T.BTN_HOME_MIS_INMUEBLES},
            {"id": "HOME_MIS_BUSQUEDAS", "title": T.BTN_HOME_MIS_BUSQUEDAS},
            {"id": "HOME_AGENTES", "title": T.BTN_HOME_AGENTES},
        ],
    }


def home_volver_boton(body):
    """Un único botón '🏠 Menú' con el texto `body` arriba."""
    return {
        "type": "buttons",
        "text": body,
        "buttons": [
            {"id": "HOME_MENU", "title": T.BTN_MENU},
        ],
    }


def home_agentes_intro():
    """Intro al flow de agentes con 2 botones: Quiero ser agente / Menú."""
    return {
        "type": "buttons",
        "text": T.HOME_AGENTES_INTRO,
        "buttons": [
            {"id": "HOME_AGENTES_START", "title": T.BTN_HOME_QUIERO_SER_AGENTE},
            {"id": "HOME_MENU", "title": T.BTN_MENU},
        ],
    }


# -------- Texto plano -------------------------------------------------------
def texto(body):
    return {"type": "text", "text": body}
