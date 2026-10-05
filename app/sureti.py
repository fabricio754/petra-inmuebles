"""Automatización del portal de agentes de Sureti (agentes.sureti.co).

Abre una sesión Playwright, inicia sesión con las credenciales configuradas
y expone dos operaciones:
  - registrar_lead(data, docs) → sureti_lead_id (str)
  - consultar_estado(sureti_lead_id) → dict con "estado" y opcionalmente
    "monto_aprobado" o "razon"

Si no hay credenciales (SURETI_EMAIL / SURETI_PASSWORD), el módulo opera en
modo DRY-RUN: registrar_lead devuelve un ID ficticio y consultar_estado
siempre devuelve {"estado": "en_estudio"}.

NOTA DE DEPLOY (Render): Se requieren las variables de entorno:
  SURETI_EMAIL      — correo del agente registrado en agentes.sureti.co
  SURETI_PASSWORD   — contraseña del agente
  PLAYWRIGHT_CHROMIUM_PATH — (opcional) ruta al ejecutable de Chromium;
      por defecto /opt/pw-browsers/chromium (preinstalado en Render via
      `playwright install chromium` en el buildCommand).
"""
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("petra")

SURETI_URL  = "https://agentes.sureti.co"
SURETI_EMAIL    = os.environ.get("SURETI_EMAIL", "")
SURETI_PASSWORD = os.environ.get("SURETI_PASSWORD", "")

# Ruta al ejecutable Chromium: variable de entorno o ruta default de Render
_CHROMIUM_DEFAULT = "/opt/pw-browsers/chromium"
_CHROMIUM_PATH    = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH") or (
    _CHROMIUM_DEFAULT if Path(_CHROMIUM_DEFAULT).exists() else None
)

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
            executable_path=_CHROMIUM_PATH,
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
        """Inicia sesión en el portal. Prueba varias rutas de login comunes."""
        page = self._page

        # Candidatos de URL de login ordenados por probabilidad
        _LOGIN_PATHS = ["/auth", "/login", "/iniciar-sesion", "/signin", "/"]
        _LANDED = False
        for path in _LOGIN_PATHS:
            try:
                page.goto(f"{SURETI_URL}{path}", wait_until="domcontentloaded", timeout=20_000)
                # Si ya estamos autenticados (redirección al dashboard) salimos
                if "/auth" not in page.url and "/login" not in page.url and path != "/":
                    log.info("[Sureti] Redirigido al dashboard en %s — sesión activa.", page.url)
                    return
                # Verificar que la página tiene formulario de login
                if page.query_selector("input[type='password']"):
                    _LANDED = True
                    break
            except Exception:
                continue

        if not _LANDED:
            raise RuntimeError(f"No se encontró página de login en {SURETI_URL}.")

        # --- Selector de email (múltiples fallbacks) ---
        _EMAIL_SELS = [
            "input[name='email']",
            "input[type='email']",
            "input[name='usuario']",
            "input[name='username']",
            "input[name='correo']",
            "input[id*='email']",
            "input[id*='usuario']",
        ]
        # --- Selector de contraseña ---
        _PASS_SELS = [
            "input[type='password']",
            "input[name='password']",
            "input[name='contrasena']",
            "input[name='clave']",
        ]
        # --- Selector de botón submit ---
        _SUBMIT_SELS = [
            "button[type='submit']",
            "input[type='submit']",
            "button.btn-login",
            "button.btn-primary",
            "button:text('Ingresar')",
            "button:text('Iniciar sesión')",
            "button:text('Entrar')",
        ]

        filled_email = False
        for sel in _EMAIL_SELS:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.fill(SURETI_EMAIL)
                    filled_email = True
                    break
            except Exception:
                continue

        filled_pass = False
        for sel in _PASS_SELS:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.fill(SURETI_PASSWORD)
                    filled_pass = True
                    break
            except Exception:
                continue

        if not filled_email or not filled_pass:
            raise RuntimeError(
                f"No se encontraron campos de login (email={filled_email}, pass={filled_pass})."
            )

        clicked = False
        for sel in _SUBMIT_SELS:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            # último recurso: Enter en el campo de contraseña
            page.keyboard.press("Enter")

        page.wait_for_load_state("networkidle", timeout=20_000)
        current = page.url
        # Consideramos fallo si la URL todavía tiene una ruta de auth/login
        if any(kw in current for kw in ("/auth", "/login", "/signin", "/iniciar-sesion")):
            raise RuntimeError(
                f"Login en Sureti falló (URL post-submit: {current}). "
                "Verifica SURETI_EMAIL y SURETI_PASSWORD."
            )
        log.info("[Sureti] Sesión iniciada. URL=%s", current)

    # ------------------------------------------------------------------
    # Operaciones de negocio
    # ------------------------------------------------------------------

    def registrar_lead(self, data: dict, docs: list[dict] | None = None) -> str:
        """Llena el formulario 'Nuevo Lead' y devuelve el sureti_lead_id."""
        page = self._page

        # Intentar varias rutas del formulario de nuevo lead
        _FORM_PATHS = ["/nuevo-lead", "/leads/nuevo", "/leads/new", "/lead/nuevo", "/agentes/lead"]
        _FORM_LOADED = False
        for path in _FORM_PATHS:
            try:
                page.goto(f"{SURETI_URL}{path}", wait_until="domcontentloaded", timeout=30_000)
                # Esperar cualquier campo de texto visible — señal de que el form cargó
                for anchor_sel in [
                    "input[name='name']", "input[name='nombre']",
                    "input[name='cedula']", "input[name='document']",
                    "form input[type='text']",
                ]:
                    try:
                        page.wait_for_selector(anchor_sel, timeout=5_000)
                        _FORM_LOADED = True
                        break
                    except Exception:
                        continue
                if _FORM_LOADED:
                    break
            except Exception:
                continue

        if not _FORM_LOADED:
            raise RuntimeError(
                f"No se encontró el formulario de nuevo lead en {SURETI_URL}."
            )

        # ---- Datos del cliente ----
        # Nombre completo
        _fill(page,
              "input[name='name'], input[name='nombre'], input[name='nombres'], "
              "input[name='full_name'], input[id*='name'], input[id*='nombre']",
              data.get("nombre", ""))

        # Cédula / documento
        _fill(page,
              "input[name='cedula'], input[name='document'], input[name='documento'], "
              "input[name='num_doc'], input[id*='cedula'], input[id*='document']",
              str(data.get("cedula", "")))

        # Correo
        _fill(page,
              "input[name='email'], input[type='email'], input[name='correo'], input[id*='email']",
              data.get("email", ""))

        # Teléfono (quitar +57 si viene prefijado)
        telefono = str(data.get("telefono", "")).lstrip("+")
        if telefono.startswith("57") and len(telefono) > 10:
            telefono = telefono[2:]
        _fill(page,
              "input[name='phone'], input[name='telefono'], input[name='celular'], "
              "input[name='movil'], input[id*='phone'], input[id*='telefono']",
              telefono)

        # ---- Inmueble ----
        _fill(page,
              "input[name='city'], input[name='ciudad'], input[id*='city'], input[id*='ciudad']",
              data.get("ciudad", ""))

        _fill(page,
              "input[name='address'], input[name='direccion'], input[name='address_property'], "
              "input[id*='address'], input[id*='direccion']",
              data.get("direccion_inmueble", ""))

        # Matrícula inmobiliaria: SELECT de oficina + campo número (sin name attr)
        matricula = str(data.get("matricula_numero", ""))
        if matricula and "-" in matricula:
            prefijo, numero = matricula.split("-", 1)
            try:
                page.locator("select").first.select_option(label=prefijo.strip())
            except Exception:
                pass
            _fill(page,
                  "input[placeholder='1234567'], input[name='matricula_numero'], "
                  "input[name='registro'], input[id*='matricula']",
                  numero.strip())
        elif matricula:
            _fill(page,
                  "input[placeholder='1234567'], input[name='matricula_numero'], "
                  "input[name='registro'], input[id*='matricula']",
                  matricula)

        # ---- Crédito ----
        valor = data.get("valor_solicitado")
        if valor:
            _fill(page,
                  "input[name='loan_amount'], input[name='monto'], input[name='valor'], "
                  "input[name='monto_solicitado'], input[id*='monto'], input[id*='loan']",
                  str(valor))

        objetivo = data.get("objetivo", "") or data.get("objetivo_prestamo", "")
        if objetivo:
            _fill(page,
                  "textarea[name='loan_objective'], textarea[name='objetivo'], "
                  "textarea[name='objetivo_prestamo'], textarea[id*='objetivo'], "
                  "textarea[id*='loan']",
                  objetivo)

        # ---- Tipo de persona ----
        tipo_persona = str(data.get("tipo_persona", "NATURAL")).upper()
        _set_tipo_persona(page, tipo_persona)

        # ---- Documentos adjuntos ----
        if docs:
            _subir_documentos(page, docs)

        # ---- Enviar ----
        _submit_form(page)
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


