"""Simula conversaciones del bot de crédito sin WhatsApp real. Corre la
MISMA lógica (app/bot.py) que usa el webhook; solo cambia el transporte
(consola en vez de WhatsApp, modo DRY-RUN).

Uso:
    python3 scripts/simulate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import bot, state  # noqa: E402


def texto(t):
    return {"type": "text", "text": t}


def boton(id_):
    return {"type": "button_reply", "id": id_}


def conversar(titulo, phone, eventos):
    print(f"\n=== {titulo} ===")
    for ev in eventos:
        entrada = ev.get("text") or ev.get("id")
        for r in bot.handle_incoming(phone, ev):
            print(f"  usuario: {entrada!r:22} → bot: {r['summary'][:80]!r}")


def main():
    for path in (state.SESSIONS_PATH, state.LEADS_PATH):
        if os.path.exists(path):
            os.remove(path)

    conversar("Lead calificado", "573000000001", [
        texto("Hola"), texto("¿quién eres?"), boton("BOTON_SI"),
        boton("BOTON_NO"), boton("BOTON_NO"), boton("BOTON_NO"), boton("BOTON_SI"),
        texto("Carlos Pérez"), texto("abc"), texto("1.020.304.050"),
        texto("no-es-correo"), texto("carlos@correo.com"), texto("Cra 45 #80-20, Bogotá"),
    ])
    conversar("Descartado por hipoteca", "573000000002", [
        texto("Hola"), texto("si"), texto("sí"),
    ])
    conversar("Pausado por paz y salvo", "573000000003", [
        texto("Hola"), boton("BOTON_SI"), boton("BOTON_NO"), boton("BOTON_NO"),
        boton("BOTON_NO"), boton("BOTON_NO"),
    ])
    conversar("No acepta / SALIR / vuelve a escribir", "573000000004", [
        texto("Hola"), boton("BOTON_NO"), texto("hola de nuevo"), texto("SALIR"),
    ])


if __name__ == "__main__":
    main()
