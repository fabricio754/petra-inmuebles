"""Simula el flujo completo de un usuario sin necesitar cuentas de
Meta ni WATI. Corre la MISMA lógica (app/bot.py) que usará el webhook
real; solo cambia el transporte (consola en vez de WhatsApp).

Uso:
    python3 scripts/simulate.py
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import bot, state  # noqa: E402

PHONE = "573000000000"


def reset_demo_state():
    for path in (state.SESSIONS_PATH, state.LEADS_PATH):
        if os.path.exists(path):
            os.remove(path)


def paso(titulo, phone, event):
    print(f"\n--- {titulo} ---")
    print(f"Usuario ({phone}) envía: {event}")
    bot.handle_incoming(phone, event)


def main():
    reset_demo_state()
    print("=" * 60)
    print("SIMULACIÓN — Petra Inmuebles — Conjunto Arrayanes")
    print("(modo DRY-RUN: no se envía nada real a WhatsApp)")
    print("=" * 60)

    paso("1. Escanea QR de Arrayanes → abre WhatsApp con texto precargado",
         PHONE, {"type": "text", "text": "ARRAYANES"})

    paso("2. Usuario toca '🏠 Tomar en arriendo'",
         PHONE, {"type": "list_reply", "id": "MENU_ARRIENDO"})

    paso("3. Usuario toca 'Apto 101' en el catálogo",
         PHONE, {"type": "list_reply", "id": "APTO_ARRAYANES-ARR-101"})

    paso("4. Usuario toca el botón 'Contactar'",
         PHONE, {"type": "button_reply", "id": "CONTACTAR_ARRAYANES-ARR-101"})

    print("\n" + "=" * 60)
    leads = state._load_json(state.LEADS_PATH, [])
    print(f"Leads guardados en data/leads.json: {len(leads)}")
    print(json.dumps(leads, indent=2, ensure_ascii=False))
    print("=" * 60)


if __name__ == "__main__":
    main()
