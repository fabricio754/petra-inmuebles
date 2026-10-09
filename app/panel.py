"""Panel de operación de Massi.

Vista web protegida por token (`PANEL_TOKEN`) que permite ver, por lead:
captación, sesión activa, pipeline, documentos recibidos y remarketing.

Monta rutas en el server principal:
  GET  /panel                    → lista de leads con filtros
  GET  /panel/stats              → embudo de conversión + métricas
  GET  /panel/<tel>              → línea de tiempo completa del lead
  POST /panel/<tel>/responder    → envía un mensaje de texto manual al lead
"""
import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from flask import (Blueprint, Response, abort, jsonify, redirect,
                   render_template, request, send_file, url_for)
from psycopg_pool import PoolTimeout

from app import db

log = logging.getLogger("petra")
panel = Blueprint("panel", __name__)

# Fallback para ordenar eventos/tarjetas cuyo timestamp es None.
# Usamos datetime.min aware (UTC) para que pueda compararse con otros
# datetimes aware sin TypeError (Python 3.14 ya no acepta mezcla int/datetime).
_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Conexión directa a Postgres (bypass del pool)
#
# El pool compartido (`db._conexion()`) se corrompe periódicamente por errores
# SSL 'bad record mac' en la red interna de Render, dejando conns muertas que
# ocupan slots hasta reiniciar el proceso. Mientras los webhooks pueden tolerar
# ese fallo (reintentos de Meta, spool en disco — PR #48), el panel sufre 503
# cada 5-10 min.
#
# Las rutas del panel abren ahora una conn TCP directa a Postgres por request
# (nueva al entrar, cerrada al salir). Paga 200-500ms extra de handshake TLS,
# pero es inmune a la corrupción del pool. El resto del app (webhooks, envíos,
# scheduler) sigue usando el pool normal.
# ---------------------------------------------------------------------------

@contextmanager
def _panel_conexion():
    """Context manager: prefiere conn directa a Postgres (bypass del pool).

    Si abrir la conn directa falla (timeout TCP, DNS, lo que sea), cae al
    pool compartido como último recurso en vez de romper el panel entero."""
    conn = None
    try:
        try:
            conn = db._conexion_directa(timeout=10)
        except Exception as exc:  # noqa: BLE001 — queremos capturar lo que sea
            log.warning("[Panel] conn directa falló (%s); cayendo al pool.", exc)
            with db._conexion() as pool_conn:
                yield pool_conn
            return
        yield conn
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — cerrar nunca debe propagar
                pass


# ---------------------------------------------------------------------------
# Helpers de token / SQL
# ---------------------------------------------------------------------------

def _check_token():
    token_cfg = os.environ.get("PANEL_TOKEN", "")
    token_req = request.args.get("token") or request.headers.get("X-Panel-Token", "")
    if not token_cfg or token_req != token_cfg:
        abort(401, description="PANEL_TOKEN no coincide")


def _rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    cols = [c.name for c in cur.description] if cur.description else []
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _scalar(conn, sql, params=()):
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Humanización y formato
# ---------------------------------------------------------------------------

# Taxonomía de buckets (humano-first, sept-2026)
# -----------------------------------------------
# Derivada on-the-fly a partir del contacto + 3 flags EXISTS por teléfono
# (tiene_in, autorizo, completo_flow). Reemplaza los chips viejos
# (nuevo / contactado / en_flujo / en_flujo:SURETI / no_contactar) por una
# vista accionable para el asesor humano:
#
#   nuevo              — todavía no se contactó
#   contactado         — contactado=TRUE pero sin IN
#   respondio          — hay IN (cliente respondió algo)
#   enviar_formulario  — Flow enviado desde el panel, o cliente ya completó
#                        (resultado_contacto IN ('enviar_formulario','flow') ó
#                         hay flow_reply en mensajes)
#   enviar_a_sureti    — resultado_contacto IN ('sureti', 'entregado')
#   recontactar        — resultado_contacto='recontactar' (asesor cerró
#                        "No le interesa ahora — recontactar después").
#                        Alta precedencia: gana sobre tiene_in / completo_flow
#                        para que no se mezclen con los que están en flujo.
#   no_contactar       — opt-out (gana sobre todo)
BUCKETS_ORDEN = (
    "nuevo", "contactado", "respondio",
    "enviar_formulario", "enviar_a_sureti", "recontactar", "no_contactar",
)

BUCKETS_LABELS = {
    "nuevo": "nuevo",
    "contactado": "contactado",
    "respondio": "respondió",
    "enviar_formulario": "enviar formulario",
    "enviar_a_sureti": "enviar a sureti",
    "recontactar": "Recontactar",
    "no_contactar": "no contactar",
}


def bucket_de(contacto, tiene_in: bool, autorizo: bool, completo_flow: bool) -> str:
    """Reglas en orden (primero que matchea gana):

    Los buckets dependen SOLO de acciones humanas (firma
    `resultado_contacto_por NOT NULL`) o auto-derivación simple
    (`tiene_in`, `contactado`). Las flags que el bot setea
    (`no_contactar=true` por detección semántica, `requiere_humano=true`
    por "pide llamada") NO afectan el bucket del panel — el bot sigue
    funcionando igual, pero no decide la clasificación.

    1. firma humana + resultado in ('broker','no_interesa') → "no_contactar"
    2. firma humana + resultado == 'recontactar'            → "recontactar"
    3. firma humana + resultado == 'sureti' (o legacy       → "enviar_a_sureti"
       'enviar_a_sureti'/'entregado')
    4. firma humana + resultado == 'enviar_formulario' (o   → "enviar_formulario"
       legacy 'flow')
    5. firma humana + resultado == 'contactado'             → "contactado"
       (acción "Regresar a contactado": vence a tiene_in)
    6. tiene_in                                             → "respondio"
    7. contactado=True                                      → "contactado"
    8. else                                                 → "nuevo"

    `autorizo` y `completo_flow` se calculan para el consumidor pero no
    entran en estas reglas: desde humano-first (sept-2026) la autorización
    y el Flow completado ya no implican bucket — sólo señalan actividad.
    """
    por = (contacto or {}).get("resultado_contacto_por")
    r = (contacto or {}).get("resultado_contacto") or ""

    # 1-4: cierres humanos explícitos (requieren firma: resultado_contacto_por)
    if por and r in ("broker", "no_interesa"):
        return "no_contactar"
    if por and r == "recontactar":
        return "recontactar"
    if por and r in ("sureti", "enviar_a_sureti", "entregado"):
        return "enviar_a_sureti"
    if por and r in ("enviar_formulario", "flow"):
        return "enviar_formulario"
    if por and r == "contactado":
        # Acción "Regresar a contactado": firma humana explícita que
        # vence a la auto-derivación `tiene_in → respondio`.
        return "contactado"

    # 6-8: derivaciones automáticas (ignoran flags del bot)
    if tiene_in:
        return "respondio"
    if contacto and contacto.get("contactado"):
        return "contactado"
    return "nuevo"


def _estado_lead(c, s, p):
    """Deriva el estado actual del lead a partir de contacto, sesión y pipeline."""
    if p:
        if p.get("fecha_desembolso"):
            return "desembolsado"
        if p.get("sureti_lead_id"):
            return "en_sureti"
        if p.get("estado"):
            return str(p["estado"])
        return "en_pipeline"
    if c and c.get("no_contactar"):
        return "no_contactar"
    if s:
        est = s.get("estado") or ""
        if est.startswith("descartado"):
            return est
        if est == "pausado_paz_salvo":
            return "pausado_paz_salvo"
        if est:
            return f"en_flujo:{est}"
        return "en_flujo"
    if c and c.get("contactado"):
        return "contactado"
    if c:
        return "nuevo"
    return "sin_contacto"


def _hace(fecha, ahora=None):
    """Fecha relativa humana en español: 'hace 3 min', 'hace 1 h 20', 'hace 2 d'."""
    if not fecha:
        return "—"
    ahora = ahora or datetime.now(timezone.utc)
    try:
        delta = ahora - fecha
    except TypeError:
        delta = ahora - fecha.replace(tzinfo=timezone.utc)
    seg = int(delta.total_seconds())
    if seg < 0:
        return "ahora"
    if seg < 60:
        return "hace unos seg"
    if seg < 3600:
        return f"hace {seg // 60} min"
    if seg < 86400:
        h = seg // 3600
        m = (seg % 3600) // 60
        return f"hace {h} h" if m == 0 else f"hace {h} h {m}"
    d = seg // 86400
    return f"hace {d} d"


def _semaforo(fecha, ahora=None):
    """Devuelve clase CSS: verde (<1h), amarillo (<24h), gris (>24h o sin fecha)."""
    if not fecha:
        return "gris"
    ahora = ahora or datetime.now(timezone.utc)
    try:
        delta = ahora - fecha
    except TypeError:
        delta = ahora - fecha.replace(tzinfo=timezone.utc)
    seg = delta.total_seconds()
    if seg < 3600:
        return "verde"
    if seg < 86400:
        return "amarillo"
    return "gris"


# Mapa de keys crudas a etiquetas humanas, para render en tabla key/value.
_KEY_HUMANO = {
    "tipo_inmueble": "Tipo",
    "avaluo_comercial": "Avalúo",
    "avaluo_catastral": "Avalúo catastral",
    "edad": "Edad",
    "direccion": "Dirección",
    "apto": "Apto",
    "barrio": "Barrio",
    "ciudad": "Ciudad",
    "estrato": "Estrato",
    "es_ph": "Es PH",
    "hipoteca": "Hipoteca vigente",
    "patrimonio": "Patrimonio",
    "paz_salvo": "Paz y salvo",
    "avaluo": "Avalúo",
    "tipo_persona": "Tipo de persona",
    "nombre": "Nombre",
    "cedula": "Cédula",
    "nit": "NIT",
    "objetivo": "Objetivo",
    "objetivo_prestamo": "Objetivo",
    "email": "Email",
    "valor_solicitado": "Valor solicitado",
    "requiere_paz_salvo": "Requiere paz y salvo",
    "patrimonio_verificar": "Patrimonio por verificar",
    "monto_estimado_m": "Monto estimado (M)",
    "autorizacion_en": "Autorización en",
    "flow": "Flow",
    "flow_step": "Paso",
    "flow_data": "Datos del flow",
    "conjunto": "Conjunto",
    "extractos_count": "Extractos recibidos",
}

