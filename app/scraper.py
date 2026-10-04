"""Scraper de portales inmobiliarios: PropDirecto, Metrocuadrado, Finca Raíz, Ciencuadras.

Usa Playwright (headless Chromium) + 2captcha para revelar teléfonos ocultos.
Aplica los mismos filtros de ciudad/tipo/precio que captacion.py y guarda
en la tabla `contactos` via db.guardar_contacto().

Requisitos de entorno:
  TWOCAPTCHA_API_KEY  — clave de 2captcha.com (requerida)
  SCRAPER_ENABLED     — "true" para activar el scheduler (default: false)
  DATABASE_URL        — requerida (la comparte con el resto del bot)
  PLAYWRIGHT_CHROMIUM_PATH — ruta al binario (opcional; útil en Render)

En Render, agregar al build command:
  pip install -r requirements.txt && playwright install chromium
"""
import json
import logging
import os
import random
import re
import time
from typing import Optional

import pytz

from apscheduler.schedulers.background import BackgroundScheduler
from playwright.sync_api import Browser, Page, TimeoutError as PWTimeout, sync_playwright

from app import db
from app.captacion import (
    CIUDADES,
    MONTO_MAX_M,
    MONTO_MIN_M,
    PORCENTAJE,
    RESIDENCIAL,
    TIPOS,
    ZONAS_EXCLUIDAS,
    _buscar,
    _precio,
    _sin_tildes,
    normalizar_telefono,
)

log = logging.getLogger("petra")

TWOCAPTCHA_KEY = os.environ.get("TWOCAPTCHA_API_KEY", "")
SCRAPER_ENABLED = os.environ.get("SCRAPER_ENABLED", "false").lower() == "true"
PD_EMAIL = os.environ.get("PD_EMAIL", "")
PD_PASSWORD = os.environ.get("PD_PASSWORD", "")
MAX_POR_PORTAL = 300
PAUSA_MIN, PAUSA_MAX = 3, 5

_scheduler: Optional[BackgroundScheduler] = None

# ── Utilidades ────────────────────────────────────────────────────────────────


def _pausa():
    time.sleep(random.uniform(PAUSA_MIN, PAUSA_MAX))


def _texto(page: Page, selector: str) -> str:
    el = page.query_selector(selector)
    return el.inner_text().strip() if el else ""


def _nueva_pagina(browser: Browser, url: str) -> Page:
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1366, "height": 768},
        locale="es-CO",
    )
    # Inject before React loads so our override is called by the app's own handlers
    ctx.add_init_script("""
        window.__massiBotUris = [];

        // 1. window.open
        const _origOpen = window.open;
        window.open = function(url, ...a) {
            if (url) window.__massiBotUris.push(String(url));
            try { return _origOpen && _origOpen.apply(this, [url, ...a]); } catch(e){}
        };

        // 2. window.location.href setter
        try {
            const desc = Object.getOwnPropertyDescriptor(window.location, 'href')
                      || Object.getOwnPropertyDescriptor(Location.prototype, 'href');
            const _set = desc && desc.set;
            Object.defineProperty(window.location, 'href', {
                set: function(v) {
                    if (v) window.__massiBotUris.push(String(v));
                    if (_set) _set.call(window.location, v);
                },
                get: desc && desc.get,
                configurable: true,
            });
        } catch(e) {}

        // 3. location.assign / location.replace
        try {
            const _assign = window.location.assign.bind(window.location);
            window.location.assign = function(url) {
                if (url) window.__massiBotUris.push(String(url));
                try { return _assign(url); } catch(e){}
            };
        } catch(e) {}
        try {
            const _replace = window.location.replace.bind(window.location);
            window.location.replace = function(url) {
                if (url) window.__massiBotUris.push(String(url));
                try { return _replace(url); } catch(e){}
            };
        } catch(e) {}

        // 4. Programmatic anchor clicks (create el + set href + click)
        try {
            const _origAnchorClick = HTMLAnchorElement.prototype.click;
            HTMLAnchorElement.prototype.click = function() {
                const h = (this.getAttribute && this.getAttribute('href')) || this.href || '';
                if (h) window.__massiBotUris.push(String(h));
                return _origAnchorClick.apply(this, arguments);
            };
        } catch(e) {}

        // 5. Also watch setAttribute on anchors for dynamic href setting
        try {
            const _origSetAttr = Element.prototype.setAttribute;
            Element.prototype.setAttribute = function(name, value) {
                if (name === 'href' && this.tagName === 'A' && value) {
                    window.__massiBotUris.push(String(value));
                }
                return _origSetAttr.apply(this, arguments);
            };
        } catch(e) {}
    """)
    page = ctx.new_page()
    # Block images/fonts to reduce memory during page load (~50% less RAM)
    page.route(
        "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,eot}",
        lambda route: route.abort(),
    )
    page.goto(url, wait_until="load", timeout=45_000)
    page.wait_for_timeout(6_000)  # wait for React hydration before any DOM queries
    return page


def _resolver_captcha(page: Page) -> bool:
    """Detecta reCAPTCHA v2 o hCAPTCHA y lo resuelve con 2captcha.
    Retorna True si se envió la solución al DOM."""
    if not TWOCAPTCHA_KEY:
        return False
    try:
        from twocaptcha import TwoCaptcha  # import tardío: no siempre instalado

        solver = TwoCaptcha(TWOCAPTCHA_KEY)

        rc_el = page.query_selector(".g-recaptcha[data-sitekey], iframe[src*='recaptcha'][src*='sitekey']")
        hc_el = page.query_selector(".h-captcha[data-sitekey]")

        if rc_el:
            sitekey = rc_el.get_attribute("data-sitekey") or _extraer_sitekey_iframe(page)
            if not sitekey:
                return False
            result = solver.recaptcha(sitekey=sitekey, url=page.url)
            page.evaluate(
                "token => { const el = document.getElementById('g-recaptcha-response'); if (el) el.innerHTML = token; }",
                result["code"],
            )
            log.debug("[Scraper] reCAPTCHA resuelto.")
            return True

        if hc_el:
            sitekey = hc_el.get_attribute("data-sitekey")
            result = solver.hcaptcha(sitekey=sitekey, url=page.url)
            page.evaluate(
                "token => { const el = document.getElementById('h-captcha-response'); if (el) el.innerHTML = token; }",
                result["code"],
            )
            log.debug("[Scraper] hCAPTCHA resuelto.")
            return True
    except Exception as exc:
        log.warning("[Scraper] Error resolviendo captcha: %s", exc)
    return False


