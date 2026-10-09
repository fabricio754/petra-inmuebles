"""Tests para el detector de FAQ pre-grabadas (`app/faq.py`).

Cubre los 5 intents canonicos (quienes_somos, tasa_interes, tiempo_desembolso,
como_pago, sigo_usando) con ejemplos reales del piloto y verifica que no
colisiona con "pide llamada" (PR #56) ni con un saludo neutral.
"""
from app import faq


def test_detectar_quienes_somos():
    for texto in [
        "quienes son?",
        "¿Quienes son ustedes?",
        "¿Que empresa son?",
        "que es petra",
        "Cuentenme mas por favor",
        "Cuéntenme mas de lo que ofrecen",
        "me pueden explicar que hacen",
        "informacion de la empresa",
        "¿Son de un banco?",
        "¿Son legales?",
        "son serios?",
        # Variantes reales que no matcheaban antes:
        "¿Quién es ustedes?",   # "quien es" (no "quien son" / "quienes son").
        "Cuentame mas",         # sin la 'n' de "cuentenme".
        "¿Son un banco?",       # sin "de" ("son un banco" vs "son de un banco").
    ]:
        res = faq.detectar(texto)
        assert res is not None, f"no detectó: {texto!r}"
        assert res["clave"] == "quienes_somos", f"{texto!r} → {res['clave']}"


def test_detectar_tasa():
    for texto in [
        "cuanto cobran?",
        "¿Cuál es la tasa?",
        "¿cuanto me cobran de interes?",
        "tasa de interes",
        "qué interés manejan",
        "cuanto me sale el credito",
        "costo del credito",
        "interes mensual?",
    ]:
        res = faq.detectar(texto)
        assert res is not None, f"no detectó: {texto!r}"
        assert res["clave"] == "tasa_interes", f"{texto!r} → {res['clave']}"


def test_detectar_tiempo():
    for texto in [
        "cuanto tarda?",
        "¿En cuanto me dan el dinero?",
        "cuánto tiempo demora el proceso",
        "tiempo de desembolso",
        "cuando me llega el dinero?",
        "cuanto toma el proceso",
        "que tan rapido es?",
    ]:
        res = faq.detectar(texto)
        assert res is not None, f"no detectó: {texto!r}"
        assert res["clave"] == "tiempo_desembolso", f"{texto!r} → {res['clave']}"


def test_detectar_pagos():
    for texto in [
        "como son los pagos?",
        "¿Cómo se paga?",
        "cuota mensual cuanto es",
        "forma de pago",
        "cuotas mensuales",
        "pago solo intereses?",
        "modalidad de pago",
        "plazo maximo?",
    ]:
        res = faq.detectar(texto)
        assert res is not None, f"no detectó: {texto!r}"
        assert res["clave"] == "como_pago", f"{texto!r} → {res['clave']}"


def test_detectar_sigo_usando():
    for texto in [
        "puedo seguir arrendando el apartamento?",
        "puedo seguir viviendo ahi",
        "¿Pierdo mi casa?",
        "me quitan el inmueble?",
        "sigo siendo dueño",
        "me quedo con mi apartamento?",
        "lo pueden quitar?",
        "van a embargar?",
    ]:
        res = faq.detectar(texto)
        assert res is not None, f"no detectó: {texto!r}"
        assert res["clave"] == "sigo_usando", f"{texto!r} → {res['clave']}"


def test_detectar_no_falsea_hola():
    """Un saludo neutral no debe matchear ninguna FAQ."""
    for texto in ["hola", "Buenos días", "Buenas tardes", "qué más", "ok", ""]:
        assert faq.detectar(texto) is None, f"falso positivo en {texto!r}"


def test_detectar_no_falsea_me_pueden_llamar():
    """No debe colisionar con 'pide llamada' (PR #56). Estos son el input
    tipico de es_pedido_llamada y NO deben disparar FAQ."""
    for texto in [
        "Me pueden llamar?",
        "me pueden llamar por favor",
        "necesito hablar con alguien",
        "quiero hablar con un asesor",
        "cual es su telefono",
    ]:
        assert faq.detectar(texto) is None, f"colisiona con pide_llamada: {texto!r}"


def test_detectar_prioridad_primera_match():
    """Si un texto podria matchear 2 FAQs, gana la primera en el orden del
    archivo. 'cuanto cobran por el credito' podria matchear tasa_interes
    (cuanto cobran) y como_pago (cuanto hay que paga). Debe ganar tasa."""
    # Texto que matchea tanto tasa como pagos.
    texto = "cuanto cobran y cual es el plazo maximo"
    res = faq.detectar(texto)
    assert res is not None
    # tasa_interes está antes que como_pago en FAQ, así que gana.
    assert res["clave"] == "tasa_interes"


def test_respuesta_de_devuelve_canonica():
    assert faq.respuesta_de("quienes_somos") is not None
    assert "Petra" in faq.respuesta_de("quienes_somos")
    assert faq.respuesta_de("tasa_interes") is not None
    assert faq.respuesta_de("inexistente") is None
