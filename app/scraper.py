"""Scraper de portales inmobiliarios colombianos.

Portales (en orden de ejecución):
  1. PropDirecto  (propdirecto.com)  — agregador de directo-propietario
  2. Metrocuadrado                   — solo directo-propietario (tipoAnunciante=1)
  3. Finca Raíz                      — solo directo-propietario (tipoAnunciante=particular)
  4. Ciencuadras

Captcha:
  Los portales ponen un captcha en "Ver teléfono". Se usa Playwright para
  hacer clic humano + 2captcha API para resolverlo (reCAPTCHA v2 y hCaptcha).
  Costo: ~$3/1,000 captchas. No se requiere cuenta en los portales.

Corre cada 2h mediante APScheduler (ver scheduler.py).
SCRAPER_ENABLED=true activa el scraper.

⚠️  Los selectores de PropDirecto no pudieron verificarse (sitio bloqueado
    en sandbox). Verificar en primera ejecución con producción.
"""
import logging
import os
import re
import time
import unicodedata
from datetime import date

from app import db

log = logging.getLogger("petra")

SCRAPER_ENABLED = os.environ.get("SCRAPER_ENABLED", "false").strip().lower() == "true"
TWOCAPTCHA_KEY = os.environ.get("TWOCAPTCHA_API_KEY", "")
MAX_POR_PORTAL = int(os.environ.get("SCRAPER_MAX_POR_PORTAL", "300"))

CIUDADES_SURETI = {
    "bogota": "Bogotá",
    "medellin": "Medellín",
    "barranquilla": "Barranquilla",
    "cartagena": "Cartagena",
    "santa marta": "Santa Marta",
    "cucuta": "Cúcuta",
}
ZONAS_EXCLUIDAS_BOG = {"san cristobal", "ciudad bolivar"}
TIPOS_VALIDOS = {"apartamento", "casa", "local", "oficina", "bodega", "lote"}