def _extraer_sitekey_iframe(page: Page) -> Optional[str]:
    """Extrae el sitekey de la URL de un iframe de reCAPTCHA."""
    iframe = page.query_selector("iframe[src*='recaptcha']")
    if not iframe:
        return None
    src = iframe.get_attribute("src") or ""
    m = re.search(r"[?&]k=([A-Za-z0-9_-]+)", src)
    return m.group(1) if m else None


def _monto_hasta(precio: int, tipo: str) -> Optional[int]:
    categoria = "residencial" if tipo in RESIDENCIAL else "comercial"
    monto = min(int(precio * PORCENTAJE[categoria] / 1_000_000), MONTO_MAX_M)
    return monto if monto >= MONTO_MIN_M else None


# Palabras que delatan a un anunciante profesional / inmobiliaria
_BROKER_KEYWORDS = {
    "inmobiliaria", "realty", "propiedades", "bienes raices", "bienes raíces",
    "constructora", "constructor", "finca raiz", "finca raíz", "inversiones",
    "soluciones inmobiliarias", "asesor inmobiliario", "agente inmobiliario",
    "ltda", "s.a.s", "s.a.", " corp", "grupo inmobiliario",
}
# Si el anunciante tiene más de este número de publicaciones activas, es broker
_MAX_PUBLICACIONES_PROPIETARIO = 4


def _anunciante_info(page: Page) -> dict:
    """Extrae nombre del anunciante y conteo de publicaciones de la página de detalle.
    Devuelve {'nombre': str, 'num_publicaciones': int}."""
    nombre = ""
    num_pub = 0

    # Selectores comunes para el nombre del anunciante en portales colombianos
    for sel in [
        "[class*='advertiser'] [class*='name']",
        "[class*='anunciante'] [class*='nombre']",
        "[class*='publisher'] [class*='name']",
        "[class*='agent-name']", "[class*='agent_name']",
        "[class*='contact-name']", "[class*='contacto'] [class*='nombre']",
        "[data-testid='advertiser-name']", "[data-testid='agent-name']",
    ]:
        t = _texto(page, sel)
        if t:
            nombre = t
            break

    # Selectores para conteo de publicaciones del anunciante
    for sel in [
        "[class*='advertiser'] [class*='count']",
        "[class*='advertiser'] [class*='listings']",
        "[class*='anunciante'] [class*='publicaciones']",
        "[class*='inmuebles-activos']", "[class*='active-listings']",
        "[data-testid='listing-count']",
    ]:
        t = _texto(page, sel)
        if t:
            digits = re.sub(r"\D", "", t)
            if digits:
                num_pub = int(digits)
                break

    # Fallback: buscar texto como "12 inmuebles" o "5 publicaciones" en el bloque del anunciante
    if num_pub == 0:
        for sel in [
            "[class*='advertiser']", "[class*='anunciante']",
            "[class*='publisher']", "[class*='agent']",
        ]:
            bloque = _texto(page, sel)
            if bloque:
                m = re.search(r"(\d+)\s*(?:inmuebles?|publicaciones?|propiedades?|listings?)", bloque, re.I)
                if m:
                    num_pub = int(m.group(1))
                    break

    return {"nombre": nombre, "num_publicaciones": num_pub}


def _es_broker(anunciante: dict) -> bool:
    """Retorna True si el anunciante parece ser un broker o inmobiliaria."""
    nombre_lower = _sin_tildes((anunciante.get("nombre") or "").lower())
    if any(kw in nombre_lower for kw in _BROKER_KEYWORDS):
        return True
    if anunciante.get("num_publicaciones", 0) > _MAX_PUBLICACIONES_PROPIETARIO:
        return True
    return False


def _guardar(portal: str, telefono_raw: str, **campos) -> bool:
    """Aplica filtros de calidad y guarda en contactos. Retorna True si se insertó."""
    telefono = normalizar_telefono(telefono_raw)
    if not telefono:
        log.info("[Guardar] tel inválido: %r", telefono_raw)
        return False

    anunciante = campos.get("anunciante") or {}
    if _es_broker(anunciante):
        log.info("[Guardar] broker: %s (pub=%d)", anunciante.get("nombre"), anunciante.get("num_publicaciones", 0))
        return False

    url = campos.get("url", "")
    ciudad_raw = campos.get("ciudad_raw", "")
    tipo_raw = campos.get("tipo_raw", "")
    direccion = campos.get("direccion") or ""
    barrio = campos.get("barrio") or ""
    nombre = campos.get("nombre") or ""

    ciudad = _buscar(CIUDADES, ciudad_raw, url, direccion, barrio, nombre)
    if not ciudad:
        log.info("[Guardar] ciudad no encontrada: ciudad_raw=%r url=%r", ciudad_raw, url[:80])
        return False

    if ciudad == "Bogotá" and any(
        z in _sin_tildes(f"{direccion} {barrio} {url}") for z in ZONAS_EXCLUIDAS
    ):
        log.info("[Guardar] zona excluida Bogotá: dir=%r barrio=%r", direccion, barrio)
        return False

    tipo = _buscar(TIPOS, tipo_raw, url)
    if not tipo:
        log.info("[Guardar] tipo no encontrado: tipo_raw=%r url=%r", tipo_raw, url[:80])
        return False

    precio = _precio(campos.get("precio_raw", 0))
    if precio < 50_000_000:
        log.info("[Guardar] precio bajo: %s → %d", campos.get("precio_raw"), precio)
        return False

    try:
        estrato = int(re.sub(r"\D", "", str(campos.get("estrato_raw") or "")) or 0) or None
    except ValueError:
        estrato = None
    if estrato == 1:
        return False

    monto_hasta = _monto_hasta(precio, tipo)
    if monto_hasta is None:
        return False

    return db.guardar_contacto(
        {
            "telefono": telefono,
            "nombre": campos.get("nombre"),
            "direccion": direccion or None,
            "barrio": barrio or None,
            "ciudad": ciudad,
            "tipo": tipo,
            "precio": precio,
            "estrato": estrato,
            "requiere_ph": estrato in (2, 3) if estrato else False,
            "url": url or None,
            "portal": portal,
            "foto": campos.get("foto"),
            "monto_hasta": monto_hasta,
        }
    )


def _links_de_pagina(page: Page, selector: str, host: str) -> list[str]:
    """Extrae hrefs únicos de elementos que coinciden con selector."""
    seen: set[str] = set()
    result = []
    for el in page.query_selector_all(selector):
        href = el.get_attribute("href") or ""
        if not href:
            continue
        if not href.startswith("http"):
            href = host.rstrip("/") + "/" + href.lstrip("/")
        if href not in seen:
            seen.add(href)
            result.append(href)
    return result


