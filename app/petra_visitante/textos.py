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
BTN_ARRIENDO = "🏠 Tomar en arriendo"
BTN_COMPRAR = "🏡 Comprar"
BTN_PUBLICAR = "📋 Publicar inmueble"

BTN_VOLVER = "⬅️ Volver"
BTN_MENU = "🏠 Menú"
BTN_CONTACTAR = "Contactar"

BTN_CONFIRM_SI = "Sí, contactar"
BTN_CONFIRM_NO = "No"

BTN_OP_ARRIENDO = "Arriendo"
BTN_OP_VENTA = "Venta"

BTN_AUTORIZO = "Autorizo"
BTN_NO_AUTORIZO = "No autorizo"


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
# automáticamente en `petra_wa.send_list`.
ROW_DESC = "{hab} hab · {banos} baños · {m2} m² · {precio_fmt}"

# -------- Detalle de inmueble -----------------------------------------------
DETALLE_ARRIENDO = (
    "🏠 *Apto {nro}* · {edificio}\n\n"
    "• Habitaciones: *{hab}*\n"
    "• Baños: *{banos}*\n"
    "• Área: *{m2} m²*\n"
    "• Canon mensual: *{precio_fmt}*\n\n"
    "_{gestor}_"
)
DETALLE_VENTA = (
    "🏡 *Apto {nro}* · {edificio}\n\n"
    "• Habitaciones: *{hab}*\n"
    "• Baños: *{banos}*\n"
    "• Área: *{m2} m²*\n"
    "• Precio: *{precio_fmt}*\n\n"
    "_{gestor}_"
)
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
PUB_HAB = (
    "¡Perfecto! 🙌\n\n"
    "*¿Cuántas habitaciones* tiene el inmueble?\n"
    "_(solo el número, 1 a 10)_"
)
PUB_BANOS = (
    "✅ {hab} habitaciones.\n\n"
    "*¿Cuántos baños*?\n"
    "_(solo el número, 1 a 10)_"
)
PUB_AREA = (
    "✅ {banos} baños.\n\n"
    "*¿Qué área tiene en m²?*\n"
    "_(solo el número, 10 a 10000)_"
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
PUB_ERR_HAB = "⚠️ Habitaciones entre *1 y 10*, solo el número."
PUB_ERR_BANOS = "⚠️ Baños entre *1 y 10*, solo el número."
PUB_ERR_AREA = "⚠️ Área entre *10 y 10000* m², solo el número."
PUB_ERR_PRECIO = "⚠️ El precio debe ser un número mayor que cero."

PUB_RESUMEN = (
    "📝 *Revisá los datos de tu inmueble:*\n\n"
    "• Edificio: *{edificio}*\n"
    "• Operación: *{operacion}*\n"
    "• Habitaciones: *{hab}*\n"
    "• Baños: *{banos}*\n"
    "• Área: *{m2} m²*\n"
    "• {precio_label}: *{precio_fmt}*\n\n"
    "{autorizacion}\n\n"
    "¿Autorizás que publiquemos y compartamos tu contacto con interesados?"
)

TEXTO_AUTORIZACION = (
    "Autorizo a Petra Inmuebles a compartir mi información de contacto "
    "con las personas interesadas en este inmueble, exclusivamente para "
    "facilitar el contacto relacionado con su arriendo o venta."
)

PUB_NO_AUTORIZO = (
    "👍 Entendido. No publicamos el inmueble.\n\n"
    "Cuando quieras, volvé a *Publicar mi inmueble* desde el menú."
)
PUB_PUBLICADO = (
    "✅ Tu inmueble quedó publicado: *Apto {nro}* · {edificio} · "
    "{hab} hab · {banos} baños · {m2} m² · {precio_fmt} · {operacion}.\n\n"
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
