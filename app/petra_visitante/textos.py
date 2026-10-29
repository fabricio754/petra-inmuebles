"""Textos centralizados del flujo VISITANTE.

Deliberadamente planos (dict + funciones) para que, cuando haga falta,
sea trivial moverlos a `petra_textos` (DB) sin reescribir el router.
Mientras tanto se editan acá y se despliega.

Convención: nombres en snake_case, el valor es el texto final o una
f-string con placeholders `{var}` que resuelve `.format(**vars)`.
"""

# -------- Bienvenida / menu principal ---------------------------------------
BIENVENIDA = (
    "¡Hola! 👋 Soy Petra ✨\n"
    "Tu asistente inmobiliaria de {edificio}.\n\n"
    "Estoy aquí para ayudarte con todo lo relacionado con arriendo, "
    "compra y publicación de inmuebles 🏠"
)

MENU_PROMPT = "¿Qué querés hacer?"

# Botón labels (max 20 chars cada uno — límite WhatsApp)
BTN_ARRIENDO = "🏠 Ver arriendos"
BTN_COMPRAR = "🏡 Ver ventas"
BTN_PUBLICAR = "📋 Publicar inmueble"

BTN_VOLVER = "⬅️ Volver"
BTN_MENU = "🏠 Menú"
BTN_CONTACTAR = "Contactar"

BTN_CONFIRM_SI = "Sí, contactar"
BTN_CONFIRM_NO = "No"

BTN_OP_ARRIENDO = "Arriendo"
BTN_OP_VENTA = "Venta"

BTN_AUTORIZO = "Publicar"
BTN_NO_AUTORIZO = "Cancelar"


# -------- Tipos de inmueble (nuevo flujo de publicar) -----------------------
# Keys lowercase sin acentos — se usan para persistir en DB y para construir
# los `list_id` de la interactive list. Los labels sí llevan acentos.
TIPOS_VALIDOS = ["apto", "casa", "parqueadero", "habitacion", "deposito"]

TIPO_LABELS = {
    "apto": "Apartamento",
    "casa": "Casa",
    "parqueadero": "Parqueadero",
    "habitacion": "Habitación",
    "deposito": "Depósito",
}


def tipo_label(tipo):
    """Devuelve el label legible para un tipo. Para el valor legacy
    'apartamento' y el canon 'apto' usa 'Apartamento'. Si no está en el
    mapa, devuelve el tipo capitalizado como fallback."""
    if not tipo:
        return "Apartamento"
    t = tipo.strip().lower()
    if t == "apartamento":
        return "Apartamento"
    return TIPO_LABELS.get(t, t.capitalize())


# -------- Arriendo / Venta — listados --------------------------------------
LISTA_ARRIENDO_HEADER = (
    "🏠 *Inmuebles en arriendo* en {edificio}\n\n"
    "Elegí uno para ver el detalle 👇"
)
LISTA_VENTA_HEADER = (
    "🏡 *Inmuebles en venta* en {edificio}\n\n"
    "Elegí uno para ver el detalle 👇"
)
LISTA_VACIA_ARRIENDO = (
    "😕 Por ahora no hay inmuebles en *arriendo* publicados en {edificio}.\n\n"
    "Si querés, podés publicar el tuyo 👇"
)
LISTA_VACIA_VENTA = (
    "😕 Por ahora no hay inmuebles en *venta* publicados en {edificio}.\n\n"
    "Si querés, podés publicar el tuyo 👇"
)

LISTA_BTN_LABEL = "Ver inmuebles"
LISTA_SECCION_TITULO = "Disponibles"

# Row description template para la lista (max 72 chars). Se corta
# automáticamente en `petra_wa.send_list`. Mantiene compat para seed data
# que trae hab/baños; la render real usa `format_row_desc`.
ROW_DESC = "{hab} hab · {banos} baños · {m2} m² · {precio_fmt}"


def format_row_desc(hab, banos, m2, precio_fmt):
    """Arma la descripción de un row en la lista de inmuebles con
    tolerancia a NULL. Si hab/baños son None (visitor-published), se
    omiten; si m2 también falta, aparece '-'."""
    partes = []
    if hab is not None:
        partes.append(f"{hab} hab")
    if banos is not None:
        partes.append(f"{banos} baños")
    if m2 is not None:
        partes.append(f"{m2} m²")
    else:
        partes.append("- m²")
    partes.append(precio_fmt)
    return " · ".join(partes)