def _extraer_tel_comun(page: Page) -> Optional[str]:
    """Intenta revelar y extraer el teléfono usando patrones comunes a los tres portales."""

    # ── Interceptores pre-click ───────────────────────────────────────────────
    # A. Red: capturar teléfonos de respuestas XHR/fetch (ej. API de contacto)
    phones_xhr: list = []

    def _on_xhr_response(response):
        try:
            if response.status not in (200, 201, 202):
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct and "text" not in ct:
                return
            body = response.text()
            blob = re.sub(r"[\s\-]", "", body)
            for m in re.finditer(r"(?:57)?3\d{9}", blob):
                d = m.group(0)
                phones_xhr.append(d if d.startswith("57") else "57" + d)
        except Exception:
            pass

    page.on("response", _on_xhr_response)

    # ── 0. __NEXT_DATA__ / JSON-LD — SSR sin interacción ─────────────────────
    try:
        next_json = page.evaluate("() => JSON.stringify(window.__NEXT_DATA__ || null)")
        if next_json and next_json != "null":
            # Primero buscar bajo claves semánticas de teléfono
            for km in re.finditer(
                r'"(?:phone|telefono|celular|movil|mobile|whatsapp|contactPhone)[^"]*"\s*:\s*"([^"]{8,16})"',
                next_json, re.IGNORECASE,
            ):
                candidate = re.sub(r"\D", "", km.group(1))
                if len(candidate) == 10 and candidate.startswith("3"):
                    return "57" + candidate
                if len(candidate) == 12 and candidate.startswith("573"):
                    return candidate
            # Fallback: primer número colombiano en el blob
            blob = re.sub(r"[\s\-]", "", next_json)
            for m in re.finditer(r"(?:57)?3\d{9}", blob):
                digits = m.group(0)
                return digits if digits.startswith("57") else "57" + digits
    except Exception:
        pass

    try:
        for script in page.query_selector_all("script[type='application/ld+json']"):
            content = script.inner_text()
            m = re.search(r'"telephone"\s*:\s*"([^"]+)"', content)
            if m:
                digits = re.sub(r"\D", "", m.group(1))
                if 10 <= len(digits) <= 13:
                    return digits
    except Exception:
        pass

    # ── 0b. Log all buttons for diagnosis ───────────────────────────────────
    try:
        all_btns = page.query_selector_all("button, a[role='button']")
        btn_texts = [b.inner_text()[:50].strip() for b in all_btns[:20]]
        log.info("[Scraper] Botones en página: %s", btn_texts)
    except Exception:
        pass

    # ── 1. Click en botón de "Ver teléfono" ──────────────────────────────────
    btn = page.query_selector(
        "button:has-text('Ver teléfono'), "
        "button:has-text('Mostrar teléfono'), "
        "button:has-text('Llamar'), "
        "a:has-text('Ver teléfono'), "
        "a:has-text('Llamar')"
    )
    if btn:
        try:
            btn.click()
            page.wait_for_timeout(2_000)
        except Exception:
            pass

    # ── 2. Resolver captcha si apareció ──────────────────────────────────────
    if page.query_selector("iframe[src*='recaptcha'], .g-recaptcha, .h-captcha"):
        _resolver_captcha(page)
        page.wait_for_timeout(3_000)

    # ── 2b. Clic en botón Contactar / WhatsApp (Metrocuadrado) ───────────────
    wa_btn = page.query_selector(
        "button:has-text('Contactar'), "
        "a:has-text('Contactar'), "
        "button:has-text('WhatsApp'), "
        "a:has-text('WhatsApp'), "
        "button[aria-label*='WhatsApp'], "
        "a[aria-label*='WhatsApp'], "
        "[class*='whatsapp' i] button, "
        "[class*='contact' i] button, "
        "button[class*='whatsapp' i], "
        "button[class*='contact' i]"
    )
    log.info("[Scraper] Botón Contactar encontrado: %s", wa_btn is not None)
    if wa_btn:
        try:
            wa_btn.click()
            page.wait_for_timeout(3_000)  # tiempo extra para modal/XHR
        except Exception:
            pass

    # ── 2c. Revisar captura de JS (window.open / location.href) ──────────────
    try:
        uris = page.evaluate("() => window.__massiBotUris || []")
        log.info("[Scraper] URIs capturadas por window.open: %s", uris)
        for uri in uris:
            # whatsapp://send?phone=573001234567
            # intent://send?phone=57300...
            # wa.me/573001234567
            m = re.search(r"(?:phone[=%/]|wa\.me/)\+?(\d{10,15})", uri)
            if m:
                digits = m.group(1)
                return digits if digits.startswith("57") else "57" + digits
    except Exception:
        pass

    # ── 2d. Revisar captura de red (XHR) ─────────────────────────────────────
    if phones_xhr:
        from collections import Counter
        log.info("[Scraper] Teléfonos capturados por XHR: %s", phones_xhr)
        top, _ = Counter(phones_xhr).most_common(1)[0]
        return top

    # ── 3. Buscar enlaces wa.me / api.whatsapp.com (antes que tel: para preferir móvil) ─
    for wa_sel in ("a[href*='wa.me/']", "a[href*='whatsapp.com']"):
        wa_link = page.query_selector(wa_sel)
        if wa_link:
            href = wa_link.get_attribute("href") or ""
            m = re.search(r"(?:wa\.me/|phone=)\+?(\d{10,15})", href)
            if m:
                return m.group(1)

    # ── 3b. Buscar todos los enlaces tel: y priorizar móvil colombiano ─────────
    tel_links = page.query_selector_all("a[href^='tel:']")
    tel_numbers = [re.sub(r"\D", "", el.get_attribute("href") or "") for el in tel_links]
    log.info("[Scraper] tel: links encontrados: %s", tel_numbers)
    for num in tel_numbers:
        if re.match(r"^(57)?3\d{9}$", num):
            return num if num.startswith("57") else "57" + num
    # Don't return landline as fallback — let step 6 search full HTML

    # ── 4. Buscar contenedor con número por clase CSS ─────────────────────────
    for sel in (
        "[class*='phone-number']", "[class*='phoneNumber']",
        "[class*='phone_number']", "[class*='telefono']",
        "[class*='celular']", "[class*='tel-']",
        "[data-testid*='phone']", "[data-testid*='tel']",
    ):
        el = page.query_selector(sel)
        if el:
            digits = re.sub(r"\D", "", el.inner_text())
            if 10 <= len(digits) <= 13:
                return digits

    # ── 5. Buscar patrón colombiano en texto visible ──────────────────────────
    try:
        body_text = page.locator("body").inner_text(timeout=3_000)
        hits = re.findall(
            r"(?:57)?3\d{9}",
            re.sub(r"[\s\-]", "", body_text),
        )
        if hits:
            from collections import Counter
            cnt = Counter(hits)
            # Prefer the least frequent number: platform numbers repeat in
            # nav/footer, while owner numbers appear once per listing.
            least = min(cnt, key=lambda k: (cnt[k], k))
            return least if least.startswith("57") else "57" + least
    except Exception:
        pass

    # ── 6. Buscar en HTML completo (data-*, atributos ocultos, scripts inline) ─
    try:
        html = page.content()
        blob = re.sub(r"[\s\-]", "", html)
        matches = re.findall(r"(?:57)?3\d{9}", blob)
        log.info("[Scraper] Números móviles en HTML: %s", list(set(matches)))
        if matches:
            from collections import Counter
            cnt = Counter(matches)
            # Same logic: the platform's number repeats in every page element;
            # the owner's number appears once.
            least = min(cnt, key=lambda k: (cnt[k], k))
            return least if least.startswith("57") else "57" + least
    except Exception:
        pass

    return None



