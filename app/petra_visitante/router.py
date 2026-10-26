"""Dispatcher principal del flujo VISITANTE (lectores de QR).

Estado persistido en `contactos.meta->>'visitante_estado'` (jsonb).
Datos auxiliares (conjunto_id, borrador de publicación, inmueble en
foco, etc.) se guardan en el mismo `contactos.meta`.

Estados (string, el router hace split por ':' para extraer sub-id):
    menu
    arriendo_lista
    arriendo_detalle:{inmueble_id}
    arriendo_contactar:{inmueble_id}
    venta_lista
    venta_detalle:{inmueble_id}
    venta_contactar:{inmueble_id}
    publicar_operacion
    publicar_tipo
    publicar_identificador
    publicar_area
    publicar_precio
    publicar_autorizacion
    publicar_hecho

Entrada ÚNICA: `handle_message(telefono, texto, button_id, list_id,
contacto_nombre, wa_msg_id)`. El webhook_worker ya hizo dedup del
wamid y mandó el typing indicator — no hace falta repetirlo acá.
"""
import json
import logging
import re

from app import db as _db
from app import petra_wa as _pwa
from app.petra_visitante import menus as _menus
from app.petra_visitante import qr_parser as _qr
from app.petra_visitante import textos as T

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Detección de "soy visitante vs soy agente"
# ---------------------------------------------------------------------------

_GREETINGS = {
    "hola", "holaa", "holaaa", "ola", "buenas", "buenos dias", "buenos días",
    "buenas tardes", "buenas noches", "hey", "hi", "hello",
    "menu", "menú", "hogar", "inicio",
}


def _normalize(texto):
    return (texto or "").strip().lower()


def _es_saludo(texto):
    t = _normalize(texto).strip(" .,!¡?¿").split("\n", 1)[0]
    if not t:
        return False
    if t in _GREETINGS:
        return True
    primera = t.split()[0] if t.split() else ""
    return primera in _GREETINGS


def _hard_override_agente(texto):
    """`soy agente` fuerza salir del flujo visitante — pase lo que pase."""
    return "soy agente" in _normalize(texto)


def _leer_contacto(conn, telefono):
    """Devuelve (estado_agente, meta_dict). Si el contacto no existe, (None, {}).

    - estado_agente se mira primero en `contactos.estado_agente` (nuevo),
      y como fallback en `petra_contacto_roles.metadata->>'estado_agente'`
      (que es donde `petra_agents` guarda el estado real hoy).
    - meta es `contactos.meta` (jsonb), o {} si no existe fila o está vacío.
    """
    cur = conn.execute(
        "SELECT estado_agente, meta FROM contactos WHERE telefono=%s",
        (telefono,),
    )
    row = cur.fetchone()
    estado_c = None
    meta = {}
    if row:
        estado_c = row[0]
        if row[1]:
            meta = dict(row[1]) if not isinstance(row[1], dict) else dict(row[1])
    # Fallback: estado real del agent flow vive en petra_contacto_roles.
    try:
        r2 = conn.execute(
            "SELECT r.metadata->>'estado_agente' FROM petra_contactos c "
            "JOIN petra_contacto_roles r "
            "  ON r.contacto_id=c.id AND r.rol='agente_petra' "
            "WHERE c.telefono=%s",
            (telefono,),
        ).fetchone()
        if r2 and r2[0]:
            estado_c = estado_c or r2[0]
    except Exception:
        # Si petra_contactos no está lista en esta DB, seguimos solo con
        # contactos.estado_agente — no es fatal.
        pass
    return estado_c, meta