def _set_tipo_persona(page, tipo_persona: str):
    """Marca el radio/checkbox de tipo de persona (NATURAL o JURIDICA)."""
    es_juridica = tipo_persona == "JURIDICA"
    # Intentar por IDs específicos
    for id_sel in (["#pt-juridica"] if es_juridica else ["#pt-natural"]):
        try:
            el = page.query_selector(id_sel)
            if el:
                el.check()
                return
        except Exception:
            pass
    # Intentar por value del radio
    for val in (["juridica", "JURIDICA", "j"] if es_juridica else ["natural", "NATURAL", "n"]):
        try:
            el = page.query_selector(f"input[type='radio'][value='{val}']")
            if el:
                el.check()
                return
        except Exception:
            pass
    # Fallback: click en label con texto descriptivo
    label_candidates = (
        ["Persona Jurídica", "Jurídica", "Juridica", "Empresa"]
        if es_juridica
        else ["Persona Natural", "Natural", "Persona física", "Física"]
    )
    for label_text in label_candidates:
        try:
            loc = page.locator(f"label:has-text('{label_text}')")
            if loc.count() > 0:
                loc.first.click()
                return
        except Exception:
            pass


def _submit_form(page):
    """Envía el formulario activo. Prueba varios selectores de botón submit."""
    _SUBMIT_SELS = [
        "button[type='submit']",
        "input[type='submit']",
        "button.btn-primary",
        "button.btn-success",
        "button:text('Guardar')",
        "button:text('Registrar')",
        "button:text('Enviar')",
        "button:text('Crear lead')",
        "button:text('Crear Lead')",
    ]
    for sel in _SUBMIT_SELS:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                return
        except Exception:
            continue
    # Último recurso: Enter
    page.keyboard.press("Enter")


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
