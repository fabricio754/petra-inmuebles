"""Runner de evals end-to-end para Petra.

Carga una golden (JSON con N turnos user ↔ bot), replay-ea contra el bot
real, mockea las funciones de envío de WhatsApp para capturar las
respuestas sin mandar nada afuera, y verifica cada aserción.

Uso:
    python -m app.evals_e2e                   # corre todas
    python -m app.evals_e2e 01_X.json         # corre una
    python -m app.evals_e2e --list            # lista disponibles
    python -m app.evals_e2e --baseline        # guarda snapshot del output
"""
import argparse
import json
import os
import re
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

GOLDEN_DIR = Path(__file__).parent / "golden"
BASELINE_FILE = Path(__file__).parent / "baseline.json"


class CapturedMessage:
    """Representa un mensaje que el bot intentó enviar (texto, botones, etc.)."""

    def __init__(self, kind, texto, extras=None):
        self.kind = kind  # "text" | "buttons" | "list" | "media"
        self.texto = texto or ""
        self.extras = extras or {}

    def __repr__(self):
        return f"[{self.kind}] {self.texto[:80]}..." if len(self.texto) > 80 else f"[{self.kind}] {self.texto}"


def _install_wa_mocks(captured):
    """Reemplaza petra_wa.send_* para capturar en vez de enviar.

    Devuelve la lista de patches activos para que el caller los limpie.
    """
    from app import petra_wa

    def _send_text(telefono, texto, **kwargs):
        captured.append(CapturedMessage("text", texto))
        return {"messages": [{"id": "mock"}]}

    def _send_buttons(telefono, body, botones, **kwargs):
        titles = [b.get("title", "") for b in (botones or [])]
        captured.append(CapturedMessage("buttons", body,
                        {"botones": titles, "ids": [b.get("id") for b in (botones or [])]}))
        return {"messages": [{"id": "mock"}]}

    def _send_list(telefono, body, boton_label, sections, **kwargs):
        rows = []
        for s in (sections or []):
            for r in s.get("rows", []):
                rows.append({"id": r.get("id"), "title": r.get("title")})
        captured.append(CapturedMessage("list", body, {"rows": rows, "cta": boton_label}))
        return {"messages": [{"id": "mock"}]}

    def _send_media_o_texto(telefono, titulo, url, **kwargs):
        captured.append(CapturedMessage("media", f"{titulo} :: {url}"))
        return {"messages": [{"id": "mock"}], "whitelist_skip": False}

    def _send_image(telefono, url, caption=None, **kwargs):
        captured.append(CapturedMessage("media", f"img :: {url}", {"caption": caption or ""}))
        return {"messages": [{"id": "mock"}]}

    patches = [
        patch.object(petra_wa, "send_text", _send_text),
        patch.object(petra_wa, "send_buttons", _send_buttons),
        patch.object(petra_wa, "send_list", _send_list),
        patch.object(petra_wa, "send_media_o_texto", _send_media_o_texto),
    ]
    if hasattr(petra_wa, "send_image"):
        patches.append(patch.object(petra_wa, "send_image", _send_image))
    for p in patches:
        p.start()
    return patches


def _reset_contacto(telefono, meta_inicial):
    """Borra meta previa del teléfono de prueba y setea meta_inicial."""
    from app import db as _db
    import json as _json
    try:
        with _db._conexion_directa(timeout=5) as conn:
            row = conn.execute(
                "SELECT id FROM petra_contactos WHERE telefono=%s", (telefono,)
            ).fetchone()
            if row:
                cid = row[0]
                conn.execute("DELETE FROM petra_contacto_roles WHERE contacto_id=%s", (cid,))
            else:
                r = conn.execute(
                    "INSERT INTO petra_contactos (telefono) VALUES (%s) RETURNING id",
                    (telefono,),
                ).fetchone()
                cid = r[0]
            if meta_inicial:
                conn.execute(
                    "INSERT INTO petra_contacto_roles (contacto_id, rol, metadata) "
                    "VALUES (%s, 'agente_petra', %s::jsonb)",
                    (cid, _json.dumps(meta_inicial)),
                )
        return cid
    except Exception as e:
        raise RuntimeError(f"No se pudo resetear contacto {telefono}: {e}")


