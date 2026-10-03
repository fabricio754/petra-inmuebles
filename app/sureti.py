"""Automatización del portal de agentes de Sureti (agentes.sureti.co).

Abre una sesión Playwright, inicia sesión con las credenciales configuradas
y expone dos operaciones:
  - registrar_lead(data, docs) → sureti_lead_id (str)
  - consultar_estado(sureti_lead_id) → dict con "estado" y opcionalmente
    "monto_aprobado" o "razon"

Si no hay credenciales (SURETI_EMAIL / SURETI_PASSWORD), el módulo opera en
modo DRY-RUN: registrar_lead devuelve un ID ficticio y consultar_estado
siempre devuelve {"estado": "en_estudio"}.
"""
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("petra")

SURETI_URL  = "https://agentes.sureti.co"
SURETI_EMAIL    = os.environ.get("SURETI_EMAIL", "")
SURETI_PASSWORD = os.environ.get("SURETI_PASSWORD", "")

DRY_RUN = not (SURETI_EMAIL and SURETI_PASSWORD)


class SuretiSession:
    """Contexto Playwright para el portal de Sureti.

    Uso recomendado:
        with SuretiSession() as s:
            lead_id = s.registrar_lead(data, docs)
    """

    def __init__(self):
        self._pw  = None
        self._browser = None
        self._page    = None

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    def __enter__(self):
        self._iniciar()
        return self

    def __exit__(self, *_):
        self.cerrar()

    def _iniciar(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_PATH") or None,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        self._page = self._browser.new_page()
        self._page.set_extra_http_headers({"Accept-Language": "es-CO,es;q=0.9"})
        self._login()

    def cerrar(self):
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    def _login(self):
        page = self._page
        page.goto(f"{SURETI_URL}/auth", wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_selector("input[name='email']", timeout=15_000)
        page.fill("input[name='email']", SURETI_EMAIL)
        page.fill("input[type='password']", SURETI_PASSWORD)
        page.click("button[type='submit']")
        page.wait_for_load_state("networkidle", timeout=20_000)
        if "/auth" in page.url:
            raise RuntimeError("Login en Sureti falló — verifica SURETI_EMAIL y SURETI_PASSWORD.")
        log.info("[Sureti] Sesión iniciada.")

    # ------------------------------------------------------------------
    # Operaciones de negocio
    # ------------------------------------------------------------------

    def registrar_lead(self, data: dict, docs: list[dict] | None = None) -> str:
        """Llena el formulario 'Nuevo Lead' y devuelve el sureti_lead_id."""
        page = self._page
        page.goto(f"{SURETI_URL}/nuevo-lead", wait_until="networkidle", timeout=30_000)

        # ---- Datos del propietario ----
        _fill(page, "[name='nombre'], #nombre", data.get("nombre", ""))
        _fill(page, "[name='cedula'], #cedula", str(data.get("cedula", "")))
        _fill(page, "[name='email'], #email", data.get("email", ""))
        # Celular: solo el número sin prefijo (el formulario ya tiene +57 seleccionado)
        telefono = str(data.get("telefono", "")).lstrip("+").lstrip("57")
        _fill(page, "[name='telefono'], #telefono, [name='celular'], #celular", telefono)

        # ---- Inmueble ----
        _fill(page, "[name='direccion'], #direccion, [name='direccion_inmueble']",
              data.get("direccion_inmueble", ""))
        _fill(page, "[name='ciudad'], #ciudad", data.get("ciudad", "Bogotá"))

        # Matrícula inmobiliaria: dos campos (oficina + número)
        matricula = str(data.get("matricula_numero", ""))
        if matricula and "-" in matricula:
            prefijo, numero = matricula.split("-", 1)
            _select_or_fill(page,
                "[name='oficina_registro'], #oficina_registro, [name='prefijo_matricula']",
                prefijo.strip())
            _fill(page,
                "[name='numero_matricula'], #numero_matricula, [name='matricula_numero']",
                numero.strip())
        elif matricula:
            _fill(page,
                "[name='numero_matricula'], #numero_matricula, [name='matricula_numero'], "
                "[name='matricula'], #matricula",
                matricula)

        # ---- Crédito ----
        valor = data.get("valor_solicitado")
        if valor:
            _fill(page, "[name='valor_solicitado'], #valor_solicitado, [name='monto']",
                  str(valor))

        objetivo = data.get("objetivo_prestamo", "")
        if objetivo:
            _fill(page,
                "[name='objetivo'], #objetivo, [name='objetivo_prestamo'], "
                "[name='comentarios'], #comentarios",
                objetivo)

        # ---- Tipo de persona (siempre Persona Natural) ----
        for sel in ["[value='natural']", "[value='persona_natural']"]:
            try:
                el = page.query_selector(f"input[type='radio']{sel}")
                if el and el.is_visible():
                    el.check()
                    break
            except Exception:
                pass
        # Fallback: label con texto "Persona Natural"
        try:
            page.locator("label:has-text('Persona Natural')").first.click()
        except Exception:
            pass

        # ---- Aceptar tratamiento de datos (obligatorio) ----
        for sel in ["[name='tratamiento_datos'][value='si']",
                    "[name='tratamiento_datos'][value='true']",
                    "[name='autoriza'][value='si']"]:
            try:
                el = page.query_selector(f"input[type='radio']{sel}")
                if el and el.is_visible():
                    el.check()
                    break
            except Exception:
                pass
        # Fallback: botón/label "Sí" en la sección de tratamiento de datos
        try:
            page.locator("label:has-text('Sí')").first.click()
        except Exception:
            pass

        # ---- Documentos ----
        if docs:
            _subir_documentos(page, docs)

        # ---- Enviar ----
        page.click("button[type='submit'], input[type='submit'], [data-action='guardar']")
        page.wait_for_load_state("networkidle", timeout=20_000)

        lead_id = _extraer_lead_id(page)
        log.info("[Sureti] Lead registrado. ID=%s", lead_id)
        return lead_id

    def consultar_estado(self, sureti_lead_id: str) -> dict:
        """Devuelve dict con al menos {"estado": str}; puede incluir
        "monto_aprobado" (int) o "razon" (str)."""
        page = self._page
        page.goto(
            f"{SURETI_URL}/leads/{sureti_lead_id}",
            wait_until="networkidle",
            timeout=30_000,
        )

        resultado: dict = {}

        # Intentar leer el estado desde un elemento visible
        for sel in [
            "[data-estado]", ".estado", "#estado",
            ".badge", ".status", "[class*='estado']", "[class*='status']",
        ]:
            el = page.query_selector(sel)
            if el:
                texto = (el.get_attribute("data-estado") or el.inner_text()).strip().lower()
                if texto:
                    resultado["estado"] = _normalizar_estado(texto)
                    break

        if not resultado.get("estado"):
            resultado["estado"] = "en_estudio"

        # Monto aprobado (si está visible)
        for sel in ["[data-monto]", ".monto-aprobado", "#monto_aprobado"]:
            el = page.query_selector(sel)
            if el:
                raw = (el.get_attribute("data-monto") or el.inner_text()).replace(".", "").replace(",", "").strip()
                try:
                    resultado["monto_aprobado"] = int("".join(c for c in raw if c.isdigit()))
                except ValueError:
                    pass
                break

        # Razón de rechazo
        for sel in ["[data-razon]", ".razon-rechazo", "#razon_rechazo", ".motivo"]:
            el = page.query_selector(sel)
            if el:
                texto = (el.get_attribute("data-razon") or el.inner_text()).strip()
                if texto:
                    resultado["razon"] = texto
                break

        log.info("[Sureti] Estado lead %s: %s", sureti_lead_id, resultado)
        return resultado


# ------------------------------------------------------------------
# API pública simplificada (con dry-run)
# ------------------------------------------------------------------

def registrar_lead(data: dict, docs: list[dict] | None = None) -> str:
    if DRY_RUN:
        fake_id = f"DRY-{data.get('telefono', 'x')}"
        log.info("[Sureti DRY-RUN] registrar_lead → %s", fake_id)
        return fake_id
    with SuretiSession() as s:
        return s.registrar_lead(data, docs)


def consultar_estado(sureti_lead_id: str) -> dict:
    if DRY_RUN:
        return {"estado": "en_estudio"}
    with SuretiSession() as s:
        return s.consultar_estado(sureti_lead_id)


# ------------------------------------------------------------------
# Helpers internos
# ------------------------------------------------------------------

def _fill(page, selector_css: str, value: str):
    """Rellena el primer selector que exista en la página."""
    for sel in selector_css.split(","):
        sel = sel.strip()
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.fill(value)
                return
        except Exception:
            pass


def _select_or_fill(page, selector_css: str, value: str):
    """Selecciona en un <select> si existe; si no, rellena como texto."""
    for sel in selector_css.split(","):
        sel = sel.strip()
        try:
            el = page.query_selector(sel)
            if not el or not el.is_visible():
                continue
            tag = el.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                # Intenta por valor exacto, luego por texto que contenga value
                try:
                    el.select_option(value=value)
                    return
                except Exception:
                    pass
                try:
                    el.select_option(label=value)
                    return
                except Exception:
                    pass
            else:
                el.fill(value)
                return
        except Exception:
            pass


def _check_radio(page, selector_css: str, value: bool):
    """Marca un radio button o checkbox booleano."""
    target = "si" if value else "no"
    for sel in selector_css.split(","):
        sel = sel.strip()
        # Busca input[value='si'] o input[value='no']
        for v in ([target, "1" if value else "0", "true" if value else "false"]):
            try:
                el = page.query_selector(f"{sel}[value='{v}']")
                if el and el.is_visible():
                    el.check()
                    return
            except Exception:
                pass
        # Fallback: click en label con texto "Sí" / "No"
        try:
            label_text = "Sí" if value else "No"
            page.locator(f"label:has-text('{label_text}')").first.click()
            return
        except Exception:
            pass


def _subir_documentos(page, docs: list[dict]):
    """Sube archivos al formulario.

    Cada elemento de docs: {"tipo": str, "url_storage": str | None,
                             "media_id": str | None}
    Solo se pueden subir rutas locales; si url_storage apunta a un archivo
    local existente, se sube directamente.
    """
    for doc in docs:
        ruta = doc.get("url_storage", "")
        if not ruta or not Path(ruta).exists():
            continue
        tipo = doc.get("tipo", "documento")
        # Busca input[type=file] filtrado por nombre/tipo
        for sel in [
            f"input[type='file'][name*='{tipo}']",
            f"input[type='file'][data-tipo='{tipo}']",
            "input[type='file']",
        ]:
            try:
                el = page.query_selector(sel)
                if el:
                    el.set_input_files(ruta)
                    log.info("[Sureti] Documento subido: %s → %s", tipo, ruta)
                    break
            except Exception:
                pass
        time.sleep(0.5)


def _extraer_lead_id(page) -> str:
    """Extrae el ID del lead de la URL o de la página tras guardar."""
    # Patrón: /leads/123 o /leads/ABC-123
    import re
    m = re.search(r"/leads/([A-Za-z0-9_-]+)", page.url)
    if m:
        return m.group(1)
    # Busca en atributos data-id
    el = page.query_selector("[data-lead-id], [data-id], #lead_id, .lead-id")
    if el:
        val = el.get_attribute("data-lead-id") or el.get_attribute("data-id") or el.inner_text()
        if val.strip():
            return val.strip()
    # Último recurso: generar un ID temporal
    import uuid
    return f"TMP-{uuid.uuid4().hex[:8]}"


def _normalizar_estado(texto: str) -> str:
    """Mapea textos libres del portal al conjunto interno de estados."""
    texto = texto.lower()
    if any(w in texto for w in ("aprobado", "approved", "aprobada")):
        return "aprobado"
    if any(w in texto for w in ("rechazado", "denied", "no aprobado", "rechazada")):
        return "rechazado"
    if any(w in texto for w in ("estudio", "análisis", "analisis", "revisión", "revision", "proceso")):
        return "en_estudio"
    if any(w in texto for w in ("registrado", "recibido", "received")):
        return "registrado"
    if any(w in texto for w in ("desembolsado", "desembolso")):
        return "desembolsado"
    return texto  # devuelve tal cual si no hay match