def es_visitante(telefono, texto):
    """¿Este mensaje le toca al flujo visitante o al de agentes?

    Reglas (en orden):
      1) `soy agente` como override duro → False (y limpia visitante_estado).
      2) Marker `Cod:` en el texto (QR recién escaneado) → True SIEMPRE,
         incluso si el teléfono ya está registrado como agente. El QR es
         una acción explícita: la persona está parada frente al edificio.
      3) Visitante ya en flow (sticky) → True, incluso si el teléfono
         también es agente. Si no, los botones del menú visitante caerían
         al flow de agentes en el próximo mensaje.
      4) Si el agente está en algún estado real (recolectando, inscrito, …),
         el flow de agentes manda → False.
      5) Saludo (`hola`, `menu`, etc.) sin ser agente conocido → True.
      6) Resto → False (que lo tome la cascada legacy).
    """
    try:
        with _db._conexion_directa(timeout=5) as conn:
            estado_agente, meta = _leer_contacto(conn, telefono)
            vis_estado = (meta or {}).get("visitante_estado")

            # 1) Override duro: `soy agente` saca al visitante del flow.
            if _hard_override_agente(texto):
                if vis_estado:
                    meta.pop("visitante_estado", None)
                    meta.pop("visitante_conjunto_id", None)
                    meta.pop("visitante_pub", None)
                    _guardar_meta(conn, telefono, meta)
                return False

            # 2) QR escaneado recién — prioridad absoluta sobre estado agente.
            if _qr.tiene_marker_qr(texto):
                return True

            # 3) Visitante ya en flow — sticky, prioridad sobre agente.
            if vis_estado:
                return True

            # 4) Agente ya identificado — el agent flow manda.
            if estado_agente and estado_agente != "desconocido":
                return False

            # 5) Saludo de alguien sin flag de agente.
            if _es_saludo(texto):
                return True
    except Exception:
        log.exception("[visitante] es_visitante fallo tel=%s", telefono)
        return False
    return False


# ---------------------------------------------------------------------------
# Persistencia de estado — contactos.meta jsonb
# ---------------------------------------------------------------------------

def _upsert_contacto(conn, telefono, nombre=None):
    row = conn.execute(
        "SELECT id FROM contactos WHERE telefono=%s", (telefono,),
    ).fetchone()
    if row:
        if nombre:
            conn.execute(
                "UPDATE contactos SET nombre=COALESCE(nombre, %s) "
                "WHERE telefono=%s",
                (nombre, telefono),
            )
        return row[0]
    r = conn.execute(
        "INSERT INTO contactos (telefono, nombre) VALUES (%s, %s) RETURNING id",
        (telefono, nombre),
    ).fetchone()
    return r[0]


def _guardar_meta(conn, telefono, meta):
    conn.execute(
        "UPDATE contactos SET meta=%s::jsonb WHERE telefono=%s",
        (json.dumps(meta or {}), telefono),
    )


def _set_estado(conn, telefono, estado, meta=None, extra=None):
    """Setea `visitante_estado` dentro de `meta`. `extra` son otras keys
    que se mezclan al mismo tiempo (ahorra una query)."""
    meta = dict(meta or {})
    meta["visitante_estado"] = estado
    if extra:
        meta.update(extra)
    _guardar_meta(conn, telefono, meta)
    return meta


# ---------------------------------------------------------------------------
# Resolución del conjunto a partir del contacto / QR
# ---------------------------------------------------------------------------

def _conjunto_por_codigo(conn, codigo):
    row = conn.execute(
        "SELECT id, codigo, nombre, ciudad FROM conjuntos_visitante "
        "WHERE codigo=%s",
        (codigo,),
    ).fetchone()
    if not row:
        return None
    return {"id": row[0], "codigo": row[1], "nombre": row[2], "ciudad": row[3]}


def _conjunto_por_id(conn, cid):
    row = conn.execute(
        "SELECT id, codigo, nombre, ciudad FROM conjuntos_visitante WHERE id=%s",
        (cid,),
    ).fetchone()
    if not row:
        return None
    return {"id": row[0], "codigo": row[1], "nombre": row[2], "ciudad": row[3]}


def _resolver_conjunto(conn, telefono, texto, meta):
    """Devuelve (conjunto_dict, meta_actualizada). Si el texto trae QR,
    actualiza el conjunto en meta. Si no, intenta recuperar el ya
    guardado. Si no hay nada, devuelve (None, meta)."""
    qr = _qr.parsear_qr(texto)
    if qr:
        c = _conjunto_por_codigo(conn, qr["codigo"])
        if c:
            meta = dict(meta or {})
            meta["visitante_conjunto_id"] = c["id"]
            return c, meta
        # Código no reconocido — avisa el router.
        return {"__no_encontrado__": True, "codigo": qr["codigo"]}, meta
    cid = (meta or {}).get("visitante_conjunto_id")
    if cid:
        c = _conjunto_por_id(conn, cid)
        if c:
            return c, meta
    return None, meta


