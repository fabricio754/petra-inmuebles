"""Exportación diaria de PostgreSQL → Google Sheets (solo lectura/consulta).

Corre a las 6am hora Colombia si DATABASE_URL y GOOGLE_SHEET_ID están presentes.
Sobreescribe las pestañas Pipeline y Captacion; las demás (Inventario, Sesiones)
no se tocan.
"""
import logging
import os

log = logging.getLogger("petra")

_TAB_PIPELINE  = "Pipeline"
_TAB_CAPTACION = "Captacion"

_COLS_PIPELINE = [
    "id", "telefono", "nombre", "cedula", "email",
    "ciudad", "direccion_inmueble", "tipo_inmueble", "estrato", "es_ph",
    "objetivo_prestamo", "valor_solicitado", "edad",
    "chip", "avaluo_catastral", "matricula_numero",
    "estado", "sureti_lead_id", "monto_aprobado", "razon_no_aprobado",
    "fecha_ingreso", "fecha_aprobacion", "fecha_desembolso",
    "comision", "comision_cobrada", "requiere_paz_salvo",
]

_COLS_CAPTACION = [
    "id", "telefono", "nombre", "direccion", "barrio", "ciudad",
    "tipo_inmueble", "precio_publicado", "estrato", "portal", "url_listing",
    "fecha_scraping", "contactado", "fecha_contacto", "resultado_contacto",
    "monto_hasta_millones", "no_contactar",
]


def ejecutar():
    """Exporta pipeline y captacion a la Sheet. No-op si falta DATABASE_URL o SHEET_ID."""
    if not os.environ.get("DATABASE_URL", "").strip():
        return
    if not os.environ.get("GOOGLE_SHEET_ID", "").strip():
        return

    for nombre, fn in ((_TAB_PIPELINE, _exportar_pipeline), (_TAB_CAPTACION, _exportar_captacion)):
        try:
            fn()
        except Exception:
            log.exception("[Export] Error exportando %s.", nombre)


# ---------------------------------------------------------------------------

def _libro():
    import gspread
    creds = os.environ.get("GOOGLE_CREDENTIALS_FILE", "/etc/secrets/google-credentials.json")
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "")
    client = gspread.service_account(filename=creds, http_client=gspread.BackOffHTTPClient)
    return client.open_by_key(sheet_id)


def _sobreescribir(libro, nombre_tab, encabezados, filas):
    import gspread
    try:
        hoja = libro.worksheet(nombre_tab)
    except gspread.WorksheetNotFound:
        hoja = libro.add_worksheet(
            title=nombre_tab, rows=max(len(filas) + 20, 100), cols=len(encabezados)
        )
    hoja.clear()
    hoja.update([encabezados] + filas, "A1", value_input_option="USER_ENTERED")
    hoja.freeze(rows=1)
    log.info("[Export] %s: %d filas.", nombre_tab, len(filas))


def _celda(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "SI" if v else "NO"
    return str(v)


def _exportar_pipeline():
    from app import db
    with db._conexion() as conn:
        filas = conn.execute(
            "SELECT " + ", ".join(_COLS_PIPELINE) + " FROM pipeline ORDER BY id"
        ).fetchall()
    libro = _libro()
    _sobreescribir(libro, _TAB_PIPELINE, _COLS_PIPELINE, [[_celda(v) for v in f] for f in filas])


def _exportar_captacion():
    from app import db
    with db._conexion() as conn:
        filas = conn.execute(
            "SELECT " + ", ".join(_COLS_CAPTACION) + " FROM contactos ORDER BY id"
        ).fetchall()
    libro = _libro()
    _sobreescribir(libro, _TAB_CAPTACION, _COLS_CAPTACION, [[_celda(v) for v in f] for f in filas])