def row_title(tipo, nro):
    """Título del row: '<Label> <nro>'. Para apartamentos (apto o legacy
    'apartamento') se usa 'Apto {nro}' porque es más corto."""
    t = (tipo or "").strip().lower()
    if t in ("apto", "apartamento", ""):
        return f"Apto {nro}"
    return f"{TIPO_LABELS.get(t, t.capitalize())} {nro}"


# -------- Detalle de inmueble -----------------------------------------------
DETALLE_ARRIENDO = (
    "🏠 *{titulo}* · {edificio}\n\n"
    "{atributos}"
    "• Canon mensual: *{precio_fmt}*\n\n"
    "_{gestor}_"
)
DETALLE_VENTA = (
    "🏡 *{titulo}* · {edificio}\n\n"
    "{atributos}"
    "• Precio: *{precio_fmt}*\n\n"
    "_{gestor}_"
)


def detalle_atributos(hab, banos, m2):
    """Bloque de bullets de atributos para el detalle. Omite los que son
    None para que inmuebles publicados por propietario (sin hab/baños)
    no muestren 'None'."""
    partes = []
    if hab is not None:
        partes.append(f"• Habitaciones: *{hab}*\n")
    if banos is not None:
        partes.append(f"• Baños: *{banos}*\n")
    if m2 is not None:
        partes.append(f"• Área: *{m2} m²*\n")
    return "".join(partes)


GESTOR_PROPIETARIO = "Publicado por el propietario"
GESTOR_PETRA = "Gestiona Petra"

# -------- Contactar ---------------------------------------------------------
CONTACTAR_PROMPT = (
    "¿Querés que lo contactemos para hablar del Apto {nro}?"
)
CONTACTAR_OK = (
    "✅ Listo. Ya le avisamos al propietario del *Apto {nro}* de tu interés.\n\n"
    "Te vamos a conectar lo antes posible 💜"
)
CONTACTAR_CANCEL = (
    "👍 Sin problema. Si cambiás de idea, volvé al menú y elegilo de nuevo."
)

# -------- Publicar ----------------------------------------------------------
PUB_OPERACION = (
    "📋 Vamos a publicar tu inmueble en *{edificio}*.\n\n"
    "¿Es para *arriendo* o para *venta*?"
)

# Nuevo paso: tipo de inmueble (interactive list).
PUB_PEDIR_TIPO = (
    "¡Perfecto! 🙌\n\n"
    "¿Qué *tipo de inmueble* es?"
)
LISTA_TIPO_BTN_LABEL = "Ver tipos"
LISTA_TIPO_SECCION_TITULO = "Tipo de inmueble"

# Ack del tipo elegido + pide identificador en el mismo mensaje
# (siguiendo el patrón PUB_BANOS/PUB_AREA del flujo anterior).
PUB_PEDIR_ID = (
    "✅ {tipo}.\n\n"
    "*¿Cuál es el # de torre, bloque, apto o casa?*\n"
    "_(ej: Torre 3 Apto 702, o Casa 15)_"
)

# Ack del identificador + pide área.
PUB_PEDIR_AREA = (
    "✅ {nro}.\n\n"
    "*¿Qué área aproximada tiene en m²?*\n"
    "_(solo el número, 1 a 10000)_"
)

PUB_PRECIO_ARRIENDO = (
    "✅ {m2} m².\n\n"
    "*¿Cuál es el canon mensual?* _(en pesos, solo números)_\n"
    "Ej: 1800000"
)
PUB_PRECIO_VENTA = (
    "✅ {m2} m².\n\n"
    "*¿Cuál es el precio de venta?* _(en pesos, solo números)_\n"
    "Ej: 285000000"
)
PUB_ERR_NUM = "⚠️ Necesito solo el *número*. Probá de nuevo."
PUB_ERR_TIPO = "⚠️ Elegí un tipo de la lista."
PUB_ERR_ID = (
    "⚠️ Decime algo como 'Torre 3 Apto 702' o 'Casa 15'."
)
PUB_ERR_AREA = "⚠️ Área entre *1 y 10000* m², solo el número."
PUB_ERR_PRECIO = "⚠️ El precio debe ser un número mayor que cero."

