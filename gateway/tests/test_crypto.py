import base64
import os

import pytest

from app.crypto import PREFIX, CryptoError, decrypt, encrypt, load_key


def _key_b64() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def test_cifra_y_descifra_con_el_mismo_contexto():
    key = load_key(_key_b64())
    sealed = encrypt("sk-ant-secreto", key=key, context="provider:anthropic")
    assert sealed.startswith(PREFIX)
    assert "sk-ant-secreto" not in sealed
    assert decrypt(sealed, key=key, context="provider:anthropic") == "sk-ant-secreto"


def test_cada_cifrado_usa_un_nonce_distinto():
    key = load_key(_key_b64())
    assert encrypt("x", key=key, context="c") != encrypt("x", key=key, context="c")


def test_otro_contexto_no_descifra():
    key = load_key(_key_b64())
    sealed = encrypt("secreto", key=key, context="provider:a")
    with pytest.raises(CryptoError):
        decrypt(sealed, key=key, context="provider:b")


def test_otra_clave_no_descifra_y_el_error_no_lleva_secretos():
    sealed = encrypt("secreto", key=load_key(_key_b64()), context="c")
    with pytest.raises(CryptoError) as exc:
        decrypt(sealed, key=load_key(_key_b64()), context="c")
    assert "secreto" not in str(exc.value)


@pytest.mark.parametrize("value", ["texto-plano", "enc:v1:%%%no-base64", "enc:v1:" + base64.b64encode(b"corto").decode()])
def test_valores_invalidos(value):
    with pytest.raises(CryptoError):
        decrypt(value, key=load_key(_key_b64()), context="c")


@pytest.mark.parametrize("encoded, message", [
    ("", "no está configurada"),
    ("ñ", "base64"),
    ("abc", "base64"),
    (base64.urlsafe_b64encode(b"16-bytes-de-clav").decode(), "32 bytes"),
])
def test_clave_mal_configurada(encoded, message):
    with pytest.raises(CryptoError, match=message):
        load_key(encoded)
