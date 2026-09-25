"""Genera el QR físico de Arrayanes. Solo se puede correr una vez que
tengas el número de WhatsApp de prueba (META_TEST_NUMBER en .env),
porque el QR codifica un link wa.me hacia ESE número.

Uso:
    python3 scripts/generate_qr.py
"""
import os
import sys
import qrcode
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

NUMERO = os.environ.get("META_TEST_NUMBER", "").strip()
TEXTO_PRECARGADO = "ARRAYANES"
OUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "qr-arrayanes.png")


def main():
    if not NUMERO:
        print("Falta META_TEST_NUMBER en el entorno (.env). Es el número de prueba")
        print("que te da Meta en developers.facebook.com al crear la app de WhatsApp.")
        print("Formato: solo dígitos con código de país, sin '+' ni espacios. Ej: 573001234567")
        sys.exit(1)

    link = f"https://wa.me/{NUMERO}?text={TEXTO_PRECARGADO}"
    img = qrcode.make(link)
    img.save(OUT_PATH)
    print(f"QR generado: {OUT_PATH}")
    print(f"Apunta a: {link}")


if __name__ == "__main__":
    main()