PUB_RESUMEN = (
    "📝 *Revisá los datos de tu inmueble:*\n\n"
    "• Edificio: *{edificio}*\n"
    "• Operación: *{operacion}*\n"
    "• Tipo: *{tipo}*\n"
    "• Identificación: *{nro}*\n"
    "• Área: *{m2} m²*\n"
    "• {precio_label}: *{precio_fmt}*\n\n"
    "{autorizacion}"
)

TEXTO_AUTORIZACION = (
    "Al publicar autorizás a Petra Plataforma Inmobiliaria a compartir "
    "tu información de contacto con las personas interesadas en este "
    "inmueble, exclusivamente para facilitar el contacto relacionado "
    "con su arriendo o venta."
)

PUB_NO_AUTORIZO = (
    "👍 Entendido. No publicamos el inmueble.\n\n"
    "Cuando quieras, volvé a *Publicar mi inmueble* desde el menú."
)
PUB_PUBLICADO = (
    "✅ Tu inmueble quedó publicado: *{titulo}* · {edificio} · "
    "{m2} m² · {precio_fmt} · {operacion}.\n\n"
    "Ya está visible para quienes busquen en tu conjunto 💜"
)

# -------- Mensajes genéricos ------------------------------------------------
FALLBACK = (
    "No te entendí bien 🤔\n\n"
    "Escribí *menú* para volver al inicio."
)
SIN_CONJUNTO = (
    "¡Hola! 👋 Soy *Petra* ✨\n\n"
    "Para ayudarte necesito que escanees el QR del edificio o mandes el "
    "código del conjunto. 🙏"
)
CODIGO_INVALIDO = (
    "😕 No reconozco el código del conjunto. Revisá el QR y volvé a "
    "escanearlo, por favor."
)


# -------- Home menu (sin QR) -----------------------------------------------
# Entrypoint cuando alguien escribe "hola" sin haber escaneado un QR.
HOME_WELCOME = (
    "¡Hola! 👋 Soy *Petra* ✨\n"
    "Tu plataforma inmobiliaria.\n\n"
    "¿En qué te ayudo?"
)
BTN_HOME_MIS_INMUEBLES = "🏘️ Mis inmuebles"
BTN_HOME_MIS_BUSQUEDAS = "🔍 Mis búsquedas"
BTN_HOME_AGENTES = "🤝 Agentes Petra"

HOME_VOLVER_PROMPT = "¿Querés volver al inicio?"

HOME_MIS_INMUEBLES_VACIO = (
    "No tenés inmuebles publicados todavía. Para publicar uno, "
    "escaneá el QR del edificio y seguí el flow 📱"
)
HOME_MIS_INMUEBLES_HEADER = "🏘️ *Tus inmuebles publicados* ({n})\n"

HOME_MIS_BUSQUEDAS_VACIO = (
    "Todavía no has contactado ningún inmueble. "
    "Buscá en un edificio escaneando su QR 📱"
)
HOME_MIS_BUSQUEDAS_HEADER = "🔍 *Tus búsquedas* ({n})\n"

HOME_AGENTES_FALLBACK = (
    "Para ser agente Petra, escribinos a este número de nuevo y "
    "te guiamos 💜"
)

HOME_AGENTES_INTRO = (
    "🤝 *Agentes Petra*\n\n"
    "Ser agente Petra es gestionar los inmuebles de hasta *5 conjuntos* "
    "de tu ciudad. Nosotros te damos la plataforma, los contactos y la "
    "capacitación 💜\n\n"
    "¿Querés arrancar?"
)
BTN_HOME_QUIERO_SER_AGENTE = "🤝 Quiero ser agente"


# -------- Helpers de formato ------------------------------------------------
def formatear_precio(valor):
    """Formatea un entero como pesos COP con separador de miles '.'.
    Ej: 1950000 -> '$ 1.950.000'. Si valor es None, devuelve 'Sin precio'.
    """
    try:
        n = int(valor)
    except (TypeError, ValueError):
        return "Sin precio"
    if n <= 0:
        return "Sin precio"
    # Formato con punto como separador de miles.
    return "$ " + f"{n:,}".replace(",", ".")


def render(clave_o_tpl, **variables):
    """Renderiza un template `.format(**variables)`. Si `clave_o_tpl` es
    un string literal, lo usa tal cual; si es una clave reconocida en
    este módulo, la resuelve. Mantiene compat futura con DB."""
    if clave_o_tpl is None:
        return ""
    tpl = globals().get(clave_o_tpl, clave_o_tpl)
    try:
        return tpl.format(**variables)
    except Exception:
        return tpl