_FLOW1_KEYS = ("direccion", "apto", "barrio", "ciudad", "estrato", "es_ph",
               "hipoteca", "patrimonio", "edad", "paz_salvo", "avaluo",
               "avaluo_comercial", "valor_solicitado")
_FLOW2_KEYS = ("tipo_persona", "nombre", "cedula", "nit", "edad", "objetivo",
               "objetivo_prestamo", "email")


def _humanizar_key(k):
    return _KEY_HUMANO.get(k, k.replace("_", " ").capitalize())


def _formatear_valor(v):
    """Convierte un valor crudo a texto legible."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return "sí" if v else "no"
    if isinstance(v, (int, float)):
        if abs(v) >= 1000:
            try:
                return "{:,}".format(int(v)).replace(",", ".")
            except (ValueError, OverflowError):
                return str(v)
        return str(v)
    return str(v)


def _tabla_kv(datos):
    """Convierte un dict en una lista [(label, value)] para render como tabla.
    Omite nulos/vacíos. Humaniza keys."""
    if not datos:
        return []
    if isinstance(datos, str):
        try:
            datos = json.loads(datos)
        except (ValueError, TypeError):
            return [("Datos", datos)]
    if not isinstance(datos, dict):
        return [("Datos", str(datos))]
    out = []
    for k, v in datos.items():
        if v is None or v == "" or v == {} or v == []:
            continue
        if isinstance(v, (dict, list)):
            val = json.dumps(v, ensure_ascii=False)
        else:
            val = _formatear_valor(v)
        if val is None:
            continue
        out.append((_humanizar_key(k), val))
    return out


def _separar_flows(datos):
    """A partir del flow_data (sesión), extrae campos relevantes para
    las cajas Flow 1 (Requisitos) y Flow 2 (Propietario)."""
    if not datos:
        datos = {}
    if isinstance(datos, str):
        try:
            datos = json.loads(datos)
        except (ValueError, TypeError):
            datos = {}

    base = dict(datos) if isinstance(datos, dict) else {}
    if isinstance(base.get("flow_data"), dict):
        for k, v in base["flow_data"].items():
            base.setdefault(k, v)

    flow1_items = []
    flow2_items = []
    for k in _FLOW1_KEYS:
        v = base.get(k)
        if v is None or v == "":
            continue
        flow1_items.append((_humanizar_key(k), _formatear_valor(v)))
    for k in _FLOW2_KEYS:
        v = base.get(k)
        if v is None or v == "":
            continue
        flow2_items.append((_humanizar_key(k), _formatear_valor(v)))

    return {
        "flow1_items": flow1_items,
        "flow2_items": flow2_items,
        "flow1_completo": len(flow1_items) >= 3,
        "flow2_completo": len(flow2_items) >= 3,
    }


def _humanizar_resumen_salida(resumen):
    """Mensajes salientes del bot con etiquetas internas → texto humano."""
    if not resumen:
        return "—"
    r = resumen
    if r.startswith("[AUTORIZACIÓN DATOS]"):
        extra = r[len("[AUTORIZACIÓN DATOS]"):].strip()
        return "📋 Pide autorización de datos (Ley 1581)" + (f" {extra}" if extra else "")
    if r.startswith("[FORM REQUISITOS]"):
        return "📝 Envía Flow 1 (Requisitos)"
    if r.startswith("[FORM DATOS]"):
        return "👤 Envía Flow 2 (Datos propietario)"
    if r.startswith("[PLANTILLA massi_apertura_b]"):
        resto = r[len("[PLANTILLA massi_apertura_b]"):].strip()
        return "💬 Plantilla apertura" + (f" — {resto}" if resto else "")
    if r.startswith("[PLANTILLA massi_apertura_a]"):
        resto = r[len("[PLANTILLA massi_apertura_a]"):].strip()
        return "💬 Plantilla apertura (a)" + (f" — {resto}" if resto else "")
    if r.startswith("[PLANTILLA "):
        m = re.match(r"\[PLANTILLA\s+([^\]]+)\](.*)", r)
        if m:
            return f"💬 Plantilla {m.group(1).strip()} — {m.group(2).strip()}".rstrip(" —")
    return r


def _formatear_flow_reply(resumen):
    """Si el resumen es un dict JSON, devuelve lista de líneas 'key: value'.
    Si no, devuelve None."""
    if not resumen:
        return None
    s = resumen.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return None
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        try:
            obj = json.loads(s.replace("'", '"'))
        except (ValueError, TypeError):
            return None
    if not isinstance(obj, dict):
        return None
    lineas = []
    for k, v in obj.items():
        if v is None or v == "":
            continue
        if isinstance(v, bool):
            v = "sí" if v else "no"
        lineas.append(f"{_humanizar_key(k)}: {v}")
    return lineas or None


def _pool_busy_response(endpoint):
    """Respuesta HTML liviana cuando el pool de Postgres está saturado.
    Antes esto producía un 500 crudo (traceback de PoolTimeout). Un 503 con
    mensaje legible permite al operador reintentar sin asustarse."""
    log.warning("[Panel] Pool saturado atendiendo %s — devolviendo 503.", endpoint)
    html = (
        "<!doctype html><meta charset='utf-8'><title>Panel ocupado</title>"
        "<body style='font-family:system-ui,sans-serif;padding:24px;max-width:640px;margin:0 auto'>"
        "<h2 style='color:#9a4b00'>El panel está saturado momentáneamente</h2>"
        "<p>La base de datos tiene todas las conexiones ocupadas (webhooks "
        "en vuelo). Reintentá en unos segundos.</p>"
        "<p><a href='javascript:location.reload()'>Reintentar</a></p>"
        "</body>"
    )
    return html, 503, {"Content-Type": "text/html; charset=utf-8", "Retry-After": "5"}


def _dia_etiqueta(fecha, ahora=None):
    """Devuelve 'Hoy', 'Ayer' o la fecha 'YYYY-MM-DD'."""
    if not fecha:
        return "—"
    ahora = ahora or datetime.now(timezone.utc)
    try:
        hoy = ahora.date()
        d = fecha.date()
    except AttributeError:
        return "—"
    if d == hoy:
        return "Hoy"
    if d == hoy - timedelta(days=1):
        return "Ayer"
    return d.isoformat()


# ---------------------------------------------------------------------------
# Media entrante (audio/image/video/document/sticker)
# ---------------------------------------------------------------------------

_MEDIA_TIPOS = {"audio", "image", "video", "document", "sticker"}


def _info_media(mensaje):
    """A partir de una fila de `mensajes`, deriva si es un media visible en el
    panel (audio/image/video/document/sticker) y los datos necesarios para
    rendear el reproductor/visor y el link `/panel/media/<id>`.

    Devuelve None si el mensaje no es media. Un dict:
      {kind, id, mime, caption, voice, disponible, filename}
    si lo es. `disponible=False` cuando no hay local_path en payload o el
    archivo no existe en disco: el template muestra el placeholder.
    """
    tipo = (mensaje.get("tipo") or "").lower()
    if tipo not in _MEDIA_TIPOS:
        return None
    payload = mensaje.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    local_path = payload.get("local_path")
    disponible = bool(local_path) and os.path.exists(local_path)
    return {
        "kind": tipo,
        "id": mensaje.get("id"),
        "mime": payload.get("mime") or "",
        "caption": payload.get("caption") or "",
        "voice": bool(payload.get("voice", False)),
        "filename": payload.get("filename") or "",
        "disponible": disponible,
    }


# ---------------------------------------------------------------------------
# Rutas
#
# Nota: todas las transformaciones humanas (fecha relativa, semáforo,
# humanizar resumen, etiquetas de día, flow_reply decodeado) se precalculan
# en Python y se pasan al template como campos nuevos. Así los templates
# usan solo filtros Jinja builtin y compilan con un `Environment` desnudo,
# lo que facilita las validaciones.
# ---------------------------------------------------------------------------

@panel.get("/panel/ping")
def panel_ping():
    _check_token()
    return "pong", 200, {"Content-Type": "text/plain"}


@panel.get("/panel")
def panel_lista():
    _check_token()
    token = request.args.get("token", "")
    f_estado = (request.args.get("estado") or "").strip()
    f_bucket = (request.args.get("bucket") or "").strip()
    f_ciudad = (request.args.get("ciudad") or "").strip()
    f_portal = (request.args.get("portal") or "").strip()
    f_pendientes = request.args.get("pendientes") == "1"
    f_pendiente_humano = request.args.get("pendiente_humano") == "1"
    q = (request.args.get("q") or "").strip()

    try:
        with _panel_conexion() as conn:
            # CTE que agrega 3 flags EXISTS por teléfono para derivar bucket:
            #   tiene_in      — cliente respondió algo
            #   autorizo      — dio BOTON_SI (interesado post-template)
            #   completo_flow — mandó al menos un flow_reply (Flow completado)
            contactos = _rows(conn, """
                SELECT c.telefono, c.nombre, c.ciudad, c.tipo_inmueble, c.portal,
                       c.monto_hasta_millones AS monto_hasta,
                       c.no_contactar, c.contactado, c.fecha_contacto,
                       c.fecha_scraping,
                       c.resultado_contacto, c.resultado_contacto_at,
                       c.resultado_contacto_por,
                       c.requiere_humano,
                       EXISTS (
                         SELECT 1 FROM mensajes m
                         WHERE m.telefono = c.telefono AND m.direccion = 'in'
                       ) AS tiene_in,
                       EXISTS (
                         SELECT 1 FROM mensajes m
                         WHERE m.telefono = c.telefono
                           AND ((m.tipo = 'button_reply' AND m.resumen = 'BOTON_SI')
                                OR (m.tipo = 'template_button'
                                    AND m.resumen ILIKE 'Sí me interesa%%'))
                       ) AS autorizo,
                       EXISTS (
                         SELECT 1 FROM mensajes m
                         WHERE m.telefono = c.telefono AND m.tipo = 'flow_reply'
                       ) AS completo_flow
                FROM contactos c
                ORDER BY COALESCE(c.fecha_contacto, c.fecha_scraping) DESC NULLS LAST
                LIMIT 2000
            """)
            sesiones = {r["telefono"]: r for r in _rows(conn, """
                SELECT telefono, estado, ultima_actividad FROM sesiones
            """)}
            pipelines = {r["telefono"]: r for r in _rows(conn, """
                SELECT telefono, estado, sureti_lead_id, fecha_ingreso,
                       fecha_aprobacion, fecha_desembolso, monto_aprobado,
                       avaluo_comercial
                FROM pipeline
                ORDER BY fecha_ingreso DESC
            """)}
    except PoolTimeout:
        return _pool_busy_response("/panel")

    filas_all = []
    for c in contactos:
        s = sesiones.get(c["telefono"])
        p = pipelines.get(c["telefono"])
        avaluo_m = None
        avaluo_fuente = None
        if p and p.get("avaluo_comercial"):
            try:
                avaluo_m = int(p["avaluo_comercial"]) // 1_000_000
                avaluo_fuente = "pipeline"
            except (ValueError, TypeError):
                avaluo_m = None
        if avaluo_m is None and c.get("monto_hasta"):
            try:
                avaluo_m = int(c["monto_hasta"])
                avaluo_fuente = "aprox"
            except (ValueError, TypeError):
                avaluo_m = None
        ultima = ((s or {}).get("ultima_actividad")
                  or c.get("fecha_contacto")
                  or c.get("fecha_scraping"))
        bucket = bucket_de(
            c,
            bool(c.get("tiene_in")),
            bool(c.get("autorizo")),
            bool(c.get("completo_flow")),
        )
        filas_all.append({
            **c,
            "estado": _estado_lead(c, s, p),
            "bucket": bucket,
            "bucket_label": BUCKETS_LABELS.get(bucket, bucket),
            "ultima_actividad": ultima,
            "ultima_actividad_hace": _hace(ultima),
            "ultima_actividad_semaforo": _semaforo(ultima),
            "avaluo_m": avaluo_m,
            "avaluo_fuente": avaluo_fuente,
        })

    por_estado = {}
    por_bucket = {k: 0 for k in BUCKETS_ORDEN}
    ciudades_set = set()
    portales_set = set()
    for f in filas_all:
        por_estado[f["estado"]] = por_estado.get(f["estado"], 0) + 1
        por_bucket[f["bucket"]] = por_bucket.get(f["bucket"], 0) + 1
        if f.get("ciudad"):
            ciudades_set.add(f["ciudad"])
        if f.get("portal"):
            portales_set.add(f["portal"])

    # Lista ordenada [(key, label, count)] para los chips del template.
    buckets_chips = [
        (k, BUCKETS_LABELS[k], por_bucket.get(k, 0))
        for k in BUCKETS_ORDEN
    ]

    def _match(f):
        if f_bucket and f["bucket"] != f_bucket:
            return False
        if f_estado and not f["estado"].startswith(f_estado):
            return False
        if f_ciudad and (f.get("ciudad") or "") != f_ciudad:
            return False
        if f_portal and (f.get("portal") or "") != f_portal:
            return False
        if f_pendientes:
            # Pendientes de cerrar: ya los contactamos pero no hay cierre humano.
            # OJO: `resultado_contacto` puede traer valores viejos del bot
            # (plantilla_enviada, en_flujo, …). Un cierre humano siempre deja
            # `resultado_contacto_por` seteado — ese es el discriminador.
            if not f.get("contactado") or f.get("resultado_contacto_por"):
                return False
        if f_pendiente_humano:
            # Pendiente humano: el bot marcó `requiere_humano=TRUE` y aún
            # no hay cierre humano (resultado_contacto_por sin setear).
            if not f.get("requiere_humano") or f.get("resultado_contacto_por"):
                return False
        if q:
            hay = " ".join([
                str(f.get("telefono") or ""),
                str(f.get("nombre") or ""),
            ]).lower()
            if q.lower() not in hay:
                return False
        return True

    filas = [f for f in filas_all if _match(f)][:500]

    # Labels de resultado_contacto para el badge en la lista.
    from app import panel_acciones as _pa
    labels_resultado = {k: v["label"] for k, v in _pa.ACCIONES.items()}

    return render_template(
        "panel_lista.html",
        filas=filas, por_estado=sorted(por_estado.items()), token=token,
        total=len(filas), total_sin_filtro=len(filas_all),
        ciudades=sorted(ciudades_set), portales=sorted(portales_set),
        f_estado=f_estado, f_bucket=f_bucket,
        f_ciudad=f_ciudad, f_portal=f_portal, q=q,
        f_pendientes=f_pendientes,
        f_pendiente_humano=f_pendiente_humano,
        labels_resultado=labels_resultado,
        buckets_chips=buckets_chips,
    )


@panel.get("/panel/stats")
def panel_stats():
    """Embudo de conversión y métricas del pilot."""
    _check_token()
    token = request.args.get("token", "")

    try:
        with _panel_conexion() as conn:
            captados = _scalar(conn, "SELECT COUNT(*) FROM contactos") or 0
            contactados = _scalar(conn,
                "SELECT COUNT(*) FROM contactos WHERE contactado") or 0
            respondieron = _scalar(conn,
                "SELECT COUNT(DISTINCT telefono) FROM mensajes WHERE direccion='in'") or 0
            autorizaron = _scalar(conn, """
                SELECT COUNT(DISTINCT telefono) FROM mensajes
                WHERE direccion='in' AND tipo='button_reply' AND resumen='BOTON_SI'
            """) or 0
            flow1 = _scalar(conn, """
                SELECT COUNT(DISTINCT telefono) FROM mensajes
                WHERE direccion='in' AND tipo='flow_reply'
                  AND resumen ILIKE '%%avaluo%%'
            """) or 0
            flow2 = _scalar(conn, """
                SELECT COUNT(DISTINCT telefono) FROM mensajes
                WHERE direccion='in' AND tipo='flow_reply'
                  AND (resumen ILIKE '%%cedula%%' OR resumen ILIKE '%%nit%%')
                  AND resumen ILIKE '%%nombre%%'
            """) or 0

            en_pipeline = _scalar(conn, "SELECT COUNT(*) FROM pipeline") or 0
            en_sureti = _scalar(conn,
                "SELECT COUNT(*) FROM pipeline WHERE sureti_lead_id IS NOT NULL") or 0
            desembolsados = _scalar(conn,
                "SELECT COUNT(*) FROM pipeline WHERE fecha_desembolso IS NOT NULL") or 0

            pipes = _rows(conn, """
                SELECT telefono, tipo_inmueble, avaluo_comercial
                FROM pipeline
                WHERE avaluo_comercial IS NOT NULL AND avaluo_comercial > 0
            """)

            volumen = _rows(conn, """
                SELECT DATE(fecha AT TIME ZONE 'America/Bogota') AS dia,
                       SUM(CASE WHEN direccion='out' THEN 1 ELSE 0 END) AS enviados,
                       COUNT(DISTINCT CASE WHEN direccion='in' THEN telefono END) AS respondieron
                FROM mensajes
                WHERE fecha >= NOW() - INTERVAL '7 days'
                GROUP BY DATE(fecha AT TIME ZONE 'America/Bogota')
                ORDER BY dia ASC
            """)

            horas = _rows(conn, """
                SELECT EXTRACT(HOUR FROM fecha AT TIME ZONE 'America/Bogota')::INT AS hora,
                       COUNT(*) AS n
                FROM mensajes
                WHERE direccion='in'
                GROUP BY hora
                ORDER BY hora ASC
            """)

            # ---------- Mini-vista 1: cierres humanos últimos 30d ----------
            cierres_rows = _rows(conn, """
                SELECT resultado_contacto,
                       COALESCE(resultado_contacto_por, '') AS asesor,
                       COUNT(*) AS n
                FROM contactos
                WHERE resultado_contacto_por IS NOT NULL
                  AND resultado_contacto_at >= NOW() - INTERVAL '30 days'
                GROUP BY resultado_contacto, resultado_contacto_por
                ORDER BY resultado_contacto, n DESC
            """)

            # ---------- Mini-vista 2: tiempos entre etapas (30d) ----------
            # OUT -> primer IN posterior
            tiempos_out_in = _rows(conn, """
                WITH primera_in AS (
                    SELECT telefono, MIN(fecha) AS primer_in
                    FROM mensajes
                    WHERE direccion='in'
                    GROUP BY telefono
                )
                SELECT
                    percentile_cont(0.25) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM pi.primer_in - c.fecha_contacto)/60
                    ) AS p25_min,
                    percentile_cont(0.5)  WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM pi.primer_in - c.fecha_contacto)/60
                    ) AS mediana_min,
                    percentile_cont(0.75) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM pi.primer_in - c.fecha_contacto)/60
                    ) AS p75_min,
                    COUNT(*) AS n
                FROM contactos c
                JOIN primera_in pi USING (telefono)
                WHERE c.fecha_contacto >= NOW() - INTERVAL '30 days'
                  AND pi.primer_in > c.fecha_contacto
            """)

            # IN -> BOTON_SI (primer button_reply BOTON_SI posterior al primer IN)
            tiempos_in_si = _rows(conn, """
                WITH primera_in AS (
                    SELECT telefono, MIN(fecha) AS primer_in
                    FROM mensajes
                    WHERE direccion='in'
                    GROUP BY telefono
                ),
                primer_si AS (
                    SELECT telefono, MIN(fecha) AS primer_si
                    FROM mensajes
                    WHERE direccion='in'
                      AND tipo='button_reply'
                      AND resumen='BOTON_SI'
                    GROUP BY telefono
                )
                SELECT
                    percentile_cont(0.25) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM ps.primer_si - pi.primer_in)/60
                    ) AS p25_min,
                    percentile_cont(0.5)  WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM ps.primer_si - pi.primer_in)/60
                    ) AS mediana_min,
                    percentile_cont(0.75) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM ps.primer_si - pi.primer_in)/60
                    ) AS p75_min,
                    COUNT(*) AS n
                FROM primera_in pi
                JOIN primer_si ps USING (telefono)
                WHERE ps.primer_si >= pi.primer_in
                  AND ps.primer_si >= NOW() - INTERVAL '30 days'
            """)

            # BOTON_SI -> primer flow_reply posterior
            tiempos_si_flow = _rows(conn, """
                WITH primer_si AS (
                    SELECT telefono, MIN(fecha) AS primer_si
                    FROM mensajes
                    WHERE direccion='in'
                      AND tipo='button_reply'
                      AND resumen='BOTON_SI'
                    GROUP BY telefono
                ),
                primer_flow AS (
                    SELECT telefono, MIN(fecha) AS primer_flow
                    FROM mensajes
                    WHERE direccion='in'
                      AND tipo='flow_reply'
                    GROUP BY telefono
                )
                SELECT
                    percentile_cont(0.25) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM pf.primer_flow - ps.primer_si)/60
                    ) AS p25_min,
                    percentile_cont(0.5)  WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM pf.primer_flow - ps.primer_si)/60
                    ) AS mediana_min,
                    percentile_cont(0.75) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM pf.primer_flow - ps.primer_si)/60
                    ) AS p75_min,
                    COUNT(*) AS n
                FROM primer_si ps
                JOIN primer_flow pf USING (telefono)
                WHERE pf.primer_flow >= ps.primer_si
                  AND pf.primer_flow >= NOW() - INTERVAL '30 days'
            """)

            # ---------- Mini-vista 3: detectados por filtros (30d) ----------
            detectados = _rows(conn, """
                SELECT
                  COUNT(*) FILTER (
                    WHERE no_contactar = TRUE AND resultado_contacto_por IS NULL
                  ) AS opt_outs_auto,
                  COUNT(*) FILTER (
                    WHERE no_contactar = TRUE AND resultado_contacto_por IS NOT NULL
                  ) AS opt_outs_humanos,
                  COUNT(*) FILTER (
                    WHERE requiere_humano_motivo ILIKE 'pid%%llamada%%'
                  ) AS pide_llamada,
                  COUNT(*) FILTER (
                    WHERE requiere_humano = TRUE
                      AND resultado_contacto_por IS NULL
                  ) AS requiere_humano_activos,
                  COUNT(*) FILTER (
                    WHERE resultado_contacto = 'broker'
                      AND resultado_contacto_at >= NOW() - INTERVAL '30 days'
                  ) AS brokers_cerrados
                FROM contactos
                WHERE (requiere_humano_at >= NOW() - INTERVAL '30 days'
                       OR resultado_contacto_at >= NOW() - INTERVAL '30 days')
            """)

            # ---------- Mini-vista 4: leads calientes activos ----------
            leads_calientes = _rows(conn, """
                SELECT c.telefono,
                       c.requiere_humano_motivo,
                       c.requiere_humano_at,
                       (SELECT MAX(fecha) FROM mensajes
                        WHERE telefono = c.telefono) AS ultima_act
                FROM contactos c
                WHERE requiere_humano = TRUE
                  AND resultado_contacto_por IS NULL
                ORDER BY requiere_humano_at DESC NULLS LAST
                LIMIT 50
            """)
    except PoolTimeout:
        return _pool_busy_response("/panel/stats")

    pasos = [
        ("Captados (contactos)", captados, captados),
        ("Contactados (contactado=TRUE)", contactados, captados),
        ("Respondieron (hay mensaje IN)", respondieron, contactados),
        ("Autorizaron (BOTON_SI)", autorizaron, respondieron),
        ("Flow 1 completo", flow1, autorizaron),
        ("Flow 2 completo", flow2, flow1),
        ("En pipeline", en_pipeline, flow2),
        ("Enviados a Sureti", en_sureti, en_pipeline),
        ("Desembolsados", desembolsados, en_sureti),
    ]
    embudo = []
    for nombre, n, prev in pasos:
        pct_total = round((n / captados * 100), 1) if captados else 0
        pct_prev = round((n / prev * 100), 1) if prev else 0
        barra = min(40, int((n / captados * 40))) if captados else 0
        embudo.append({
            "nombre": nombre,
            "n": n,
            "pct_total": pct_total,
            "pct_prev": pct_prev,
            "barra": barra,
            "barra_pct": round(barra / 40 * 100, 1),
        })

    RESIDENCIAL = {"apartamento", "casa", "parqueadero", "habitacion", "habitación"}
    monto_potencial_pesos = 0
    for p in pipes:
        tipo = str(p.get("tipo_inmueble") or "").strip().lower()
        avaluo = int(p.get("avaluo_comercial") or 0)
        pct = 0.40 if tipo in RESIDENCIAL else 0.30
        monto_potencial_pesos += int(avaluo * pct)
    monto_potencial_m = monto_potencial_pesos // 1_000_000

    def _comision(monto_pesos):
        m_millones = monto_pesos / 1_000_000
        if m_millones <= 150:
            return int(monto_pesos * 0.035)
        if m_millones <= 500:
            return int(monto_pesos * 0.030)
        return int(monto_pesos * 0.025)
    comision_potencial_pesos = _comision(monto_potencial_pesos)
    comision_potencial_m = comision_potencial_pesos // 1_000_000

    # Hoy en Colombia (UTC-5, sin DST).
    hoy_bog = (datetime.now(timezone.utc) - timedelta(hours=5)).date()
    vol_map = {v["dia"]: v for v in volumen}
    max_vol = 1
    dias = []
    for i in range(6, -1, -1):
        d = hoy_bog - timedelta(days=i)
        v = vol_map.get(d)
        env = int(v["enviados"]) if v else 0
        resp = int(v["respondieron"]) if v else 0
        max_vol = max(max_vol, env, resp)
        dias.append({
            "dia": d.strftime("%a %d/%m"),
            "enviados": env,
            "respondieron": resp,
        })
    for d in dias:
        d["bar_env"] = int(d["enviados"] / max_vol * 100) if max_vol else 0
        d["bar_resp"] = int(d["respondieron"] / max_vol * 100) if max_vol else 0

    hora_map = {int(h["hora"]): int(h["n"]) for h in horas}
    max_h = max([v for v in hora_map.values()] + [1])
    hist = []
    for h in range(24):
        n = hora_map.get(h, 0)
        hist.append({
            "hora": f"{h:02d}",
            "n": n,
            "alto": int(n / max_h * 100) if max_h else 0,
        })

    # ---------- Mini 1: agrupar cierres por tipo con sub-breakdown asesor ----
    cierres_por_tipo = {}
    cierres_total = 0
    for r in cierres_rows:
        tipo = r.get("resultado_contacto") or "(sin tipo)"
        asesor = r.get("asesor") or "(sin asesor)"
        n = int(r.get("n") or 0)
        cierres_total += n
        slot = cierres_por_tipo.setdefault(tipo, {"total": 0, "por_asesor": []})
        slot["total"] += n
        slot["por_asesor"].append({"asesor": asesor, "n": n})
    # Orden por total desc, mantener orden interno de asesores por volumen desc.
    cierres = []
    for tipo, slot in sorted(cierres_por_tipo.items(),
                             key=lambda kv: kv[1]["total"], reverse=True):
        slot["por_asesor"].sort(key=lambda a: a["n"], reverse=True)
        detalle = ", ".join(f"{a['asesor']}: {a['n']}" for a in slot["por_asesor"])
        cierres.append({
            "tipo": tipo,
            "total": slot["total"],
            "detalle": detalle,
        })

    # ---------- Mini 2: formatear tiempos entre etapas --------------------
    def _fmt_min(valor):
        if valor is None:
            return "-"
        try:
            m = float(valor)
        except (TypeError, ValueError):
            return "-"
        if m < 1:
            return "<1m"
        if m < 60:
            return f"{int(round(m))}m"
        if m < 60 * 24:
            return f"{m / 60:.1f}h"
        return f"{m / (60 * 24):.1f}d"

    def _etapa(nombre, rows):
        r = (rows[0] if rows else {}) or {}
        n = int(r.get("n") or 0)
        return {
            "etapa": nombre,
            "n": n,
            "p25": _fmt_min(r.get("p25_min")) if n else "-",
            "mediana": _fmt_min(r.get("mediana_min")) if n else "-",
            "p75": _fmt_min(r.get("p75_min")) if n else "-",
        }

    tiempos = [
        _etapa("OUT → primer IN", tiempos_out_in),
        _etapa("IN → BOTON_SI", tiempos_in_si),
        _etapa("BOTON_SI → Flow completo", tiempos_si_flow),
    ]

    # ---------- Mini 3: tiles de detección por filtros --------------------
    d = (detectados[0] if detectados else {}) or {}
    detectados_tiles = [
        {
            "label": "Opt-outs automáticos",
            "n": int(d.get("opt_outs_auto") or 0),
            "nota": "cerrados por el bot (no_contactar=true, sin asesor)",
        },
        {
            "label": "Opt-outs humanos",
            "n": int(d.get("opt_outs_humanos") or 0),
            "nota": "no_contactar=true cerrado por asesor",
        },
        {
            "label": "Pidieron llamada",
            "n": int(d.get("pide_llamada") or 0),
            "nota": "detectado por filtros.es_pedido_llamada()",
        },
        {
            "label": "Requiere humano activos",
            "n": int(d.get("requiere_humano_activos") or 0),
            "nota": "lead caliente aún sin cerrar",
        },
        {
            "label": "Brokers cerrados",
            "n": int(d.get("brokers_cerrados") or 0),
            "nota": "resultado_contacto='broker'",
        },
        {
            "label": "Brokers scraper",
            "n": None,
            "nota": "n/a — solo visible en logs",
        },
    ]

    # ---------- Mini 4: formatear leads calientes -------------------------
    def _fmt_dt(dt):
        if not dt:
            return "-"
        try:
            # dt es tz-aware (UTC). Convertir a Bogotá para legibilidad.
            local = dt.astimezone(timezone(timedelta(hours=-5)))
            return local.strftime("%d/%m %H:%M")
        except Exception:  # noqa: BLE001
            return str(dt)

    calientes = []
    for lc in leads_calientes:
        calientes.append({
            "telefono": lc.get("telefono") or "",
            "motivo": lc.get("requiere_humano_motivo") or "(sin motivo)",
            "requiere_humano_at": _fmt_dt(lc.get("requiere_humano_at")),
            "ultima_act": _fmt_dt(lc.get("ultima_act")),
        })

    return render_template(
        "panel_stats.html",
        token=token,
        embudo=embudo,
        monto_potencial_m=monto_potencial_m,
        comision_potencial_m=comision_potencial_m,
        comision_potencial_pesos=comision_potencial_pesos,
        dias=dias,
        hist=hist,
        max_hora_n=max_h,
        cierres=cierres,
        cierres_total=cierres_total,
        tiempos=tiempos,
        detectados_tiles=detectados_tiles,
        calientes=calientes,
    )


def _clasificar_tag(mensajes_hilo):
    """Clasifica un hilo (lista de mensajes de un teléfono) en un tag.

    Reglas (en orden de prioridad):
      - convertido        → hay flow_reply con 'objetivo'
      - pide_llamada      → algún IN con 'me pueden llamar'
      - autorizando       → button_reply BOTON_SI (sin flow_reply)
      - rechazo           → plantilla/botón 'No me interesa'
      - no_vende_ya       → 'ya no está disponible' / 'ya vendí'
      - bot_externo       → patrones típicos de bot inmobiliario
      - lead_tibio        → default
    """
    tiene_flow_objetivo = False
    tiene_boton_si = False
    textos_in = []
    for m in mensajes_hilo:
        if m.get("direccion") != "in":
            continue
        tipo = (m.get("tipo") or "").lower()
        resumen = (m.get("resumen") or "")
        resumen_l = resumen.lower()
        if tipo == "flow_reply" and "objetivo" in resumen_l:
            tiene_flow_objetivo = True
        if tipo == "button_reply" and resumen.strip().upper() == "BOTON_SI":
            tiene_boton_si = True
        textos_in.append(resumen_l)

    texto_combinado = " ".join(textos_in)

    if tiene_flow_objetivo:
        return "convertido"
    if "me pueden llamar" in texto_combinado:
        return "pide_llamada"
    if tiene_boton_si:
        return "autorizando"
    if "no me interesa" in texto_combinado:
        return "rechazo"
    if "ya no está disponible" in texto_combinado \
            or "ya no esta disponible" in texto_combinado \
            or "ya vendí" in texto_combinado \
            or "ya vendi" in texto_combinado:
        return "no_vende_ya"
    patrones_bot = (
        "gracias por tu mensaje",
        "inmobiliaria",
        "agencia",
    )
    if any(p in texto_combinado for p in patrones_bot):
        return "bot_externo"
    # "soy X" como patrón de bot externo (presentación automatizada)
    if re.search(r"\bsoy\s+\w+", texto_combinado):
        # Evitar colisión con mensajes muy cortos del usuario tipo "soy yo" —
        # pedimos que haya al menos un texto largo que sugiera presentación.
        for t in textos_in:
            if re.search(r"\bsoy\s+\w+", t) and len(t) > 25:
                return "bot_externo"
    return "lead_tibio"


_TAG_META = {
    "convertido":   {"label": "Convertido",  "color": "verde"},
    "autorizando":  {"label": "Autorizando", "color": "azul"},
    "lead_tibio":   {"label": "Lead tibio",  "color": "amarillo"},
    "pide_llamada": {"label": "Pide llamada", "color": "rojo"},
    "bot_externo":  {"label": "Bot externo", "color": "gris_claro"},
    "rechazo":      {"label": "Rechazo",     "color": "gris"},
    "no_vende_ya":  {"label": "No vende ya", "color": "gris"},
}


@panel.get("/panel/respuestas")
def panel_respuestas():
    """Timeline literal de todas las respuestas de los usuarios del día.

    Muestra, agrupado por teléfono, el hilo entero (IN del usuario + OUT del
    bot cercanos) tal cual fue, con un tag de clasificación arriba.
    Útil para leer letra por letra qué contestó la gente a la plantilla de
    apertura. Query param ``?dias=N`` extiende la ventana.
    """
    _check_token()
    token = request.args.get("token", "")

    try:
        dias = int(request.args.get("dias", "1"))
    except (TypeError, ValueError):
        dias = 1
    if dias < 1:
        dias = 1
    if dias > 30:
        dias = 30

    f_tag = (request.args.get("tag") or "").strip()

    try:
        with _panel_conexion() as conn:
            if dias == 1:
                sql = """
                    SELECT
                      m.telefono,
                      m.direccion,
                      m.tipo,
                      COALESCE(m.resumen, '') AS resumen,
                      m.fecha,
                      c.tipo_inmueble,
                      c.ciudad,
                      c.barrio,
                      c.no_contactar,
                      c.monto_hasta_millones AS monto_hasta
                    FROM mensajes m
                    LEFT JOIN contactos c ON c.telefono = m.telefono
                    WHERE m.telefono IN (
                      SELECT DISTINCT telefono FROM mensajes
                      WHERE direccion='in'
                        AND (fecha AT TIME ZONE 'America/Bogota')::date
                            = (NOW() AT TIME ZONE 'America/Bogota')::date
                    )
                    AND (m.fecha AT TIME ZONE 'America/Bogota')::date
                        = (NOW() AT TIME ZONE 'America/Bogota')::date
                    ORDER BY m.telefono, m.fecha
                """
                filas = _rows(conn, sql)
            else:
                sql = """
                    SELECT
                      m.telefono,
                      m.direccion,
                      m.tipo,
                      COALESCE(m.resumen, '') AS resumen,
                      m.fecha,
                      c.tipo_inmueble,
                      c.ciudad,
                      c.barrio,
                      c.no_contactar,
                      c.monto_hasta_millones AS monto_hasta
                    FROM mensajes m
                    LEFT JOIN contactos c ON c.telefono = m.telefono
                    WHERE m.telefono IN (
                      SELECT DISTINCT telefono FROM mensajes
                      WHERE direccion='in'
                        AND fecha >= NOW() - make_interval(days => %s)
                    )
                    AND m.fecha >= NOW() - make_interval(days => %s)
                    ORDER BY m.telefono, m.fecha
                """
                filas = _rows(conn, sql, (dias, dias))
    except PoolTimeout:
        return _pool_busy_response("/panel/respuestas")

    # Agrupar por teléfono.
    por_tel = {}
    for r in filas:
        tel = r["telefono"]
        if tel not in por_tel:
            por_tel[tel] = {
                "telefono": tel,
                "tipo_inmueble": r.get("tipo_inmueble"),
                "ciudad": r.get("ciudad"),
                "barrio": r.get("barrio"),
                "no_contactar": r.get("no_contactar"),
                "monto_hasta": r.get("monto_hasta"),
                "mensajes": [],
            }
        por_tel[tel]["mensajes"].append(r)

    # Construir tarjetas humanizadas.
    tarjetas = []
    conteo_tags = {}
    for tel, info in por_tel.items():
        mensajes_hilo = info["mensajes"]
        tag = _clasificar_tag(mensajes_hilo)
        conteo_tags[tag] = conteo_tags.get(tag, 0) + 1

        mensajes_vista = []
        for m in mensajes_hilo:
            direccion = m.get("direccion")
            tipo = m.get("tipo") or ""
            resumen = m.get("resumen") or ""
            flow_lineas = None
            if direccion == "in" and tipo == "flow_reply":
                flow_lineas = _formatear_flow_reply(resumen)
                resumen_render = resumen if not flow_lineas else ""
            elif direccion == "out":
                resumen_render = _humanizar_resumen_salida(resumen)
            else:
                resumen_render = resumen or "—"
            fecha = m.get("fecha")
            hora_hm = fecha.strftime("%H:%M") if fecha else "—"
            mensajes_vista.append({
                "direccion": direccion,
                "tipo": tipo,
                "resumen_render": resumen_render,
                "flow_lineas": flow_lineas,
                "hora_hm": hora_hm,
            })

        # Última actividad para ordenar.
        ult = None
        for m in mensajes_hilo:
            if m.get("fecha") and (ult is None or m["fecha"] > ult):
                ult = m["fecha"]

        tarjetas.append({
            "telefono": tel,
            "tipo_inmueble": info["tipo_inmueble"],
            "ciudad": info["ciudad"],
            "barrio": info["barrio"],
            "no_contactar": info["no_contactar"],
            "monto_hasta": info["monto_hasta"],
            "tag": tag,
            "tag_label": _TAG_META.get(tag, {}).get("label", tag),
            "tag_color": _TAG_META.get(tag, {}).get("color", "gris"),
            "mensajes": mensajes_vista,
            "ultima": ult,
        })

    # Orden: más reciente arriba.
    tarjetas.sort(key=lambda t: t["ultima"] or _MIN_DT, reverse=True)

    # Filtro por tag.
    if f_tag:
        tarjetas_filtradas = [t for t in tarjetas if t["tag"] == f_tag]
    else:
        tarjetas_filtradas = tarjetas

    # Chips: todos los tags que existen + "todos" al principio.
    chips = [{
        "key": "",
        "label": "Todos",
        "color": "neutro",
        "count": len(tarjetas),
        "activo": f_tag == "",
    }]
    # Orden fijo de tags para los chips.
    for key in ("convertido", "autorizando", "lead_tibio", "pide_llamada",
                "bot_externo", "rechazo", "no_vende_ya"):
        n = conteo_tags.get(key, 0)
        if n == 0 and f_tag != key:
            continue
        meta = _TAG_META.get(key, {})
        chips.append({
            "key": key,
            "label": meta.get("label", key),
            "color": meta.get("color", "gris"),
            "count": n,
            "activo": f_tag == key,
        })

    return render_template(
        "panel_respuestas.html",
        token=token,
        tarjetas=tarjetas_filtradas,
        total=len(tarjetas_filtradas),
        total_sin_filtro=len(tarjetas),
        chips=chips,
        f_tag=f_tag,
        dias=dias,
    )


@panel.get("/panel/<telefono>")
def panel_detalle(telefono):
    _check_token()
    token = request.args.get("token", "")
    try:
        with _panel_conexion() as conn:
            contactos = _rows(conn,
                "SELECT * FROM contactos WHERE telefono = %s", (telefono,))
            sesiones = _rows(conn,
                "SELECT * FROM sesiones WHERE telefono = %s", (telefono,))
            pipelines = _rows(conn,
                "SELECT * FROM pipeline WHERE telefono = %s "
                "ORDER BY fecha_ingreso DESC", (telefono,))
            documentos = _rows(conn,
                "SELECT * FROM documentos WHERE telefono = %s "
                "ORDER BY fecha_recepcion DESC", (telefono,))
            remarketing = _rows(conn,
                "SELECT * FROM remarketing WHERE telefono = %s "
                "ORDER BY fecha_envio DESC", (telefono,))
            leads = _rows(conn,
                "SELECT fecha, operacion, datos FROM leads WHERE telefono = %s "
                "ORDER BY fecha DESC LIMIT 100", (telefono,))
            mensajes = _rows(conn,
                "SELECT id, fecha, direccion, tipo, resumen, payload, "
                "       wa_msg_id, estado_entrega, estado_entrega_at, "
                "       estado_entrega_error "
                "FROM mensajes "
                "WHERE telefono = %s ORDER BY fecha DESC LIMIT 500", (telefono,))
            acciones_historial = _rows(conn,
                "SELECT id, telefono, accion, nota, actor, fecha "
                "FROM panel_acciones WHERE telefono = %s "
                "ORDER BY fecha DESC LIMIT 50", (telefono,))
    except PoolTimeout:
        return _pool_busy_response(f"/panel/{telefono}")

    contacto = contactos[0] if contactos else None
    sesion = sesiones[0] if sesiones else None
    pipeline = pipelines[0] if pipelines else None

    # Línea de tiempo
    eventos = []
    if contacto:
        portal = contacto.get("portal") or ""
        p_lower = portal.lower()
        if "manual" in p_lower or "sheet" in p_lower:
            captacion_detalle = "Lead orgánico (Google Sheet)"
        elif portal:
            captacion_detalle = f"Captado por {portal}"
        else:
            captacion_detalle = "Lead orgánico (origen desconocido)"
        if contacto.get("url_listing"):
            captacion_detalle += f" — {contacto['url_listing']}"
        eventos.append({
            "cuando": contacto.get("fecha_scraping"),
            "tipo": "captacion",
            "detalle": captacion_detalle,
        })
        if contacto.get("contactado"):
            eventos.append({
                "cuando": contacto.get("fecha_contacto"),
                "tipo": "mensaje_apertura",
                "detalle": f"Plantilla massi_apertura enviada "
                           f"(hasta ${contacto.get('monto_hasta_millones') or '?'}M)",
            })
        if contacto.get("no_contactar"):
            eventos.append({
                "cuando": contacto.get("fecha_contacto"),
                "tipo": "opt_out",
                "detalle": "Pidió no recibir más mensajes (STOP / botón Stop)",
            })
    for r in remarketing:
        eventos.append({
            "cuando": r.get("fecha_envio"),
            "tipo": f"remarketing:{r.get('tipo') or ''}",
            "detalle": (r.get("mensaje_enviado") or "")[:160]
                       + (" — RESPONDIÓ" if r.get("respondio") else ""),
        })
    for d in documentos:
        auto = " (automático)" if d.get("obtenido_automaticamente") else ""
        eventos.append({
            "cuando": d.get("fecha_recepcion"),
            "tipo": "documento",
            "detalle": f"{d.get('tipo') or 'archivo'}{auto}: {d.get('url_storage') or d.get('media_id')}",
        })
    if pipeline:
        eventos.append({
            "cuando": pipeline.get("fecha_ingreso"),
            "tipo": "pipeline",
            "detalle": f"Entró al pipeline: {pipeline.get('estado') or 'sin estado'}",
        })
        if pipeline.get("sureti_lead_id"):
            eventos.append({
                "cuando": pipeline.get("fecha_ingreso"),
                "tipo": "sureti",
                "detalle": f"Registrado en Sureti como {pipeline['sureti_lead_id']}",
            })
        if pipeline.get("fecha_aprobacion"):
            eventos.append({
                "cuando": pipeline.get("fecha_aprobacion"),
                "tipo": "aprobacion",
                "detalle": f"Aprobado por ${pipeline.get('monto_aprobado') or '?':,}",
            })
        if pipeline.get("fecha_desembolso"):
            eventos.append({
                "cuando": pipeline.get("fecha_desembolso"),
                "tipo": "desembolso",
                "detalle": f"Desembolsado — comisión ${pipeline.get('comision') or 0:,}"
                           + (" (cobrada)" if pipeline.get("comision_cobrada") else " (por cobrar)"),
            })
    for lead in leads:
        eventos.append({
            "cuando": lead.get("fecha"),
            "tipo": f"lead:{lead.get('operacion') or ''}",
            "detalle": str(lead.get("datos") or {})[:200],
        })
    eventos.sort(key=lambda e: e["cuando"] or _MIN_DT, reverse=True)

    estado = _estado_lead(contacto, sesion, pipeline)

    # Bucket humano-first para el badge del header (mismo que los chips de
    # /panel). Se deriva de 3 flags EXISTS sobre `mensajes` — en el detalle
    # ya tenemos todos los mensajes del teléfono, así que las computamos
    # inline en vez de hacer EXISTS en SQL.
    tiene_in = any(m.get("direccion") == "in" for m in mensajes)
    autorizo = any(
        (m.get("tipo") == "button_reply" and m.get("resumen") == "BOTON_SI")
        or (m.get("tipo") == "template_button"
            and (m.get("resumen") or "").lower().startswith("sí me interesa"))
        for m in mensajes
    )
    completo_flow = any(m.get("tipo") == "flow_reply" for m in mensajes)
    bucket = bucket_de(contacto, tiene_in, autorizo, completo_flow)
    bucket_label = BUCKETS_LABELS.get(bucket, bucket)

    # Avalúo / monto estimado para la caja superior.
    avaluo_m = None
    monto_estimado_m = None
    if pipeline:
        if pipeline.get("avaluo_comercial"):
            try:
                avaluo_m = int(pipeline["avaluo_comercial"]) // 1_000_000
            except (ValueError, TypeError):
                avaluo_m = None
        if pipeline.get("valor_solicitado"):
            try:
                monto_estimado_m = int(pipeline["valor_solicitado"]) // 1_000_000
            except (ValueError, TypeError):
                monto_estimado_m = None
    datos_sesion_raw = (sesion or {}).get("datos") or {}
    if isinstance(datos_sesion_raw, str):
        try:
            datos_sesion_raw = json.loads(datos_sesion_raw)
        except (ValueError, TypeError):
            datos_sesion_raw = {}
    if avaluo_m is None and isinstance(datos_sesion_raw, dict):
        fd = datos_sesion_raw.get("flow_data") or {}
        if isinstance(fd, dict) and fd.get("avaluo_comercial"):
            try:
                avaluo_m = int(fd["avaluo_comercial"]) // 1_000_000
            except (ValueError, TypeError):
                pass
        if monto_estimado_m is None and isinstance(fd, dict) and fd.get("monto_estimado_m"):
            try:
                monto_estimado_m = int(fd["monto_estimado_m"])
            except (ValueError, TypeError):
                pass

    datos_tabla = _tabla_kv(datos_sesion_raw)
    flows = _separar_flows(datos_sesion_raw)

    # Prepara mensajes para la vista: separador por día + flow_reply decodeado.
    mensajes_vista = []
    dia_prev = None
    # mensajes vienen ORDER BY fecha DESC — invertimos para render cronológico.
    for m in reversed(mensajes):
        etiqueta = _dia_etiqueta(m.get("fecha"))
        flow_lineas = None
        if m.get("direccion") == "in" and m.get("tipo") == "flow_reply":
            flow_lineas = _formatear_flow_reply(m.get("resumen"))
        resumen_render = m.get("resumen") or "—"
        if m.get("direccion") == "out":
            resumen_render = _humanizar_resumen_salida(m.get("resumen"))
        media_info = _info_media(m)
        mensajes_vista.append({
            **m,
            "etiqueta_dia": etiqueta,
            "nuevo_dia": etiqueta != dia_prev,
            "flow_lineas": flow_lineas,
            "resumen_render": resumen_render,
            "hora_hm": m.get("fecha").strftime("%H:%M") if m.get("fecha") else "—",
            "media": media_info,
        })
        dia_prev = etiqueta

    sesion_hace = _hace(sesion.get("ultima_actividad")) if sesion else ""

    # Ventana de servicio 24h de WhatsApp.
    ultimo_inbound = None
    for m in mensajes:
        if m["direccion"] == "in" and m.get("fecha"):
            ultimo_inbound = m["fecha"]
            break
    puede_responder = False
    if ultimo_inbound:
        delta = datetime.now(timezone.utc) - ultimo_inbound
        puede_responder = delta < timedelta(hours=24)

    flash = request.args.get("flash", "")
    from app import panel_acciones as _pa
    labels_resultado = {k: v["label"] for k, v in _pa.ACCIONES.items()}
    # El modal muestra solo el subconjunto de 6 acciones humanas (ver
    # panel_acciones.acciones_modal). `completar_formulario_manual` queda
    # fuera del modal porque se dispara desde su botón verde separado.
    return render_template(
        "panel_detalle.html",
        telefono=telefono, estado=estado, token=token,
        bucket=bucket, bucket_label=bucket_label,
        contacto=contacto, sesion=sesion, pipeline=pipeline,
        documentos=documentos, remarketing=remarketing,
        eventos=eventos, mensajes=mensajes, mensajes_vista=mensajes_vista,
        puede_responder=puede_responder, ultimo_inbound=ultimo_inbound,
        flash=flash,
        avaluo_m=avaluo_m, monto_estimado_m=monto_estimado_m,
        datos_tabla=datos_tabla, flows=flows,
        sesion_hace=sesion_hace,
        acciones=_pa.acciones_modal(),
        acciones_historial=acciones_historial,
        labels_resultado=labels_resultado,
    )


@panel.get("/panel/media/<int:mensaje_id>")
def servir_media(mensaje_id):
    """Sirve el binario local asociado a un mensaje de media (audio/image/
    video/document/sticker). Mismo token que el resto del panel.

    Lee `payload.local_path` y `payload.mime` de la fila `mensajes[id]`. Si
    el archivo no existe en disco (descarga que falló en su momento, disco
    rotado, media_id expirado), devuelve 404 — el template renderiza el
    placeholder "[tipo no disponible]".
    """
    _check_token()
    try:
        with _panel_conexion() as conn:
            fila = conn.execute(
                "SELECT tipo, payload FROM mensajes WHERE id = %s AND direccion = 'in'",
                (mensaje_id,),
            ).fetchone()
    except PoolTimeout:
        return _pool_busy_response(f"/panel/media/{mensaje_id}")
    if not fila:
        abort(404, description="mensaje no encontrado")

    tipo, payload = fila
    if (tipo or "").lower() not in _MEDIA_TIPOS:
        abort(404, description="mensaje no es media")

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            payload = {}
    if not isinstance(payload, dict):
        payload = {}

    local_path = payload.get("local_path") or ""
    mime = payload.get("mime") or "application/octet-stream"
    if not local_path or not os.path.exists(local_path):
        abort(404, description="archivo no disponible en disco")

    # Defensa: solo servir archivos bajo MEDIA_DIR. Evita que un payload
    # tocado a mano pida un archivo arbitrario del filesystem.
    media_dir = os.environ.get("MEDIA_DIR", "/var/data/media/wa")
    try:
        real_media = os.path.realpath(media_dir)
        real_path = os.path.realpath(local_path)
        if not real_path.startswith(real_media + os.sep) and real_path != real_media:
            log.warning("[Panel media] Intento de leer fuera de MEDIA_DIR: %s", local_path)
            abort(404)
    except Exception:
        abort(404)

    filename = payload.get("filename") or os.path.basename(local_path)
    descarga = (tipo or "").lower() == "document"
    return send_file(
        local_path,
        mimetype=mime,
        as_attachment=descarga,
        download_name=filename if descarga else None,
    )


@panel.get("/panel/<telefono>/mensajes.json")
def panel_mensajes_json(telefono):
    """Endpoint liviano para el polling del chat del panel.

    Query params:
      - token: obligatorio (mismo PANEL_TOKEN).
      - since_id (opcional): devuelve solo mensajes con id > since_id.
      - updates_window (opcional, default 50): además de los nuevos,
        devuelve el estado_entrega actual de los últimos N OUT para
        actualizar los chulos sin re-renderizar el bloque entero.

    Respuesta:
      {
        "mensajes": [ ... nuevos ... ],
        "updates":  [ {id, estado_entrega, estado_entrega_at,
                        estado_entrega_error}, ... ],
        "ultimo_id": <int|null>,
        "ultimo_inbound": "<iso-8601>|null"
      }

    Usa `_conexion_directa` (bypass del pool) porque esto se pollea cada
    segundo por panel; no queremos saturar el pool. Si falla la DB
    devolvemos 503 con `{error: "db_busy"}` y el frontend hace backoff.
    """
    _check_token()
    try:
        since_id = int(request.args.get("since_id") or 0)
    except (TypeError, ValueError):
        since_id = 0
    try:
        updates_window = int(request.args.get("updates_window") or 50)
    except (TypeError, ValueError):
        updates_window = 50
    updates_window = max(0, min(updates_window, 200))

    try:
        with db._conexion_directa(timeout=10) as conn:
            if since_id > 0:
                nuevos = _rows(conn,
                    "SELECT id, fecha, direccion, tipo, resumen, payload, "
                    "       wa_msg_id, estado_entrega, estado_entrega_at, "
                    "       estado_entrega_error "
                    "FROM mensajes "
                    "WHERE telefono = %s AND id > %s "
                    "ORDER BY id ASC LIMIT 500",
                    (telefono, since_id))
            else:
                # Sin since_id: devolvemos los últimos 50 ordenados ASC para
                # render cronológico (como el template hace al cargar).
                nuevos = _rows(conn,
                    "SELECT id, fecha, direccion, tipo, resumen, payload, "
                    "       wa_msg_id, estado_entrega, estado_entrega_at, "
                    "       estado_entrega_error "
                    "FROM mensajes "
                    "WHERE telefono = %s "
                    "ORDER BY id DESC LIMIT 50",
                    (telefono,))
                nuevos = list(reversed(nuevos))

            updates = []
            if updates_window > 0:
                updates = _rows(conn,
                    "SELECT id, estado_entrega, estado_entrega_at, "
                    "       estado_entrega_error "
                    "FROM mensajes "
                    "WHERE telefono = %s AND direccion = 'out' "
                    "ORDER BY id DESC LIMIT %s",
                    (telefono, updates_window))

            cur = conn.execute(
                "SELECT MAX(fecha) FROM mensajes "
                "WHERE telefono = %s AND direccion = 'in'",
                (telefono,))
            row = cur.fetchone()
            ultimo_inbound = row[0] if row else None
    except Exception as exc:  # noqa: BLE001 — queremos degradar a 503
        log.warning("[Panel JSON] db error para %s: %s", telefono, exc)
        return jsonify({"error": "db_busy"}), 503

    def _ser(m, incluir_payload=True):
        d = dict(m)
        # datetimes a ISO.
        for k in ("fecha", "estado_entrega_at"):
            v = d.get(k)
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
        # payload puede venir como str (algunas filas viejas) o dict.
        if incluir_payload:
            p = d.get("payload")
            if isinstance(p, str):
                try:
                    d["payload"] = json.loads(p)
                except (ValueError, TypeError):
                    d["payload"] = None
        else:
            d.pop("payload", None)
        return d

    mensajes_ser = [_ser(m) for m in nuevos]
    # Precomputamos info de render para que el JS no duplique lógica.
    for mv, mo in zip(mensajes_ser, nuevos):
        mv["hora_hm"] = mo["fecha"].strftime("%H:%M") if mo.get("fecha") else "—"
        mv["resumen_render"] = (
            _humanizar_resumen_salida(mo.get("resumen"))
            if mo.get("direccion") == "out" else (mo.get("resumen") or "—")
        )
        if mo.get("direccion") == "in" and mo.get("tipo") == "flow_reply":
            mv["flow_lineas"] = _formatear_flow_reply(mo.get("resumen"))
        else:
            mv["flow_lineas"] = None
        mv["media"] = _info_media(mo)

    updates_ser = [_ser(u, incluir_payload=False) for u in updates]

    ultimo_id = max((m["id"] for m in nuevos), default=since_id or 0)

    return jsonify({
        "mensajes": mensajes_ser,
        "updates": updates_ser,
        "ultimo_id": ultimo_id,
        "ultimo_inbound": ultimo_inbound.isoformat() if ultimo_inbound else None,
    })


@panel.post("/panel/<telefono>/responder")
def panel_responder(telefono):
    _check_token()
    token = request.args.get("token", "")
    texto = (request.form.get("texto") or "").strip()
    if not texto:
        return redirect(url_for("panel.panel_detalle", telefono=telefono,
                                token=token, flash="vacio"))

    try:
        with _panel_conexion() as conn:
            cur = conn.execute(
                "SELECT MAX(fecha) FROM mensajes WHERE telefono=%s AND direccion='in'",
                (telefono,))
            ultimo = cur.fetchone()[0]
    except PoolTimeout:
        return _pool_busy_response(f"/panel/{telefono}/responder")
    if not ultimo or (datetime.now(timezone.utc) - ultimo) >= timedelta(hours=24):
        return redirect(url_for("panel.panel_detalle", telefono=telefono,
                                token=token, flash="fuera_24h"))

    from app import whatsapp as wa
    try:
        # log=False: evitamos que `whatsapp._log_out` escriba al pool
        # compartido. Si el pool está saturado, log_mensaje cae al spool
        # a disco (drain cada 10 min, PR #64) y el chat queda vacío hasta
        # entonces. En su lugar insertamos directo via conn directa aquí.
        resp = wa.send_text(telefono, texto, log=False)
    except Exception as exc:
        log.exception("[Panel] Falló envío manual a %s", telefono)
        return redirect(url_for("panel.panel_detalle", telefono=telefono,
                                token=token, flash=f"error:{exc}"[:120]))

    # `_dispatch` ya extrae `wa_msg_id` del body de Meta (PR chulos). Lo
    # capturamos acá para persistirlo junto al OUT; sin él los webhooks de
    # status (sent/delivered/read) no pueden matchear la fila y el chulo
    # se queda en 🕐 para siempre.
    wa_msg_id = (resp or {}).get("wa_msg_id") if isinstance(resp, dict) else None

    # Log DIRECTO (bypass del pool) para que el OUT aparezca al instante
    # en el chat tras el redirect. Si falla, no rompemos el envío — el
    # mensaje ya se mandó por WhatsApp y lo peor que pasa es que no
    # quede en la DB (muy raro: _conexion_directa abre una conn fresca
    # a Postgres por request).
    try:
        with db._conexion_directa(timeout=10) as conn:
            conn.execute(
                "INSERT INTO mensajes (telefono, direccion, tipo, resumen, "
                "       payload, wa_msg_id) "
                "VALUES (%s, 'out', 'text', %s, %s::jsonb, %s)",
                (
                    str(telefono)[:150],
                    (texto or "")[:4000],
                    json.dumps({"origen": "panel_responder", "actor": "humano"}),
                    wa_msg_id,
                ),
            )
            conn.commit()
    except Exception:
        log.exception("[Panel] log directo falló para %s; el mensaje salió por "
                      "WhatsApp pero puede no aparecer inmediatamente en el chat",
                      telefono)

    return redirect(url_for("panel.panel_detalle", telefono=telefono,
                            token=token, flash="enviado"))


# ---------------------------------------------------------------------------
# Formulario manual: el asesor completa los Flows 1+2 EN NOMBRE del cliente
# mientras habla con él por WhatsApp o teléfono. Al guardar queda igual que
# un flow_reply completo (pipeline poblado, bucket pasa a "enviar a sureti",
# chat sigue vivo para documentos pendientes).
# ---------------------------------------------------------------------------

# Mapas que replican la lógica del bot al procesar un flow_reply: ciudad
# llega como id del Flow (BOGOTA, CAJICA, …) y acá la convertimos a nombre
# humano para que el pipeline quede con el mismo shape que si lo hubiera
# llenado el cliente.
_CIUDADES_FORM_MANUAL = {
    "BARRANQUILLA": "Barranquilla", "BOGOTA": "Bogotá", "CAJICA": "Cajicá",
    "CARTAGENA": "Cartagena", "CHIA": "Chía", "CUCUTA": "Cúcuta",
    "MEDELLIN": "Medellín", "SANTA_MARTA": "Santa Marta",
    "ZIPAQUIRA": "Zipaquirá", "OTRA": "Otra",
}

_OBJETIVO_FORM_MANUAL = {
    "OBJ_CAPITAL": "capital de trabajo",
    "OBJ_DEUDAS": "pagar deudas",
    "OBJ_INVERSION": "inversión",
    "OBJ_GASTOS": "gastos personales",
    "OBJ_OTRO": "otro",
}


def _limpiar_cedula_form_manual(valor):
    """Idéntico a bot._limpiar_cedula pero local al panel para no acoplarnos
    con el handler del bot. Devuelve la cédula normalizada o "" si no es
    válida (solo dígitos, 5–12 caracteres)."""
    valor = str(valor or "").strip()
    if valor.endswith(".0"):
        valor = valor[:-2]
    valor = valor.replace(".", "").replace("-", "").replace(" ", "")
    return valor if valor.isdigit() and 5 <= len(valor) <= 12 else ""


def _correo_valido_form_manual(valor):
    usuario, _, dominio = valor.partition("@")
    return bool(usuario) and "." in dominio and " " not in valor


@panel.get("/panel/<telefono>/formulario-manual")
def formulario_manual_vista(telefono):
    """Renderiza la vista del formulario manual con pre-llenado desde contactos.

    El asesor abre esta vista cuando ya habló con el cliente por WA/teléfono
    y quiere capturar los Flows 1 y 2 en nombre del cliente. Precarga lo que
    ya tenemos (ciudad, barrio, nombre, tipo_inmueble, precio) y pide todo
    lo demás, incluyendo el checkbox de autorización verbal (Ley 1581).
    """
    _check_token()
    token = request.args.get("token", "")
    try:
        with _panel_conexion() as conn:
            contactos = _rows(
                conn, "SELECT * FROM contactos WHERE telefono = %s",
                (telefono,))
    except PoolTimeout:
        return _pool_busy_response(f"/panel/{telefono}/formulario-manual")

    contacto = contactos[0] if contactos else None
    flash = request.args.get("flash", "")
    return render_template(
        "panel_formulario_manual.html",
        telefono=telefono, token=token, contacto=contacto, flash=flash,
    )


@panel.post("/panel/<telefono>/formulario-manual")
def formulario_manual_guardar(telefono):
    """Guarda los datos del formulario manual como si fuera un Flow completo.

    Al guardar:
      1. `state.save_pipeline(...)` — mismo shape que bot._guardar.
      2. Fila de audit en `panel_acciones` con accion
         `completar_formulario_manual`.
      3. `UPDATE contactos SET resultado_contacto='enviar_a_sureti',
         requiere_humano=false, resultado_contacto_* = now/actor/nota`.
      4. Mensaje sintético `tipo='flow_reply_manual'` en `mensajes` con un
         resumen JSON para que aparezca en el chat del panel.

    Validaciones: `avaluo`, `cedula`, `email`, `nombre` son obligatorios, el
    checkbox `autorizacion_verbal` también. Si falta cualquiera → redirect
    con flash=error_campos (nada se guarda). Si `avaluo < 50` → warning
    pero igual guarda (flash=guardado_warn_avaluo).
    """
    _check_token()
    token = request.args.get("token", "")
    form = request.form

    # Validaciones mínimas: sin estos campos el pipeline queda inútil.
    requeridos = ("avaluo", "cedula", "email", "nombre")
    faltan = [k for k in requeridos if not (form.get(k) or "").strip()]
    if faltan or not form.get("autorizacion_verbal"):
        return redirect(url_for(
            "panel.formulario_manual_vista", telefono=telefono,
            token=token, flash="error_campos"))

    actor = (form.get("actor") or "").strip()
    nota = (form.get("nota") or "").strip()

    cedula = _limpiar_cedula_form_manual(form.get("cedula"))
    email = (form.get("email") or "").strip().lower()
    nombre = (form.get("nombre") or "").strip()
    if not cedula or not _correo_valido_form_manual(email):
        return redirect(url_for(
            "panel.formulario_manual_vista", telefono=telefono,
            token=token, flash="error_campos"))

    # Avalúo viene en millones (como el Flow 1 lo pide).
    try:
        avaluo_m = int(re.sub(r"\D", "", form.get("avaluo") or "")) or 0
    except (ValueError, TypeError):
        avaluo_m = 0
    if avaluo_m <= 0:
        return redirect(url_for(
            "panel.formulario_manual_vista", telefono=telefono,
            token=token, flash="error_campos"))
    avaluo_pesos = avaluo_m * 1_000_000
    avaluo_bajo = avaluo_m < 50  # warning pero guarda igual.

    # Flow 1 — inmueble.
    direccion = (form.get("direccion") or "").strip()
    apto = (form.get("apto") or "").strip()
    barrio = (form.get("barrio") or "").strip()
    ciudad_raw = (form.get("ciudad") or "").strip()
    ciudad = _CIUDADES_FORM_MANUAL.get(ciudad_raw.upper(), ciudad_raw)
    try:
        estrato = int((form.get("estrato") or "").strip() or 0) or None
    except (ValueError, TypeError):
        estrato = None
    es_ph = (form.get("es_ph") or "").upper() == "SI"
    paz_salvo_si = (form.get("paz_salvo") or "").upper() == "SI"
    hipoteca_si = (form.get("hipoteca") or "").upper() == "SI"
    patrimonio_si = (form.get("patrimonio") or "").upper() == "SI"
    edad_mayor_75 = (form.get("edad") or "").upper() == "SI"

    # Flow 2 — propietario.
    tipo_persona = (form.get("tipo_persona") or "NATURAL").upper()
    objetivo_raw = (form.get("objetivo") or "").strip()
    objetivo = _OBJETIVO_FORM_MANUAL.get(objetivo_raw.upper(), objetivo_raw)
    try:
        edad_propietario = int(form.get("edad_propietario") or 0) or None
    except (ValueError, TypeError):
        edad_propietario = None

    direccion_inmueble = ", ".join(
        p for p in (direccion, apto, barrio) if p)

    # Shape de pipeline idéntico al que escribe el bot en _guardar().
    data_pipeline = {
        "telefono": telefono,
        "nombre": nombre,
        "cedula": cedula,
        "email": email,
        "edad": edad_propietario,
        "direccion_inmueble": direccion_inmueble,
        "ciudad": ciudad,
        "estrato": estrato,
        "es_ph": es_ph,
        "objetivo_prestamo": objetivo,
        "valor_solicitado": None,
        "avaluo_comercial": avaluo_pesos,
        "requiere_paz_salvo": paz_salvo_si,
        "autorizacion_datos_en": None,  # verbal, no hay timestamp del Flow
        "estado": "nuevo",
    }

    try:
        from app import state as _state
        _state.save_pipeline(data_pipeline)
    except Exception:
        log.exception("[FormManual] save_pipeline falló tel=%s", telefono)
        return redirect(url_for(
            "panel.formulario_manual_vista", telefono=telefono,
            token=token, flash="error_db"))

    # UPDATE contactos + INSERT en panel_acciones (misma conn/transacción).
    try:
        updates = {
            "resultado_contacto": "enviar_a_sureti",
            "resultado_contacto_at": "NOW()",
            "resultado_contacto_por": actor or "",
            "resultado_contacto_nota": nota or "",
            "requiere_humano": False,
        }
        db.aplicar_accion_panel(
            telefono, updates, "completar_formulario_manual",
            nota, actor)
    except Exception:
        log.exception("[FormManual] aplicar_accion_panel falló tel=%s",
                      telefono)
        return redirect(url_for(
            "panel.formulario_manual_vista", telefono=telefono,
            token=token, flash="error_db"))

    # Mensaje sintético para que el cierre aparezca en el chat del panel.
    # Si el INSERT falla, no revertimos el guardado: el pipeline ya está
    # escrito y el audit es la fuente de verdad del cierre humano.
    resumen_flow = {
        "nombre": nombre, "cedula": cedula, "email": email,
        "ciudad": ciudad, "barrio": barrio, "direccion": direccion,
        "apto": apto, "estrato": estrato,
        "es_ph": "sí" if es_ph else "no",
        "hipoteca": "sí" if hipoteca_si else "no",
        "patrimonio": "sí" if patrimonio_si else "no",
        "edad_mayor_75": "sí" if edad_mayor_75 else "no",
        "paz_salvo": "sí" if paz_salvo_si else "no",
        "avaluo": avaluo_m, "objetivo": objetivo,
        "edad_propietario": edad_propietario,
        "tipo_persona": tipo_persona,
        "origen": "panel_formulario_manual", "actor": actor,
    }
    try:
        with db._conexion_directa(timeout=10) as conn:
            conn.execute(
                "INSERT INTO mensajes (telefono, direccion, tipo, resumen, "
                "       payload) VALUES (%s, 'in', 'flow_reply_manual', "
                "       %s, %s::jsonb)",
                (
                    str(telefono)[:150],
                    json.dumps(resumen_flow, ensure_ascii=False)[:4000],
                    json.dumps({
                        "origen": "panel_formulario_manual",
                        "actor": actor,
                        "nota": nota,
                    }),
                ),
            )
            conn.commit()
    except Exception:
        log.exception("[FormManual] no se pudo insertar mensaje sintético "
                      "tel=%s (el guardado principal ya ocurrió)", telefono)

    flash = "guardado_warn_avaluo" if avaluo_bajo else "guardado"
    return redirect(url_for(
        "panel.panel_detalle", telefono=telefono, token=token, flash=flash))


@panel.post("/panel/<telefono>/accion")
def ejecutar_accion(telefono):
    """Aplica una acción humana del panel (cierre de conversación / audit).

    Acepta form-encoded (desde el modal del template) o JSON (desde scripts).
    Siempre valida token. Si viene de form, redirige al detalle con `flash`;
    si viene de JSON, devuelve el dict de resultado como body.
    """
    _check_token()
    token = request.args.get("token", "")

    if request.is_json:
        body = request.get_json(silent=True) or {}
        accion = (body.get("accion") or "").strip()
        nota = (body.get("nota") or "").strip()
        actor = (body.get("actor") or "").strip()
    else:
        accion = (request.form.get("accion") or "").strip()
        nota = (request.form.get("nota") or "").strip()
        actor = (request.form.get("actor") or "").strip()

    if not accion:
        if request.is_json:
            return jsonify({"ok": False, "error": "accion_requerida"}), 400
        return redirect(url_for("panel.panel_detalle", telefono=telefono,
                                token=token, flash="accion_requerida"))

    try:
        from app import panel_acciones
        resultado = panel_acciones.aplicar(telefono, accion, nota, actor)
        if not resultado["ok"]:
            if request.is_json:
                return jsonify(resultado), 400
            return redirect(url_for("panel.panel_detalle", telefono=telefono,
                                    token=token,
                                    flash=f"accion_err:{resultado['error']}"))
        if request.is_json:
            return jsonify(resultado)
        return redirect(url_for("panel.panel_detalle", telefono=telefono,
                                token=token, flash="accion_ok"))
    except Exception as exc:  # noqa: BLE001
        log.exception("[Panel] Error aplicando acción %s a %s: %s",
                      accion, telefono, exc)
        if request.is_json:
            return jsonify({"ok": False, "error": "server_error"}), 500
        return redirect(url_for("panel.panel_detalle", telefono=telefono,
                                token=token, flash="accion_err:server"))