def _cleanup_contacto(telefono):
    """Borra el contacto de prueba al terminar."""
    from app import db as _db
    try:
        with _db._conexion_directa(timeout=5) as conn:
            conn.execute(
                "DELETE FROM petra_contactos WHERE telefono=%s", (telefono,)
            )
    except Exception:
        pass


def _seed_fixtures(fixtures):
    """Seedea contactos extra para que la golden tenga contexto en DB
    (ej. una agente ya 'activa' en un conjunto para probar duplicados).

    Cada entrada: {"telefono": ..., "meta": {...}}
    Devuelve lista de teléfonos a limpiar al final.
    """
    from app import db as _db
    import json as _json
    telefonos = []
    if not fixtures:
        return telefonos
    try:
        with _db._conexion_directa(timeout=5) as conn:
            for fx in fixtures:
                tel = fx.get("telefono")
                meta = fx.get("meta") or {}
                if not tel:
                    continue
                telefonos.append(tel)
                # Limpiamos cualquier residuo previo del mismo teléfono.
                conn.execute("DELETE FROM petra_contactos WHERE telefono=%s", (tel,))
                r = conn.execute(
                    "INSERT INTO petra_contactos (telefono) VALUES (%s) RETURNING id",
                    (tel,),
                ).fetchone()
                cid = r[0]
                conn.execute(
                    "INSERT INTO petra_contacto_roles (contacto_id, rol, metadata) "
                    "VALUES (%s, 'agente_petra', %s::jsonb)",
                    (cid, _json.dumps(meta)),
                )
    except Exception as e:
        raise RuntimeError(f"No se pudieron seedear fixtures: {e}")
    return telefonos


def _cleanup_fixtures(telefonos):
    from app import db as _db
    try:
        with _db._conexion_directa(timeout=5) as conn:
            for tel in telefonos:
                conn.execute(
                    "DELETE FROM petra_contactos WHERE telefono=%s", (tel,)
                )
    except Exception:
        pass


def _simular_cron(cron):
    """Dispara un cron del scheduler como si corriese ahora. Pasa
    `forzar_horario=True` a los helpers de nudges para que los tests
    no dependan del día/hora en que corre el runner."""
    from app.petra_agents import nudges
    if cron == "nudges":
        nudges.evaluar_citas_pendientes(forzar_horario=True)
        nudges.evaluar_y_enviar(forzar_horario=True)
    elif cron == "nudges_cita":
        nudges.evaluar_citas_pendientes(forzar_horario=True)
    elif cron == "nudges_fase":
        nudges.evaluar_y_enviar(forzar_horario=True)
    else:
        raise ValueError(f"simular_cron desconocido: {cron}")


def _procesar_mensaje(telefono, texto):
    """Entra un mensaje al bot como si fuera un webhook de WhatsApp."""
    from app.petra_agents import flow_onboarding_agente as flow
    try:
        flow.handle(telefono, texto)
        return True
    except Exception as e:
        traceback.print_exc()
        return False


def _check_assertions(turno, captured, estado_actual):
    """Verifica las aserciones del turno contra lo capturado. Devuelve
    (ok, list_of_errors)."""
    errors = []
    all_text = "\n".join(m.texto for m in captured)
    all_text_lower = all_text.lower()

    for expected in turno.get("bot_contiene", []):
        if expected.lower() not in all_text_lower:
            errors.append(f"  ❌ falta en respuesta: {expected!r}")

    for forbidden in turno.get("bot_no_contiene", []):
        if forbidden.lower() in all_text_lower:
            errors.append(f"  ❌ aparece pero no debería: {forbidden!r}")

    for pattern in turno.get("bot_regex", []):
        if not re.search(pattern, all_text, re.IGNORECASE | re.MULTILINE):
            errors.append(f"  ❌ regex no matchea: {pattern!r}")

    if "estado_esperado" in turno:
        exp = turno["estado_esperado"]
        if estado_actual != exp:
            errors.append(f"  ❌ estado: esperaba {exp!r}, got {estado_actual!r}")

    return (not errors, errors)


