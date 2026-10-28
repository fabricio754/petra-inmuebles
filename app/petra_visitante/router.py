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
import unicodedata

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
    "hola", "holaa", "holaaa", "ola", "buenas",
    "buenos dias", "buenas tardes", "buenas noches",
    "hey", "hi", "hello",
    "menu", "hogar", "inicio",
}

# Frases/palabras que gatillan el HOME MENU cuando llegan sin QR y sin
# flow activo. Match por substring o como palabra suelta — insensible a
# mayúsculas y a acentos.
_GREETINGS_HOME = (
    "hola", "buenas", "buenos dias", "buenas tardes", "buenas noches",
    "quiero info", "info", "me interesa", "hey", "hi",
)


def _strip_accents(s):
    if not s:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def _normalize(texto):
    return _strip_accents((texto or "").strip().lower())


def _es_saludo(texto):
    t = _normalize(texto).strip(" .,!¡?¿").split("\n", 1)[0]
    if not t:
        return False
    if t in _GREETINGS:
        return True
    palabras = t.split()
    primera = palabras[0] if palabras else ""
    if primera in _GREETINGS:
        return True
    # Nuevo: frases/palabras del HOME menu (substring o palabra suelta).
    tokens = set(re.findall(r"\w+", t))
    for g in _GREETINGS_HOME:
        if " " in g:
            if g in t:
                return True
        elif g in tokens:
            return True
    return False


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

def _listar_inmuebles(conn, conjunto_id, operacion, limite=10, excluir_ids=None):
    """Lista inmuebles activos. `excluir_ids` evita mostrar los ya elegidos
    en el carrito (multi-select PR #65 camino B)."""
    excluir_ids = list(excluir_ids or [])
    sql = (
        "SELECT id, nro, habitaciones, banos, m2, precio, fuente, tipo "
        "FROM inmuebles_visitante "
        "WHERE conjunto_id=%s AND operacion=%s AND activo=TRUE"
    )
    params = [conjunto_id, operacion]
    if excluir_ids:
        sql += " AND NOT (id = ANY(%s))"
        params.append(excluir_ids)
    sql += " ORDER BY creado_en DESC LIMIT %s"
    params.append(limite)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        {"id": r[0], "nro": r[1], "hab": r[2], "banos": r[3],
         "m2": r[4], "precio": r[5], "fuente": r[6], "tipo": r[7]}
        for r in rows
    ]


def _get_inmuebles_batch(conn, conjunto_id, ids):
    """Trae {id: dict} para una lista de ids. Útil para render del carrito."""
    if not ids:
        return {}
    try:
        rows = conn.execute(
            "SELECT id, nro, tipo FROM inmuebles_visitante "
            "WHERE conjunto_id=%s AND id = ANY(%s) AND activo=TRUE",
            (conjunto_id, list(ids)),
        ).fetchall()
    except Exception:
        log.exception("[visitante] get_inmuebles_batch fallo")
        return {}
    return {r[0]: {"id": r[0], "nro": r[1], "tipo": r[2]} for r in rows}


# --- Carrito multi-select (PR #65 camino B) --------------------------------
_CARRITO_KEY = {"arriendo": "carrito_arr", "venta": "carrito_ven"}


def _carrito_ids(meta, operacion):
    key = _CARRITO_KEY[operacion]
    val = meta.get(key) or []
    # Normaliza por si vino de JSONB como lista de str o mixto.
    out = []
    for v in val:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def _carrito_set(meta, operacion, ids):
    meta[_CARRITO_KEY[operacion]] = list({int(i) for i in ids})


def _carrito_vacio(meta, operacion):
    meta.pop(_CARRITO_KEY[operacion], None)


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

# Estados del HOME flow (sin conjunto). Son handleados por `_dispatch_home`
# y no requieren resolver un conjunto.
_HOME_STATES = {"home_menu", "home_mis_inmuebles", "home_mis_busquedas",
                "home_agentes_intro"}


