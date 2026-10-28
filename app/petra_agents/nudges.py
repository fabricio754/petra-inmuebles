"""Motor de nudges (remarketing automático de FASE F).

Un nudge es una regla: "si una agente lleva N horas en estado X sin
avanzar, mandale el mensaje Y". Las reglas viven en `petra_nudges` y
los textos en `petra_textos`. El cron horario llama a `evaluar_y_enviar`.

Tracking: cada contacto guarda en metadata.nudges_enviados un dict
{clave_nudge: iso_timestamp} para no re-enviar el mismo nudge dos veces
(mientras siga en el mismo estado).

Cuando el agente cambia de estado, el flow setea metadata.estado_desde,
que se usa como ancla para medir "hace cuántas horas está acá".

M5.A — Reglas diferenciadas por fase + ventanas horarias:
- Pausa fines de semana, feriados Colombia y horario no laboral (fuera
  de 8–20 Bogotá). El cron de capacitación NO se pausa (eso vive en
  `scheduler.py`, no acá).
- Fases "silencio total" (autorizacion_rechazada, kit_libro_recibido):
  simplemente no tienen reglas seedeadas.
- Fase 'activa' + cita nueva sin responder: cadencia agresiva
  (5min x3 → 1h x6 → 24h) → `evaluar_citas_pendientes()` (minute cron).
"""
import datetime as _dt
import json as _json
import logging

log = logging.getLogger("petra")

# ---------------------------------------------------------------------------
# Helpers de ventana horaria (Colombia)
# ---------------------------------------------------------------------------

def _ahora_bogota():
    try:
        from zoneinfo import ZoneInfo
        return _dt.datetime.now(ZoneInfo("America/Bogota"))
    except Exception:
        # Fallback: aproxima a UTC-5 sin DST (Colombia no tiene DST).
        return _dt.datetime.utcnow() - _dt.timedelta(hours=5)


def _es_dia_habil(ahora=None):
    """True si es lunes–viernes y NO es feriado nacional en Colombia.
    Usa `holidays>=0.50` (ya en requirements)."""
    ahora = ahora or _ahora_bogota()
    if ahora.weekday() >= 5:  # 5=sáb, 6=dom
        return False
    try:
        import holidays
        co = holidays.country_holidays("CO")
        if ahora.date() in co:
            return False
    except Exception:
        log.exception("[nudges] holidays lookup fallo — asumo día hábil")
    return True


def _en_horario_laboral(ahora=None):
    """True si estamos entre 8:00 y 20:00 Bogotá (exclusive 20:00)."""
    ahora = ahora or _ahora_bogota()
    return 8 <= ahora.hour < 20


def _horario_valido_para_nudge():
    """Combina: día hábil + horario laboral. Si falso → se saltea el envío."""
    ahora = _ahora_bogota()
    return _es_dia_habil(ahora) and _en_horario_laboral(ahora)


# ---------------------------------------------------------------------------

def listar_reglas(solo_activas=False):
    """Lee todas las reglas de petra_nudges."""
    try:
        from app import db as _db
        with _db._conexion_directa(timeout=10) as conn:
            where = "WHERE activo = TRUE" if solo_activas else ""
            rows = conn.execute(
                f"SELECT clave, descripcion, estado_origen, horas_sin_avanzar, "
                f"       texto_clave, activo, created_at, updated_at "
                f"FROM petra_nudges {where} "
                f"ORDER BY estado_origen, horas_sin_avanzar"
            ).fetchall()
        return [
            {
                "clave": r[0], "descripcion": r[1], "estado_origen": r[2],
                "horas_sin_avanzar": r[3], "texto_clave": r[4],
                "activo": r[5], "created_at": r[6], "updated_at": r[7],
            } for r in rows
        ]
    except Exception:
        log.exception("[nudges] listar fallo")
        return []


def upsert_regla(clave, estado_origen, horas, texto_clave,
                 descripcion=None, activo=True):
    """Crea o actualiza una regla."""
    try:
        from app import db as _db
        with _db._conexion_directa(timeout=10) as conn:
            conn.execute(
                "INSERT INTO petra_nudges "
                "(clave, descripcion, estado_origen, horas_sin_avanzar, "
                " texto_clave, activo, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, NOW()) "
                "ON CONFLICT (clave) DO UPDATE SET "
                "  descripcion = EXCLUDED.descripcion, "
                "  estado_origen = EXCLUDED.estado_origen, "
                "  horas_sin_avanzar = EXCLUDED.horas_sin_avanzar, "
                "  texto_clave = EXCLUDED.texto_clave, "
                "  activo = EXCLUDED.activo, "
                "  updated_at = NOW()",
                (clave, descripcion, estado_origen, horas, texto_clave,
                 activo),
            )
        return True
    except Exception:
        log.exception("[nudges] upsert fallo clave=%s", clave)
        return False


