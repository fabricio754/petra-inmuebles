"""Broadcasts filtrados — envíos masivos de templates WhatsApp.

Un broadcast toma un dict de filtros (ciudad / portal / bucket) + un template
aprobado + variables, y envía secuencialmente a los contactos que matchean.
Respeta Ley 2300 implícita porque los templates sólo pueden ser los que ya
estén aprobados por Meta.

Reglas:
  - SKIP contactos con `no_contactar = TRUE` siempre.
  - SKIP contactos con `bot_pausado_hasta` futuro: NO, se envía igual pero
    logueamos warning (decisión documentada: una pausa de bot per lead es
    para dejar al humano manejar la conversación, no para prohibir broadcasts
    deliberados del asesor).
  - SKIP contactos que ya recibieron ESTE template en las últimas 24h
    (`ya_recibio_template_en_24h`) → `razon='duplicado'`.
  - Rate limiting: `time.sleep(RATE_SLEEP)` entre envíos → ≤10 msgs/s.
  - Si WhatsApp devuelve 429 → backoff exponencial (hasta 60s).
"""
import logging
import random
import time

from app import db, whatsapp

log = logging.getLogger("petra")

# Pausa entre envíos: 10 msgs/s = 100ms. En producción, el primer envío de
# cada día puede necesitar más pausa (los primeros 50 van a ~5 msgs/s para
# no gatillar el "warm-up" de Meta).
RATE_SLEEP = 0.1
RATE_SLEEP_INICIAL = 0.25
UMBRAL_INICIAL = 50  # los primeros 50 van más lentos


def preview_broadcast(filtros: dict) -> list[dict]:
    """Devuelve lista de contactos que matchean los filtros. No envía nada.

    Incluye SOLO contactos con `no_contactar = FALSE`. Filtros vacíos no
    aplican. Precomputa el bucket igual que `panel_lista` para que el filtro
    de bucket funcione.
    """
    f_ciudad = (filtros or {}).get("ciudad") or ""
    f_portal = (filtros or {}).get("portal") or ""
    f_bucket = (filtros or {}).get("bucket") or ""

    wh = ["COALESCE(c.no_contactar, FALSE) = FALSE"]
    params: list = []
    if f_ciudad:
        wh.append("c.ciudad = %s")
        params.append(f_ciudad)
    if f_portal:
        wh.append("c.portal = %s")
        params.append(f_portal)
    where_sql = " AND ".join(wh)

    sql = f"""
        SELECT c.id, c.telefono, c.nombre, c.ciudad, c.portal,
               c.resultado_contacto, c.resultado_contacto_por, c.contactado,
               EXISTS (SELECT 1 FROM mensajes m
                       WHERE m.telefono = c.telefono AND m.direccion = 'in'
                      ) AS tiene_in
        FROM contactos c
        WHERE {where_sql}
        ORDER BY COALESCE(c.fecha_contacto, c.fecha_scraping) DESC NULLS LAST
        LIMIT 5000
    """
    try:
        with db._conexion_directa(timeout=15) as conn:
            cur = conn.execute(sql, params)
            cols = [c.name for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        log.warning("[broadcasts.preview] fallo: %s", exc)
        return []

    # Derivar bucket inline (misma lógica que panel.bucket_de pero mini).
    def _bucket(c):
        por = c.get("resultado_contacto_por")
        r = c.get("resultado_contacto") or ""
        if por and r in ("broker", "no_interesa"):
            return "no_contactar"
        if por and r == "recontactar":
            return "recontactar"
        if por and r in ("sureti", "enviar_a_sureti", "entregado"):
            return "enviar_a_sureti"
        if por and r in ("enviar_formulario", "flow"):
            return "enviar_formulario"
        if por and r == "contactado":
            return "contactado"
        if c.get("tiene_in"):
            return "respondio"
        if c.get("contactado"):
            return "contactado"
        return "nuevo"

    out = []
    for c in rows:
        c["bucket"] = _bucket(c)
        if f_bucket and c["bucket"] != f_bucket:
            continue
        out.append(c)
    return out


def _enviar_template(telefono: str, template_name: str,
                     params: dict) -> dict:
    """Envía un template aprobado via whatsapp.Cloud API.

    Si el nombre del template matchea `massi_apertura_*`, lo despacha por
    `send_plantilla_apertura` (ya existe). Sino, construye el payload
    directamente con los `params` como variables body.

    Devuelve el result de `_dispatch` (dict con `status`).
    """
    if template_name.startswith("massi_apertura"):
        return whatsapp.send_plantilla_apertura(
            telefono,
            tipo=params.get("tipo"),
            portal=params.get("portal"),
            monto_hasta=params.get("monto_hasta"),
            operacion=params.get("operacion"),
        )
    # Genérico: construye payload template con body params ordenados por
    # clave numérica (1, 2, 3, …).
    body_params = []
    for k in sorted(params.keys(), key=lambda x: (len(x), x)):
        body_params.append({"type": "text", "text": str(params[k])[:1024]})
    payload = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": whatsapp.PLANTILLA_IDIOMA or "es_CO"},
            "components": [{"type": "body", "parameters": body_params}]
            if body_params else [],
        },
    }
    return whatsapp._dispatch(payload, f"[TEMPLATE {template_name}]")


