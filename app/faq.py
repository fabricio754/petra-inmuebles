"""Preguntas frecuentes del bot.

Detector por regex + respuesta canónica. Hoy, cuando un cliente pregunta
"¿cuánto cobran de interés?" o "¿quiénes son ustedes?", el bot no entiende
y le manda el consent de autorización — lead frío. Este módulo intercepta
esas preguntas y responde al instante con un texto canónico, sin interrumpir
el flujo (ver `app/bot.py: handle_incoming`).

Cada FAQ tiene:
- `clave` (str): identificador único (para analytics y respuesta_de).
- `patrones` (list[str]): regex compilados sobre texto normalizado
  (lowercase + sin acentos — ver `app.filtros._normalizar`). NO llevan
  tildes ni enies.
- `respuesta` (str): texto canónico a enviar.

API pública:
- `detectar(texto) -> dict | None`: devuelve el primer FAQ que matchea.
- `respuesta_de(clave) -> str | None`: la respuesta canónica por clave.

El orden importa: si un texto matchea varias FAQs, gana la primera.
"""
import re

from app.filtros import _normalizar  # reusar normalizador (lowercase + sin acentos)


FAQ = [
    {
        "clave": "quienes_somos",
        "patrones": [
            r"qui[ei]n(?:es)? son",
            r"quien me (?:habla|escribe|contact)",
            r"que (?:empresa|compania|hacen|ofrecen)",
            r"son (?:de un )?banco",
            r"son (?:de |una |un )?(?:estafa|legales|serios|confiables|reales)",
            r"me pueden explic",
            r"que es petra",
            r"cuentenm?e mas",
            r"informacion de (?:la empresa|petra|ustedes)",
        ],
        "respuesta": (
            "Somos Petra, una empresa de credito con garantia inmobiliaria 🏛️\n\n"
            "Le prestamos dinero a propietarios usando su inmueble como respaldo\n"
            "— sin necesidad de venderlo.\n\n"
            "Nos encargamos de todo el proceso (avaluo, estudio juridico, escrituracion)\n"
            "y usted recibe el dinero en su cuenta. El inmueble sigue siendo suyo.\n\n"
            "¿Le interesaria saber cuanto podria obtener con el suyo?"
        ),
    },
    {
        "clave": "tasa_interes",
        "patrones": [
            r"(?:cuanto|cual) (?:es )?(?:la |el )?(?:tasa|interes|ea|em|tasa.*mes|dtf)",
            r"cuanto (?:cobran|cuesta|me cobran)",
            r"tasa de interes",
            r"interes mensual",
            r"que (?:tasa|interes)",
            r"cuanto me sale",
            r"costo del credito",
        ],
        "respuesta": (
            "La tasa depende del monto, el plazo y el inmueble,\n"
            "pero trabajamos en rangos competitivos 📉\n\n"
            "Le damos la tasa exacta una vez evaluamos su propiedad\n"
            "— sin costo ni compromiso de su parte.\n\n"
            "¿Que monto aproximado necesita y a que plazo?"
        ),
    },
    {
        "clave": "tiempo_desembolso",
        "patrones": [
            r"(?:cuanto|en cuanto) (?:tiempo|dura|tarda|demora)",
            r"en cuanto (?:me |nos )?(?:dan|desembolsan|entregan|pagan)",
            r"tiempo (?:de )?desembolso",
            r"cuando (?:me |nos )?(?:llega|dan|desembolsan|pagan) el dinero",
            r"cuanto (?:toma|tarda) el proceso",
            r"que tan (?:rapido|agil)",
        ],
        "respuesta": (
            "Una vez aprobada la operacion, el desembolso tarda entre 15 y 30 dias habiles ⚡\n\n"
            "El proceso completo incluye:\n"
            "1. Avaluo del inmueble\n"
            "2. Estudio de titulos\n"
            "3. Firma de escritura\n"
            "4. Registro y desembolso\n\n"
            "Nosotros coordinamos todo — usted solo firma y recibe el dinero en su cuenta."
        ),
    },
    {
        "clave": "como_pago",
        "patrones": [
            r"como (?:son |funcionan |se hacen )?(?:los )?pagos?",
            r"como (?:se |me )(?:paga|pagan|debe)",
            r"(?:la )?cuota (?:mensual|cuanto)",
            r"cuanto (?:se |hay que )?paga",
            r"(?:forma|modalidad) de pago",
            r"plazo (?:maximo|minimo)",
            r"cuotas mensuales",
            r"pago (?:solo )?(?:interes|intereses)",
        ],
        "respuesta": (
            "Tiene flexibilidad en la forma de pago 💪\n\n"
            "*Opcion 1 — Solo intereses:*\n"
            "Paga unicamente los intereses durante el plazo y el capital al final.\n"
            "Ideal si necesita cuotas bajas y espera flujo futuro.\n\n"
            "*Opcion 2 — Cuota normal:*\n"
            "Paga capital e intereses desde el inicio, reduciendo la deuda cada mes.\n\n"
            "¿Cual se adapta mejor a su situacion?"
        ),
    },
    {
        "clave": "sigo_usando",
        "patrones": [
            r"puedo (?:seguir )?(?:usar|usando|habitar|viviendo|arrendando|rentando|alquilando)",
            r"pierdo (?:el|la|mi) (?:apartamento|casa|inmueble|propiedad|local)",
            r"me quit[aeo]n (?:el|la|mi)",
            r"sigo siendo (?:dueno|duena|propietario|propietaria)",
            r"me quedo con (?:el|la|mi)",
            r"que pasa (?:con |si no |cuando)",
            r"(?:lo |la )?pueden quitar",
            r"embargar",
        ],
        "respuesta": (
            "Si, el inmueble sigue siendo suyo y puede seguir usandolo o arrendandolo ✅\n\n"
            "La garantia es hipotecaria — no lo perdemos. Usted conserva la posesion\n"
            "y puede seguir generando ingresos con el mientras paga el credito.\n\n"
            "Al cancelar el credito, la hipoteca se levanta y el inmueble\n"
            "queda completamente libre.\n\n"
            "¿Quiere que avancemos y le enviemos la propuesta?"
        ),
    },
]

# Compilar patrones una sola vez al importar.
for _faq in FAQ:
    _faq["regex"] = [re.compile(p) for p in _faq["patrones"]]


def detectar(texto: str) -> dict | None:
    """Retorna el FAQ dict si matchea alguna, None si no.

    El texto se normaliza (lowercase + sin acentos) antes de aplicar los
    patrones. Si un texto matchea varias FAQs, se devuelve la primera.
    """
    norm = _normalizar(texto)
    if not norm:
        return None
    for faq in FAQ:
        if any(r.search(norm) for r in faq["regex"]):
            return faq
    return None


def respuesta_de(clave: str) -> str | None:
    """Devuelve el texto canónico asociado a una clave FAQ, o None."""
    for faq in FAQ:
        if faq["clave"] == clave:
            return faq["respuesta"]
    return None