# ── PropDirecto ───────────────────────────────────────────────────────────────
# PropDirecto es un agregador que muestra SOLO avisos de propietarios directos
# (filtra agentes automáticamente), tomando datos de Finca Raíz, Metrocuadrado,
# Ciencuadras, MercadoLibre y Facebook Marketplace. Ideal para Massi porque
# el target son exactamente propietarios directos.
#
# ⚠ Los selectores de esta función son estimados (el sandbox bloquea la red a
# propdirecto.com). Verificar en la primera ejecución con logs DEBUG y ajustar
# _PD_LISTING_SEL y _pd_extraer_datos() si los selectores no coinciden.

_PD_HOST = "https://propdirecto.com"
_PD_CIUDADES_SLUGS = {
    "bogota": "Bogota",
    "medellin": "Medellin",
    "barranquilla": "Barranquilla",
    "cartagena": "Cartagena",
    "santa marta": "Santa+Marta",
    "cucuta": "Cucuta",
    "chia": "Chia",
}
_PD_BASE = "https://propdirecto.com/propiedades.php?ciudad={ciudad}&pagina={page}"
# Selector de enlaces a fichas individuales. PropDirecto usa /detalle.php?id=
_PD_LISTING_SEL = (
    "a[href*='detalle.php'], "
    "a[href*='/inmueble/'], "
    "a[href*='/propiedad/'], "
    "[class*='listing'] a[href*='detalle'], "
    "[class*='property-card'] a[href*='detalle']"
)


def _pd_extraer_datos(page: Page, url: str, ciudad_hint: str = "") -> Optional[dict]:
    try:
        page.wait_for_selector("h1, [class*='price'], [class*='precio'], main", timeout=8_000)
    except PWTimeout:
        return None

    nombre = _texto(page, "h1") or None
    titulo_pagina = page.title() or ""
    ciudad_raw = (
        _texto(page, "[class*='ciudad'], [class*='city'], [class*='location'], [data-testid='location']")
        or ciudad_hint   # slug from listing URL, e.g. "bogota"
        or titulo_pagina
        or url
    )
    tipo_raw = (
        _texto(page, "[class*='tipo'], [class*='property-type'], [class*='tipoInmueble'], [data-testid='property-type']")
        or titulo_pagina  # title often has "Apartamento en venta..."
        or nombre
        or url
    )

    log.info("[PD] url=%s hint=%r titulo=%r ciudad_raw=%r", url[-60:], ciudad_hint, titulo_pagina[:60], ciudad_raw[:60])

    precio_raw = _texto(page, "[class*='price'], [class*='precio'], [class*='valor'], [data-testid='price'], [data-testid='precio']")
    if not precio_raw:
        # Try title first: "$185 millones", "$148.000.000"
        texto_precio = titulo_pagina + " " + (nombre or "")
        m_mill = re.search(r'\$\s*([\d.,]+)\s*millones?', texto_precio, re.I)
        if m_mill:
            digits = re.sub(r"[.,\s]", "", m_mill.group(1))
            precio_raw = str(int(digits) * 1_000_000) if digits.isdigit() else ""
        else:
            m_num = re.search(r'\$\s*([\d.,]{6,})', texto_precio)
            if m_num:
                precio_raw = re.sub(r"[.,]", "", m_num.group(1))
    if not precio_raw:
        # Last resort: scan full body text for price patterns
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        m_mill = re.search(r'\$\s*([\d.,]+)\s*millones?', body, re.I)
        if m_mill:
            digits = re.sub(r"[.,\s]", "", m_mill.group(1))
            precio_raw = str(int(digits) * 1_000_000) if digits.isdigit() else ""
        else:
            m_num = re.search(r'\$\s*([\d.,]{6,})', body)
            if m_num:
                precio_raw = re.sub(r"[.,]", "", m_num.group(1))
            else:
                # Numbers like "185.000.000" or "185,000,000" without "$"
                m_bare = re.search(r'\b(1\d{2}|[2-9]\d{2}|\d{4})[.,](\d{3})[.,](\d{3})\b', body)
                if m_bare:
                    precio_raw = m_bare.group(1) + m_bare.group(2) + m_bare.group(3)

    return {
        "nombre": nombre,
        "precio_raw": precio_raw,
        "estrato_raw": _texto(page, "[class*='estrato'], :text-matches('Estrato [0-9]')"),
        "tipo_raw": tipo_raw,
        "ciudad_raw": ciudad_raw,
        "direccion": _texto(page, "[class*='address'], [class*='direccion']") or None,
        "barrio": _texto(page, "[class*='barrio'], [class*='sector'], [class*='neighborhood']") or None,
        "foto": _src_img(
            page,
            "img[class*='gallery'], img[class*='principal'], img[class*='foto'], "
            "img[class*='slider'], img[class*='photo']",
        ),
        "anunciante": _anunciante_info(page),
        "url": url,
    }


def _pd_redirigido(url: str) -> bool:
    """True si PropDirecto nos redirigió a un muro de login/registro."""
    return any(p in url for p in ("/login", "/registro", "/registro.php"))