def ejecutar_broadcast_lista(broadcast_id: int, destinatarios: list[dict],
                             template_name: str, params: dict,
                             autor: str = "asesor") -> dict:
    """Ejecuta el envío secuencial. Devuelve {total, enviados, fallidos,
    errores}. No lanza — captura cada error por contacto."""
    total = len(destinatarios)
    enviados = 0
    fallidos = 0
    errores: list = []

    for idx, c in enumerate(destinatarios):
        cid = c.get("id")
        tel = c.get("telefono")
        if not tel or not cid:
            fallidos += 1
            errores.append({"telefono": tel, "razon": "sin_telefono"})
            continue
        # Pausa de bot: warning pero envía igual.
        try:
            if db.bot_esta_pausado(tel):
                log.warning("[broadcasts] tel=%s bot pausado — se envía igual",
                            tel)
        except Exception:
            pass
        # Idempotencia 24h.
        try:
            if db.ya_recibio_template_en_24h(cid, template_name):
                errores.append({"telefono": tel, "razon": "duplicado"})
                fallidos += 1
                continue
        except Exception:
            pass

        # Rate limiting: inicial más lento.
        if idx < UMBRAL_INICIAL:
            time.sleep(RATE_SLEEP_INICIAL)
        else:
            time.sleep(RATE_SLEEP)

        try:
            res = _enviar_template(tel, template_name, params)
            status = res.get("status") if isinstance(res, dict) else None
            if status == 429:
                # Backoff exponencial hasta 60s, luego reintentar una vez.
                back = min(60, 2 ** min(5, idx))
                log.warning("[broadcasts] rate limit 429 — backoff %ss", back)
                time.sleep(back + random.random())
                res = _enviar_template(tel, template_name, params)
                status = res.get("status") if isinstance(res, dict) else None
            ok = (status in (200, 201, "dry-run", "paused")
                  or (isinstance(status, int) and status < 300))
            if ok:
                enviados += 1
                try:
                    db.registrar_evento(
                        cid, "broadcast_enviado",
                        {"broadcast_id": broadcast_id,
                         "template_name": template_name},
                        autor=autor,
                    )
                except Exception:
                    pass
            else:
                fallidos += 1
                errores.append({"telefono": tel,
                                "razon": f"status={status}",
                                "body": (str(res)[:200] if res else "")})
        except Exception as exc:
            log.exception("[broadcasts] send fallo tel=%s", tel)
            fallidos += 1
            errores.append({"telefono": tel, "razon": str(exc)[:200]})

    return {"total": total, "enviados": enviados, "fallidos": fallidos,
            "errores": errores[:200]}