def _leer_estado(telefono):
    from app import db as _db
    try:
        with _db._conexion_directa(timeout=5) as conn:
            row = conn.execute(
                "SELECT r.metadata->>'estado_agente' FROM petra_contactos c "
                "JOIN petra_contacto_roles r ON r.contacto_id=c.id "
                "AND r.rol='agente_petra' WHERE c.telefono=%s",
                (telefono,),
            ).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def run_golden(path, verbose=True):
    """Corre una golden. Devuelve (ok: bool, resumen: dict)."""
    with open(path) as f:
        golden = json.load(f)

    name = golden.get("name") or path.stem
    telefono = golden.get("telefono") or "+99999000001"
    meta_inicial = golden.get("meta_inicial") or {}
    turnos = golden.get("turnos", [])

    if verbose:
        print(f"\n━━━ {name} ━━━")
        if golden.get("description"):
            print(f"    {golden['description']}")

    _reset_contacto(telefono, meta_inicial)
    fixtures_telefonos = _seed_fixtures(golden.get("fixtures") or [])

    all_turns = []
    total_errors = []

    try:
        for i, turno in enumerate(turnos, 1):
            captured = []
            patches = _install_wa_mocks(captured)
            try:
                if "user" in turno:
                    input_desc = f"user: {turno['user']}"
                    _procesar_mensaje(telefono, turno["user"])
                elif "boton" in turno:
                    input_desc = f"boton: {turno['boton']}"
                    _procesar_mensaje(telefono, turno["boton"])
                elif "simular_cron" in turno:
                    cron = turno["simular_cron"]
                    input_desc = f"cron: {cron}"
                    _simular_cron(cron)
                else:
                    input_desc = "(sin input)"
            finally:
                for p in patches:
                    p.stop()

            estado = _leer_estado(telefono)
            ok, errors = _check_assertions(turno, captured, estado)
            all_turns.append({
                "n": i, "input": input_desc, "ok": ok,
                "respuestas": [str(m) for m in captured],
                "estado": estado, "errores": errors,
            })
            if verbose:
                flag = "✅" if ok else "❌"
                print(f"  {flag} Turno {i}: {input_desc}")
                if not ok:
                    for e in errors:
                        print(e)
                    for m in captured:
                        print(f"      → {m}")
            if not ok:
                total_errors.extend(errors)
    finally:
        _cleanup_contacto(telefono)
        _cleanup_fixtures(fixtures_telefonos)

    resumen = {
        "name": name,
        "ok": not total_errors,
        "turnos": all_turns,
        "errores_total": len(total_errors),
    }
    return (not total_errors, resumen)


def _listar():
    return sorted(GOLDEN_DIR.glob("*.json"))


def main():
    parser = argparse.ArgumentParser(prog="python -m app.evals_e2e")
    parser.add_argument("golden", nargs="?", help="Nombre del archivo golden (opcional)")
    parser.add_argument("--list", action="store_true", help="Lista goldens disponibles")
    parser.add_argument("--baseline", action="store_true", help="Guarda snapshot del output")
    parser.add_argument("--quiet", action="store_true", help="Menos verbose")
    args = parser.parse_args()

    if args.list:
        for p in _listar():
            print(p.name)
        return 0

    goldens = []
    if args.golden:
        p = GOLDEN_DIR / args.golden
        if not p.exists():
            print(f"❌ No existe {p}")
            return 1
        goldens = [p]
    else:
        goldens = _listar()

    if not goldens:
        print("No hay goldens en", GOLDEN_DIR)
        return 1

    resultados = []
    failed = 0
    for p in goldens:
        try:
            ok, res = run_golden(p, verbose=not args.quiet)
            resultados.append(res)
            if not ok:
                failed += 1
        except Exception as e:
            print(f"\n❌ {p.name}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'═' * 50}")
    print(f"Total: {len(goldens)} · OK: {len(goldens)-failed} · FAIL: {failed}")

    if args.baseline:
        with open(BASELINE_FILE, "w") as f:
            json.dump(resultados, f, indent=2, ensure_ascii=False, default=str)
        print(f"Baseline guardado en {BASELINE_FILE}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