# ---------------------------------------------------------------------------
# Query de inmuebles
# ---------------------------------------------------------------------------

def _listar_inmuebles(conn, conjunto_id, operacion, limite=10):
    rows = conn.execute(
        "SELECT id, nro, habitaciones, banos, m2, precio, fuente, tipo "
        "FROM inmuebles_visitante "
        "WHERE conjunto_id=%s AND operacion=%s AND activo=TRUE "
        "ORDER BY creado_en DESC LIMIT %s",
        (conjunto_id, operacion, limite),
    ).fetchall()
    return [
        {"id": r[0], "nro": r[1], "hab": r[2], "banos": r[3],
         "m2": r[4], "precio": r[5], "fuente": r[6], "tipo": r[7]}
        for r in rows
    ]


def _get_inmueble(conn, inmueble_id, conjunto_id):
    row = conn.execute(
        "SELECT id, nro, habitaciones, banos, m2, precio, fuente, operacion, tipo "
        "FROM inmuebles_visitante "
        "WHERE id=%s AND conjunto_id=%s AND activo=TRUE",
        (inmueble_id, conjunto_id),
    ).fetchone()
    if not row:
        return None
    return {"id": row[0], "nro": row[1], "hab": row[2], "banos": row[3],
            "m2": row[4], "precio": row[5], "fuente": row[6],
            "operacion": row[7], "tipo": row[8]}


def _crear_inmueble(conn, conjunto_id, operacion, tipo, hab, banos, m2, precio,
                    propietario_telefono, nro):
    """Inserta un inmueble publicado por un visitante. `tipo` es la key
    canónica (apto/casa/parqueadero/habitacion/deposito). `nro` lo
    escribió el propietario — no se auto-genera."""
    r = conn.execute(
        "INSERT INTO inmuebles_visitante "
        "(conjunto_id, tipo, nro, operacion, habitaciones, banos, m2, precio, "
        " propietario_telefono, fuente) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'propietario') "
        "RETURNING id",
        (conjunto_id, tipo, nro, operacion, hab, banos, m2, precio,
         propietario_telefono),
    ).fetchone()
    return r[0]


# ---------------------------------------------------------------------------
# Helpers de envío
# ---------------------------------------------------------------------------

def _enviar(telefono, msg):
    try:
        _pwa.send_message(telefono, msg)
    except Exception:
        log.exception("[visitante] envío fallo tel=%s", telefono)


def _ir_a_menu(conn, telefono, conjunto, meta):
    meta = _set_estado(conn, telefono, "menu", meta=meta)
    _enviar(telefono, _menus.menu_principal(conjunto["nombre"]))
    return meta


# ---------------------------------------------------------------------------
# Dispatcher principal
# ---------------------------------------------------------------------------

_RE_NUM = re.compile(r"\d+")

ESTADOS_MENU_RESET = {"menu", None}


def handle_message(telefono, texto, button_id=None, list_id=None,
                   contacto_nombre=None, wa_msg_id=None):
    """Punto de entrada único del flujo visitante."""
    try:
        with _db._conexion_directa(timeout=5) as conn:
            _upsert_contacto(conn, telefono, nombre=contacto_nombre)
            _, meta = _leer_contacto(conn, telefono)

            # Resolver conjunto: QR en el texto, o el persistido.
            conjunto, meta = _resolver_conjunto(conn, telefono, texto, meta)

            if conjunto and conjunto.get("__no_encontrado__"):
                _enviar(telefono, _menus.texto(T.CODIGO_INVALIDO))
                return

            if not conjunto:
                _enviar(telefono, _menus.texto(T.SIN_CONJUNTO))
                return

            # Si el texto trae QR nuevo, siempre reinicia al menú.
            if _qr.tiene_marker_qr(texto):
                _guardar_meta(conn, telefono, meta)
                _ir_a_menu(conn, telefono, conjunto, meta)
                return

            # "menu" / "hola" / "inicio" / "hogar" como escape global.
            if _es_saludo(texto) and not button_id and not list_id:
                _ir_a_menu(conn, telefono, conjunto, meta)
                return

            estado = (meta or {}).get("visitante_estado") or "menu"
            try:
                _dispatch(conn, telefono, texto, button_id, list_id,
                          conjunto, meta, estado)
            except Exception:
                log.exception(
                    "[visitante] dispatch fallo tel=%s estado=%s",
                    telefono, estado,
                )
                _enviar(telefono, _menus.texto(T.FALLBACK))
    except Exception:
        log.exception("[visitante] handle_message fatal tel=%s", telefono)