def eliminar_regla(clave):
    try:
        from app import db as _db
        with _db._conexion_directa(timeout=10) as conn:
            conn.execute("DELETE FROM petra_nudges WHERE clave=%s", (clave,))
        return True
    except Exception:
        log.exception("[nudges] eliminar fallo clave=%s", clave)
        return False


def evaluar_y_enviar(forzar_horario=False):
    """Pasada horaria: por cada regla activa, encuentra agentes que
    llevan N+ horas en ese estado y no recibieron ese nudge aún.
    Envía el mensaje y marca metadata.nudges_enviados[clave]=ahora.

    Si estamos fuera de horario laboral o es fin de semana/feriado CO,
    saltea todo (excepto si forzar_horario=True — útil para tests).

    Devuelve (reglas_evaluadas, nudges_enviados)."""
    if not forzar_horario and not _horario_valido_para_nudge():
        log.info("[nudges] saltado — fuera de horario laboral / feriado CO")
        return 0, 0
    reglas = listar_reglas(solo_activas=True)
    if not reglas:
        return 0, 0
    from app import db as _db
    from app import petra_wa as _pwa
    from app.petra_agents.textos import t as _t
    enviados = 0
    ahora = _dt.datetime.utcnow()
    for r in reglas:
        try:
            horas = int(r["horas_sin_avanzar"])
            umbral = ahora - _dt.timedelta(hours=horas)
            umbral_iso = umbral.isoformat()
            with _db._conexion_directa(timeout=20) as conn:
                rows = conn.execute(
                    "SELECT c.id, c.telefono, c.nombre, rol.metadata "
                    "FROM petra_contacto_roles rol "
                    "JOIN petra_contactos c ON c.id = rol.contacto_id "
                    "WHERE rol.rol = 'agente_petra' "
                    "  AND rol.metadata->>'estado_agente' = %s "
                    "  AND COALESCE(rol.metadata->>'estado_desde', "
                    "               rol.updated_at::text) <= %s "
                    "  AND (rol.metadata->'nudges_enviados'->>%s) IS NULL",
                    (r["estado_origen"], umbral_iso, r["clave"]),
                ).fetchall()
                for cid, telefono, nombre, meta in rows:
                    meta = dict(meta or {})
                    try:
                        texto = _t(
                            r["texto_clave"],
                            fallback=f"[nudge {r['clave']} — texto no cargado]",
                            nombre=(nombre or "").split()[0] if nombre else "",
                        )
                        _pwa.send_text(telefono, texto)
                        # Marcar enviado
                        enviados_dict = meta.get("nudges_enviados") or {}
                        enviados_dict[r["clave"]] = ahora.isoformat()
                        meta["nudges_enviados"] = enviados_dict
                        conn.execute(
                            "UPDATE petra_contacto_roles SET metadata=%s::jsonb, "
                            "updated_at=NOW() "
                            "WHERE contacto_id=%s AND rol='agente_petra'",
                            (_json.dumps(meta), cid),
                        )
                        enviados += 1
                        log.info("[nudges] enviado %s a tel=%s", r["clave"], telefono)
                    except Exception:
                        log.exception("[nudges] fallo envío %s a tel=%s",
                                      r["clave"], telefono)
        except Exception:
            log.exception("[nudges] fallo regla %s", r.get("clave"))
    log.info("[nudges] evaluación: %d reglas → %d nudges enviados",
             len(reglas), enviados)
    return len(reglas), enviados


# ---------------------------------------------------------------------------
# M5.A — Nudges de "cita pendiente" (cadencia agresiva)
# ---------------------------------------------------------------------------

# Cadencia: 3 pings cada 5 min → 6 pings cada 1h → luego cada 24h
# hasta responder o cancelar. Expresado como lista de minutos desde que
# llegó la cita.
_CITA_PENDIENTE_HITOS_MIN = [
    5, 10, 15,                 # 3 pings cada 5 min
    75, 135, 195, 255, 315, 375,  # +1h x6 (desde el 3er ping)
]
# Después del último hito, cada 24h.
_CITA_PENDIENTE_DAILY_MIN = 60 * 24