def _pd_login(browser: Browser) -> Optional[dict]:
    """Inicia sesión en PropDirecto y devuelve el storage_state con cookies.

    Devuelve None si las credenciales no están configuradas o el login falla.
    El storage_state se puede pasar a browser.new_context(storage_state=...) para
    reutilizar la sesión en todas las páginas PD sin tener que volver a loguearse.
    """
    if not PD_EMAIL or not PD_PASSWORD:
        log.warning("[PD] PD_EMAIL/PD_PASSWORD no configurados — se omitirá el login.")
        return None

    log.info("[PD] Iniciando sesión como %s …", PD_EMAIL)
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        locale="es-CO",
    )
    try:
        page = ctx.new_page()
        page.goto("https://propdirecto.com/login.php", timeout=30_000)

        # Esperar el formulario de login
        try:
            page.wait_for_selector("input[type='email'], input[name='email'], input[name='usuario']", timeout=10_000)
        except PWTimeout:
            log.warning("[PD] No se encontró el formulario de login en %s", page.url)
            return None

        # Rellenar email
        email_sel = "input[type='email'], input[name='email'], input[name='usuario']"
        page.fill(email_sel, PD_EMAIL)

        # Rellenar contraseña
        pass_sel = "input[type='password'], input[name='password'], input[name='contrasena'], input[name='clave']"
        page.fill(pass_sel, PD_PASSWORD)

        # Submit
        submit_sel = "button[type='submit'], input[type='submit'], button:has-text('Ingresar'), button:has-text('Iniciar')"
        page.click(submit_sel)

        # Esperar navegación post-login
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout:
            pass

        final_url = page.url
        if _pd_redirigido(final_url):
            log.warning("[PD] Login falló — redirigió de vuelta a %s", final_url)
            return None

        log.info("[PD] Login exitoso. URL final: %s", final_url)
        state = ctx.storage_state()
        return state
    except Exception as exc:
        log.warning("[PD] Error durante login: %s", exc)
        return None
    finally:
        ctx.close()


def _pd_nueva_pagina(browser: Browser, url: str, storage_state: Optional[dict] = None) -> Page:
    """Crea una nueva página PD reutilizando la sesión autenticada si está disponible."""
    ctx_kwargs: dict = {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1366, "height": 768},
        "locale": "es-CO",
    }
    if storage_state:
        ctx_kwargs["storage_state"] = storage_state
    ctx = browser.new_context(**ctx_kwargs)
    ctx.add_init_script("""
        window.__massiBotUris = [];
        const _origOpen = window.open;
        window.open = function(url, ...a) {
            if (url) window.__massiBotUris.push(String(url));
            try { return _origOpen && _origOpen.apply(this, [url, ...a]); } catch(e){}
        };
    """)
    page = ctx.new_page()
    page.goto(url, timeout=30_000, wait_until="domcontentloaded")
    return page


def scrape_propdirecto(browser: Browser) -> int:
    """Scraper para PropDirecto (propietarios directos, sin agentes).

    PropDirecto expone los teléfonos de la fuente original (Finca Raíz,
    Metrocuadrado, etc.). Los duplicados los maneja la DB con ON CONFLICT.
    """
    log.info("[PD] Iniciando.")
    guardados = total = 0

    # Login para superar el muro de registro
    pd_session = _pd_login(browser)
    if not pd_session:
        log.warning("[PD] Sin sesión autenticada — muchas fichas requerirán login. Continuando sin sesión.")

    for ciudad_key, ciudad_slug in _PD_CIUDADES_SLUGS.items():
        if guardados >= MAX_POR_PORTAL:
            break
        log.info("[PD] Ciudad: %s (%s)", ciudad_key, ciudad_slug)
        page_n = 1

        while guardados < MAX_POR_PORTAL:
            url = _PD_BASE.format(ciudad=ciudad_slug, page=page_n)
            try:
                lp = _pd_nueva_pagina(browser, url, storage_state=pd_session)
            except Exception as exc:
                log.warning("[PD] Página %d inaccesible: %s", page_n, exc)
                break

            # Si aun con sesión nos redirige a login, las cookies caducaron
            if _pd_redirigido(lp.url):
                lp.context.close()
                log.warning("[PD] Sesión expirada o inválida en pág %d (%s) — re-intentando login.", page_n, lp.url)
                pd_session = _pd_login(browser)
                if not pd_session:
                    log.warning("[PD] Re-login falló — abortando PD.")
                    return guardados
                # Retry this page with fresh session
                try:
                    lp = _pd_nueva_pagina(browser, url, storage_state=pd_session)
                except Exception as exc:
                    log.warning("[PD] Página %d inaccesible tras re-login: %s", page_n, exc)
                    break
                if _pd_redirigido(lp.url):
                    lp.context.close()
                    log.warning("[PD] Muro de registro persiste tras re-login — abortando PD.")
                    return guardados

            if page_n > 1 and lp.url.rstrip("/") == _PD_HOST:
                lp.context.close()
                log.warning("[PD] Redirigido a home en pág %d — fin de resultados.", page_n)
                break

            try:
                lp.wait_for_selector(_PD_LISTING_SEL, timeout=15_000)
            except PWTimeout:
                log.warning("[PD] Timeout pág %d. url_final=%s title=%r html=%r",
                            page_n, lp.url, lp.title(), lp.content()[:2000])
                lp.context.close()
                break

            links = _links_de_pagina(lp, _PD_LISTING_SEL, _PD_HOST)
            links = [l for l in links if "propdirecto.com" in l]
            lp.context.close()
            if not links:
                break

            log.debug("[PD] Ciudad=%s pág %d: %d links.", ciudad_key, page_n, len(links))

            for href in links:
                if guardados >= MAX_POR_PORTAL:
                    break
                total += 1
                try:
                    dp = _pd_nueva_pagina(browser, href, storage_state=pd_session)
                    if _pd_redirigido(dp.url):
                        dp.context.close()
                        # Session may have expired mid-scrape; try re-login once
                        log.warning("[PD] Detalle redirigió a registro — re-intentando login.")
                        pd_session = _pd_login(browser)
                        if not pd_session:
                            log.warning("[PD] Re-login falló — abortando PD.")
                            log.info("[PD] %d visitados, %d guardados.", total, guardados)
                            return guardados
                        try:
                            dp = _pd_nueva_pagina(browser, href, storage_state=pd_session)
                        except Exception as exc:
                            log.debug("[PD] Error tras re-login %s: %s", href, exc)
                            continue
                        if _pd_redirigido(dp.url):
                            dp.context.close()
                            log.warning("[PD] Muro persiste tras re-login — abortando PD.")
                            return guardados
                    try:
                        datos = _pd_extraer_datos(dp, href, ciudad_hint=ciudad_key)
                        if datos:
                            tel = _extraer_tel_comun(dp)
                            if tel and _guardar("propdirecto", tel, **datos):
                                guardados += 1
                    finally:
                        dp.context.close()
                except Exception as exc:
                    log.debug("[PD] Error %s: %s", href, exc)
                _pausa()

            page_n += 1

    log.info("[PD] %d visitados, %d guardados.", total, guardados)
    return guardados


