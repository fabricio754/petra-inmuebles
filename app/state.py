"""Persistencia del bot. bot.py solo llama a estas funciones, así que el
almacenamiento se puede cambiar sin tocar la lógica del bot.

Tres modos, en este orden de prioridad:
- PostgreSQL (producción): si existe DATABASE_URL. Ver app/db.py. La
  primera vez copia lo que había en la Google Sheet.
- Google Sheets: si existe GOOGLE_SHEET_ID (y no DATABASE_URL). Pestañas
  Inventario, Sesiones y Contactos, editables a mano.
- Archivos JSON en data/ (desarrollo local): si no existe ninguna.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("petra")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
INVENTARIO_PATH = os.path.join(DATA_DIR, "inventario.json")
INVENTARIO_SEED_PATH = os.path.join(DATA_DIR, "inventario.seed.json")
SESSIONS_PATH = os.path.join(DATA_DIR, "sessions.json")
LEADS_PATH = os.path.join(DATA_DIR, "leads.json")

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
# En Render, un "Secret File" llamado google-credentials.json queda en
# /etc/secrets/google-credentials.json.
CREDENTIALS_FILE = os.environ.get(
    "GOOGLE_CREDENTIALS_FILE", "/etc/secrets/google-credentials.json"
)
USE_POSTGRES = bool(os.environ.get("DATABASE_URL", "").strip())
USE_SHEETS = bool(SHEET_ID) and not USE_POSTGRES


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def next_apto_id(conjunto, operacion, numero):
    prefijo = "ARR" if operacion == "ARRIENDO" else "VTA"
    return f"{conjunto}-{prefijo}-{numero}"


# === MODO ARCHIVOS JSON (local) =============================================

def _load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        return json.loads(content) if content else default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _json_load_inventario():
    if not os.path.exists(INVENTARIO_PATH) and os.path.exists(INVENTARIO_SEED_PATH):
        # Primera vez que corre en un entorno nuevo: arranca con el
        # inventario base en vez de vacío.
        _save_json(INVENTARIO_PATH, _load_json(INVENTARIO_SEED_PATH, []))
    return _load_json(INVENTARIO_PATH, [])


def _json_save_inmueble(apto):
    inventario = _load_json(INVENTARIO_PATH, [])
    inventario.append(apto)
    _save_json(INVENTARIO_PATH, inventario)


def _json_get_session(phone):
    return _load_json(SESSIONS_PATH, {}).get(phone, {})


def _json_save_session(phone, session):
    sessions = _load_json(SESSIONS_PATH, {})
    sessions[phone] = session
    _save_json(SESSIONS_PATH, sessions)


def _json_append_lead(lead):
    leads = _load_json(LEADS_PATH, [])
    leads.append(lead)
    _save_json(LEADS_PATH, leads)


# === MODO GOOGLE SHEETS (producción) ========================================

TAB_INVENTARIO = "Inventario"
TAB_SESIONES = "Sesiones"
TAB_CONTACTOS = "Contactos"

INVENTARIO_COLS = [
    "id", "conjunto", "operacion", "apartamento", "habitaciones", "banos",
    "m2", "precio", "gestion", "disponibilidad", "propietario_telefono",
    "owner_contact_authorized", "owner_contact_authorized_at",
]
INVENTARIO_NUMEROS = {"habitaciones", "banos", "m2", "precio"}
SESIONES_COLS = ["telefono", "datos", "actualizado"]
CONTACTOS_COLS = [
    "timestamp", "telefono", "conjunto", "apto_id", "apartamento", "operacion",
]

# Cuánto tiempo se reutiliza el inventario leído, para no leer la Sheet en
# cada mensaje. Un cambio hecho a mano en la Sheet se ve en máximo esto.
INVENTARIO_CACHE_SEG = 30

_lock = threading.Lock()
_spreadsheet = None
_hojas = {}
_inventario_cache = {"datos": None, "leido": 0.0}
_sesiones_cache = None  # {telefono: (fila, dict)}


def _libro():
    global _spreadsheet
    if _spreadsheet is None:
        import gspread
        client = gspread.service_account(
            filename=CREDENTIALS_FILE, http_client=gspread.BackOffHTTPClient
        )
        _spreadsheet = client.open_by_key(SHEET_ID)
    return _spreadsheet


def _hoja(nombre, columnas, al_crear=None):
    """Devuelve la pestaña; si no existe la crea con sus encabezados."""
    if nombre in _hojas:
        return _hojas[nombre]
    import gspread
    libro = _libro()
    try:
        hoja = libro.worksheet(nombre)
    except gspread.WorksheetNotFound:
        hoja = libro.add_worksheet(title=nombre, rows=100, cols=len(columnas))
        hoja.update([columnas], "A1", value_input_option="RAW")
        hoja.freeze(rows=1)
        log.info("[Sheets] Pestaña '%s' creada.", nombre)
        if al_crear:
            al_crear(hoja)
    _hojas[nombre] = hoja
    return hoja


def _encabezados(hoja, claves):
    """Lee la fila 1 y agrega al final las columnas que falten para `claves`
    (así un campo nuevo en un lead no se pierde)."""
    encabezados = hoja.row_values(1)
    faltantes = [c for c in claves if c not in encabezados]
    if faltantes:
        encabezados = encabezados + faltantes
        if hoja.col_count < len(encabezados):
            hoja.add_cols(len(encabezados) - hoja.col_count)
        hoja.update([encabezados], "A1", value_input_option="RAW")
    return encabezados


def _celda(valor):
    if valor is None:
        return ""
    if isinstance(valor, (dict, list)):
        return json.dumps(valor, ensure_ascii=False)
    return valor


def _agregar_fila(hoja, datos):
    encabezados = _encabezados(hoja, list(datos.keys()))
    fila = [_celda(datos.get(col)) for col in encabezados]
    hoja.append_row(fila, value_input_option="RAW", table_range="A1")


def _leer_filas(hoja):
    """Filas como dicts {encabezado: valor}, sin filas vacías."""
    valores = hoja.get_all_values(value_render_option="UNFORMATTED_VALUE")
    if not valores:
        return []
    encabezados = [str(h).strip() for h in valores[0]]
    filas = []
    for fila in valores[1:]:
        if not any(str(v).strip() for v in fila):
            continue
        fila = list(fila) + [""] * (len(encabezados) - len(fila))
        filas.append({h: fila[i] for i, h in enumerate(encabezados) if h})
    return filas


def _a_entero(valor):
    """Números escritos a mano: 2300000, "2.300.000", "68,5" o vacío."""
    if isinstance(valor, bool):
        return int(valor)
    if isinstance(valor, (int, float)):
        return int(valor)
    texto = str(valor).strip().replace("$", "").replace(" ", "")
    if not texto:
        return 0
    if "," in texto:  # coma decimal (68,5) y puntos de miles
        texto = texto.replace(".", "").split(",")[0]
    elif texto.count(".") > 1 or (texto.count(".") == 1 and len(texto.split(".")[1]) == 3):
        texto = texto.replace(".", "")  # puntos de miles (2.300.000)
    try:
        return int(float(texto))
    except ValueError:
        return 0


def _a_texto(valor):
    # Un teléfono escrito a mano puede leerse como número (573001112233.0).
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    return str(valor).strip()


def _limpiar_inmueble(fila):
    apto = {}
    for clave, valor in fila.items():
        if clave in INVENTARIO_NUMEROS:
            apto[clave] = _a_entero(valor)
        elif clave == "owner_contact_authorized":
            apto[clave] = str(valor).strip().upper() in ("TRUE", "VERDADERO", "SI", "SÍ", "1")
        else:
            apto[clave] = _a_texto(valor)
    # Mayúsculas en los campos que bot.py compara, por si se editan a mano.
    for clave in ("conjunto", "operacion", "gestion", "disponibilidad"):
        apto[clave] = apto.get(clave, "").upper()
    return apto


def _sembrar_inventario(hoja):
    seed = _load_json(INVENTARIO_SEED_PATH, [])
    if seed:
        filas = [[_celda(item.get(col)) for col in INVENTARIO_COLS] for item in seed]
        hoja.append_rows(filas, value_input_option="RAW", table_range="A1")
        log.info("[Sheets] Inventario base cargado (%d inmuebles).", len(seed))


def _hoja_inventario():
    return _hoja(TAB_INVENTARIO, INVENTARIO_COLS, al_crear=_sembrar_inventario)


def _sheets_load_inventario():
    ahora = time.monotonic()
    if (_inventario_cache["datos"] is not None
            and ahora - _inventario_cache["leido"] < INVENTARIO_CACHE_SEG):
        return _inventario_cache["datos"]
    filas = _leer_filas(_hoja_inventario())
    datos = [_limpiar_inmueble(f) for f in filas if str(f.get("id", "")).strip()]
    _inventario_cache.update(datos=datos, leido=ahora)
    return datos


def _sheets_save_inmueble(apto):
    with _lock:
        _agregar_fila(_hoja_inventario(), apto)
        _inventario_cache["datos"] = None  # el próximo listado ya lo incluye


def _sesiones():
    """Las sesiones se leen una sola vez al arrancar y luego se mantienen en
    memoria; cada cambio se escribe en su fila de la Sheet."""
    global _sesiones_cache
    if _sesiones_cache is None:
        hoja = _hoja(TAB_SESIONES, SESIONES_COLS)
        valores = hoja.get_all_values()
        cache = {}
        for i, fila in enumerate(valores[1:], start=2):
            if not fila or not fila[0].strip():
                continue
            try:
                datos = json.loads(fila[1]) if len(fila) > 1 and fila[1] else {}
            except json.JSONDecodeError:
                datos = {}
            cache[fila[0].strip()] = (i, datos)
        _sesiones_cache = cache
    return _sesiones_cache


def _sheets_get_session(phone):
    with _lock:
        return dict(_sesiones().get(phone, (None, {}))[1])


def _sheets_save_session(phone, session):
    with _lock:
        sesiones = _sesiones()
        hoja = _hojas[TAB_SESIONES]
        fila_datos = [phone, json.dumps(session, ensure_ascii=False), now_iso()]
        if phone in sesiones:
            fila = sesiones[phone][0]
            hoja.update([fila_datos], f"A{fila}:C{fila}", value_input_option="RAW")
        else:
            respuesta = hoja.append_row(fila_datos, value_input_option="RAW", table_range="A1")
            # "updatedRange" es tipo "Sesiones!A7:C7" -> fila 7.
            rango = respuesta.get("updates", {}).get("updatedRange", "")
            fila = int("".join(c for c in rango.split("!")[-1].split(":")[0] if c.isdigit()))
        sesiones[phone] = (fila, dict(session))


def _sheets_append_lead(lead):
    with _lock:
        _agregar_fila(_hoja(TAB_CONTACTOS, CONTACTOS_COLS), lead)


def inventario_seed():
    return _load_json(INVENTARIO_SEED_PATH, [])


def leer_todo_de_sheets():
    """Para la migración a Postgres: (inventario, sesiones, leads) tal como
    están hoy en la Sheet. No crea pestañas que no existan."""
    libro = _libro()
    pestañas = {h.title for h in libro.worksheets()}

    inventario = []
    if TAB_INVENTARIO in pestañas:
        filas = _leer_filas(libro.worksheet(TAB_INVENTARIO))
        inventario = [_limpiar_inmueble(f) for f in filas if str(f.get("id", "")).strip()]

    sesiones = {}
    if TAB_SESIONES in pestañas:
        for fila in libro.worksheet(TAB_SESIONES).get_all_values()[1:]:
            if not fila or not fila[0].strip():
                continue
            try:
                sesiones[fila[0].strip()] = json.loads(fila[1]) if len(fila) > 1 and fila[1] else {}
            except json.JSONDecodeError:
                pass

    leads = []
    if TAB_CONTACTOS in pestañas:
        for fila in _leer_filas(libro.worksheet(TAB_CONTACTOS)):
            leads.append({k: (_a_texto(v) if v != "" else None) for k, v in fila.items()})
    return inventario, sesiones, leads


# === API usada por bot.py ===================================================

def load_inventario():
    if USE_POSTGRES:
        return _db().load_inventario()
    return _sheets_load_inventario() if USE_SHEETS else _json_load_inventario()


def save_inmueble(apto):
    """Agrega un inmueble nuevo (publicado por un propietario) al inventario."""
    if USE_POSTGRES:
        _db().save_inmueble(apto)
    elif USE_SHEETS:
        _sheets_save_inmueble(apto)
    else:
        _json_save_inmueble(apto)
    return apto


def get_session(phone):
    if USE_POSTGRES:
        return _db().get_session(phone)
    return _sheets_get_session(phone) if USE_SHEETS else _json_get_session(phone)


def set_session(phone, **fields):
    session = get_session(phone)
    session.update(fields)
    if USE_POSTGRES:
        _db().save_session(phone, session)
    elif USE_SHEETS:
        _sheets_save_session(phone, session)
    else:
        _json_save_session(phone, session)
    return session


def save_lead(phone, apto, **extra):
    """extra permite agregar campos como estado/autorizado_interesado/autorizado_en
    para leads de inmuebles PROPIETARIO, sin cambiar el registro que ya se
    guardaba para leads de inmuebles PETRA (llamadas sin extra quedan igual)."""
    lead = {
        "timestamp": now_iso(),
        "telefono": phone,
        "conjunto": apto.get("conjunto"),
        "apto_id": apto.get("id"),
        "apartamento": apto.get("apartamento"),
        "operacion": apto.get("operacion"),
    }
    lead.update(extra)
    if USE_POSTGRES:
        _db().append_lead(lead)
    elif USE_SHEETS:
        _sheets_append_lead(lead)
    else:
        _json_append_lead(lead)
    return lead


def save_pipeline(data):
    """Guarda un lead calificado en la tabla pipeline (Hito 6+)."""
    if USE_POSTGRES:
        _db().save_pipeline(data)
    else:
        lead = {"operacion": "SURETI_PIPELINE", **data}
        if USE_SHEETS:
            _sheets_append_lead(lead)
        else:
            _json_append_lead(lead)


def set_no_contactar(phone, valor=True):
    """valor=True: pidió no recibir más mensajes (nunca se le escribe primero).
    valor=False: volvió a escribir y aceptó la autorización de datos."""
    if USE_POSTGRES:
        _db().set_no_contactar(phone, valor)
    set_session(phone, no_contactar=valor)


def _db():
    from app import db  # solo se importa (y se necesita psycopg) en modo Postgres
    return db