# Intent: "quiero ser agente petra" (y variantes).  Si el texto matchea,
# arrancamos directo el flujo de inscripción, sin pasar por el home.
_RE_QUIERO_SER_AGENTE = re.compile(
    r"\b(quiero|me\s+quiero|me\s+gustar[ií]a|quisiera)\s+"
    r"(ser|unir(me)?\s+a|hacer(me)?|trabajar\s+con|aplicar)"
    r"[\w\s]{0,30}?"
    r"(agente|asesora?|petra)",
    re.IGNORECASE,
)


def _es_intent_quiero_agente(texto):
    t = (texto or "").strip().lower()
    if not t:
        return False
    if _RE_QUIERO_SER_AGENTE.search(t):
        return True
    # Matches explícitos cortos.
    if ("ser agente petra" in t) or ("quiero ser agente" in t) \
            or ("quiero ser asesora" in t) or ("ser agente de petra" in t):
        return True
    return False


def handle_message(telefono, texto, button_id=None, list_id=None,
                   contacto_nombre=None, wa_msg_id=None):
    """Punto de entrada único del flujo visitante."""
    try:
        with _db._conexion_directa(timeout=5) as conn:
            _upsert_contacto(conn, telefono, nombre=contacto_nombre)
            _, meta = _leer_contacto(conn, telefono)

            vis_estado_raw = (meta or {}).get("visitante_estado") or ""
            base_vis = vis_estado_raw.split(":", 1)[0]
            qr_present = _qr.tiene_marker_qr(texto)

            # ---- Intent "quiero ser agente petra" ------------------------
            # Si el usuario escribe directamente esa intención (sin botón),
            # saltamos todo el home y lo mandamos al flujo de inscripción.
            if (not qr_present and not button_id and not list_id
                    and _es_intent_quiero_agente(texto)):
                _start_agent_flow(conn, telefono, meta, contacto_nombre)
                return

            # ---- HOME flow (sin QR, sin conjunto) ------------------------
            # Si el usuario ya está en home_* y no mandó un QR nuevo, atiende
            # el home dispatcher. Un QR hace fall-through al flow con conjunto.
            if not qr_present and base_vis in _HOME_STATES:
                _dispatch_home(conn, telefono, texto, button_id, list_id,
                               meta, base_vis, contacto_nombre)
                return

            # Saludo sin flow activo → HOME, aunque haya un conjunto sticky.
            # Caso típico: alguien escaneó un QR hace días (Arrayanes queda
            # pegado en meta con visitante_estado=menu) y hoy escribe
            # "Hola quiero info" — queremos el home menu, no el menú de
            # Arrayanes. Consideramos "sin flow activo" tanto el estado
            # vacío como "menu" (apenas abrió el menú del conjunto, no se
            # comprometió a nada).
            _NO_LOCK_STATES = {"", "menu"}
            if (base_vis in _NO_LOCK_STATES and not qr_present
                    and _es_saludo(texto)
                    and not button_id and not list_id):
                _dispatch_home(conn, telefono, texto, button_id, list_id,
                               meta, "home_menu", contacto_nombre)
                return

            # Resolver conjunto: QR en el texto, o el persistido.
            conjunto, meta = _resolver_conjunto(conn, telefono, texto, meta)

            if conjunto and conjunto.get("__no_encontrado__"):
                _enviar(telefono, _menus.texto(T.CODIGO_INVALIDO))
                return

            if not conjunto:
                # Sin conjunto. Si no hay flow activo y parece saludo → HOME.
                if not vis_estado_raw and _es_saludo(texto):
                    _dispatch_home(conn, telefono, texto, button_id, list_id,
                                   meta, "home_menu", contacto_nombre)
                    return
                _enviar(telefono, _menus.texto(T.SIN_CONJUNTO))
                return

            # Si el texto trae QR nuevo, siempre reinicia al menú.
            if qr_present:
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

    # ---------- ARRIENDO / VENTA — lista + carrito --------------------
    if base in ("arriendo_lista", "venta_lista", "arriendo_carrito",
                "venta_carrito"):
        op = "arriendo" if base.startswith("arriendo") else "venta"

        # Elegir item desde la lista → agregar al carrito.
        prefix_det = "VIS_ARR_DET:" if op == "arriendo" else "VIS_VEN_DET:"
        if list_id and list_id.startswith(prefix_det):
            try:
                iid = int(list_id.split(":", 1)[1])
            except (TypeError, ValueError):
                iid = None
            if iid is not None:
                _agregar_carrito(conn, telefono, conjunto, meta, op, iid)
                return

        # Botones del carrito.
        if button_id == "VIS_CART_AGREGAR":
            _mostrar_lista(conn, telefono, conjunto, meta, op)
            return
        if button_id == "VIS_CART_VACIAR":
            _carrito_vacio(meta, op)
            _guardar_meta(conn, telefono, meta)
            _ir_a_menu(conn, telefono, conjunto, meta)
            return
        if button_id == "VIS_CART_PEDIR":
            _pedir_contactos_carrito(conn, telefono, conjunto, meta, op)
            return

        # Si no reconocemos el botón, re-mostrar carrito o lista según
        # tenga items o no.
        if _carrito_ids(meta, op):
            _mostrar_carrito(conn, telefono, conjunto, meta, op)
        else:
            _mostrar_lista(conn, telefono, conjunto, meta, op)
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
    # Excluye los inmuebles ya elegidos en el carrito así no re-aparecen.
    ya_en_carrito = _carrito_ids(meta, operacion)
    inmuebles = _listar_inmuebles(conn, conjunto["id"], operacion,
                                  excluir_ids=ya_en_carrito)
    if not inmuebles:
        # Si quedan items en carrito, el "fin de lista" lleva al carrito,
        # no al menú. Si no hay carrito y la lista era vacía, menú.
        if ya_en_carrito:
            _mostrar_carrito(conn, telefono, conjunto, meta, operacion)
            return
        vacio = (T.LISTA_VACIA_ARRIENDO if operacion == "arriendo"
                 else T.LISTA_VACIA_VENTA).format(edificio=conjunto["nombre"])
        _enviar(telefono, _menus.solo_menu_boton(vacio))
        _set_estado(conn, telefono, "menu", meta=meta)
        return
    nuevo_estado = "arriendo_lista" if operacion == "arriendo" else "venta_lista"
    _set_estado(conn, telefono, nuevo_estado, meta=meta)
    _enviar(telefono, _menus.lista_inmuebles(conjunto["nombre"],
                                             operacion, inmuebles))


