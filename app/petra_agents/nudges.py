"""Motor de nudges (remarketing automático de FASE F).

Un nudge es una regla: "si una agente lleva N horas en estado X sin
avanzar, mandale el mensaje Y". Las reglas viven en `petra_nudges` y
los textos en `petra_textos`. El cron horario llama a `evaluar_y_enviar`.

Tracking: cada contacto guarda en metadata.nudges_enviados un dict
{clave_nudge: iso_timestamp} para no re-enviar el mismo nudge dos veces
(mientras siga en el mismo estado).

Cuando el agente cambia de estado, el flow setea metadata.estado_desde,
que se usa como ancla para medir "hace cuántas horas está acá".
"""
import datetime as _dt
import json as _json
import logging

log = logging.getLogger("petra")


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


def evaluar_y_enviar():
    """Pasada horaria: por cada regla activa, encuentra agentes que
    llevan N+ horas en ese estado y no recibieron ese nudge aún.
    Envía el mensaje y marca metadata.nudges_enviados[clave]=ahora.
    Devuelve (reglas_evaluadas, nudges_enviados)."""
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


# =============================================================================
# Seed de reglas default (idempotente)
# =============================================================================

SEED_REGLAS = [
    # (clave, estado_origen, horas, texto_clave, descripcion, activo)
    ("recolectando_12h", "recolectando", 12, "nudges.recolectando_12h",
     "Agente que arrancó la inscripción pero la abandonó.", True),
    ("inscrito_12h", "inscrito", 12, "nudges.inscrito_12h",
     "Agente inscrita pero no empezó la capacitación.", True),
    ("bienvenida_enviada_12h", "bienvenida_enviada", 12,
     "nudges.bienvenida_enviada_12h",
     "Agente vio bienvenida pero no pulsó 'Empezar ahora'.", True),
    ("en_capacitacion_24h", "en_capacitacion", 24,
     "nudges.capacitacion_24h",
     "Agente en capacitación pero sin confirmar último video por >1 día.", True),
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
     "Nudge 14d: pausa amable, retomar cuando quiera.", True),
    ("kit_pendiente_24h", "kit_pendiente", 24,
     "nudges.kit_pendiente_24h",
     "Agente que no completó dirección después de 1 día.", True),
    ("kit_enviado_72h", "kit_enviado", 72,
     "nudges.kit_enviado_72h",
     "Agente que recibió el kit pero no confirmó instalación QR en 3 días.", True),
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
