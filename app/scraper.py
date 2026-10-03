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
import logging
import os
import random
import re
import time
from typing import Optional

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
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
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


def _guardar(portal: str, telefono_raw: str, **campos) -> bool:
    """Aplica filtros de calidad y guarda en contactos. Retorna True si se insertó."""
    telefono = normalizar_telefono(telefono_raw)
    if not telefono:
        return False

    url = campos.get("url", "")
    ciudad_raw = campos.get("ciudad_raw", "")
    tipo_raw = campos.get("tipo_raw", "")
    direccion = campos.get("direccion") or ""
    barrio = campos.get("barrio") or ""

    ciudad = _buscar(CIUDADES, ciudad_raw, url, direccion, barrio)
    if not ciudad:
        return False

    if ciudad == "Bogotá" and any(
        z in _sin_tildes(f"{direccion} {barrio} {url}") for z in ZONAS_EXCLUIDAS
    ):
        return False

    tipo = _buscar(TIPOS, tipo_raw, url)
    if not tipo:
        return False

    precio = _precio(campos.get("precio_raw", 0))
    if precio < 50_000_000:
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
        "button[aria-label*='WhatsApp'], "
        "a[aria-label*='WhatsApp']"
    )
    if wa_btn:
        try:
            wa_btn.click()
            page.wait_for_timeout(3_000)  # tiempo extra para respuesta XHR
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

    # ── 3b. Buscar enlace tel: (puede ser fijo/landline) ─────────────────────
    tel_link = page.query_selector("a[href^='tel:']")
    if tel_link:
        return re.sub(r"\D", "", tel_link.get_attribute("href") or "")

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
        m = re.search(
            r"\b(57\s*3\d{2}[\s.\-]?\d{3}[\s.\-]?\d{4}|3\d{2}[\s.\-]?\d{3}[\s.\-]?\d{4})\b",
            body_text,
        )
        if m:
            return re.sub(r"\D", "", m.group(0))
    except Exception:
        pass

    # ── 6. Buscar en HTML completo (data-*, atributos ocultos, scripts inline) ─
    try:
        html = page.content()
        blob = re.sub(r"[\s\-]", "", html)
        matches = re.findall(r"(?:57)?3\d{9}", blob)
        if matches:
            from collections import Counter
            top, count = Counter(matches).most_common(1)[0]
            if count >= 2:
                return top if top.startswith("57") else "57" + top
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

_PD_HOST = "https://www.propdirecto.com"
# URL de búsqueda: patrón observado en portales colombianos similares.
# Ajustar si el portal usa otra estructura de paginación.
_PD_BASE = "https://www.propdirecto.com/inmuebles?pagina={page}"
# Selector de enlaces a fichas individuales. PropDirecto puede usar
# /inmueble/, /propiedad/ o /aviso/ — se prueban los tres.
_PD_LISTING_SEL = (
    "a[href*='/inmueble/'], "
    "a[href*='/propiedad/'], "
    "a[href*='/aviso/'], "
    "[class*='listing'] a, "
    "[class*='property-card'] a, "
    "[class*='card'] a[href*='/']"
)


def _pd_extraer_datos(page: Page, url: str) -> Optional[dict]:
    try:
        page.wait_for_selector("h1, [class*='price'], [class*='precio']", timeout=8_000)
    except PWTimeout:
        return None

    return {
        "nombre": _texto(page, "h1") or None,
        "precio_raw": _texto(
            page,
            "[class*='price'], [class*='precio'], [class*='valor'], "
            "[data-testid='price'], [data-testid='precio']",
        ),
        "estrato_raw": _texto(page, "[class*='estrato'], :text-matches('Estrato [0-9]')"),
        "tipo_raw": _texto(
            page,
            "[class*='tipo'], [class*='property-type'], [class*='tipoInmueble'], "
            "[data-testid='property-type']",
        ) or url,
        "ciudad_raw": _texto(
            page,
            "[class*='ciudad'], [class*='city'], [class*='location'], "
            "[data-testid='location']",
        ) or url,
        "direccion": _texto(page, "[class*='address'], [class*='direccion']") or None,
        "barrio": _texto(page, "[class*='barrio'], [class*='sector'], [class*='neighborhood']") or None,
        "foto": _src_img(
            page,
            "img[class*='gallery'], img[class*='principal'], img[class*='foto'], "
            "img[class*='slider'], img[class*='photo']",
        ),
        "url": url,
    }


