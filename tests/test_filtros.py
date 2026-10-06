from app.filtros import es_broker, es_opt_out


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