# ── Metrocuadrado ─────────────────────────────────────────────────────────────

# City slugs as they appear in MQ URLs, mapped from captacion.CIUDADES keys.
_MQ_CIUDADES_SLUGS = {
    "bogota": "bogota",
    "medellin": "medellin",
    "barranquilla": "barranquilla",
    "cartagena": "cartagena",
    "santa marta": "santa-marta",
    "cucuta": "cucuta",
    "chia": "chia",
}
_MQ_BASE = (
    "https://www.metrocuadrado.com/inmuebles/venta/{ciudad}/"
    "?search=form&tipoAnunciante=particular&page={page}"
)
_MQ_HOST = "https://www.metrocuadrado.com"
_MQ_LISTING_SEL = "a[href*='/inmueble/']"


def _mq_next_data(page: Page) -> dict:
    """Extract window.__NEXT_DATA__ from a MQ property page."""
    try:
        raw = page.evaluate("() => JSON.stringify(window.__NEXT_DATA__ || null)")
        if not raw:
            return {}
        data = json.loads(raw)
        pp = data.get("props", {}).get("pageProps", {})
        # MQ nests the listing under different keys depending on page type.
        for key in ("realEstate", "listing", "inmueble", "property"):
            if key in pp and isinstance(pp[key], dict):
                return pp[key]
        # Last resort: find the first dict with a price-like key
        for v in pp.values():
            if isinstance(v, dict) and any(k in v for k in ("price", "precio", "valorVenta", "canonicalUrl")):
                return v
    except Exception as exc:
        log.debug("[MQ] __NEXT_DATA__ error: %s", exc)
    return {}


def _mq_extraer_datos(page: Page, url: str) -> Optional[dict]:
    try:
        page.wait_for_selector("h1, main", timeout=10_000)
    except PWTimeout:
        return None

    nd = _mq_next_data(page)
    log.debug("[MQ] __NEXT_DATA__ keys: %s", list(nd.keys())[:15])

    precio_raw = str(nd.get("price") or nd.get("precio") or nd.get("valorVenta") or "")
    tipo_raw = str(nd.get("propertyType") or nd.get("tipoInmueble") or nd.get("tipo") or "")
    estrato_raw = str(nd.get("stratum") or nd.get("estrato") or "")
    nombre = str(nd.get("title") or nd.get("titulo") or "") or None

    loc = nd.get("location") or nd.get("ubicacion") or {}
    ciudad_raw = str(loc.get("city") or loc.get("ciudad") or "") if isinstance(loc, dict) else ""
    barrio = str(loc.get("neighborhood") or loc.get("barrio") or loc.get("sector") or "") if isinstance(loc, dict) else None
    direccion = str(loc.get("address") or loc.get("direccion") or "") if isinstance(loc, dict) else None

    # Always fall back to URL for city so _buscar can parse the slug
    if not ciudad_raw:
        ciudad_raw = url

    # CSS fallbacks for fields __NEXT_DATA__ didn't provide
    if not precio_raw:
        precio_raw = _texto(page, "[data-testid='price'], [class*='price']:not([class*='anterior'])")
    if not nombre:
        nombre = _texto(page, "h1") or None
    if not tipo_raw:
        tipo_raw = _texto(page, "[data-testid='property-type']") or url
    if not estrato_raw:
        estrato_raw = _texto(page, "[class*='estrato']")
    if not barrio:
        barrio = _texto(page, "[class*='neighborhood'], [class*='barrio'], [class*='sector']") or None
    if not direccion:
        direccion = _texto(page, "[class*='address'], [class*='direccion']") or None

    log.info("[MQ] precio_raw=%r tipo_raw=%r ciudad_raw=%r", precio_raw, tipo_raw, ciudad_raw[:60])

    return {
        "nombre": nombre,
        "precio_raw": precio_raw,
        "estrato_raw": estrato_raw,
        "tipo_raw": tipo_raw,
        "ciudad_raw": ciudad_raw,
        "direccion": direccion,
        "barrio": barrio,
        "foto": _src_img(page, "img[class*='gallery'], img[class*='slider'], img[class*='photo']"),
        "anunciante": _anunciante_info(page),
        "url": url,
    }


def scrape_metrocuadrado(browser: Browser) -> int:
    log.info("[MQ] Iniciando.")
    guardados = total = 0

    for ciudad_key, ciudad_slug in _MQ_CIUDADES_SLUGS.items():
        if guardados >= MAX_POR_PORTAL:
            break
        log.info("[MQ] Ciudad: %s (%s)", ciudad_key, ciudad_slug)
        page_n = 1

        while guardados < MAX_POR_PORTAL:
            url = _MQ_BASE.format(ciudad=ciudad_slug, page=page_n)
            try:
                lp = _nueva_pagina(browser, url)
            except Exception as exc:
                log.warning("[MQ] %s pág %d inaccesible: %s", ciudad_slug, page_n, exc)
                break

            try:
                lp.wait_for_selector(_MQ_LISTING_SEL, timeout=15_000)
            except PWTimeout:
                log.warning("[MQ] Timeout %s pág %d. url_final=%s title=%r html=%r",
                            ciudad_slug, page_n, lp.url, lp.title(), lp.content()[:2000])
                lp.context.close()
                break

            links = _links_de_pagina(lp, _MQ_LISTING_SEL, _MQ_HOST)
            lp.context.close()
            if not links:
                log.info("[MQ] %s pág %d sin links, siguiente ciudad.", ciudad_slug, page_n)
                break

            for href in links:
                if guardados >= MAX_POR_PORTAL:
                    break
                total += 1
                try:
                    dp = _nueva_pagina(browser, href)
                    try:
                        datos = _mq_extraer_datos(dp, href)
                        if datos:
                            tel = _extraer_tel_comun(dp)
                            if tel and _guardar("metrocuadrado", tel, **datos):
                                guardados += 1
                    finally:
                        dp.context.close()
                except Exception as exc:
                    log.debug("[MQ] Error %s: %s", href, exc)
                _pausa()

            page_n += 1

    log.info("[MQ] %d visitados, %d guardados.", total, guardados)
    return guardados