def _dispatch(conn, telefono, texto, button_id, list_id,
              conjunto, meta, estado):
    # Botón global "VIS_MENU" vuelve al menú desde cualquier parte.
    if button_id == "VIS_MENU":
        _ir_a_menu(conn, telefono, conjunto, meta)
        return

    base = estado.split(":", 1)[0]

    # ---------- MENU ---------------------------------------------------
    if base == "menu":
        if button_id == "VIS_ARRIENDO":
            _mostrar_lista(conn, telefono, conjunto, meta, "arriendo")
            return
        if button_id == "VIS_COMPRAR":
            _mostrar_lista(conn, telefono, conjunto, meta, "venta")
            return
        if button_id == "VIS_PUBLICAR":
            _publicar_inicio(conn, telefono, conjunto, meta)
            return
        _ir_a_menu(conn, telefono, conjunto, meta)
        return

    # ---------- ARRIENDO / VENTA — lista ------------------------------
    if base == "arriendo_lista":
        if list_id and list_id.startswith("VIS_ARR_DET:"):
            _mostrar_detalle(conn, telefono, conjunto, meta, "arriendo",
                             list_id.split(":", 1)[1])
            return
        _mostrar_lista(conn, telefono, conjunto, meta, "arriendo")
        return

    if base == "venta_lista":
        if list_id and list_id.startswith("VIS_VEN_DET:"):
            _mostrar_detalle(conn, telefono, conjunto, meta, "venta",
                             list_id.split(":", 1)[1])
            return
        _mostrar_lista(conn, telefono, conjunto, meta, "venta")
        return

    # ---------- Detalle ----------------------------------------------
    if base in ("arriendo_detalle", "venta_detalle"):
        op = "arriendo" if base.startswith("arriendo") else "venta"
        if button_id and (button_id.startswith("VIS_ARR_CT:") or
                          button_id.startswith("VIS_VEN_CT:")):
            inmueble_id = button_id.split(":", 1)[1]
            _mostrar_confirmar_contactar(conn, telefono, conjunto, meta, op,
                                         inmueble_id)
            return
        # Caen cosas raras — reintentamos el detalle actual.
        sid = estado.split(":", 1)[1] if ":" in estado else None
        if sid:
            _mostrar_detalle(conn, telefono, conjunto, meta, op, sid)
            return
        _ir_a_menu(conn, telefono, conjunto, meta)
        return

    # ---------- Contactar — confirmación ------------------------------
    if base in ("arriendo_contactar", "venta_contactar"):
        op = "arriendo" if base.startswith("arriendo") else "venta"
        if button_id and (button_id.startswith("VIS_ARR_CT_OK:") or
                          button_id.startswith("VIS_VEN_CT_OK:")):
            inmueble_id = button_id.split(":", 1)[1]
            _marcar_contactar(conn, telefono, conjunto, meta, op, inmueble_id)
            return
        _enviar(telefono, _menus.texto(T.CONTACTAR_CANCEL))
        _ir_a_menu(conn, telefono, conjunto, meta)
        return

    # ---------- PUBLICAR ---------------------------------------------
    if base == "publicar_operacion":
        if button_id and button_id.startswith("VIS_PUB_OP:"):
            op = button_id.split(":", 1)[1]
            if op in ("arriendo", "venta"):
                _publicar_set_operacion(conn, telefono, conjunto, meta, op)
                return
        _publicar_inicio(conn, telefono, conjunto, meta)
        return

    if base == "publicar_tipo":
        if list_id and list_id.startswith("VIS_PUB_TIPO:"):
            _publicar_recibir_tipo(conn, telefono, conjunto, meta, list_id)
            return
        _enviar(telefono, _menus.texto(T.PUB_ERR_TIPO))
        _enviar(telefono, _menus.lista_tipos_publicar())
        return

    if base == "publicar_identificador":
        _publicar_recibir_identificador(conn, telefono, conjunto, meta, texto)
        return

    if base == "publicar_area":
        _publicar_recibir_area(conn, telefono, conjunto, meta, texto)
        return

    if base == "publicar_precio":
        _publicar_recibir_precio(conn, telefono, conjunto, meta, texto)
        return

    if base == "publicar_autorizacion":
        if button_id == "VIS_PUB_AUTH:si":
            _publicar_confirmar(conn, telefono, conjunto, meta)
            return
        if button_id == "VIS_PUB_AUTH:no":
            _enviar(telefono, _menus.texto(T.PUB_NO_AUTORIZO))
            meta.pop("visitante_pub", None)
            _set_estado(conn, telefono, "menu", meta=meta)
            _enviar(telefono, _menus.menu_principal(conjunto["nombre"]))
            return
        # Reenvía el resumen.
        _publicar_mostrar_resumen(conn, telefono, conjunto, meta)
        return

    if base == "publicar_hecho":
        _ir_a_menu(conn, telefono, conjunto, meta)
        return

    # Fallback — llevamos al menú.
    _ir_a_menu(conn, telefono, conjunto, meta)