def scrape_propdirecto(browser: Browser) -> int:
    """Scraper para PropDirecto (propietarios directos, sin agentes).

    PropDirecto expone los teléfonos de la fuente original (Finca Raíz,
    Metrocuadrado, etc.). Los duplicados los maneja la DB con ON CONFLICT.
    """
    guardados = total = 0
    page_n = 1

    while guardados < MAX_POR_PORTAL:
        url = _PD_BASE.format(page=page_n)
        try:
            lp = _nueva_pagina(browser, url)
        except Exception as exc:
            log.warning("[PD] Página %d inaccesible: %s", page_n, exc)
            break

        # Si redirige a la home o a /login, el portal cambió estructura.
        if "/login" in lp.url or (page_n > 1 and lp.url == f"{_PD_HOST}/"):
            lp.context.close()
            log.warning("[PD] Redirigido a %s — revisar _PD_BASE.", lp.url)
            break

        try:
            lp.wait_for_selector(_PD_LISTING_SEL, timeout=15_000)
        except PWTimeout:
            lp.context.close()
            log.info("[PD] No se encontraron listings en página %d (selector: %s).", page_n, _PD_LISTING_SEL)
            break

        links = _links_de_pagina(lp, _PD_LISTING_SEL, _PD_HOST)
        # Filtrar links que apunten fuera del dominio si el portal redirige al origen
        links = [l for l in links if _PD_HOST in l or l.startswith("/")]
        lp.context.close()
        if not links:
            break

        log.debug("[PD] Página %d: %d links encontrados.", page_n, len(links))

        for href in links:
            if guardados >= MAX_POR_PORTAL:
                break
            total += 1
            try:
                dp = _nueva_pagina(browser, href)
                try:
                    datos = _pd_extraer_datos(dp, href)
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

_MQ_BASE = (
    "https://www.metrocuadrado.com/inmuebles/venta/"
    "?search=form&propertyType=Apartamento,Casa,Local,Oficina,Lote&page={page}"
)
_MQ_HOST = "https://www.metrocuadrado.com"
_MQ_LISTING_SEL = "a[href*='/inmueble/venta/']"


def _mq_extraer_datos(page: Page, url: str) -> Optional[dict]:
    try:
        page.wait_for_selector("h1, [class*='price'], [class*='title']", timeout=8_000)
    except PWTimeout:
        return None

    return {
        "nombre": _texto(page, "h1") or None,
        "precio_raw": _texto(page, "[class*='price']:not([class*='anterior']), [data-testid='price']"),
        "estrato_raw": _texto(page, "[class*='estrato'], :text-matches('Estrato [0-9]')"),
        "tipo_raw": _texto(page, "[class*='tipo-inmueble'], [data-testid='property-type']") or url,
        "ciudad_raw": _texto(page, "[class*='city'], [class*='ciudad'], [class*='location']") or url,
        "direccion": _texto(page, "[class*='address'], [class*='direccion']") or None,
        "barrio": _texto(page, "[class*='neighborhood'], [class*='barrio'], [class*='sector']") or None,
        "foto": (page.query_selector("img[class*='gallery'], img[class*='slider'], img[class*='photo']") or _primer_img(page)) and _src_img(page, "img[class*='gallery'], img[class*='slider'], img[class*='photo']"),
        "url": url,
    }


def scrape_metrocuadrado(browser: Browser) -> int:
    guardados = total = 0
    page_n = 1

    while guardados < MAX_POR_PORTAL:
        url = _MQ_BASE.format(page=page_n)
        try:
            lp = _nueva_pagina(browser, url)
        except Exception as exc:
            log.warning("[MQ] Página %d inaccesible: %s", page_n, exc)
            break

        try:
            lp.wait_for_selector(_MQ_LISTING_SEL, timeout=15_000)
        except PWTimeout:
            lp.context.close()
            break

        links = _links_de_pagina(lp, _MQ_LISTING_SEL, _MQ_HOST)
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

