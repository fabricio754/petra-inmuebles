"""Captación (Hito 7): recibe los anuncios que manda la extensión de Chrome
"Enviar a Massi", los filtra y los deja en `contactos` para que app/envios.py
les escriba con la plantilla aprobada.

La extensión solo lee lo que el portal ya muestra después de que una persona
llenó su formulario: no se salta reCAPTCHA ni formularios."""
import re
import unicodedata

from app import db

# Ciudades que cubre Sureti.
CIUDADES = {
    "bogota": "Bogotá", "medellin": "Medellín", "barranquilla": "Barranquilla",
    "cartagena": "Cartagena", "santa marta": "Santa Marta", "cucuta": "Cúcuta",
    "chia": "Chía", "cajica": "Cajicá", "zipaquira": "Zipaquirá",
}
# Zonas de Bogotá que Sureti no cubre.
ZONAS_EXCLUIDAS = ("san cristobal", "ciudad bolivar")

TIPOS = {
    "apartamento": "apartamento", "apartaestudio": "apartamento", "casa": "casa",
    "local": "local", "oficina": "oficina", "bodega": "bodega", "lote": "lote",
}
RESIDENCIAL = {"apartamento", "casa"}
PORCENTAJE = {"residencial": 0.40, "comercial": 0.30}
# Rango de crédito de Sureti, en millones.
MONTO_MIN_M, MONTO_MAX_M = 20, 800

PORTALES = {"metrocuadrado": "Metrocuadrado", "fincaraiz": "Finca Raíz"}


def _sin_tildes(texto):
    texto = unicodedata.normalize("NFD", str(texto or "").lower())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    # En las URL las palabras van con guiones: "santa-marta", "ciudad-bolivar".
    return re.sub(r"[-_/]+", " ", texto)


def normalizar_telefono(valor):
    """'+57 300 123 4567', 'wa.me/573001234567', '3001234567' → '573001234567'."""
    digitos = re.sub(r"\D", "", str(valor or ""))
    if len(digitos) == 10 and digitos.startswith("3"):
        digitos = "57" + digitos
    if len(digitos) == 12 and digitos.startswith("573"):
        return digitos
    return ""


def _precio(valor):
    if isinstance(valor, (int, float)):
        return int(valor)
    digitos = re.sub(r"\D", "", str(valor or ""))
    return int(digitos) if digitos else 0


def _buscar(diccionario, *textos):
    juntos = " ".join(_sin_tildes(t) for t in textos)
    for clave, valor in diccionario.items():
        if clave in juntos:
            return valor
    return None


def procesar(anuncio):
    """anuncio: dict que manda la extensión. Devuelve (resultado, detalle)
    con resultado en {"nuevo", "duplicado", "descartado", "invalido"}."""
    telefono = normalizar_telefono(anuncio.get("telefono"))
    if not telefono:
        return "invalido", "No se encontró un celular colombiano válido."

    url = str(anuncio.get("url") or "")
    titulo = str(anuncio.get("titulo") or "")
    ubicacion = " ".join(str(anuncio.get(k) or "") for k in ("ciudad", "barrio", "direccion"))

    portal = _buscar(PORTALES, url) or str(anuncio.get("portal") or "")
    ciudad = _buscar(CIUDADES, ubicacion, url, titulo)
    tipo = _buscar(TIPOS, str(anuncio.get("tipo") or ""), titulo, url)
    precio = _precio(anuncio.get("precio"))
    try:
        estrato = int(re.sub(r"\D", "", str(anuncio.get("estrato") or "")) or 0) or None
    except ValueError:
        estrato = None

    if not ciudad:
        return "descartado", "Ciudad no cubierta por Sureti (o no se pudo leer)."
    if ciudad == "Bogotá" and any(z in _sin_tildes(ubicacion + " " + url) for z in ZONAS_EXCLUIDAS):
        return "descartado", "Zona de Bogotá no cubierta (San Cristóbal / Ciudad Bolívar)."
    if not tipo:
        return "descartado", "Tipo de inmueble no aceptado (o no se pudo leer)."
    if estrato == 1:
        return "descartado", "Estrato 1 no aplica."
    if precio < 50_000_000:
        return "descartado", "No se pudo leer el precio (o es menor a $50M)."

    categoria = "residencial" if tipo in RESIDENCIAL else "comercial"
    monto_hasta = min(int(precio * PORCENTAJE[categoria] / 1_000_000), MONTO_MAX_M)
    if monto_hasta < MONTO_MIN_M:
        return "descartado", f"El monto daría menos de ${MONTO_MIN_M}M (mínimo de Sureti)."

    contacto = {
        "telefono": telefono,
        "nombre": (str(anuncio.get("nombre") or "").strip() or None),
        "direccion": (str(anuncio.get("direccion") or "").strip() or None),
        "barrio": (str(anuncio.get("barrio") or "").strip() or None),
        "ciudad": ciudad,
        "tipo": tipo,
        "precio": precio,
        "estrato": estrato,
        "requiere_ph": estrato in (2, 3),
        "url": url or None,
        "portal": portal or None,
        "foto": (str(anuncio.get("foto") or "").strip() or None),
        "monto_hasta": monto_hasta,
    }
    if not db.guardar_contacto(contacto):
        return "duplicado", "Ese teléfono ya estaba en la lista (no se le vuelve a escribir)."
    return "nuevo", f"{tipo} en {ciudad}, hasta ${monto_hasta}M. Queda en cola de envío."
