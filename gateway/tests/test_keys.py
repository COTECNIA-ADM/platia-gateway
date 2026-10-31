from app.keys import APP_PREFIX, MAX_KEY_LENGTH, hash_key, kind_of, new_application_key
from app.models import KeyKind


def test_clave_nueva_de_aplicacion():
    key = new_application_key("platia")
    assert key.raw.startswith("plgw-app-platia-") and len(key.raw) == len("plgw-app-platia-") + 48
    assert key.prefix == key.raw[:len("plgw-app-platia-") + 8] and len(key.prefix) <= 48
    assert key.key_hash == hash_key(key.raw) and len(key.key_hash) == 64
    assert new_application_key("platia").raw != key.raw


def test_slug_largo_o_raro_se_recorta():
    key = new_application_key("Una_Aplicación con nombre larguísimo de verdad")
    assert key.raw.startswith(APP_PREFIX) and len(key.prefix) <= 48
    assert new_application_key("___").raw.startswith("plgw-app-app-")


def test_tipo_por_prefijo():
    assert kind_of("plgw-app-x-123") == KeyKind.APPLICATION
    assert kind_of("plgw-usr-123") == KeyKind.USER
    assert kind_of("sk-ant-123") is None
    assert kind_of("") is None
    assert kind_of(APP_PREFIX + "x" * MAX_KEY_LENGTH) is None
