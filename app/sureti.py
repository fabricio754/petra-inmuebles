"""Integración con el portal de agentes de Sureti (agentes.sureti.co).

⚠️  CRÍTICO: Los selectores CSS de este módulo fueron escritos SIN ver el
    formulario real. La proxy del sandbox bloquea agentes.sureti.co.
    ANTES de que llegue el primer lead real, hay que:
      1. Abrir agentes.sureti.co con las credenciales reales
      2. Navegar al formulario "Nuevo Lead"
      3. Inspeccionar los selectores y actualizar las constantes de abajo
      4. Hacer lo mismo con la pantalla de "Detalle del lead" para consultar estado

Funciones públicas:
  registrar_lead(data, docs) → sureti_lead_id
  consultar_estado(sureti_lead_id) → {estado, monto_aprobado, razon}
  subir_documentos_fase2(sureti_lead_id, docs) → bool
"""
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("petra")

SURETI_EMAIL = os.environ.get("SURETI_EMAIL", "")
SURETI_PASSWORD = os.environ.get("SURETI_PASSWORD", "")
SURETI_URL = "https://agentes.sureti.co"

# Ruta donde se persisten las cookies de sesión para evitar login en cada llamada.
_COOKIES_PATH = Path(os.environ.get("MEDIA_DIR", "/var/data/media")) / ".sureti_session.json"

# ─────────────────────────────────────────────────────────────────────────────
# ⚠️  SELECTORES — DEBEN VERIFICARSE ANTES DE USAR EN PRODUCCIÓN
# ─────────────────────────────────────────────────────────────────────────────
# Cómo verificarlos:
#   1. Conectar al portal con las credenciales
#   2. Hacer clic en "Nuevo Lead"
#   3. Inspeccionar cada campo con DevTools (F12 → Inspector)
#   4. Reemplazar los valores con los selectores reales
# ─────────────────────────────────────────────────────────────────────────────

SEL = {
    # Login
    "login_email":       "#email, input[name='email'], input[type='email']",
    "login_password":    "#password, input[name='password'], input[type='password']",
    "login_submit":      "button[type='submit'], #btnLogin, button:has-text('Ingresar')",

    # Nuevo Lead — botón para abrir formulario
    "btn_nuevo_lead":    "a:has-text('Nuevo Lead'), button:has-text('Nuevo Lead'), [href*='nuevo']",

    # Formulario Nuevo Lead — campos de texto
    "campo_nombre":       "#nombre, input[name='nombre'], [placeholder*='Nombre']",
    "campo_cedula":       "#cedula, input[name='cedula'], [placeholder*='Cédula']",
    "campo_edad":         "#edad, input[name='edad'], [placeholder*='Edad']",
    "campo_email":        "#emailCliente, input[name='email'], [placeholder*='Correo']",
    "campo_celular":      "#celular, input[name='celular'], [placeholder*='Celular']",
    "campo_direccion":    "#direccion, input[name='direccion'], [placeholder*='Dirección']",
    "campo_matricula":    "#matricula, input[name='matricula'], [placeholder*='Matrícula']",
    "campo_avaluo":       "#avaluo, input[name='avaluo'], [placeholder*='Avalúo']",
    "campo_objetivo":     "#objetivo, textarea[name='objetivo'], [placeholder*='Objetivo']",

    # Dropdowns / selects
    "select_ciudad":      "#ciudad, select[name='ciudad']",
    "select_tipo":        "#tipoInmueble, select[name='tipoInmueble'], select[name='tipo']",
    "select_estrato":     "#estrato, select[name='estrato']",
    "check_ph":           "#esPH, input[name='esPH'], input[type='checkbox'][name*='ph']",

    # Upload de documentos
    "upload_predial":     "input[type='file'][name*='predial'], #filePredial",
    "upload_ctl":         "input[type='file'][name*='tradicion'], input[type='file'][name*='ctl'], #fileCtl",
    "upload_fachada":     "input[type='file'][name*='fachada'], input[type='file'][name*='foto'], #fileFachada",

    # Enviar formulario
    "btn_enviar":         "button[type='submit']:has-text('Enviar'), button:has-text('Guardar'), #btnEnviarLead",

    # Confirmación / ID del lead
    "lead_id_texto":      ".lead-id, [class*='leadId'], #leadId, .alert-success",

    # Lista de leads — para consultar estado
    "tabla_leads":        ".leads-table, #tableleads, table[class*='lead']",
    "fila_lead":          "tr[data-id='{id}'], tr:has([data-lead-id='{id}'])",
    "estado_celda":       "td[class*='estado'], td[class*='status']",
    "monto_celda":        "td[class*='monto'], td[class*='amount']",
}