_FR_BASE = (
    "https://www.fincaraiz.com.co/venta/inmuebles/"
    "?tipoAnunciante=particular&pagina={page}"
)
_FR_HOST = "https://www.fincaraiz.com.co"
_FR_LISTING_SEL = "a[href*='/inmueble/']"


def _fr_extraer_datos(page: Page, url: str) -> Optional[dict]:
    try:
        page.wait_for_selector("h1, [class*='price'], [class*='precio']", timeout=8_000)
    except PWTimeout:
        return None

    return {
        "nombre": _texto(page, "h1") or None,
        "precio_raw": _texto(page, "[class*='price'], [class*='precio']"),
        "estrato_raw": _texto(page, ":text-matches('Estrato [0-9]'), [class*='estrato']"),
        "tipo_raw": _texto(page, "[class*='property-type'], [class*='tipoInmueble'], [class*='tipo']") or url,
        "ciudad_raw": _texto(page, "[class*='city'], [class*='ciudad']") or url,
        "direccion": _texto(page, "[class*='address'], [class*='direccion']") or None,
        "barrio": _texto(page, "[class*='neighborhood'], [class*='barrio'], [class*='sector']") or None,
        "foto": _src_img(page, "img[class*='gallery'], img[class*='principal'], img[class*='foto']"),
        "url": url,
    }


def scrape_fincaraiz(browser: Browser) -> int:
    guardados = total = 0
    page_n = 1

    while guardados < MAX_POR_PORTAL:
        url = _FR_BASE.format(page=page_n)
        try:
            lp = _nueva_pagina(browser, url)
        except Exception as exc:
            log.warning("[FR] Página %d inaccesible: %s", page_n, exc)
            break

        try:
            lp.wait_for_selector(_FR_LISTING_SEL, timeout=15_000)
        except PWTimeout:
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

_CC_BASE = "https://www.ciencuadras.com/venta?pagina={page}"
_CC_HOST = "https://www.ciencuadras.com"
_CC_LISTING_SEL = "a[href*='/inmueble/']"


def _cc_extraer_datos(page: Page, url: str) -> Optional[dict]:
    try:
        page.wait_for_selector("h1, [class*='price'], [class*='precio'], [class*='valor']", timeout=8_000)
    except PWTimeout:
        return None

    return {
        "nombre": _texto(page, "h1") or None,
        "precio_raw": _texto(page, "[class*='price'], [class*='precio'], [class*='valor']"),
        "estrato_raw": _texto(page, "[class*='estrato'], :text-matches('Estrato [0-9]')"),
        "tipo_raw": _texto(page, "[class*='tipo'], [class*='property-type']") or url,
        "ciudad_raw": _texto(page, "[class*='ciudad'], [class*='city'], [class*='location']") or url,
        "direccion": _texto(page, "[class*='direccion'], [class*='address']") or None,
        "barrio": _texto(page, "[class*='barrio'], [class*='sector'], [class*='neighborhood']") or None,
        "foto": _src_img(page, "img[class*='gallery'], img[class*='principal'], img[class*='foto']"),
        "url": url,
    }


def scrape_ciencuadras(browser: Browser) -> int:
    guardados = total = 0
    page_n = 1

    while guardados < MAX_POR_PORTAL:
        url = _CC_BASE.format(page=page_n)
        try:
            lp = _nueva_pagina(browser, url)
        except Exception as exc:
            log.warning("[CC] Página %d inaccesible: %s", page_n, exc)
            break

        try:
            lp.wait_for_selector(_CC_LISTING_SEL, timeout=15_000)
        except PWTimeout:
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
    launch_kwargs: dict = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    if chromium_path:
        launch_kwargs["executable_path"] = chromium_path

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_kwargs)
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
        log.warning("[Scraper] Sin TWOCAPTCHA_API_KEY; scheduler no iniciado.")
        return

    global _scheduler
    if _scheduler is not None:
        return

    _scheduler = BackgroundScheduler(timezone="America/Bogota")
    _scheduler.add_job(correr_todos, "interval", hours=2, id="scraper_portales")
    _scheduler.start()
    log.info("[Scraper] Scheduler iniciado (cada 2h).")