# ── Finca Raíz ────────────────────────────────────────────────────────────────

_FR_CIUDADES_SLUGS = {
    "bogota": "bogota",
    "medellin": "medellin",
    "barranquilla": "barranquilla",
    "cartagena": "cartagena",
    "santa marta": "santa-marta",
    "cucuta": "cucuta",
    "chia": "chia",
}
_FR_BASE = (
    "https://www.fincaraiz.com.co/venta/inmuebles/{ciudad}/"
    "?tipoAnunciante=particular&pagina={page}"
)
_FR_HOST = "https://www.fincaraiz.com.co"
_FR_LISTING_SEL = "a[href*='/inmueble/'], a[href*='.htm']"


def _fr_extraer_datos(page: Page, url: str) -> Optional[dict]:
    try:
        page.wait_for_selector("h1, main", timeout=10_000)
    except PWTimeout:
        return None

    nd = _mq_next_data(page)  # FR is also Next.js
    log.debug("[FR] __NEXT_DATA__ keys: %s", list(nd.keys())[:15])

    precio_raw = str(nd.get("price") or nd.get("precio") or nd.get("valorVenta") or "")
    tipo_raw = str(nd.get("propertyType") or nd.get("tipoInmueble") or nd.get("tipo") or "")
    estrato_raw = str(nd.get("stratum") or nd.get("estrato") or "")
    nombre = str(nd.get("title") or nd.get("titulo") or "") or None
    loc = nd.get("location") or nd.get("ubicacion") or {}
    ciudad_raw = str(loc.get("city") or loc.get("ciudad") or "") if isinstance(loc, dict) else ""
    barrio = str(loc.get("neighborhood") or loc.get("barrio") or loc.get("sector") or "") if isinstance(loc, dict) else None
    direccion = str(loc.get("address") or loc.get("direccion") or "") if isinstance(loc, dict) else None

    # Fallbacks to CSS / URL
    if not ciudad_raw:
        ciudad_raw = url
    if not precio_raw:
        precio_raw = _texto(page, "[data-testid='price'], [class*='price']:not([class*='anterior']), [class*='precio']")
    if not nombre:
        nombre = _texto(page, "h1") or None
    if not tipo_raw:
        tipo_raw = _texto(page, "[data-testid='property-type'], [class*='tipoInmueble'], [class*='tipo']") or url
    if not estrato_raw:
        estrato_raw = _texto(page, "[class*='estrato'], :text-matches('Estrato [0-9]')")
    if not barrio:
        barrio = _texto(page, "[class*='neighborhood'], [class*='barrio'], [class*='sector']") or None
    if not direccion:
        direccion = _texto(page, "[class*='address'], [class*='direccion']") or None

    log.info("[FR] precio_raw=%r tipo_raw=%r ciudad_raw=%r", precio_raw, tipo_raw, ciudad_raw[:60])
    return {
        "nombre": nombre, "precio_raw": precio_raw, "estrato_raw": estrato_raw,
        "tipo_raw": tipo_raw, "ciudad_raw": ciudad_raw, "direccion": direccion,
        "barrio": barrio,
        "foto": _src_img(page, "img[class*='gallery'], img[class*='principal'], img[class*='foto']"),
        "anunciante": _anunciante_info(page), "url": url,
    }


def scrape_fincaraiz(browser: Browser) -> int:
    log.info("[FR] Iniciando.")
    guardados = total = 0

    for ciudad_key, ciudad_slug in _FR_CIUDADES_SLUGS.items():
        if guardados >= MAX_POR_PORTAL:
            break
        log.info("[FR] Ciudad: %s (%s)", ciudad_key, ciudad_slug)
        page_n = 1

        while guardados < MAX_POR_PORTAL:
            url = _FR_BASE.format(ciudad=ciudad_slug, page=page_n)
            try:
                lp = _nueva_pagina(browser, url)
            except Exception as exc:
                log.warning("[FR] Página %d inaccesible: %s", page_n, exc)
                break

            try:
                lp.wait_for_selector(_FR_LISTING_SEL, timeout=15_000)
            except PWTimeout:
                log.warning("[FR] Timeout pág %d. url_final=%s title=%r html=%r",
                            page_n, lp.url, lp.title(), lp.content()[:2000])
                lp.context.close()
                break

            links = _links_de_pagina(lp, _FR_LISTING_SEL, _FR_HOST)
            lp.context.close()
            if not links:
                break

            for href in links:
                if guardados >= MAX_POR_PORTAL:
                    break
                total += 1
                try:
                    dp = _nueva_pagina(browser, href)
                    try:
                        datos = _fr_extraer_datos(dp, href)
                        if datos:
                            tel = _extraer_tel_comun(dp)
                            if tel and _guardar("fincaraiz", tel, **datos):
                                guardados += 1
                    finally:
                        dp.context.close()
                except Exception as exc:
                    log.debug("[FR] Error %s: %s", href, exc)
                _pausa()

            page_n += 1

    log.info("[FR] %d visitados, %d guardados.", total, guardados)
    return guardados


# ── Ciencuadras ───────────────────────────────────────────────────────────────

_CC_CIUDADES_SLUGS = {
    "bogota": "bogota",
    "medellin": "medellin",
    "barranquilla": "barranquilla",
    "cartagena": "cartagena",
    "santa marta": "santa-marta",
    "cucuta": "cucuta",
    "chia": "chia",
}
_CC_BASE = (
    "https://www.ciencuadras.com/venta/{ciudad}"
    "?tipoAnunciante=particular&pagina={page}"
)
_CC_HOST = "https://www.ciencuadras.com"
_CC_LISTING_SEL = "a[href*='/inmueble/'], a[href*='/propiedad/']"