def _agregar_carrito(conn, telefono, conjunto, meta, operacion, iid):
    """Agrega un inmueble al carrito del visitante y muestra confirmación."""
    inm = _get_inmueble(conn, iid, conjunto["id"])
    if not inm:
        _mostrar_carrito(conn, telefono, conjunto, meta, operacion)
        return
    ids = _carrito_ids(meta, operacion)
    if iid not in ids:
        ids.append(iid)
    _carrito_set(meta, operacion, ids)
    _guardar_meta(conn, telefono, meta)
    estado_nuevo = "arriendo_carrito" if operacion == "arriendo" else "venta_carrito"
    _set_estado(conn, telefono, estado_nuevo, meta=meta)
    disponibles = _listar_inmuebles(conn, conjunto["id"], operacion,
                                    excluir_ids=ids)
    _enviar(telefono, _menus.carrito_item_agregado(
        operacion, inm, len(ids), bool(disponibles),
    ))


def _mostrar_carrito(conn, telefono, conjunto, meta, operacion):
    ids = _carrito_ids(meta, operacion)
    if not ids:
        _ir_a_menu(conn, telefono, conjunto, meta)
        return
    items_map = _get_inmuebles_batch(conn, conjunto["id"], ids)
    items = [items_map[i] for i in ids if i in items_map]
    # Si todos los items quedaron huerfanos (desactivados), vaciamos
    # carrito y volvemos al menú — mejor que mostrar un carrito vacío
    # con botón Contactar(0).
    if not items:
        _carrito_vacio(meta, operacion)
        _guardar_meta(conn, telefono, meta)
        _ir_a_menu(conn, telefono, conjunto, meta)
        return
    disponibles = _listar_inmuebles(conn, conjunto["id"], operacion,
                                    excluir_ids=ids)
    estado_nuevo = "arriendo_carrito" if operacion == "arriendo" else "venta_carrito"
    _set_estado(conn, telefono, estado_nuevo, meta=meta)
    _enviar(telefono, _menus.carrito_estado(operacion, items,
                                            bool(disponibles)))


