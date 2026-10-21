"""Eval harness para el tool-calling de Petra.

Qué hace: carga cada caso de app/evals/golden/*.json, lo replay-ea contra
el LLM real, y verifica que la tool esperada se haya llamado con los args
esperados. Los handlers se INTERCEPTAN (stub que solo registra la llamada)
para no tocar DB ni mandar WhatsApp — así el eval es seguro de correr
sin side-effects.

Uso:
    python -m app.evals                      # corre todos
    python -m app.evals caso_X.json          # corre uno
    python -m app.evals --list               # lista los disponibles

Un caso golden es un JSON así:
    {
      "name": "inscripcion_captura_ciudad",
      "fase": "inscripcion",
      "meta_inicial": {"nombre": "Fabricio Zabala", "cedula": "1020800837"},
      "turnos": [
        {
          "user_msg": "Zipaquira",
          "expect_tool": "capturar_datos_personales",
          "expect_args_contains": {"ciudad": "Zipaquirá"},
          "expect_silenciar_llm": true
        }
      ]
    }

Si `expect_tool` es null/omitido, se espera que el LLM responda SIN llamar
tool (ej. preguntas abiertas tipo FAQ).

Devuelve exit code 0 si todos pasan, 1 si alguno falla.
"""
import json
import os
import sys
import time
from pathlib import Path

GOLDEN_DIR = Path(__file__).parent / "golden"


# Módulos por fase (mismo mapping que flow_onboarding_agente)
_FASES = {
    "inscripcion": "app.petra_agents.tools_inscripcion",
    "autorizacion": "app.petra_agents.tools_autorizacion",
    "kit": "app.petra_agents.tools_kit",
    "capacitacion": "app.petra_agents.tools_capacitacion",
    "activa": "app.petra_agents.tools_activa",
}


class _StubConn:
    """Conexión fake. Las queries de los handlers no corren (los handlers
    se interceptan antes). Solo existe para que ctx['conn'].execute() no
    rompa si algún build_system_prompt lo llamara."""

    def execute(self, *a, **kw):
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _parchar_envios():
    """Monkey-patch petra_wa.* para que no mande mensajes reales.
    Devuelve lista donde se registra lo enviado (para debug)."""
    from app import petra_wa
    enviados = []
    petra_wa.send_text = lambda tel, txt, **kw: enviados.append(("text", tel, txt)) or True
    petra_wa.send_buttons = lambda tel, txt, botones, **kw: enviados.append(("btn", tel, txt, botones)) or True
    petra_wa.send_image = lambda tel, url, **kw: enviados.append(("img", tel, url)) or True
    petra_wa.send_video = lambda tel, url, **kw: enviados.append(("vid", tel, url)) or True
    petra_wa.send_media_o_texto = lambda tel, titulo, url, **kw: enviados.append(("media", tel, titulo, url)) or True
    return enviados


def _ejecutar_turno_eval(fase, meta, user_msg):
    """Corre un turno del LLM contra las tools reales pero con handlers
    stubbed. Devuelve dict con lo que invocó el LLM."""
    import importlib
    from app.petra_agents import toolcalling as tc

    mod = importlib.import_module(_FASES[fase])
    tools = mod.TOOLS
    # Interceptamos cada handler para que solo registre
    registro = []
    def _stub(nombre):
        def _fn(args, ctx):
            registro.append({"tool": nombre, "args": args})
            # Algunos handlers tienen efectos de state que el prompt
            # posterior lee. Para evals de 1 turno no importa.
            return {"ok": True, "_silenciar_llm": True, "_stub": True}
        return _fn
    handlers_stub = {name: _stub(name) for name in mod.HANDLERS.keys()}

    # Prompt real con meta real
    if fase == "activa":
        # tools_activa.build_system_prompt necesita conn para citas
        system_prompt = mod.build_system_prompt(meta, _StubConn(), cid=999999)
    else:
        system_prompt = mod.build_system_prompt(meta)

    ctx = {"telefono": "573000000000", "cid": 999999, "meta": dict(meta),
           "conn": _StubConn()}

    _parchar_envios()
    resultado = tc.ejecutar_turno(
        telefono="573000000000", texto=user_msg, contexto=ctx,
        tools=tools, handlers=handlers_stub,
        system_prompt=system_prompt,
        fase=fase,
    )
    return {
        "tools_invocadas": registro,
        "texto_respuesta": resultado.get("texto_respuesta"),
        "ok": resultado.get("ok"),
        "fallo": resultado.get("fallo"),
    }


