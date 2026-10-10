"""Avatares auto-generados para contactos del panel.

Dado un contacto (dict con posibles claves `nombre` y `telefono`), devuelve
las iniciales (dos letras) y una pareja de colores (background suave +
foreground oscuro) determinística: el mismo contacto siempre pinta igual,
y colores similares nunca colisionan con el estado del bucket.

Se usa como filtro Jinja (`{{ contacto | avatar }}` → `<div class="avatar">…</div>`)
y como helper para construir cards del Kanban desde Python.

Paleta mantenida pequeña (10 pares), elegida para contrastar bien tanto
contra el fondo claro como contra bloques coloreados del design system
(éstos usan los colores "soft" de tokens: info/success/warning/orange/
danger — nuestros pares son hermanos pero más saturados en texto).
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

# 10 pares (background suave, foreground oscuro) usados en light mode.
# El orden importa — si el hash de un contacto cae en el índice N,
# siempre caerá en el mismo índice (el array es append-only de ahora en más).
_PAIRS_LIGHT: tuple[tuple[str, str], ...] = (
    ("#fecaca", "#991b1b"),  # red
    ("#fed7aa", "#9a3412"),  # orange
    ("#fde68a", "#92400e"),  # amber
    ("#d9f99d", "#3f6212"),  # lime
    ("#bbf7d0", "#166534"),  # green
    ("#a7f3d0", "#065f46"),  # emerald
    ("#99f6e4", "#115e59"),  # teal
    ("#bae6fd", "#075985"),  # sky
    ("#c7d2fe", "#3730a3"),  # indigo
    ("#f5d0fe", "#86198f"),  # fuchsia
)

# Variante dark: fondos más oscuros (saturados hacia 7xx) y texto claro
# (hacia 2xx). Mismos índices que _PAIRS_LIGHT para que un mismo contacto
# conserve su "color" al cambiar de tema.
_PAIRS_DARK: tuple[tuple[str, str], ...] = (
    ("#7f1d1d", "#fecaca"),
    ("#7c2d12", "#fed7aa"),
    ("#78350f", "#fde68a"),
    ("#365314", "#d9f99d"),
    ("#14532d", "#bbf7d0"),
    ("#064e3b", "#a7f3d0"),
    ("#134e4a", "#99f6e4"),
    ("#0c4a6e", "#bae6fd"),
    ("#312e81", "#c7d2fe"),
    ("#701a75", "#f5d0fe"),
)


def _iniciales(nombre: str, telefono: str) -> str:
    """Dos letras en mayúscula.

    - Si hay `nombre`, usamos la primera letra de las primeras 2 palabras
      alfanuméricas (ignorando partículas de 1 carácter).
    - Si no, caemos a los últimos 2 dígitos del teléfono.
    - Si no hay ni uno ni lo otro, '?'.
    """
    if nombre:
        # Partes alfabéticas, ignorando separadores comunes.
        partes = [p for p in re.split(r"[\s\.\-_,]+", nombre.strip()) if p]
        letras: list[str] = []
        for p in partes:
            # Nos quedamos sólo con letras unicode (sirve para acentos).
            match = re.search(r"[^\W\d_]", p, flags=re.UNICODE)
            if match:
                letras.append(match.group(0))
            if len(letras) >= 2:
                break
        if letras:
            return "".join(letras[:2]).upper()
    if telefono:
        digitos = re.sub(r"\D", "", str(telefono))
        if len(digitos) >= 2:
            return digitos[-2:]
        if digitos:
            return digitos[-1:] + "·"
    return "?"


def _indice_color(semilla: str) -> int:
    """Hash determinístico → índice [0, len(_PAIRS_LIGHT))."""
    if not semilla:
        semilla = "?"
    h = hashlib.md5(semilla.encode("utf-8")).digest()
    # Primeros 4 bytes como int → mod len paleta.
    n = int.from_bytes(h[:4], "big", signed=False)
    return n % len(_PAIRS_LIGHT)


def avatar_data(contacto: Any) -> dict:
    """Devuelve ``{iniciales, color_bg, color, color_bg_dark, color_dark, idx}``.

    Acepta cualquier objeto con acceso tipo dict (incluyendo contactos cargados
    con ``_rows()`` del panel). Nunca lanza — si el contacto está vacío,
    devuelve iniciales "?" y el primer par de la paleta.
    """
    if contacto is None:
        contacto = {}
    try:
        nombre = (contacto.get("nombre") if hasattr(contacto, "get")
                  else getattr(contacto, "nombre", "")) or ""
    except Exception:
        nombre = ""
    try:
        telefono = (contacto.get("telefono") if hasattr(contacto, "get")
                    else getattr(contacto, "telefono", "")) or ""
    except Exception:
        telefono = ""
    nombre = str(nombre).strip()
    telefono = str(telefono).strip()

    iniciales = _iniciales(nombre, telefono)
    # Semilla preferida: nombre normalizado; si no hay, teléfono.
    semilla = nombre.lower() if nombre else telefono
    idx = _indice_color(semilla)
    bg, fg = _PAIRS_LIGHT[idx]
    bg_dark, fg_dark = _PAIRS_DARK[idx]
    return {
        "iniciales": iniciales,
        "color_bg": bg,
        "color": fg,
        "color_bg_dark": bg_dark,
        "color_dark": fg_dark,
        "idx": idx,
    }


def avatar_html(contacto: Any, size: str = "") -> str:
    """Render HTML del avatar — usado como filtro Jinja ``| avatar``.

    `size` admite '', 'sm' o 'lg' (añade clase .avatar-sm / .avatar-lg).
    Expone también vars CSS ``--avatar-bg-dark`` / ``--avatar-fg-dark`` para
    que ``panel.css`` pueda cambiar los colores en dark mode sin JS.
    """
    data = avatar_data(contacto)
    extra_cls = ""
    if size in ("sm", "lg"):
        extra_cls = f" avatar-{size}"
    style = (
        f"background:{data['color_bg']};color:{data['color']};"
        f"--avatar-bg-dark:{data['color_bg_dark']};"
        f"--avatar-fg-dark:{data['color_dark']};"
    )
    # Nota: usamos markup minimal. Si el contacto es None/vacio, iniciales='?'.
    from markupsafe import Markup, escape
    return Markup(
        f'<span class="avatar{extra_cls}" style="{style}" '
        f'aria-hidden="true">{escape(data["iniciales"])}</span>'
    )