def _pedir_contactos_carrito(conn, telefono, conjunto, meta, operacion):
    """Procesa el carrito completo: guarda la solicitud en petra_interesados_qr
    + visitante_contactos (una fila por inmueble) y responde al visitante con
    el link del contacto Watt.1 (igual que _marcar_contactar hoy)."""
    ids = _carrito_ids(meta, operacion)
    if not ids:
        _ir_a_menu(conn, telefono, conjunto, meta)
        return
    items_map = _get_inmuebles_batch(conn, conjunto["id"], ids)
    items = [items_map[i] for i in ids if i in items_map]
    if not items:
        _carrito_vacio(meta, operacion)
        _guardar_meta(conn, telefono, meta)
        _ir_a_menu(conn, telefono, conjunto, meta)
        return

    nombre_in = None
    username_in = None
    try:
        r = conn.execute(
            "SELECT nombre, username FROM petra_contactos WHERE telefono=%s",
            (telefono,),
        ).fetchone()
        if r:
            nombre_in = r[0]
            username_in = r[1]
    except Exception:
        log.exception("[visitante] carrito: lectura nombre fallo tel=%s",
                      telefono)

    # 1) petra_interesados_qr: una sola fila con todos los inmueble_ids.
    try:
        conn.execute(
            "INSERT INTO petra_interesados_qr "
            "(interesado_telefono, interesado_nombre, interesado_username, "
            " conjunto_id, inmueble_ids) "
            "VALUES (%s, %s, %s, %s, %s)",
            (telefono, nombre_in, username_in,
             conjunto["id"], [int(i["id"]) for i in items]),
        )
    except Exception:
        log.exception("[visitante] carrito: insert interesados_qr fallo tel=%s",
                      telefono)

    # 2) visitante_contactos: una fila por inmueble (compat con lo existente).
    for inm in items:
        try:
            conn.execute(
                "INSERT INTO visitante_contactos "
                "(telefono, contacto_nombre, conjunto_id, inmueble_id) "
                "VALUES (%s, %s, %s, %s)",
                (telefono, nombre_in, conjunto["id"], int(inm["id"])),
            )
        except Exception:
            log.exception(
                "[visitante] carrito: insert visitante_contactos fallo tel=%s inm=%s",
                telefono, inm.get("id"),
            )
    log.info("[visitante] CARRITO_SUBMIT tel=%s conjunto=%s op=%s ids=%s",
             telefono, conjunto["codigo"], operacion, ids)

    # 3) Confirmación al visitante.
    emoji_op = "🏠" if operacion == "arriendo" else "💰"
    tipos_resumen = ", ".join(
        f"{emoji_op} {(x.get('tipo') or 'Inmueble').capitalize()} #{x.get('nro') or x['id']}"
        for x in items
    )
    _enviar(telefono, _menus.texto(
        f"¡Listo! Guardamos tu solicitud por *{len(items)} inmueble"
        f"{'s' if len(items) != 1 else ''}*:\n{tipos_resumen}"
    ))

    # 4) Link único del contacto Watt.1 — como hoy; el equipo Petra se
    # encarga del handoff con el/los propietario(s). Si no hay link
    # activo, silencio (mejor que un link roto).
    try:
        row = conn.execute(
            "SELECT url FROM petra_contenidos "
            "WHERE titulo=%s AND activo=TRUE "
            "ORDER BY id DESC LIMIT 1",
            ("Watt.1",),
        ).fetchone()
        if row and row[0]:
            _enviar(telefono, _menus.texto(
                "Para avanzar con los propietarios, escribinos acá 👇\n\n"
                + row[0]
            ))
    except Exception:
        log.exception("[visitante] carrito: buscar Watt.1 fallo tel=%s",
                      telefono)

    _carrito_vacio(meta, operacion)
    _guardar_meta(conn, telefono, meta)
    _set_estado(conn, telefono, "menu", meta=meta)
    _enviar(telefono, _menus.menu_principal(conjunto["nombre"]))


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
            _enviar(
                telefono,
                _menus.texto(
                    "Aquí te paso el link de WhatsApp para que te "
                    "comuniques con él 👇\n\n" + row[0]
                ),
            )
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


