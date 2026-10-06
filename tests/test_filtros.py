from app.filtros import es_broker, es_opt_out, es_pedido_llamada


def test_es_broker_detecta_inmobiliaria():
    assert es_broker("Bienvenido a Inmobiliaria Bogotá")


def test_es_broker_no_falsea_particular():
    assert not es_broker("Hola, soy Juan y quiero vender mi apto")


def test_es_opt_out_detecta_no_me_interesa():
    assert es_opt_out("No me interesa")


def test_es_opt_out_detecta_no_estamos_vendiendo():
    assert es_opt_out("Ya no estamos vendiendo")


def test_es_opt_out_detecta_ya_vendido():
    assert es_opt_out("Ya se vendió gracias")


def test_es_opt_out_no_falsea_interes():
    assert not es_opt_out("Sí me interesa, cuéntame más")


def test_es_opt_out_no_falsea_para_preposicion():
    # "para" como preposicion no debe disparar opt-out.
    assert not es_opt_out("Hola, esto es un apto para la venta")


def test_es_opt_out_detecta_no_soy_dueno_con_enie():
    # El input lleva "ñ" pero el patron (normalizado) es "duen"; debe matchear.
    assert es_opt_out("No soy el dueño")


# ---------- es_pedido_llamada (lead caliente del piloto 6-oct) ---------------

def test_es_pedido_llamada_detecta_me_pueden_llamar():
    # Caso real: 573508463133 lo mandó 3 veces y nadie se enteró.
    assert es_pedido_llamada("Me pueden llamar?")


def test_es_pedido_llamada_detecta_con_quien_hablo():
    assert es_pedido_llamada("¿Con quién hablo?")


def test_es_pedido_llamada_detecta_quiero_hablar_con_asesor():
    assert es_pedido_llamada("Quiero hablar con un asesor")


def test_es_pedido_llamada_detecta_hablar_con_humano():
    assert es_pedido_llamada("Prefiero hablar con una persona")


def test_es_pedido_llamada_detecta_necesito_hablar():
    assert es_pedido_llamada("Necesito hablar con alguien ya")


def test_es_pedido_llamada_detecta_telefono():
    assert es_pedido_llamada("¿Me pasas un teléfono?")


def test_es_pedido_llamada_detecta_quien_atiende():
    assert es_pedido_llamada("¿Quién me atiende?")


def test_es_pedido_llamada_no_falsea_buenos_dias():
    assert not es_pedido_llamada("Buenos días")


def test_es_pedido_llamada_no_falsea_hola():
    assert not es_pedido_llamada("Hola")


def test_es_pedido_llamada_no_falsea_vacio():
    assert not es_pedido_llamada("")
    assert not es_pedido_llamada(None)