def _sin_tildes(t: str) -> str:
    t = unicodedata.normalize("NFD", t.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def _normalizar_tel(v: str) -> str:
    d = re.sub(r"\D", "", str(v or ""))
    if len(d) == 10 and d.startswith("3"):
        d = "57" + d
    return d if len(d) == 12 and d.startswith("573") else ""


def _filtrar(c: dict) -> bool:
    """True si el contacto cumple criterios de Sureti."""
    ciudad = _sin_tildes(c.get("ciudad", ""))
    if not any(k in ciudad for k in CIUDADES_SURETI):
        return False
    if any(z in ciudad for z in ZONAS_EXCLUIDAS_BOG):
        return False
    tipo = _sin_tildes(c.get("tipo", ""))
    if not any(t in tipo for t in TIPOS_VALIDOS):
        return False
    estrato = c.get("estrato") or 0
    if estrato == 1:
        return False
    precio = c.get("precio", 0) or 0
    # Monto mínimo Sureti: $20M → precio base ~50M
    if 0 < precio < 50_000_000:
        return False
    return True


def _guardar(contacto: dict):
    if not _filtrar(contacto):
        return
    tel = _normalizar_tel(contacto.get("telefono", ""))
    if not tel:
        return
    estrato = contacto.get("estrato") or 0
    contacto["telefono"] = tel
    contacto["requiere_ph"] = estrato in (2, 3)
    try:
        db.guardar_contacto(contacto)
    except Exception as e:
        log.debug("[Scraper] guardar_contacto: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# 2captcha helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolver_captcha(page) -> str | None:
    """Detecta reCAPTCHA v2 u hCaptcha y lo resuelve con 2captcha."""
    if not TWOCAPTCHA_KEY:
        log.warning("[Scraper] TWOCAPTCHA_API_KEY no configurado.")
        return None

    import requests as req

    page_url = page.url
    site_key = None
    captcha_type = None

    # Detectar reCAPTCHA v2
    rcap = page.query_selector(".g-recaptcha, iframe[src*='recaptcha']")
    if rcap:
        site_key = rcap.get_attribute("data-sitekey") or ""
        if not site_key:
            src = rcap.get_attribute("src") or ""
            m = re.search(r"k=([^&]+)", src)
            site_key = m.group(1) if m else None
        captcha_type = "recaptcha"

    # Detectar hCaptcha
    if not site_key:
        hcap = page.query_selector(".h-captcha, iframe[src*='hcaptcha']")
        if hcap:
            site_key = hcap.get_attribute("data-sitekey") or ""
            captcha_type = "hcaptcha"

    if not site_key or not captcha_type:
        return None

    log.info("[Scraper] Resolviendo %s (site_key: %s…)", captcha_type, site_key[:8])

    # Enviar a 2captcha
    endpoint = "http://2captcha.com"
    params = {
        "key": TWOCAPTCHA_KEY,
        "method": "userrecaptcha" if captcha_type == "recaptcha" else "hcaptcha",
        "googlekey" if captcha_type == "recaptcha" else "sitekey": site_key,
        "pageurl": page_url,
        "json": 1,
    }
    r = req.post(f"{endpoint}/in.php", data=params, timeout=30)
    if not r.ok or r.json().get("status") != 1:
        log.error("[Scraper] 2captcha rechazó la solicitud: %s", r.text[:200])
        return None

    captcha_id = r.json()["request"]

    # Esperar solución (hasta 120s)
    for _ in range(24):
        time.sleep(5)
        r2 = req.get(
            f"{endpoint}/res.php",
            params={"key": TWOCAPTCHA_KEY, "action": "get", "id": captcha_id, "json": 1},
            timeout=15,
        )
        data = r2.json()
        if data.get("status") == 1:
            log.info("[Scraper] Captcha resuelto.")
            return data["request"]
        if data.get("request") != "CAPCHA_NOT_READY":
            log.error("[Scraper] 2captcha error: %s", data)
            return None

    return None


def _extraer_tel_comun(page) -> str:
    """Hace clic en 'Ver teléfono', resuelve captcha si aparece, extrae número."""
    # Buscar botón de teléfono
    btns = page.query_selector_all(
        "button:has-text('Ver teléfono'), button:has-text('Mostrar'), "
        ".btn-phone, [class*='phone'] button, [data-track='call']"
    )
    for btn in btns:
        if btn.is_visible():
            btn.click()
            time.sleep(1.5)
            break

    # ¿Apareció captcha?
    if page.query_selector(".g-recaptcha, .h-captcha, iframe[src*='captcha']"):
        token = _resolver_captcha(page)
        if token:
            # Inyectar respuesta
            page.evaluate(
                f"document.getElementById('g-recaptcha-response') && "
                f"(document.getElementById('g-recaptcha-response').value = '{token}')"
            )
            page.evaluate("typeof ___grecaptcha_cfg !== 'undefined' && grecaptcha.execute()")
            time.sleep(2)

    # Extraer número
    for sel in [".phone-number", ".tel", "[class*='phone']", "a[href^='tel:']"]:
        el = page.query_selector(sel)
        if el:
            txt = el.inner_text().strip()
            tel = _normalizar_tel(txt)
            if tel:
                return tel
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# PropDirecto
# ─────────────────────────────────────────────────────────────────────────────

def scrape_propdirecto(browser) -> int:
    """
    ⚠️  Selectores estimados — propdirecto.com bloqueado en sandbox.
    Verificar en primera ejecución real.
    """
    guardados = 0
    page = browser.new_page()
    try:
        for ciudad_key, ciudad_nombre in CIUDADES_SURETI.items():
            url = f"https://propdirecto.com/venta/{ciudad_key.replace(' ', '-')}"
            try:
                page.goto(url, timeout=30_000)
                page.wait_for_load_state("networkidle")
            except Exception as e:
                log.warning("[Scraper] PropDirecto ciudad %s: %s", ciudad_nombre, e)
                continue

            listados = page.query_selector_all(
                ".property-card, .listing-card, article[class*='property'], "
                "[class*='listing-item'], .inmueble-card"
            )
            log.info("[Scraper] PropDirecto %s: %d listados", ciudad_nombre, len(listados))

            for card in listados[:MAX_POR_PORTAL]:
                try:
                    card.click()
                    page.wait_for_load_state("domcontentloaded")
                    time.sleep(1)

                    tel = _extraer_tel_comun(page)
                    if not tel:
                        page.go_back()
                        continue

                    # Extraer datos del listado
                    precio_txt = page.inner_text(
                        ".price, .precio, [class*='price'], [data-price]"
                    ) or ""
                    precio = int(re.sub(r"\D", "", precio_txt)) if re.search(r"\d{5,}", precio_txt) else 0

                    tipo_txt = (
                        page.inner_text(".property-type, .tipo, [class*='tipo']") or
                        page.title()
                    ).lower()

                    tipo = next((t for t in TIPOS_VALIDOS if t in tipo_txt), "apartamento")

                    estrato_txt = page.inner_text(".estrato, [class*='estrato']") or ""
                    m = re.search(r"\d", estrato_txt)
                    estrato = int(m.group()) if m else None

                    dir_txt = page.inner_text(
                        ".address, .direccion, [class*='address'], [class*='direccion']"
                    ) or ""

                    fotos = [
                        el.get_attribute("src")
                        for el in page.query_selector_all("img.property-img, .gallery img")
                        if el.get_attribute("src", "").startswith("http")
                    ][:3]

                    _guardar({
                        "telefono": tel,
                        "direccion": dir_txt.strip(),
                        "ciudad": ciudad_nombre,
                        "tipo": tipo,
                        "precio": precio,
                        "estrato": estrato,
                        "fotos": fotos,
                        "url": page.url,
                        "portal": "propdirecto",
                        "fecha_publicacion": str(date.today()),
                    })
                    guardados += 1
                    page.go_back()
                    time.sleep(3)
                except Exception as e:
                    log.debug("[Scraper] PropDirecto listing error: %s", e)
                    try:
                        page.go_back()
                    except Exception:
                        pass

    finally:
        page.close()

    log.info("[Scraper] PropDirecto: %d guardados.", guardados)
    return guardados


# ─────────────────────────────────────────────────────────────────────────────
# Metrocuadrado
# ─────────────────────────────────────────────────────────────────────────────

def scrape_metrocuadrado(browser) -> int:
    guardados = 0
    page = browser.new_page()
    try:
        for ciudad_key, ciudad_nombre in CIUDADES_SURETI.items():
            ciudad_slug = ciudad_key.replace(" ", "-")
            # tipoAnunciante=1 = particular (directo propietario)
            url = (
                f"https://www.metrocuadrado.com/inmuebles/venta/{ciudad_slug}/"
                f"?tipoAnunciante=1"
            )
            try:
                page.goto(url, timeout=30_000)
                page.wait_for_load_state("networkidle")
            except Exception as e:
                log.warning("[Scraper] Metrocuadrado %s: %s", ciudad_nombre, e)
                continue

            pagina = 1
            while guardados < MAX_POR_PORTAL:
                cards = page.query_selector_all(
                    ".result-item, .listing-item, [class*='cardResult'], "
                    "[class*='listing-card']"
                )
                if not cards:
                    break

                for card in cards:
                    try:
                        a = card.query_selector("a[href]")
                        if not a:
                            continue
                        href = a.get_attribute("href")
                        if href and not href.startswith("http"):
                            href = "https://www.metrocuadrado.com" + href

                        page.goto(href, timeout=20_000)
                        page.wait_for_load_state("networkidle")

                        tel = _extraer_tel_comun(page)
                        if not tel:
                            page.go_back()
                            continue

                        precio_txt = page.inner_text(
                            ".price, .precio, [data-testid='price'], [class*='price']"
                        ) or ""
                        precio = int(re.sub(r"\D", "", precio_txt)) if re.search(r"\d{5,}", precio_txt) else 0

                        tipo_txt = (
                            page.inner_text("[class*='type'], [class*='tipo'], h1") or ""
                        ).lower()
                        tipo = next((t for t in TIPOS_VALIDOS if t in tipo_txt), "apartamento")

                        estrato_txt = page.inner_text(
                            "[class*='estrato'], [data-id='estrato']"
                        ) or ""
                        m = re.search(r"\d", estrato_txt)
                        estrato = int(m.group()) if m else None

                        dir_txt = page.inner_text(
                            "[class*='address'], [class*='ubicacion'], [itemprop='address']"
                        ) or ""

                        fotos = [
                            el.get_attribute("src")
                            for el in page.query_selector_all(
                                ".carousel img, .gallery img, picture img"
                            )
                            if (el.get_attribute("src") or "").startswith("http")
                        ][:3]

                        _guardar({
                            "telefono": tel,
                            "direccion": dir_txt.strip(),
                            "ciudad": ciudad_nombre,
                            "tipo": tipo,
                            "precio": precio,
                            "estrato": estrato,
                            "fotos": fotos,
                            "url": href,
                            "portal": "metrocuadrado",
                            "fecha_publicacion": str(date.today()),
                        })
                        guardados += 1
                        page.go_back()
                        time.sleep(4)
                    except Exception as e:
                        log.debug("[Scraper] Metrocuadrado listing: %s", e)
                        try:
                            page.go_back()
                        except Exception:
                            pass

                # Siguiente página
                next_btn = page.query_selector(
                    "a[aria-label='Siguiente'], button.next, [class*='next-page']"
                )
                if not next_btn:
                    break
                next_btn.click()
                page.wait_for_load_state("networkidle")
                pagina += 1
                time.sleep(3)

    finally:
        page.close()

    log.info("[Scraper] Metrocuadrado: %d guardados.", guardados)
    return guardados


# ─────────────────────────────────────────────────────────────────────────────
# Finca Raíz
# ─────────────────────────────────────────────────────────────────────────────

def scrape_fincaraiz(browser) -> int:
    guardados = 0
    page = browser.new_page()
    try:
        for ciudad_key, ciudad_nombre in CIUDADES_SURETI.items():
            ciudad_slug = ciudad_key.replace(" ", "-")
            # tipoAnunciante=particular en Finca Raíz
            url = (
                f"https://www.fincaraiz.com.co/venta/inmuebles/{ciudad_slug}/"
                f"?tipoAnunciante=particular"
            )
            try:
                page.goto(url, timeout=30_000)
                page.wait_for_load_state("networkidle")
            except Exception as e:
                log.warning("[Scraper] FincaRaiz %s: %s", ciudad_nombre, e)
                continue

            pagina = 1
            while guardados < MAX_POR_PORTAL:
                cards = page.query_selector_all(
                    ".listing-card, .result-card, [class*='listing-item'], "
                    ".listing-real-state"
                )
                if not cards:
                    break

                for card in cards:
                    try:
                        a = card.query_selector("a[href]")
                        if not a:
                            continue
                        href = a.get_attribute("href") or ""
                        if href and not href.startswith("http"):
                            href = "https://www.fincaraiz.com.co" + href

                        page.goto(href, timeout=20_000)
                        page.wait_for_load_state("networkidle")

                        tel = _extraer_tel_comun(page)
                        if not tel:
                            page.go_back()
                            continue

                        precio_txt = page.inner_text(
                            ".price, [class*='price'], .valor"
                        ) or ""
                        precio = int(re.sub(r"\D", "", precio_txt)) if re.search(r"\d{5,}", precio_txt) else 0

                        tipo_txt = (
                            page.inner_text("[class*='type'], .tipo-inmueble, h1") or ""
                        ).lower()
                        tipo = next((t for t in TIPOS_VALIDOS if t in tipo_txt), "apartamento")

                        estrato_txt = page.inner_text(".estrato, [class*='estrato']") or ""
                        m = re.search(r"\d", estrato_txt)
                        estrato = int(m.group()) if m else None

                        dir_txt = page.inner_text(
                            ".address, [class*='address'], .ubicacion"
                        ) or ""

                        fotos = [
                            el.get_attribute("src")
                            for el in page.query_selector_all(".gallery img, .foto img")
                            if (el.get_attribute("src") or "").startswith("http")
                        ][:3]

                        _guardar({
                            "telefono": tel,
                            "direccion": dir_txt.strip(),
                            "ciudad": ciudad_nombre,
                            "tipo": tipo,
                            "precio": precio,
                            "estrato": estrato,
                            "fotos": fotos,
                            "url": href,
                            "portal": "fincaraiz",
                            "fecha_publicacion": str(date.today()),
                        })
                        guardados += 1
                        page.go_back()
                        time.sleep(4)
                    except Exception as e:
                        log.debug("[Scraper] FincaRaiz listing: %s", e)
                        try:
                            page.go_back()
                        except Exception:
                            pass

                next_btn = page.query_selector(
                    "a[title='Siguiente'], .pagination-next, [class*='next-page']"
                )
                if not next_btn:
                    break
                next_btn.click()
                page.wait_for_load_state("networkidle")
                pagina += 1
                time.sleep(3)

    finally:
        page.close()

    log.info("[Scraper] FincaRaiz: %d guardados.", guardados)
    return guardados


# ─────────────────────────────────────────────────────────────────────────────
# Ciencuadras
# ─────────────────────────────────────────────────────────────────────────────

def scrape_ciencuadras(browser) -> int:
    guardados = 0
    page = browser.new_page()
    try:
        for ciudad_key, ciudad_nombre in CIUDADES_SURETI.items():
            ciudad_slug = ciudad_key.replace(" ", "-")
            url = f"https://www.ciencuadras.com/venta/{ciudad_slug}"
            try:
                page.goto(url, timeout=30_000)
                page.wait_for_load_state("networkidle")
            except Exception as e:
                log.warning("[Scraper] Ciencuadras %s: %s", ciudad_nombre, e)
                continue

            cards = page.query_selector_all(
                ".property-card, .listing-card, [class*='property']"
            )
            for card in cards[:MAX_POR_PORTAL]:
                try:
                    a = card.query_selector("a[href]")
                    if not a:
                        continue
                    href = a.get_attribute("href") or ""
                    if href and not href.startswith("http"):
                        href = "https://www.ciencuadras.com" + href

                    page.goto(href, timeout=20_000)
                    page.wait_for_load_state("networkidle")

                    tel = _extraer_tel_comun(page)
                    if not tel:
                        page.go_back()
                        continue

                    precio_txt = page.inner_text(".price, .valor, [class*='price']") or ""
                    precio = int(re.sub(r"\D", "", precio_txt)) if re.search(r"\d{5,}", precio_txt) else 0

                    tipo_txt = (page.inner_text("h1, [class*='type']") or "").lower()
                    tipo = next((t for t in TIPOS_VALIDOS if t in tipo_txt), "apartamento")

                    estrato_txt = page.inner_text(".estrato, [class*='estrato']") or ""
                    m = re.search(r"\d", estrato_txt)
                    estrato = int(m.group()) if m else None

                    dir_txt = page.inner_text(".address, .ubicacion") or ""

                    fotos = [
                        el.get_attribute("src")
                        for el in page.query_selector_all(".gallery img")
                        if (el.get_attribute("src") or "").startswith("http")
                    ][:3]

                    _guardar({
                        "telefono": tel,
                        "direccion": dir_txt.strip(),
                        "ciudad": ciudad_nombre,
                        "tipo": tipo,
                        "precio": precio,
                        "estrato": estrato,
                        "fotos": fotos,
                        "url": href,
                        "portal": "ciencuadras",
                        "fecha_publicacion": str(date.today()),
                    })
                    guardados += 1
                    page.go_back()
                    time.sleep(4)
                except Exception as e:
                    log.debug("[Scraper] Ciencuadras listing: %s", e)
                    try:
                        page.go_back()
                    except Exception:
                        pass

    finally:
        page.close()

    log.info("[Scraper] Ciencuadras: %d guardados.", guardados)
    return guardados


# ─────────────────────────────────────────────────────────────────────────────
# Punto de entrada
# ─────────────────────────────────────────────────────────────────────────────

def correr_todos():
    """Corre todos los scrapers en secuencia. Llamado por scheduler.py."""
    if not SCRAPER_ENABLED:
        log.info("[Scraper] SCRAPER_ENABLED=false — omitiendo.")
        return

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("[Scraper] playwright no instalado. Ejecutar: playwright install chromium")
        return

    total = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            total += scrape_propdirecto(browser)
            time.sleep(5)
            total += scrape_metrocuadrado(browser)
            time.sleep(5)
            total += scrape_fincaraiz(browser)
            time.sleep(5)
            total += scrape_ciencuadras(browser)
        finally:
            browser.close()

    log.info("[Scraper] Ronda completa: %d nuevos contactos.", total)
    return total


def iniciar():
    """Arranca el scraper (llamado desde scheduler.py)."""
    pass  # La ejecución real la maneja APScheduler