# ---------------------------------------------------------------------------
# HOME flow (sin QR / sin conjunto)
# ---------------------------------------------------------------------------

def _dispatch_home(conn, telefono, texto, button_id, list_id,
                   meta, estado_base, contacto_nombre):
    """Dispatcher del flujo HOME (entry point cuando no hay QR).

    Estados: home_menu, home_mis_inmuebles, home_mis_busquedas.
    """
    # Botón global para volver al home desde cualquier parte.
    if button_id == "HOME_MENU":
        _mostrar_home(conn, telefono, meta)
        return

    if estado_base == "home_menu":
        if button_id == "HOME_MIS_INMUEBLES":
            _mostrar_mis_inmuebles(conn, telefono, meta)
            return
        if button_id == "HOME_MIS_BUSQUEDAS":
            _mostrar_mis_busquedas(conn, telefono, meta)
            return
        if button_id == "HOME_AGENTES":
            _handle_home_agentes(conn, telefono, meta, contacto_nombre)
            return
        # Cualquier otro input desde home_menu → re-mostrar home.
        _mostrar_home(conn, telefono, meta)
        return

    if estado_base in ("home_mis_inmuebles", "home_mis_busquedas"):
        # Cualquier otro input que no sea HOME_MENU → re-mostrar home.
        # (HOME_MENU ya se interceptó arriba).
        _mostrar_home(conn, telefono, meta)
        return

    if estado_base == "home_agentes_intro":
        if button_id == "HOME_AGENTES_START":
            _start_agent_flow(conn, telefono, meta, contacto_nombre)
            return
        # Cualquier otro input → re-mostrar la intro (HOME_MENU ya interceptado).
        _enviar(telefono, _menus.home_agentes_intro())
        return

    # Estado raro — fallback.
    _mostrar_home(conn, telefono, meta)


def _mostrar_home(conn, telefono, meta):
    _set_estado(conn, telefono, "home_menu", meta=meta)
    _enviar(telefono, _menus.home_menu())


def _listar_mis_inmuebles(conn, telefono):
    rows = conn.execute(
        "SELECT i.id, i.tipo, i.nro, i.operacion, i.m2, i.precio, i.activo, "
        "       c.nombre, c.codigo "
        "FROM inmuebles_visitante i "
        "JOIN conjuntos_visitante c ON c.id=i.conjunto_id "
        "WHERE i.propietario_telefono = %s "
        "ORDER BY i.creado_en DESC LIMIT 10",
        (telefono,),
    ).fetchall()
    return [
        {"id": r[0], "tipo": r[1], "nro": r[2], "operacion": r[3],
         "m2": r[4], "precio": r[5], "activo": r[6],
         "conjunto": r[7], "codigo": r[8]}
        for r in rows
    ]


def _listar_mis_busquedas(conn, telefono):
    rows = conn.execute(
        "SELECT v.id, v.creado_en, i.tipo, i.nro, i.operacion, i.precio, "
        "       c.nombre "
        "FROM visitante_contactos v "
        "JOIN inmuebles_visitante i ON i.id=v.inmueble_id "
        "JOIN conjuntos_visitante c ON c.id=v.conjunto_id "
        "WHERE v.telefono = %s "
        "ORDER BY v.creado_en DESC LIMIT 10",
        (telefono,),
    ).fetchall()
    return [
        {"id": r[0], "fecha": r[1], "tipo": r[2], "nro": r[3],
         "operacion": r[4], "precio": r[5], "conjunto": r[6]}
        for r in rows
    ]