# ---------------------------------------------------------------------------
# Sub-flows
# ---------------------------------------------------------------------------

def _mostrar_lista(conn, telefono, conjunto, meta, operacion):
    inmuebles = _listar_inmuebles(conn, conjunto["id"], operacion)
    if not inmuebles:
        vacio = (T.LISTA_VACIA_ARRIENDO if operacion == "arriendo"
                 else T.LISTA_VACIA_VENTA).format(edificio=conjunto["nombre"])
        _enviar(telefono, _menus.solo_menu_boton(vacio))
        _set_estado(conn, telefono, "menu", meta=meta)
        return
    nuevo_estado = "arriendo_lista" if operacion == "arriendo" else "venta_lista"
    _set_estado(conn, telefono, nuevo_estado, meta=meta)
    _enviar(telefono, _menus.lista_inmuebles(conjunto["nombre"],
                                             operacion, inmuebles))


def _mostrar_detalle(conn, telefono, conjunto, meta, operacion, inmueble_id):
    try:
        iid = int(inmueble_id)
    except (TypeError, ValueError):
        _ir_a_menu(conn, telefono, conjunto, meta)
        return
    inm = _get_inmueble(conn, iid, conjunto["id"])
    if not inm:
        _enviar(telefono, _menus.texto(T.FALLBACK))
        _ir_a_menu(conn, telefono, conjunto, meta)
        return
    nuevo = f"{'arriendo' if operacion == 'arriendo' else 'venta'}_detalle:{iid}"
    _set_estado(conn, telefono, nuevo, meta=meta)
    _enviar(telefono, _menus.detalle_inmueble(conjunto["nombre"], operacion, inm))


def _mostrar_confirmar_contactar(conn, telefono, conjunto, meta, operacion,
                                 inmueble_id):
    try:
        iid = int(inmueble_id)
    except (TypeError, ValueError):
        _ir_a_menu(conn, telefono, conjunto, meta)
        return
    inm = _get_inmueble(conn, iid, conjunto["id"])
    if not inm:
        _ir_a_menu(conn, telefono, conjunto, meta)
        return
    nuevo = f"{'arriendo' if operacion == 'arriendo' else 'venta'}_contactar:{iid}"
    _set_estado(conn, telefono, nuevo, meta=meta)
    _enviar(telefono, _menus.confirmar_contactar(inm["nro"], iid, operacion))