class SuretiSession:
    def __init__(self):
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().__enter__()
            self._browser = self._pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            self._ctx = self._browser.new_context()
            self._page = self._ctx.new_page()
            self._logged_in = False
        except ImportError:
            raise RuntimeError("playwright no instalado")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        try:
            # Guardar cookies para próximas sesiones
            cookies = self._ctx.cookies()
            _COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
            _COOKIES_PATH.write_text(json.dumps(cookies))
        except Exception:
            pass
        try:
            self._browser.close()
            self._pw.__exit__(None, None, None)
        except Exception:
            pass

    def _cargar_cookies(self):
        try:
            if _COOKIES_PATH.exists():
                cookies = json.loads(_COOKIES_PATH.read_text())
                self._ctx.add_cookies(cookies)
                return True
        except Exception:
            pass
        return False

    def _login(self):
        if self._logged_in:
            return True

        # Intentar con cookies guardadas primero
        self._cargar_cookies()
        self._page.goto(f"{SURETI_URL}/dashboard", timeout=30_000)
        time.sleep(2)
        if "/login" not in self._page.url and "/auth" not in self._page.url:
            self._logged_in = True
            log.info("[Sureti] Sesión recuperada con cookies.")
            return True

        # Login fresco
        self._page.goto(f"{SURETI_URL}/login", timeout=30_000)
        self._page.wait_for_load_state("networkidle")

        self._page.fill(SEL["login_email"], SURETI_EMAIL)
        self._page.fill(SEL["login_password"], SURETI_PASSWORD)
        self._page.click(SEL["login_submit"])
        self._page.wait_for_load_state("networkidle")
        time.sleep(2)

        if "/login" in self._page.url or "/auth" in self._page.url:
            log.error("[Sureti] Login falló. Verificar credenciales y selectores.")
            return False

        self._logged_in = True
        log.info("[Sureti] Login exitoso.")
        return True

    def registrar_lead(self, data: dict, docs: dict) -> str | None:
        """
        Llena el formulario 'Nuevo Lead' y sube documentos.
        data: {nombre, cedula, edad, email, celular, ciudad, direccion,
               tipo_inmueble, estrato, es_ph, matricula, avaluo_catastral,
               objetivo_prestamo}
        docs: {predial: path, fachada: path, cert_tradicion: path}
        Retorna sureti_lead_id o None.
        """
        if not SURETI_EMAIL or not SURETI_PASSWORD:
            log.warning("[Sureti] Sin credenciales → dry-run.")
            return "DRY_RUN_ID"

        if not self._login():
            return None

        # Navegar a Nuevo Lead
        self._page.click(SEL["btn_nuevo_lead"])
        self._page.wait_for_load_state("networkidle")
        time.sleep(1.5)

        # Llenar campos de texto
        for campo, valor in [
            ("campo_nombre",    data.get("nombre", "")),
            ("campo_cedula",    str(data.get("cedula", ""))),
            ("campo_edad",      str(data.get("edad", ""))),
            ("campo_email",     data.get("email", "")),
            ("campo_celular",   str(data.get("celular", "")).replace("+57", "").replace("57", "", 1)),
            ("campo_direccion", data.get("direccion", "")),
            ("campo_matricula", str(data.get("matricula", ""))),
            ("campo_objetivo",  data.get("objetivo_prestamo", "")),
        ]:
            el = self._page.query_selector(SEL[campo])
            if el:
                el.fill(str(valor))
            else:
                log.warning("[Sureti] No encontrado: %s (selector: %s)", campo, SEL[campo])

        # Avalúo
        if data.get("avaluo_catastral"):
            el = self._page.query_selector(SEL["campo_avaluo"])
            if el:
                el.fill(str(data["avaluo_catastral"]))

        # Dropdowns
        sel_ciudad = self._page.query_selector(SEL["select_ciudad"])
        if sel_ciudad:
            sel_ciudad.select_option(label=data.get("ciudad", "Bogotá"))

        sel_tipo = self._page.query_selector(SEL["select_tipo"])
        if sel_tipo:
            sel_tipo.select_option(label=data.get("tipo_inmueble", "apartamento"))

        sel_estrato = self._page.query_selector(SEL["select_estrato"])
        if sel_estrato:
            sel_estrato.select_option(str(data.get("estrato", "")))

        if data.get("es_ph"):
            ch = self._page.query_selector(SEL["check_ph"])
            if ch and not ch.is_checked():
                ch.check()

        # Upload de documentos
        for campo_upload, key_doc in [
            ("upload_predial",  "predial"),
            ("upload_ctl",      "cert_tradicion"),
            ("upload_fachada",  "fachada"),
        ]:
            ruta = docs.get(key_doc)
            if ruta and Path(ruta).exists():
                el = self._page.query_selector(SEL[campo_upload])
                if el:
                    el.set_input_files(ruta)
                    time.sleep(1)
                else:
                    log.warning("[Sureti] Input upload no encontrado: %s", campo_upload)

        # Enviar
        self._page.click(SEL["btn_enviar"])
        self._page.wait_for_load_state("networkidle")
        time.sleep(2)

        # Extraer ID del lead
        el = self._page.query_selector(SEL["lead_id_texto"])
        lead_id = None
        if el:
            texto = el.inner_text()
            m_id = __import__("re").search(r"[A-Z0-9\-]{4,30}", texto)
            if m_id:
                lead_id = m_id.group()

        if not lead_id:
            # Intentar desde la URL
            url = self._page.url
            m_url = __import__("re").search(r"/leads?/([^/?&#]+)", url)
            if m_url:
                lead_id = m_url.group(1)

        log.info("[Sureti] Lead registrado → ID: %s", lead_id)
        return lead_id

    def consultar_estado(self, lead_id: str) -> dict:
        """
        Consulta el estado de un lead en Sureti.
        Retorna {estado, monto_aprobado, razon}.
        """
        if not SURETI_EMAIL or not SURETI_PASSWORD:
            return {"estado": "dry_run", "monto_aprobado": None, "razon": "dry-run"}

        if not self._login():
            return {"estado": "error", "monto_aprobado": None, "razon": "login_fallido"}

        # Intentar URL directa del lead
        self._page.goto(f"{SURETI_URL}/leads/{lead_id}", timeout=20_000)
        self._page.wait_for_load_state("networkidle")
        time.sleep(1)

        estado = None
        monto = None
        razon = None

        # Buscar estado en la página
        for sel_cand in [".lead-status", "[class*='status']", "[class*='estado']", ".badge"]:
            el = self._page.query_selector(sel_cand)
            if el:
                txt = el.inner_text().strip().lower()
                if any(k in txt for k in ("aprobad", "approved")):
                    estado = "aprobado"
                elif any(k in txt for k in ("rechaz", "no aprobad", "rejected")):
                    estado = "no_aprobado"
                elif any(k in txt for k in ("estudio", "review", "proceso")):
                    estado = "en_estudio"
                elif any(k in txt for k in ("faltante", "pendiente", "requiere")):
                    estado = "info_faltante"
                if estado:
                    break

        if not estado:
            estado = "en_estudio"

        # Monto aprobado
        el_monto = self._page.query_selector(
            "[class*='monto'], [class*='amount'], [class*='aprobado']"
        )
        if el_monto:
            import re
            m = re.search(r"[\d.,]+", el_monto.inner_text())
            if m:
                monto = int(re.sub(r"[.,]", "", m.group()))

        # Razón si fue rechazado
        el_razon = self._page.query_selector("[class*='razon'], [class*='reason'], .alert-danger")
        if el_razon:
            razon = el_razon.inner_text().strip()

        return {"estado": estado, "monto_aprobado": monto, "razon": razon}

    def subir_documentos_fase2(self, lead_id: str, docs: list[dict]) -> bool:
        """
        Sube documentos de fase 2 (escrituras, paz y salvos, extractos, admon).
        docs: lista de {"tipo": str, "ruta": str}
        ⚠️  Selectores estimados — verificar.
        """
        if not SURETI_EMAIL or not SURETI_PASSWORD:
            log.warning("[Sureti] dry-run subir_documentos_fase2")
            return True

        if not self._login():
            return False

        self._page.goto(f"{SURETI_URL}/leads/{lead_id}", timeout=20_000)
        self._page.wait_for_load_state("networkidle")

        for doc in docs:
            ruta = doc.get("ruta")
            tipo = doc.get("tipo", "")
            if not ruta or not Path(ruta).exists():
                continue

            # Buscar input de upload para este tipo de documento
            el = self._page.query_selector(
                f"input[type='file'][name*='{tipo}'], "
                f"input[type='file'][data-tipo='{tipo}']"
            )
            if not el:
                # Fallback genérico
                el = self._page.query_selector("input[type='file']:last-of-type")
            if el:
                el.set_input_files(ruta)
                time.sleep(1)

        btn = self._page.query_selector(
            "button:has-text('Guardar'), button:has-text('Enviar documentos'), "
            "button[type='submit']"
        )
        if btn:
            btn.click()
            self._page.wait_for_load_state("networkidle")

        return True


# ─────────────────────────────────────────────────────────────────────────────
# API pública
# ─────────────────────────────────────────────────────────────────────────────

def registrar_lead(data: dict, docs: dict) -> str | None:
    """Crea un lead en Sureti. Retorna sureti_lead_id o None."""
    with SuretiSession() as s:
        for intento in range(2):
            result = s.registrar_lead(data, docs)
            if result:
                return result
            if intento == 0:
                # Borrar cookies y reintentar
                _COOKIES_PATH.unlink(missing_ok=True)
    return None


def consultar_estado(lead_id: str) -> dict:
    """Consulta el estado de un lead en Sureti."""
    with SuretiSession() as s:
        return s.consultar_estado(lead_id)


def subir_documentos_fase2(lead_id: str, docs: list[dict]) -> bool:
    """Sube documentos de fase 2 a un lead existente."""
    with SuretiSession() as s:
        return s.subir_documentos_fase2(lead_id, docs)