def _render_mis_inmuebles(rows):
    partes = [T.HOME_MIS_INMUEBLES_HEADER.format(n=len(rows))]
    for i, r in enumerate(rows, 1):
        titulo = T.row_title(r.get("tipo"), r.get("nro") or r["id"])
        op_label = "Arriendo" if r.get("operacion") == "arriendo" else "Venta"
        precio_fmt = T.formatear_precio(r.get("precio"))
        m2 = r.get("m2") if r.get("m2") is not None else "-"
        estado = "Activo" if r.get("activo") else "Pausado"
        partes.append(
            f"{i}. {titulo} · {r.get('conjunto') or '-'}\n"
            f"   {op_label} · {precio_fmt} · {m2}m² · {estado}"
        )
    return "\n\n".join(partes)


def _render_mis_busquedas(rows):
    partes = [T.HOME_MIS_BUSQUEDAS_HEADER.format(n=len(rows))]
    for i, r in enumerate(rows, 1):
        titulo = T.row_title(r.get("tipo"), r.get("nro") or r["id"])
        op_label = "Arriendo" if r.get("operacion") == "arriendo" else "Venta"
        precio_fmt = T.formatear_precio(r.get("precio"))
        fecha = r.get("fecha")
        try:
            fecha_fmt = fecha.strftime("%d/%m/%Y") if fecha else "-"
        except Exception:
            fecha_fmt = "-"
        partes.append(
            f"{i}. {titulo} · {r.get('conjunto') or '-'}\n"
            f"   {op_label} · {precio_fmt}\n"
            f"   📅 {fecha_fmt}"
        )
    return "\n\n".join(partes)


def _mostrar_mis_inmuebles(conn, telefono, meta):
    try:
        rows = _listar_mis_inmuebles(conn, telefono)
    except Exception:
        log.exception("[visitante] query mis_inmuebles fallo tel=%s", telefono)
        rows = []
    _set_estado(conn, telefono, "home_mis_inmuebles", meta=meta)
    if not rows:
        _enviar(telefono, _menus.home_volver_boton(T.HOME_MIS_INMUEBLES_VACIO))
        return
    _enviar(telefono, _menus.texto(_render_mis_inmuebles(rows)))
    _enviar(telefono, _menus.home_volver_boton(T.HOME_VOLVER_PROMPT))


def _mostrar_mis_busquedas(conn, telefono, meta):
    try:
        rows = _listar_mis_busquedas(conn, telefono)
    except Exception:
        log.exception("[visitante] query mis_busquedas fallo tel=%s", telefono)
        rows = []
    _set_estado(conn, telefono, "home_mis_busquedas", meta=meta)
    if not rows:
        _enviar(telefono, _menus.home_volver_boton(T.HOME_MIS_BUSQUEDAS_VACIO))
        return
    _enviar(telefono, _menus.texto(_render_mis_busquedas(rows)))
    _enviar(telefono, _menus.home_volver_boton(T.HOME_VOLVER_PROMPT))


def _handle_home_agentes(conn, telefono, meta, contacto_nombre):
    """Muestra la intro de Agentes Petra con un botón 'Quiero ser agente'.
    El flow de reclutamiento sólo se dispara cuando piquen ese botón."""
    _set_estado(conn, telefono, "home_agentes_intro", meta=meta)
    _enviar(telefono, _menus.home_agentes_intro())


def _start_agent_flow(conn, telefono, meta, contacto_nombre):
    """Limpia estado visitante y arranca el flow de reclutamiento."""
    new_meta = dict(meta or {})
    new_meta.pop("visitante_estado", None)
    new_meta.pop("visitante_conjunto_id", None)
    new_meta.pop("visitante_pub", None)
    try:
        _guardar_meta(conn, telefono, new_meta)
    except Exception:
        log.exception(
            "[visitante] AGENTES_START clear meta fallo tel=%s", telefono,
        )
    try:
        from app import petra_agents  # import local — evita ciclo
        petra_agents.handle_message(
            telefono=telefono, texto="quiero ser agente petra",
            button_id=None, list_id=None,
            contacto_nombre=contacto_nombre, wa_msg_id=None,
        )
    except Exception:
        log.exception(
            "[visitante] AGENTES_START → petra_agents fallo tel=%s", telefono,
        )
        _enviar(telefono, _menus.texto(T.HOME_AGENTES_FALLBACK))
