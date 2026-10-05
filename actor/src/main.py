"""Actor Apify — Petra Inmuebles Scraper (sin SDK apify).

Lee el input desde ACTOR_INPUT_BODY (inyectado por Apify) o un archivo JSON.
Envía contactos al endpoint /scraper/ingest del bot Petra.
"""
import asyncio
import base64
import json
import logging
import os
import re
import unicodedata
from typing import Optional

import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
    async_playwright,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("petra-actor")

# ── Ciudades y slugs por portal ───────────────────────────────────────────────

_CIUDADES_MQ = {
    "bogota": "bogota", "medellin": "medellin", "barranquilla": "barranquilla",
    "cartagena": "cartagena", "santa marta": "santa-marta",
    "cucuta": "cucuta", "chia": "chia",
}
_CIUDADES_FR = {
    "bogota": "bogota-dc",
    "medellin": "medellin/antioquia",
    "barranquilla": "barranquilla/atlantico",
    "cartagena": "cartagena/bolivar",
    "santa marta": "santa-marta/magdalena",
    "cucuta": "cucuta/norte-de-santander",
    "chia": "chia/cundinamarca",
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


def _decrypt_apify_secret(encrypted: str) -> Optional[str]:
    """Desencripta ENCRYPTED_VALUE:base64(RSA_key):base64(nonce+ciphertext+tag) con la clave privada."""
    # Apify inyecta la clave privada RSA bajo APIFY_ACTOR_INPUT_PRIVATE_KEY
    private_key_pem = os.environ.get("APIFY_ACTOR_INPUT_PRIVATE_KEY", "")
    if not private_key_pem or not encrypted.startswith("ENCRYPTED_VALUE:"):
        return None
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding as apadding
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        parts = encrypted.split(":")
        if len(parts) < 3:
            return None
        # parts[1] = RSA-OAEP(SHA256) encrypted AES key
        # parts[2] = AES-256-GCM: nonce(12) + ciphertext + tag(16)
        enc_key_rsa = base64.b64decode(parts[1])
        enc_value_aes = base64.b64decode(parts[2])

        priv = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
        aes_key = priv.decrypt(enc_key_rsa, apadding.OAEP(
            mgf=apadding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(), label=None,
        ))
        nonce = enc_value_aes[:12]
        ciphertext_with_tag = enc_value_aes[12:]
        return AESGCM(aes_key).decrypt(nonce, ciphertext_with_tag, None).decode("utf-8")
    except Exception as e:
        log.warning("No se pudo desencriptar campo secret: %s", e)
        return None


def _leer_input() -> dict:
    """Lee el input del actor desde el key-value store de Apify (cloud) o archivo (local)."""
    # 1. apify-client: forma canónica en cloud runs
    token = os.environ.get("APIFY_TOKEN")
    kv_id = os.environ.get("APIFY_DEFAULT_KEY_VALUE_STORE_ID")
    if token and kv_id:
        try:
            from apify_client import ApifyClient
            record = ApifyClient(token).key_value_store(kv_id).get_record(
                os.environ.get("APIFY_INPUT_KEY", "INPUT")
            )
            if record and isinstance(record.get("value"), dict):
                data = record["value"]
                # Desencriptar campos ENCRYPTED_VALUE si el build los cifró
                for k, v in list(data.items()):
                    if isinstance(v, str) and v.startswith("ENCRYPTED_VALUE:"):
                        decrypted = _decrypt_apify_secret(v)
                        if decrypted is not None:
                            data[k] = decrypted
                        else:
                            log.warning("Campo '%s' cifrado pero no se pudo desencriptar. Quita isSecret del input schema.", k)
                            del data[k]
                return data
        except Exception as e:
            log.warning("No se pudo leer input via apify-client: %s", e)

    # 2. ACTOR_INPUT_BODY (algunos runtimes locales lo inyectan)
    raw = os.environ.get("ACTOR_INPUT_BODY", "")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass

    # 3. archivo INPUT.json en storage local
    storage_dir = os.environ.get("APIFY_LOCAL_STORAGE_DIR", "/root/apify_storage")
    input_path = os.path.join(storage_dir, "key_value_stores", "default", "INPUT.json")
    if os.path.exists(input_path):
        with open(input_path) as f:
            return json.load(f)

    return {}


# ── Utilidades ────────────────────────────────────────────────────────────────

def _sin_tildes(texto: str) -> str:
    texto = unicodedata.normalize("NFD", str(texto or "").lower())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.sub(r"[-_/]+", " ", texto)


def _extraer_tel(texto: str) -> Optional[str]:
    blob = re.sub(r"[\s\-]", "", texto)
    for m in re.finditer(r"(?:57)?3\d{9}", blob):
        return m.group(0) if m.group(0).startswith("57") else "57" + m.group(0)
    return None


def _buscar_tel_json(obj, depth: int = 0) -> Optional[str]:
    """Búsqueda recursiva de teléfonos colombianos en un objeto JSON."""
    if depth > 12:
        return None
    if isinstance(obj, str):
        d = re.sub(r"\D", "", obj)
        if len(d) == 10 and d.startswith("3"):
            return "57" + d
        if len(d) == 12 and d.startswith("573"):
            return d
    elif isinstance(obj, dict):
        # Claves prioritarias relacionadas con teléfono
        for key in ("phone", "telefono", "celular", "movil", "mobile", "whatsapp",
                    "contactPhone", "phoneNumber", "phoneNumbers", "phones",
                    "cellphone", "phone1", "phone2", "advertiserPhone", "contacto"):
            if key in obj:
                result = _buscar_tel_json(obj[key], depth + 1)
                if result:
                    return result
        for v in obj.values():
            result = _buscar_tel_json(v, depth + 1)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = _buscar_tel_json(item, depth + 1)
            if result:
                return result
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
    # 0. __NEXT_DATA__ — búsqueda recursiva en el objeto completo
    try:
        raw = await page.evaluate("() => JSON.stringify(window.__NEXT_DATA__ || null)")
        if raw and raw != "null":
            try:
                nd_obj = json.loads(raw)
                tel = _buscar_tel_json(nd_obj)
                if tel:
                    return tel
            except Exception:
                pass
            # Fallback: regex directo en texto plano
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

    # 4. URIs capturadas por window.open
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
        except Exception:
            pass

    tipo_raw = titulo_limpio or nombre or url

    return {
        "nombre": nombre, "precio_raw": precio_raw, "estrato_raw": "",
        "tipo_raw": tipo_raw, "ciudad_raw": ciudad_hint or url,
        "direccion": None, "barrio": None, "url": url,
    }


# ── Scraper genérico ──────────────────────────────────────────────────────────

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
    proxy_pw_cfg: Optional[dict] = None,
) -> int:
    saved = total = 0
    ctx_kwargs = {
        "user_agent": _UA,
        "viewport": {"width": 1366, "height": 768},
        "locale": "es-CO",
    }
    if proxy_pw_cfg:
        ctx_kwargs["proxy"] = proxy_pw_cfg
    elif proxy_url:
        ctx_kwargs["proxy"] = {"server": proxy_url}

    for ciudad_key, ciudad_slug in ciudades.items():
        if saved >= max_results:
            break
        log.info("[%s] Ciudad: %s", portal, ciudad_key)
        page_n = 1

        while saved < max_results:
            url = base_url.format(ciudad=ciudad_slug, page=page_n)
            ctx = await browser.new_context(**ctx_kwargs)
            try:
                lp = await _nueva_pagina(ctx, url)
                if any(p in lp.url for p in ("/login", "/registro")):
                    log.warning("[%s] Muro de login en pág %d — siguiente ciudad.", portal, page_n)
                    await ctx.close()
                    break
                if page_n > 1 and lp.url.rstrip("/") in (host, host + "/"):
                    await ctx.close()
                    break

                try:
                    await lp.wait_for_selector(listing_sel, timeout=30_000)
                except PWTimeout:
                    try:
                        title = await lp.title()
                        n_match = await lp.eval_on_selector_all(
                            listing_sel, "els => els.length"
                        )
                        n_any = await lp.eval_on_selector_all(
                            "a[href]", "els => els.length"
                        )
                        sample = await lp.evaluate(
                            """() => Array.from(document.querySelectorAll('a[href]'))
                                .map(a => a.getAttribute('href'))
                                .filter(h => h && !h.startsWith('#') && !h.startsWith('mailto') && !h.startsWith('tel') && h.length > 5)
                                .slice(0, 15)"""
                        )
                        log.warning(
                            "[%s] Timeout pág %d | title=%r | links_match=%d | links_any=%d | url=%s | sample=%s",
                            portal, page_n, title, n_match, n_any, lp.url, sample,
                        )
                    except Exception:
                        log.warning("[%s] Timeout pág %d — %s", portal, page_n, lp.url)
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

                log.info("[%s] Ciudad=%s pág %d: %d links", portal, ciudad_key, page_n, len(links))

                for href in links:
                    if saved >= max_results:
                        break
                    # Saltar proyectos de constructora (sin teléfono directo)
                    if any(p in href for p in ("/proyecto", "/vivienda-nueva", "/proyectos-vivienda")):
                        log.info("[%s] Saltando proyecto: %s", portal, href[-60:])
                        continue
                    total += 1
                    log.info("[%s] Detalle %d: %s", portal, total, href[-70:])
                    detail_ctx = await browser.new_context(**ctx_kwargs)
                    try:
                        dp = await _nueva_pagina(detail_ctx, href)
                        if any(p in dp.url for p in ("/login", "/registro")):
                            await detail_ctx.close()
                            log.warning("[%s] Detalle redirigió a login — saltando.", portal)
                            continue

                        datos = await extractor(dp, href, ciudad_key)
                        if not datos:
                            log.warning("[%s] extractor=None url=%s title=%r", portal, href[-60:], await dp.title())
                        else:
                            tel = await _extraer_telefono(dp)
                            anunc = await _anunciante(dp)
                            if tel:
                                log.info("[%s] datos OK tel=%s url=%s", portal, tel, href[-60:])
                            else:
                                log.warning("[%s] tel=None url=%s", portal, href[-60:])
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
                                        log.info("[%s] Guardado #%d: %s", portal, saved, href[-60:])
                                    else:
                                        log.info("[%s] Ingest status=%d: %s", portal, r.status_code, r.text[:100])
                                except Exception as exc:
                                    log.warning("[%s] Ingest error: %s", portal, exc)
                    except Exception as exc:
                        log.warning("[%s] Error detalle %s: %s", portal, href[-60:], exc)
                    finally:
                        await detail_ctx.close()
                    await asyncio.sleep(0.5)

                page_n += 1
            except Exception as exc:
                log.warning("[%s] Error pág %d: %s", portal, page_n, exc)
                try:
                    await ctx.close()
                except Exception:
                    pass
                break

    log.info("[%s] %d visitados, %d guardados.", portal, total, saved)
    return saved


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    inp = _leer_input()
    log.info("Input recibido: %s", {k: v for k, v in inp.items() if k != "ingest_token"})

    portals: list = inp.get("portals", ["metrocuadrado", "fincaraiz", "ciencuadras", "propdirecto"])
    max_per: int = int(inp.get("max_per_portal", 300))
    ingest_url: str = inp.get("ingest_url", "")
    ingest_token: str = inp.get("ingest_token", "")

    if not ingest_url or not ingest_token:
        log.error("Faltan ingest_url o ingest_token en el input.")
        return

    # Proxy: APIFY_PROXY_URL (legacy) > proxy_config del input > APIFY_PROXY_PASSWORD auto
    proxy_url: Optional[str] = os.environ.get("APIFY_PROXY_URL") or None
    proxy_pw_cfg: Optional[dict] = None  # formato Playwright {server, username, password}

    if not proxy_url:
        pwd = os.environ.get("APIFY_PROXY_PASSWORD", "")
        host_px = os.environ.get("APIFY_PROXY_HOSTNAME", "proxy.apify.com")
        port_px = os.environ.get("APIFY_PROXY_PORT", "8000")
        if pwd:
            proxy_cfg = inp.get("proxy_config", {})
            groups = proxy_cfg.get("apifyProxyGroups", []) if proxy_cfg.get("useApifyProxy") else []
            group_str = "groups-" + "+".join(groups) if groups else "auto"
            proxy_url = f"http://{group_str}:{pwd}@{host_px}:{port_px}"
            proxy_pw_cfg = {
                "server": f"http://{host_px}:{port_px}",
                "username": group_str,
                "password": pwd,
            }
            log.info("Proxy: server=http://%s:%s username=%s", host_px, port_px, group_str)
    else:
        log.info("Proxy: %s", proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url)

    if not proxy_url:
        log.warning("Sin proxy — los portales probablemente bloquearán requests directos")

    log.info("Portales: %s | max/portal: %d | proxy: %s", portals, max_per, bool(proxy_url))

    portal_config = {
        "metrocuadrado": {
            "base_url": "https://www.metrocuadrado.com/inmuebles/venta/{ciudad}/?tipoAnunciante=particular&page={page}",
            "ciudades": _CIUDADES_MQ,
            "listing_sel": "a[href*='/inmueble/']",
            "host": "https://www.metrocuadrado.com",
            "extractor": _extraer_nextjs,
        },
        "fincaraiz": {
            "base_url": "https://www.fincaraiz.com.co/venta/{ciudad}/?tipoAnunciante=particular&pagina={page}",
            "ciudades": _CIUDADES_FR,
            "listing_sel": "a[href*='-en-venta-en-']",
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
                    log.warning("Portal desconocido: %s", portal)
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
                    proxy_pw_cfg=proxy_pw_cfg,
                )
        await browser.close()

    log.info("Actor finalizado.")


if __name__ == "__main__":
    asyncio.run(main())