def _cc_extraer_datos(page: Page, url: str) -> Optional[dict]:
    try:
        page.wait_for_selector("h1, main", timeout=10_000)
    except PWTimeout:
        return None

    nd = _mq_next_data(page)  # CC is also a Next.js / React app
    log.debug("[CC] __NEXT_DATA__ keys: %s", list(nd.keys())[:15])

    precio_raw = str(nd.get("price") or nd.get("precio") or nd.get("valorVenta") or "")
    tipo_raw = str(nd.get("propertyType") or nd.get("tipoInmueble") or nd.get("tipo") or "")
    estrato_raw = str(nd.get("stratum") or nd.get("estrato") or "")
    nombre = str(nd.get("title") or nd.get("titulo") or "") or None
    loc = nd.get("location") or nd.get("ubicacion") or {}
    ciudad_raw = str(loc.get("city") or loc.get("ciudad") or "") if isinstance(loc, dict) else ""
    barrio = str(loc.get("neighborhood") or loc.get("barrio") or loc.get("sector") or "") if isinstance(loc, dict) else None
    direccion = str(loc.get("address") or loc.get("direccion") or "") if isinstance(loc, dict) else None

    # Fallbacks to CSS / URL
    if not ciudad_raw:
        ciudad_raw = url
    if not precio_raw:
        precio_raw = _texto(page, "[data-testid='price'], [class*='price'], [class*='precio'], [class*='valor']")
    if not nombre:
        nombre = _texto(page, "h1") or None
    if not tipo_raw:
        tipo_raw = _texto(page, "[class*='tipo'], [class*='property-type']") or url
    if not estrato_raw:
        estrato_raw = _texto(page, "[class*='estrato'], :text-matches('Estrato [0-9]')")
    if not barrio:
        barrio = _texto(page, "[class*='barrio'], [class*='sector'], [class*='neighborhood']") or None
    if not direccion:
        direccion = _texto(page, "[class*='direccion'], [class*='address']") or None

    log.info("[CC] precio_raw=%r tipo_raw=%r ciudad_raw=%r", precio_raw, tipo_raw, ciudad_raw[:60])
    return {
        "nombre": nombre, "precio_raw": precio_raw, "estrato_raw": estrato_raw,
        "tipo_raw": tipo_raw, "ciudad_raw": ciudad_raw, "direccion": direccion,
        "barrio": barrio,
        "foto": _src_img(page, "img[class*='gallery'], img[class*='principal'], img[class*='foto']"),
        "anunciante": _anunciante_info(page), "url": url,
    }


def scrape_ciencuadras(browser: Browser) -> int:
    log.info("[CC] Iniciando.")
    guardados = total = 0

    for ciudad_key, ciudad_slug in _CC_CIUDADES_SLUGS.items():
        if guardados >= MAX_POR_PORTAL:
            break
        log.info("[CC] Ciudad: %s (%s)", ciudad_key, ciudad_slug)
        page_n = 1

        while guardados < MAX_POR_PORTAL:
            url = _CC_BASE.format(ciudad=ciudad_slug, page=page_n)
            try:
                lp = _nueva_pagina(browser, url)
            except Exception as exc:
                log.warning("[CC] Página %d inaccesible: %s", page_n, exc)
                break

            try:
                lp.wait_for_selector(_CC_LISTING_SEL, timeout=15_000)
            except PWTimeout:
                log.warning("[CC] Timeout pág %d. url_final=%s title=%r html=%r",
                            page_n, lp.url, lp.title(), lp.content()[:2000])
                lp.context.close()
                break

            links = _links_de_pagina(lp, _CC_LISTING_SEL, _CC_HOST)
            lp.context.close()
            if not links:
                break

            for href in links:
                if guardados >= MAX_POR_PORTAL:
                    break
                total += 1
                try:
                    dp = _nueva_pagina(browser, href)
                    try:
                        datos = _cc_extraer_datos(dp, href)
                        if datos:
                            tel = _extraer_tel_comun(dp)
                            if tel and _guardar("ciencuadras", tel, **datos):
                                guardados += 1
                    finally:
                        dp.context.close()
                except Exception as exc:
                    log.debug("[CC] Error %s: %s", href, exc)
                _pausa()

            page_n += 1

    log.info("[CC] %d visitados, %d guardados.", total, guardados)
    return guardados


# ── Helpers de imagen ─────────────────────────────────────────────────────────


def _primer_img(page: Page) -> bool:
    return bool(page.query_selector("img"))


def _src_img(page: Page, selector: str) -> Optional[str]:
    el = page.query_selector(selector)
    return el.get_attribute("src") if el else None


# ── Orchestrator ──────────────────────────────────────────────────────────────


def correr_todos():
    """Ejecuta los cuatro portales en secuencia dentro de un solo browser.
    Orden: PropDirecto → Metrocuadrado → Finca Raíz → Ciencuadras."""
    log.info("[Scraper] Iniciando ronda.")
    chromium_path = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH", "")
    launch_kwargs: dict = {
        "headless": True,
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-zygote",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-sync",
            "--hide-scrollbars",
            "--mute-audio",
            "--no-first-run",
            "--disable-setuid-sandbox",
            "--disable-software-rasterizer",
        ],
    }
    if chromium_path:
        launch_kwargs["executable_path"] = chromium_path

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_kwargs)
        log.info("[Scraper] Chromium lanzado OK.")
        try:
            for nombre, fn in [
                ("propdirecto",  scrape_propdirecto),
                ("metrocuadrado", scrape_metrocuadrado),
                ("fincaraiz",     scrape_fincaraiz),
                ("ciencuadras",   scrape_ciencuadras),
            ]:
                try:
                    n = fn(browser)
                    log.info("[Scraper] %s → %d contactos nuevos.", nombre, n)
                except Exception:
                    log.exception("[Scraper] Error en portal '%s'.", nombre)
        finally:
            browser.close()
    log.info("[Scraper] Ronda completa.")


# ── Scheduler ─────────────────────────────────────────────────────────────────


def iniciar():
    """Inicia APScheduler para correr el scraper cada 2 horas.
    Se llama desde server.py al arrancar el proceso."""
    if not SCRAPER_ENABLED:
        log.info("[Scraper] SCRAPER_ENABLED no está en 'true'; scheduler no iniciado.")
        return
    if not os.environ.get("DATABASE_URL"):
        log.warning("[Scraper] Sin DATABASE_URL; scheduler no iniciado.")
        return
    if not TWOCAPTCHA_KEY:
        log.warning("[Scraper] Sin TWOCAPTCHA_API_KEY; el scraper correrá sin resolver captchas (teléfonos ocultos no se revelarán).")

    global _scheduler
    if _scheduler is not None:
        return

    from datetime import datetime
    _tz = pytz.timezone("America/Bogota")
    _scheduler = BackgroundScheduler(timezone=_tz)
    _scheduler.add_job(
        correr_todos, "interval", hours=2, id="scraper_portales",
        next_run_time=datetime.now(tz=_tz),
    )
    _scheduler.start()
    log.info("[Scraper] Scheduler iniciado (cada 2h). Primera ronda inmediata.")