def _siguiente_hito_cita(minutos_desde_creacion, ultimo_hito_enviado):
    """Devuelve el próximo hito (en min) que corresponde enviar, o None.
    `ultimo_hito_enviado` es el último minuto-marca ya enviado (int) o 0."""
    for hito in _CITA_PENDIENTE_HITOS_MIN:
        if minutos_desde_creacion >= hito and ultimo_hito_enviado < hito:
            return hito
    # Modo diario después del último hito
    ultimo_hito = _CITA_PENDIENTE_HITOS_MIN[-1]
    if minutos_desde_creacion < ultimo_hito:
        return None
    extra = minutos_desde_creacion - ultimo_hito
    hito_diario = ultimo_hito + ((extra // _CITA_PENDIENTE_DAILY_MIN) + 1) * _CITA_PENDIENTE_DAILY_MIN
    # Verificamos que ya haya pasado uno nuevo desde el último enviado
    daily_prev = ultimo_hito + (extra // _CITA_PENDIENTE_DAILY_MIN) * _CITA_PENDIENTE_DAILY_MIN
    if daily_prev > ultimo_hito_enviado and minutos_desde_creacion >= daily_prev:
        return daily_prev
    return None


def evaluar_citas_pendientes(forzar_horario=False):
    """Evalúa agentes 'activa' con `meta.cita_pendiente` sin responder y
    envía recordatorios con la cadencia especificada.

    `cita_pendiente` es dict en metadata:
        {id, cliente, created_at (iso)}
    Se marca `cita_pendiente.ultimo_nudge_min` (int, minutos) para
    saber qué hito ya se envió. Se detiene cuando la agente responde
    (la respuesta borra `cita_pendiente`) o escribe cancelar.

    Pausado fuera de horario / feriado CO, salvo que forzar_horario=True.
    """
    if not forzar_horario and not _horario_valido_para_nudge():
        return 0
    from app import db as _db
    from app import petra_wa as _pwa
    from app.petra_agents.textos import t as _t
    enviados = 0
    ahora_utc = _dt.datetime.utcnow()
    try:
        with _db._conexion_directa(timeout=15) as conn:
            rows = conn.execute(
                "SELECT c.id, c.telefono, c.nombre, rol.metadata "
                "FROM petra_contacto_roles rol "
                "JOIN petra_contactos c ON c.id = rol.contacto_id "
                "WHERE rol.rol = 'agente_petra' "
                "  AND rol.metadata->>'estado_agente' = 'activa' "
                "  AND rol.metadata->'cita_pendiente' IS NOT NULL"
            ).fetchall()
            for cid, telefono, nombre, meta in rows:
                meta = dict(meta or {})
                cita = meta.get("cita_pendiente") or {}
                if not cita:
                    continue
                created_at = cita.get("created_at")
                if not created_at:
                    continue
                try:
                    created_dt = _dt.datetime.fromisoformat(
                        str(created_at).replace("Z", ""))
                except Exception:
                    continue
                minutos = int((ahora_utc - created_dt).total_seconds() // 60)
                ultimo = int(cita.get("ultimo_nudge_min") or 0)
                hito = _siguiente_hito_cita(minutos, ultimo)
                if hito is None:
                    continue
                try:
                    nombre_corto = (nombre or "").split()[0] if nombre else ""
                    cliente = cita.get("cliente") or "un cliente"
                    texto = _t(
                        "nudges.cita_pendiente",
                        fallback=(
                            f"👋 {nombre_corto}, ¿pudiste ver la cita con "
                            f"*{cliente}*? Hace rato que llegó y todavía "
                            f"no la respondiste. Confirmá o rechazá "
                            f"cuando puedas 💜"
                        ),
                        nombre=nombre_corto, cliente=cliente,
                    )
                    _pwa.send_text(telefono, texto)
                    cita["ultimo_nudge_min"] = hito
                    meta["cita_pendiente"] = cita
                    conn.execute(
                        "UPDATE petra_contacto_roles SET metadata=%s::jsonb, "
                        "updated_at=NOW() "
                        "WHERE contacto_id=%s AND rol='agente_petra'",
                        (_json.dumps(meta), cid),
                    )
                    enviados += 1
                    log.info(
                        "[nudges:cita] enviado tel=%s hito=%smin",
                        telefono, hito,
                    )
                except Exception:
                    log.exception("[nudges:cita] fallo envío tel=%s", telefono)
    except Exception:
        log.exception("[nudges:cita] query fallo")
    return enviados


# =============================================================================
# Seed de reglas default (idempotente) — M5.A: alineado con la especificación
# =============================================================================
#
# Fases con silencio total (NO se seedean):
#   - autorizacion_rechazada (pausada hasta "ya me autorizó")
#   - kit_libro_recibido / kit_enviado post-foto-libro (humano trabajando)
#
# Fases con cadencia especial (manejadas aparte, NO por este seed):
#   - activa + cita_pendiente → `evaluar_citas_pendientes`
#   - capacitación cron 11am → scheduler.py (sigue sin pausarse)
#
SEED_REGLAS = [
    # (clave, estado_origen, horas, texto_clave, descripcion, activo)

    # Inscripción recolectando: cada 6h "terminá o cancela"
    ("recolectando_6h", "recolectando", 6, "nudges.recolectando_6h",
     "Agente que no completó la inscripción — recordatorio cada 6h.", True),

    # Capacitación: 48h si no pulsa Listo
    ("en_capacitacion_48h", "en_capacitacion", 48,
     "nudges.capacitacion_48h",
     "Agente en capacitación sin confirmar último video por >48h.", True),

    # Autorización pendiente: 24h → 48h → 7d → 14d (indefinido = una vez)
    ("autorizacion_pendiente_24h", "autorizacion_pendiente", 24,
     "nudges.autorizacion_pendiente_24h",
     "Chequeo amable 24h en autorización.", True),
    ("autorizacion_pendiente_48h", "autorizacion_pendiente", 48,
     "nudges.autorizacion_pendiente_48h",
     "Recordatorio con CTA 48h en autorización.", True),
    ("autorizacion_pendiente_7d", "autorizacion_pendiente", 168,
     "nudges.autorizacion_pendiente_7d",
     "Nudge 7d con argumentos para destrabar.", True),
    ("autorizacion_pendiente_14d", "autorizacion_pendiente", 336,
     "nudges.autorizacion_pendiente_14d",
     "Nudge 14d: pausa amable, retomar cuando quiera (indefinido).", True),

    # Kit pendiente (esperando dirección): 24h → 72h → indefinido (una vez)
    ("kit_pendiente_24h", "kit_pendiente", 24,
     "nudges.kit_pendiente_24h",
     "Agente que no completó dirección después de 1 día.", True),
    ("kit_pendiente_72h", "kit_pendiente", 72,
     "nudges.kit_pendiente_72h",
     "Agente sin dirección después de 3 días (indefinido).", True),

    # Kit enviado (guía enviada, esperando llegada): 7 días "¿llegó?"
    ("kit_enviado_7d", "kit_enviado", 168,
     "nudges.kit_enviado_7d",
     "Agente con guía enviada: a los 7 días pregunta '¿llegó el kit?'.", True),
]


def seed_reglas(forzar=False):
    """Carga reglas base. Idempotente: no sobrescribe las existentes
    salvo que forzar=True."""
    try:
        from app import db as _db
        ins = upd = omi = 0
        with _db._conexion_directa(timeout=20) as conn:
            for clave, estado, horas, texto_clave, desc, activo in SEED_REGLAS:
                ex = conn.execute(
                    "SELECT 1 FROM petra_nudges WHERE clave=%s", (clave,),
                ).fetchone()
                if ex and not forzar:
                    omi += 1
                    continue
                if ex:
                    conn.execute(
                        "UPDATE petra_nudges SET estado_origen=%s, "
                        "horas_sin_avanzar=%s, texto_clave=%s, "
                        "descripcion=%s, activo=%s, updated_at=NOW() "
                        "WHERE clave=%s",
                        (estado, horas, texto_clave, desc, activo, clave),
                    )
                    upd += 1
                else:
                    conn.execute(
                        "INSERT INTO petra_nudges "
                        "(clave, estado_origen, horas_sin_avanzar, "
                        " texto_clave, descripcion, activo) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (clave, estado, horas, texto_clave, desc, activo),
                    )
                    ins += 1
        return ins, upd, omi
    except Exception:
        log.exception("[nudges] seed fallo")
        return 0, 0, 0


# ---------------------------------------------------------------------------
# Recordatorios automáticos del paso actual (complementa los nudges-texto).
#
# A diferencia de evaluar_y_enviar() que manda textos genéricos, esta función
# dispara el call-to-action REAL del paso en que está la agente (equivalente
# a pulsar "📣 Recordar" del panel admin para cada agente atascada), usando
# `_enviar_recordatorio_retorno` del router.
#
# Diseño anti-spam:
#   - Solo estados determinísticos del flow (ESTADOS_FLOW).
#   - Mínimo 72h sin cambio en estado_desde (le damos tiempo real).
#   - Mínimo 48h desde el último OUT del bot (evita apilarse con nudges-texto).
#   - Máximo 1 recordatorio automático por agente cada 72h
#     (tracking en metadata.ultimo_recordatorio_auto_at).
#   - Respeta la misma ventana horaria que los nudges existentes.
# ---------------------------------------------------------------------------
ESTADOS_RECORDATORIO_AUTO = (
    "recolectando",
    "inscrito",
    "bienvenida_enviada",
    "esperando_capacitacion_dia1",
    "en_capacitacion",
    "capacitacion_completa",
    "autorizacion_pendiente",
    "kit_pendiente",
)
HORAS_SIN_AVANZAR_MIN = 72
HORAS_SIN_OUT_BOT_MIN = 48
COOLDOWN_RECORDATORIO_HORAS = 72


def evaluar_recordatorios_automaticos(forzar_horario=False):
    """Pasada periódica: busca agentes atascadas y dispara
    `_enviar_recordatorio_retorno` del router (= botón "📣 Recordar" del panel).

    Devuelve (candidatas_evaluadas, recordatorios_enviados)."""
    if not forzar_horario and not _horario_valido_para_nudge():
        log.info("[recordatorio-auto] saltado — fuera de horario")
        return 0, 0

    from app import db as _db
    from app.petra_agents.router import _enviar_recordatorio_retorno
    import datetime as _dt
    ahora = _dt.datetime.now(_dt.timezone.utc)
    corte_estado = ahora - _dt.timedelta(hours=HORAS_SIN_AVANZAR_MIN)
    corte_cooldown = ahora - _dt.timedelta(hours=COOLDOWN_RECORDATORIO_HORAS)
    corte_out_bot = ahora - _dt.timedelta(hours=HORAS_SIN_OUT_BOT_MIN)

    enviados = 0
    candidatas = 0
    try:
        with _db._conexion_directa(timeout=20) as conn:
            # Agentes en estados accionables con tiempo suficiente sin avanzar.
            rows = conn.execute(
                """
                SELECT c.id, c.telefono, c.nombre,
                       rol.metadata->>'estado_agente' AS estado,
                       rol.metadata->>'ultimo_recordatorio_auto_at' AS ult_rec,
                       (SELECT MAX(fecha) FROM mensajes
                         WHERE telefono=c.telefono AND direccion='out') AS ult_out,
                       rol.metadata AS meta
                  FROM petra_contacto_roles rol
                  JOIN petra_contactos c ON c.id = rol.contacto_id
                 WHERE rol.rol = 'agente_petra'
                   AND rol.metadata->>'estado_agente' = ANY(%s)
                   AND COALESCE(
                         (rol.metadata->>'estado_desde')::timestamptz,
                         rol.updated_at
                       ) <= %s
                """,
                (list(ESTADOS_RECORDATORIO_AUTO), corte_estado),
            ).fetchall()

            for cid, tel, nombre, estado, ult_rec_raw, ult_out, meta in rows:
                candidatas += 1

                # Cooldown: no mandar si ya se mandó recordatorio en últimas 72h.
                if ult_rec_raw:
                    try:
                        ult_rec = _dt.datetime.fromisoformat(ult_rec_raw)
                        if ult_rec.tzinfo is None:
                            ult_rec = ult_rec.replace(tzinfo=_dt.timezone.utc)
                        if ult_rec > corte_cooldown:
                            continue
                    except Exception:
                        pass

                # Si el bot le habló recién, no insistir todavía.
                if ult_out and ult_out > corte_out_bot:
                    continue

                try:
                    _enviar_recordatorio_retorno(tel)
                    nueva_meta = dict(meta or {})
                    nueva_meta["ultimo_recordatorio_auto_at"] = ahora.isoformat()
                    conn.execute(
                        "UPDATE petra_contacto_roles SET metadata=%s::jsonb, "
                        "updated_at=NOW() "
                        "WHERE contacto_id=%s AND rol='agente_petra'",
                        (_json.dumps(nueva_meta), cid),
                    )
                    enviados += 1
                    log.info("[recordatorio-auto] enviado tel=%s estado=%s",
                             tel, estado)
                except Exception:
                    log.exception("[recordatorio-auto] fallo tel=%s", tel)
    except Exception:
        log.exception("[recordatorio-auto] query/loop fallo")

    log.info("[recordatorio-auto] %d candidatas evaluadas → %d recordatorios",
             candidatas, enviados)
    return candidatas, enviados