def _marcar_contactar(conn, telefono, conjunto, meta, operacion, inmueble_id):
    try:
        iid = int(inmueble_id)
    except (TypeError, ValueError):
        _ir_a_menu(conn, telefono, conjunto, meta)
        return
    inm = _get_inmueble(conn, iid, conjunto["id"])
    if not inm:
        _ir_a_menu(conn, telefono, conjunto, meta)
        return
    log.info(
        "[visitante] CONTACT_REQ tel=%s inmueble=%s conjunto=%s op=%s",
        telefono, iid, conjunto["codigo"], operacion,
    )
    # Best-effort: persistir la solicitud para el panel. Si falla, no romper
    # el flujo — el log de CONTACT_REQ sigue siendo la fuente de verdad.
    try:
        nombre = None
        try:
            r = conn.execute(
                "SELECT nombre FROM contactos WHERE telefono=%s", (telefono,)
            ).fetchone()
            if r:
                nombre = r[0]
        except Exception:
            nombre = None
        conn.execute(
            "INSERT INTO visitante_contactos "
            "(telefono, contacto_nombre, conjunto_id, inmueble_id) "
            "VALUES (%s, %s, %s, %s)",
            (telefono, nombre, conjunto["id"], iid),
        )
    except Exception:
        log.exception(
            "[visitante] fallo guardando visitante_contactos tel=%s inm=%s",
            telefono, iid,
        )
    _enviar(telefono, _menus.texto(T.CONTACTAR_OK.format(nro=inm["nro"])))
    # Link editable desde el panel (petra_contenidos.titulo='Watt.1'). Si no
    # existe o está inactivo, no mandamos nada extra — mejor silencio que un
    # link roto. El panel es la fuente de verdad del link.
    try:
        row = conn.execute(
            "SELECT url FROM petra_contenidos "
            "WHERE titulo=%s AND activo=TRUE "
            "ORDER BY id DESC LIMIT 1",
            ("Watt.1",),
        ).fetchone()
        if row and row[0]:
            _enviar(telefono, _menus.texto(row[0]))
    except Exception:
        log.exception(
            "[visitante] fallo buscando Watt.1 tel=%s inm=%s", telefono, iid
        )
    _set_estado(conn, telefono, "menu", meta=meta)
    _enviar(telefono, _menus.menu_principal(conjunto["nombre"]))


# -------- Publicar sub-flow -------------------------------------------------

def _publicar_inicio(conn, telefono, conjunto, meta):
    meta = dict(meta or {})
    meta["visitante_pub"] = {}
    _set_estado(conn, telefono, "publicar_operacion", meta=meta)
    _enviar(telefono, _menus.pub_operacion(conjunto["nombre"]))


def _publicar_set_operacion(conn, telefono, conjunto, meta, operacion):
    meta = dict(meta or {})
    pub = dict(meta.get("visitante_pub") or {})
    pub["operacion"] = operacion
    meta["visitante_pub"] = pub
    _set_estado(conn, telefono, "publicar_tipo", meta=meta)
    _enviar(telefono, _menus.lista_tipos_publicar())


def _parse_num(texto):
    """Extrae el primer entero positivo del texto, o None."""
    if not texto:
        return None
    m = _RE_NUM.search(texto)
    if not m:
        return None
    try:
        return int(m.group(0))
    except Exception:
        return None


def _publicar_recibir_tipo(conn, telefono, conjunto, meta, list_id):
    key = (list_id or "").split(":", 1)[1] if ":" in (list_id or "") else ""
    key = key.strip().lower()
    if key not in T.TIPOS_VALIDOS:
        _enviar(telefono, _menus.texto(T.PUB_ERR_TIPO))
        _enviar(telefono, _menus.lista_tipos_publicar())
        return
    pub = dict((meta or {}).get("visitante_pub") or {})
    pub["tipo"] = key
    new_meta = dict(meta or {})
    new_meta["visitante_pub"] = pub
    _set_estado(conn, telefono, "publicar_identificador", meta=new_meta)
    _enviar(telefono, _menus.texto(
        T.PUB_PEDIR_ID.format(tipo=T.tipo_label(key))
    ))


def _publicar_recibir_identificador(conn, telefono, conjunto, meta, texto):
    nro = (texto or "").strip()
    if not nro:
        _enviar(telefono, _menus.texto(T.PUB_ERR_ID))
        return
    # Cap defensivo — también lo valida WhatsApp con los límites de texto.
    if len(nro) > 60:
        nro = nro[:60].strip()
    if not nro:
        _enviar(telefono, _menus.texto(T.PUB_ERR_ID))
        return
    pub = dict((meta or {}).get("visitante_pub") or {})
    pub["nro"] = nro
    new_meta = dict(meta or {})
    new_meta["visitante_pub"] = pub
    _set_estado(conn, telefono, "publicar_area", meta=new_meta)
    _enviar(telefono, _menus.texto(T.PUB_PEDIR_AREA.format(nro=nro)))