def _validar_turno(expect, actual):
    """Devuelve lista de errores (vacía = pass)."""
    errores = []
    expect_tool = expect.get("expect_tool")
    tools_llamadas = [t["tool"] for t in actual["tools_invocadas"]]

    if expect_tool is None:
        if tools_llamadas:
            errores.append(f"esperaba SIN tool, pero el LLM llamó: {tools_llamadas}")
        return errores

    if expect_tool not in tools_llamadas:
        errores.append(f"esperaba tool '{expect_tool}', LLM llamó: {tools_llamadas or 'ninguna'}")
        return errores

    expect_args = expect.get("expect_args_contains") or {}
    if expect_args:
        llamada = next(t for t in actual["tools_invocadas"] if t["tool"] == expect_tool)
        for k, v in expect_args.items():
            if k not in llamada["args"]:
                errores.append(f"args.{k} ausente (esperaba {v!r}); args reales={llamada['args']}")
            else:
                actual_v = llamada["args"][k]
                # Comparación tolerante: tildes, mayúsculas
                if isinstance(v, str) and isinstance(actual_v, str):
                    if v.lower().strip() != actual_v.lower().strip():
                        errores.append(f"args.{k}: esperaba {v!r}, LLM pasó {actual_v!r}")
                elif actual_v != v:
                    errores.append(f"args.{k}: esperaba {v!r}, LLM pasó {actual_v!r}")

    expect_not_args = expect.get("expect_args_not_contains") or []
    if expect_not_args and tools_llamadas:
        llamada = next(t for t in actual["tools_invocadas"] if t["tool"] == expect_tool)
        for k in expect_not_args:
            if k in llamada["args"]:
                errores.append(f"args.{k} NO debería estar (campo ya guardado); args={llamada['args']}")

    return errores


def _correr_caso(path):
    with open(path, encoding="utf-8") as f:
        caso = json.load(f)
    nombre = caso.get("name") or path.stem
    fase = caso["fase"]
    meta = dict(caso.get("meta_inicial") or {})
    print(f"\n[eval] {nombre}  (fase={fase})")
    errores_totales = []
    for i, turno in enumerate(caso.get("turnos") or []):
        user_msg = turno["user_msg"]
        t0 = time.monotonic()
        actual = _ejecutar_turno_eval(fase, meta, user_msg)
        dt_ms = int((time.monotonic() - t0) * 1000)
        tools_str = ",".join(t["tool"] for t in actual["tools_invocadas"]) or "(ninguna)"
        print(f"   turno {i+1}  user={user_msg!r}  →  tools=[{tools_str}]  ({dt_ms}ms)")
        errores = _validar_turno(turno, actual)
        for e in errores:
            print(f"       ❌ {e}")
            errores_totales.append(f"turno {i+1}: {e}")
    if not errores_totales:
        print("   ✅ PASS")
        return True, nombre, []
    print(f"   ❌ FAIL ({len(errores_totales)} error/es)")
    return False, nombre, errores_totales


def main(argv):
    if "--list" in argv:
        for p in sorted(GOLDEN_DIR.glob("*.json")):
            print(p.name)
        return 0

    archivos_filtro = [a for a in argv if a.endswith(".json")]
    if archivos_filtro:
        paths = []
        for a in archivos_filtro:
            p = GOLDEN_DIR / a if not os.path.isabs(a) else Path(a)
            if not p.exists():
                print(f"❌ no encuentro: {a}")
                return 2
            paths.append(p)
    else:
        paths = sorted(GOLDEN_DIR.glob("*.json"))

    if not paths:
        print("Sin casos en app/evals/golden/. Agregá un .json y volvé a correr.")
        return 1

    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        print("❌ ANTHROPIC_API_KEY no seteada. Exportá la env var antes de correr.")
        return 2

    resultados = [_correr_caso(p) for p in paths]
    pasaron = sum(1 for r in resultados if r[0])
    fallaron = [r for r in resultados if not r[0]]
    print(f"\n{'='*60}")
    print(f"[eval] {pasaron}/{len(resultados)} pasaron")
    for ok, nombre, errs in fallaron:
        print(f"  ❌ {nombre}")
        for e in errs:
            print(f"     · {e}")
    return 0 if not fallaron else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
