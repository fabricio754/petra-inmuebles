"""Persistencia mínima en JSON. Sin base de datos: son los mismos
archivos que luego se pueden reemplazar por Google Sheets sin tocar
la lógica del bot (bot.py solo llama a estas funciones)."""
import json
import os
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
INVENTARIO_PATH = os.path.join(DATA_DIR, "inventario.json")
INVENTARIO_SEED_PATH = os.path.join(DATA_DIR, "inventario.seed.json")
SESSIONS_PATH = os.path.join(DATA_DIR, "sessions.json")
LEADS_PATH = os.path.join(DATA_DIR, "leads.json")


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


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_inventario():
    if not os.path.exists(INVENTARIO_PATH) and os.path.exists(INVENTARIO_SEED_PATH):
        # Primera vez que corre en un entorno nuevo (ej. Render): arranca
        # con el inventario base en vez de vacío.
        _save_json(INVENTARIO_PATH, _load_json(INVENTARIO_SEED_PATH, []))
    return _load_json(INVENTARIO_PATH, [])


def save_inmueble(apto):
    """Agrega un inmueble nuevo (publicado por un propietario) al inventario."""
    inventario = _load_json(INVENTARIO_PATH, [])
    inventario.append(apto)
    _save_json(INVENTARIO_PATH, inventario)
    return apto


def next_apto_id(conjunto, operacion, numero):
    prefijo = "ARR" if operacion == "ARRIENDO" else "VTA"
    return f"{conjunto}-{prefijo}-{numero}"


def get_session(phone):
    sessions = _load_json(SESSIONS_PATH, {})
    return sessions.get(phone, {})


def set_session(phone, **fields):
    sessions = _load_json(SESSIONS_PATH, {})
    session = sessions.get(phone, {})
    session.update(fields)
    sessions[phone] = session
    _save_json(SESSIONS_PATH, sessions)
    return session


def save_lead(phone, apto, **extra):
    """extra permite agregar campos como estado/autorizado_interesado/autorizado_en
    para leads de inmuebles PROPIETARIO, sin cambiar el registro que ya se
    guardaba para leads de inmuebles PETRA (llamadas sin extra quedan igual)."""
    leads = _load_json(LEADS_PATH, [])
    lead = {
        "timestamp": now_iso(),
        "telefono": phone,
        "conjunto": apto.get("conjunto"),
        "apto_id": apto.get("id"),
        "apartamento": apto.get("apartamento"),
        "operacion": apto.get("operacion"),
    }
    lead.update(extra)
    leads.append(lead)
    _save_json(LEADS_PATH, leads)
    return lead