def _publicar_recibir_area(conn, telefono, conjunto, meta, texto):
    n = _parse_num(texto)
    if n is None or n < 1 or n > 10000:
        _enviar(telefono, _menus.texto(T.PUB_ERR_AREA))
        return
    pub = dict((meta or {}).get("visitante_pub") or {})
    pub["m2"] = n
    new_meta = dict(meta or {})
    new_meta["visitante_pub"] = pub
    _set_estado(conn, telefono, "publicar_precio", meta=new_meta)
    tpl = T.PUB_PRECIO_ARRIENDO if pub.get("operacion") == "arriendo" else T.PUB_PRECIO_VENTA
    _enviar(telefono, _menus.texto(tpl.format(m2=n)))


def _publicar_recibir_precio(conn, telefono, conjunto, meta, texto):
    # El precio puede traer puntos/espacios/comas — los quitamos antes
    # de parsear.
    limpio = re.sub(r"[^\d]", "", texto or "")
    if not limpio:
        _enviar(telefono, _menus.texto(T.PUB_ERR_PRECIO))
        return
    try:
        n = int(limpio)
    except Exception:
        _enviar(telefono, _menus.texto(T.PUB_ERR_PRECIO))
        return
    if n <= 0:
        _enviar(telefono, _menus.texto(T.PUB_ERR_PRECIO))
        return
    pub = dict((meta or {}).get("visitante_pub") or {})
    pub["precio"] = n
    new_meta = dict(meta or {})
    new_meta["visitante_pub"] = pub
    _set_estado(conn, telefono, "publicar_autorizacion", meta=new_meta)
    _publicar_mostrar_resumen(conn, telefono, conjunto, new_meta)


def _publicar_mostrar_resumen(conn, telefono, conjunto, meta):
    pub = (meta or {}).get("visitante_pub") or {}
    _enviar(telefono, _menus.pub_resumen(
        edificio=conjunto["nombre"],
        operacion=pub.get("operacion") or "arriendo",
        tipo=pub.get("tipo") or "apto",
        nro=pub.get("nro") or "-",
        m2=pub.get("m2"),
        precio=pub.get("precio"),
    ))


def _publicar_confirmar(conn, telefono, conjunto, meta):
    pub = (meta or {}).get("visitante_pub") or {}
    op = pub.get("operacion") or "arriendo"
    tipo = pub.get("tipo") or "apto"
    nro = pub.get("nro") or ""
    m2 = pub.get("m2")
    precio = pub.get("precio")
    if not nro:
        # Defensa: no deberíamos llegar acá sin identificador, pero si pasó,
        # mandamos al usuario al inicio del flujo.
        _enviar(telefono, _menus.texto(T.FALLBACK))
        _ir_a_menu(conn, telefono, conjunto, meta)
        return
    try:
        iid = _crear_inmueble(
            conn, conjunto["id"], op, tipo,
            hab=None, banos=None, m2=m2, precio=precio,
            propietario_telefono=telefono, nro=nro,
        )
        log.info(
            "[visitante] PUBLICADO tel=%s inmueble=%s conjunto=%s op=%s tipo=%s nro=%s",
            telefono, iid, conjunto["codigo"], op, tipo, nro,
        )
    except Exception:
        log.exception("[visitante] insert inmueble fallo tel=%s", telefono)
        _enviar(telefono, _menus.texto(T.FALLBACK))
        _ir_a_menu(conn, telefono, conjunto, meta)
        return
    _enviar(telefono, _menus.texto(T.PUB_PUBLICADO.format(
        titulo=T.row_title(tipo, nro),
        edificio=conjunto["nombre"],
        m2=m2 if m2 is not None else "-",
        precio_fmt=T.formatear_precio(precio),
        operacion="Arriendo" if op == "arriendo" else "Venta",
    )))
    new_meta = dict(meta or {})
    new_meta.pop("visitante_pub", None)
    _set_estado(conn, telefono, "menu", meta=new_meta)
    _enviar(telefono, _menus.menu_principal(conjunto["nombre"]))
