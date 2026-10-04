"""Actor Apify — Petra Inmuebles Scraper.

Scraper de propietarios directos en 4 portales colombianos.
Usa proxies residenciales de Apify para evadir bloqueos.
Los contactos crudos se envían al endpoint /scraper/ingest del bot Petra,
que aplica los filtros de calidad (ciudad, tipo, precio, estrato) y guarda en DB.
"""
import asyncio
import json
import re
import unicodedata
from typing import Optional

import httpx
from apify import Actor
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
    async_playwright,
)

# ── Ciudades y slugs por portal ───────────────────────────────────────────────

_CIUDADES_MQ = {
    "bogota": "bogota", "medellin": "medellin", "barranquilla": "barranquilla",
    "cartagena": "cartagena", "santa marta": "santa-marta",
    "cucuta": "cucuta", "chia": "chia",
}
_CIUDADES_FR = {
    "bogota": "bogota", "medellin": "medellin", "barranquilla": "barranquilla",
    "cartagena": "cartagena", "santa marta": "santa-marta",
    "cucuta": "cucuta", "chia": "chia",
}
_CIUDADES_CC = {
    "bogota": "bogota", "medellin": "medellin", "barranquilla": "barranquilla",
    "cartagena": "cartagena", "santa marta": "santa-marta",
    "cucuta": "cucuta", "chia": "chia",
}
_CIUDADES_PD = {
    "bogota": "Bogota", "medellin": "Medellin", "barranquilla": "Barranquilla",
    "cartagena": "Cartagena", "santa marta": "Santa+Marta",
    "cucuta": "Cucuta", "chia": "Chia",
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ── Utilidades ────────────────────────────────────────────────────────────────

def _sin_tildes(texto: str) -> str:
    texto = unicodedata.normalize("NFD", str(texto or "").lower())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.sub(r"[-_/]+", " ", texto)


def _extraer_tel(texto: str) -> Optional[str]:
    """Extrae primer número celular colombiano del texto."""
    blob = re.sub(r"[\s\-]", "", texto)
    for m in re.finditer(r"(?:57)?3\d{9}", blob):
        return m.group(0) if m.group(0).startswith("57") else "57" + m.group(0)
    return None


async def _nueva_pagina(ctx: BrowserContext, url: str) -> Page:
    page = await ctx.new_page()
    await page.add_init_script("""
        window.__massiBotUris = [];
        const _orig = window.open;
        window.open = function(u, ...a) {
            if (u) window.__massiBotUris.push(String(u));
            try { return _orig && _orig.apply(this, [u, ...a]); } catch(e){}
        };
    """)
    await page.goto(url, timeout=30_000, wait_until="domcontentloaded")
    return page


def _next_data(raw_json: str) -> dict:
    """Extrae el primer objeto relevante de window.__NEXT_DATA__."""
    try:
        data = json.loads(raw_json)
        pp = data.get("props", {}).get("pageProps", {})
        for key in ("realEstate", "listing", "inmueble", "property"):
            if key in pp and isinstance(pp[key], dict):
                return pp[key]
        for v in pp.values():
            if isinstance(v, dict) and any(
                k in v for k in ("price", "precio", "valorVenta", "canonicalUrl")
            ):
                return v
    except Exception:
        pass
    return {}


async def _extraer_telefono(page: Page) -> Optional[str]:
    """Intenta obtener teléfono del propietario por múltiples estrategias."""
    phones_xhr: list[str] = []

    def _on_response(response):
        try:
            if response.status not in (200, 201):
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct and "text" not in ct:
                return
        except Exception:
            pass

    page.on("response", _on_response)

    # 0. __NEXT_DATA__ — teléfono en SSR
    try:
        raw = await page.evaluate("() => JSON.stringify(window.__NEXT_DATA__ || null)")
        if raw and raw != "null":
            for m in re.finditer(
                r'"(?:phone|telefono|celular|movil|mobile|whatsapp|contactPhone)[^"]*"\s*:\s*"([^"]{8,16})"',
                raw, re.IGNORECASE,
            ):
                d = re.sub(r"\D", "", m.group(1))
                if len(d) == 10 and d.startswith("3"):
                    return "57" + d
                if len(d) == 12 and d.startswith("573"):
                    return d
            tel = _extraer_tel(raw)
            if tel:
                return tel
    except Exception:
        pass

    # 1. JSON-LD
    try:
        for script in await page.query_selector_all("script[type='application/ld+json']"):
            content = await script.inner_text()
            m = re.search(r'"telephone"\s*:\s*"([^"]+)"', content)
            if m:
                d = re.sub(r"\D", "", m.group(1))
                if 10 <= len(d) <= 13:
                    return d
    except Exception:
        pass

    # 2. Click "Ver teléfono"
    for sel in [
        "button:has-text('Ver teléfono')", "button:has-text('Mostrar teléfono')",
        "button:has-text('Llamar')", "a:has-text('Ver teléfono')",
    ]:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                await page.wait_for_timeout(2_000)
                break
        except Exception:
            pass

    # 3. Click "Contactar"/"WhatsApp"
    for sel in [
        "button:has-text('Contactar')", "a:has-text('Contactar')",
        "button:has-text('WhatsApp')", "a:has-text('WhatsApp')",
        "[class*='whatsapp' i] button", "[class*='contact' i] button",
    ]:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                await page.wait_for_timeout(2_000)
                break
        except Exception:
            pass

    # 4. URIs capturadas por window.open (wa.me, whatsapp://)
    try:
        uris = await page.evaluate("() => window.__massiBotUris || []")
        for uri in uris:
            m = re.search(r"(?:phone[=%/]|wa\.me/)\+?(\d{10,15})", uri)
            if m:
                d = m.group(1)
                return d if d.startswith("57") else "57" + d
    except Exception:
        pass

    # 5. href="tel:..."
    try:
        for el in await page.query_selector_all("a[href^='tel:']"):
            href = await el.get_attribute("href") or ""
            d = re.sub(r"\D", "", href)
            if len(d) >= 10:
                return d if d.startswith("57") else "57" + d
    except Exception:
        pass

    # 6. Escanear body text
    try:
        body = await page.inner_text("body")
        tel = _extraer_tel(body)
        if tel:
            return tel
    except Exception:
        pass

    return None


async def _anunciante(page: Page) -> dict:
    nombre, num_pub = "", 0
    for sel in [
        "[class*='advertiser'] [class*='name']", "[data-testid='advertiser-name']",
        "[class*='agent-name']", "[class*='contact-name']",
    ]:
        try:
            el = await page.query_selector(sel)
            if el:
                nombre = (await el.inner_text()).strip()
                break
        except Exception:
            pass
    for sel in [
        "[class*='advertiser'] [class*='count']", "[data-testid='listing-count']",
        "[class*='active-listings']",
    ]:
        try:
            el = await page.query_selector(sel)
            if el:
                t = (await el.inner_text()).strip()
                d = re.sub(r"\D", "", t)
                if d:
                    num_pub = int(d)
                    break
        except Exception:
            pass
    return {"nombre": nombre, "num_publicaciones": num_pub}


# ── Extractor genérico Next.js (MQ / FR / CC) ────────────────────────────────

async def _extraer_nextjs(page: Page, url: str, ciudad_hint: str) -> Optional[dict]:
    try:
        await page.wait_for_selector("h1, main", timeout=10_000)
    except PWTimeout:
        return None

    nd: dict = {}
    try:
        raw = await page.evaluate("() => JSON.stringify(window.__NEXT_DATA__ || null)")
        if raw and raw != "null":
            nd = _next_data(raw)
    except Exception:
        pass

    loc = nd.get("location") or nd.get("ubicacion") or {}
    if not isinstance(loc, dict):
        loc = {}

    precio_raw = str(nd.get("price") or nd.get("precio") or nd.get("valorVenta") or "")
    tipo_raw = str(nd.get("propertyType") or nd.get("tipoInmueble") or nd.get("tipo") or "")
    estrato_raw = str(nd.get("stratum") or nd.get("estrato") or "")
    nombre = str(nd.get("title") or nd.get("titulo") or "") or None
    ciudad_raw = str(loc.get("city") or loc.get("ciudad") or "") or ciudad_hint or url
    barrio = str(loc.get("neighborhood") or loc.get("barrio") or "") or None
    direccion = str(loc.get("address") or loc.get("direccion") or "") or None

    # CSS fallbacks
    if not precio_raw:
        for sel in ["[data-testid='price']", "[class*='price']", "[class*='precio']"]:
            try:
                el = await page.query_selector(sel)
                if el:
                    precio_raw = (await el.inner_text()).strip()
                    break
            except Exception:
                pass
    if not nombre:
        try:
            el = await page.query_selector("h1")
            if el:
                nombre = (await el.inner_text()).strip() or None
        except Exception:
            pass
    if not tipo_raw:
        tipo_raw = (await page.title()) or url

    return {
        "nombre": nombre, "precio_raw": precio_raw, "estrato_raw": estrato_raw,
        "tipo_raw": tipo_raw, "ciudad_raw": ciudad_raw,
        "direccion": direccion, "barrio": barrio, "url": url,
    }


# ── Extractor PropDirecto ─────────────────────────────────────────────────────

async def _extraer_pd(page: Page, url: str, ciudad_hint: str) -> Optional[dict]:
    try:
        await page.wait_for_selector("h1, main, [class*='price']", timeout=8_000)
    except PWTimeout:
        return None

    titulo = await page.title() or ""
    titulo_limpio = re.sub(r'\s*\|\s*PropDirecto.*', '', titulo, flags=re.I).strip()
    titulo_limpio = re.sub(r'[\U00010000-\U0010ffff]', '', titulo_limpio).strip()

    nombre = None
    try:
        el = await page.query_selector("h1")
        if el:
            nombre = (await el.inner_text()).strip() or None
    except Exception:
        pass

    precio_raw = ""
    for sel in ["[class*='price']", "[class*='precio']", "[class*='valor']"]:
        try:
            el = await page.query_selector(sel)
            if el:
                precio_raw = (await el.inner_text()).strip()
                break
        except Exception:
            pass

    if not precio_raw:
        texto = titulo_limpio + " " + (nombre or "")
        m = re.search(r'\$\s*([\d.,]+)\s*millones?', texto, re.I)
        if m:
            d = re.sub(r"[.,\s]", "", m.group(1))
            precio_raw = str(int(d) * 1_000_000) if d.isdigit() else ""
        else:
            m = re.search(r'\$\s*([\d.,]{6,})', texto)
            if m:
                precio_raw = re.sub(r"[.,]", "", m.group(1))

    if not precio_raw:
        try:
            body = await page.inner_text("body")
            m = re.search(r'\$\s*([\d.,]+)\s*millones?', body, re.I)
            if m:
                d = re.sub(r"[.,\s]", "", m.group(1))
                precio_raw = str(int(d) * 1_000_000) if d.isdigit() else ""
            else:
                m = re.search(r'\$\s*([\d.,]{6,})', body)
                if m:
                    precio_raw = re.sub(r"[.,]", "", m.group(1))
                else:
                    m = re.search(r'\b(1\d{2}|[2-9]\d{2}|\d{4})[.,](\d{3})[.,](\d{3})\b', body)
                    if m:
                        precio_raw = m.group(1) + m.group(2) + m.group(3)
        except Exception:
            pass

    tipo_raw = titulo_limpio or nombre or url
    if not tipo_raw or not any(
        kw in _sin_tildes(tipo_raw)
        for kw in ("apartamento", "apto", "casa", "oficina", "bodega", "local", "lote", "finca")
    ):
        try:
            body_snippet = await page.inner_text("body")
            tipo_raw = tipo_raw + " " + body_snippet[:3000]
        except Exception:
            pass

    return {
        "nombre": nombre, "precio_raw": precio_raw, "estrato_raw": "",
        "tipo_raw": tipo_raw, "ciudad_raw": ciudad_hint or url,
        "direccion": None, "barrio": None, "url": url,
    }


# ── Scraper genérico con paginación ──────────────────────────────────────────

async def _scrape_portal(
    browser: Browser,
    portal: str,
    base_url: str,
    ciudades: dict,
    listing_sel: str,
    host: str,
    extractor,
    max_results: int,
    proxy_url: Optional[str],
    ingest_url: str,
    ingest_token: str,
    http: httpx.AsyncClient,
) -> int:
    saved = total = 0
    ctx_kwargs = {
        "user_agent": _UA,
        "viewport": {"width": 1366, "height": 768},
        "locale": "es-CO",
    }
    if proxy_url:
        ctx_kwargs["proxy"] = {"server": proxy_url}

    for ciudad_key, ciudad_slug in ciudades.items():
        if saved >= max_results:
            break
        Actor.log.info("[%s] Ciudad: %s", portal, ciudad_key)
        page_n = 1

        while saved < max_results:
            url = base_url.format(ciudad=ciudad_slug, page=page_n)
            ctx = await browser.new_context(**ctx_kwargs)
            try:
                lp = await _nueva_pagina(ctx, url)
                # Detect login/registration wall
                if any(p in lp.url for p in ("/login", "/registro")):
                    Actor.log.warning("[%s] Muro de login en pág %d — siguiente ciudad.", portal, page_n)
                    await ctx.close()
                    break
                if page_n > 1 and lp.url.rstrip("/") in (host, host + "/"):
                    await ctx.close()
                    break

                try:
                    await lp.wait_for_selector(listing_sel, timeout=15_000)
                except PWTimeout:
                    Actor.log.warning("[%s] Timeout pág %d — %s", portal, page_n, lp.url)
                    await ctx.close()
                    break

                links: list[str] = []
                for el in await lp.query_selector_all(listing_sel):
                    href = await el.get_attribute("href") or ""
                    if not href:
                        continue
                    if not href.startswith("http"):
                        href = host.rstrip("/") + "/" + href.lstrip("/")
                    if host.split("//")[1].split("/")[0] in href and href not in links:
                        links.append(href)

                await ctx.close()
                if not links:
                    break

                Actor.log.info("[%s] Ciudad=%s pág %d: %d links", portal, ciudad_key, page_n, len(links))

                for href in links:
                    if saved >= max_results:
                        break
                    total += 1
                    detail_ctx = await browser.new_context(**ctx_kwargs)
                    try:
                        dp = await _nueva_pagina(detail_ctx, href)
                        if any(p in dp.url for p in ("/login", "/registro")):
                            await detail_ctx.close()
                            Actor.log.warning("[%s] Detalle redirigió a login — saltando.", portal)
                            continue

                        datos = await extractor(dp, href, ciudad_key)
                        if datos:
                            tel = await _extraer_telefono(dp)
                            anunc = await _anunciante(dp)
                            if tel:
                                payload = {
                                    "portal": portal,
                                    "telefono": tel,
                                    "anunciante": anunc,
                                    **datos,
                                }
                                try:
                                    r = await http.post(
                                        ingest_url,
                                        json=payload,
                                        headers={"X-Ingest-Token": ingest_token},
                                        timeout=10,
                                    )
                                    if r.status_code == 200 and r.json().get("saved"):
                                        saved += 1
                                        Actor.log.info("[%s] Guardado #%d: %s", portal, saved, href[-60:])
                                except Exception as exc:
                                    Actor.log.warning("[%s] Ingest error: %s", portal, exc)
                    except Exception as exc:
                        Actor.log.debug("[%s] Error %s: %s", portal, href, exc)
                    finally:
                        await detail_ctx.close()
                    await asyncio.sleep(2)

                page_n += 1
            except Exception as exc:
                Actor.log.warning("[%s] Error pág %d: %s", portal, page_n, exc)
                try:
                    await ctx.close()
                except Exception:
                    pass
                break

    Actor.log.info("[%s] %d visitados, %d guardados.", portal, total, saved)
    return saved


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    async with Actor:
        inp = await Actor.get_input() or {}

        portals: list = inp.get("portals", ["metrocuadrado", "fincaraiz", "ciencuadras", "propdirecto"])
        max_per: int = int(inp.get("max_per_portal", 300))
        ingest_url: str = inp["ingest_url"]
        ingest_token: str = inp["ingest_token"]
        proxy_cfg: dict = inp.get("proxy_config", {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]})

        # Resolver proxy URL de Apify
        proxy_url: Optional[str] = None
        if proxy_cfg.get("useApifyProxy"):
            proxy_url = Actor.create_proxy_configuration(
                groups=proxy_cfg.get("apifyProxyGroups", []),
                country_code=proxy_cfg.get("apifyProxyCountry"),
            )
            if proxy_url:
                proxy_url = await proxy_url.new_url()
        elif proxy_cfg.get("proxyUrls"):
            proxy_url = proxy_cfg["proxyUrls"][0]

        Actor.log.info("Portales: %s | max/portal: %d | proxy: %s", portals, max_per, bool(proxy_url))

        portal_config = {
            "metrocuadrado": {
                "base_url": "https://www.metrocuadrado.com/inmuebles/venta/{ciudad}/?tipoAnunciante=particular&page={page}",
                "ciudades": _CIUDADES_MQ,
                "listing_sel": "a[href*='/inmueble/']",
                "host": "https://www.metrocuadrado.com",
                "extractor": _extraer_nextjs,
            },
            "fincaraiz": {
                "base_url": "https://www.fincaraiz.com.co/venta/inmuebles/{ciudad}/?tipoAnunciante=particular&pagina={page}",
                "ciudades": _CIUDADES_FR,
                "listing_sel": "a[href*='/inmueble/'], a[href*='.htm']",
                "host": "https://www.fincaraiz.com.co",
                "extractor": _extraer_nextjs,
            },
            "ciencuadras": {
                "base_url": "https://www.ciencuadras.com/venta/{ciudad}?tipoAnunciante=particular&pagina={page}",
                "ciudades": _CIUDADES_CC,
                "listing_sel": "a[href*='/inmueble/'], a[href*='/propiedad/']",
                "host": "https://www.ciencuadras.com",
                "extractor": _extraer_nextjs,
            },
            "propdirecto": {
                "base_url": "https://propdirecto.com/propiedades.php?ciudad={ciudad}&pagina={page}",
                "ciudades": _CIUDADES_PD,
                "listing_sel": "a[href*='detalle.php'], a[href*='/inmueble/']",
                "host": "https://propdirecto.com",
                "extractor": _extraer_pd,
            },
        }

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            async with httpx.AsyncClient() as http:
                for portal in portals:
                    cfg = portal_config.get(portal)
                    if not cfg:
                        Actor.log.warning("Portal desconocido: %s", portal)
                        continue
                    await _scrape_portal(
                        browser=browser,
                        portal=portal,
                        base_url=cfg["base_url"],
                        ciudades=cfg["ciudades"],
                        listing_sel=cfg["listing_sel"],
                        host=cfg["host"],
                        extractor=cfg["extractor"],
                        max_results=max_per,
                        proxy_url=proxy_url,
                        ingest_url=ingest_url,
                        ingest_token=ingest_token,
                        http=http,
                    )
            await browser.close()

    Actor.log.info("Actor finalizado.")


if __name__ == "__main__":
    asyncio.run(main())
