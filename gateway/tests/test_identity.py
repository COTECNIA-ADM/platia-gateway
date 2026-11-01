"""Contraseñas, TOTP y claves personales sin base de datos (G10)."""
import pytest

from app import passwords, totp
from app.keys import USER_PREFIX, hash_key, kind_of, new_user_key
from app.models import KeyKind
from app.settings import Settings


def test_contrasena_se_guarda_con_scrypt_y_se_comprueba():
    stored = passwords.hash_password("una contraseña larga")
    assert stored.startswith("scrypt$15$8$1$")
    assert passwords.verify_password("una contraseña larga", stored)
    assert not passwords.verify_password("otra contraseña larga", stored)
    assert not passwords.needs_rehash(stored)
    # Dos veces la misma contraseña dan hashes distintos (sal aleatoria)
    assert passwords.hash_password("una contraseña larga") != stored


@pytest.mark.parametrize("stored", [None, "", "bcrypt$x", "scrypt$15$8$1$no-base64$@@", "scrypt$a$b$c$d$e"])
def test_contrasena_guardada_rara_no_entra(stored):
    assert not passwords.verify_password("lo que sea", stored)


def test_contrasena_demasiado_larga_no_se_calcula():
    stored = passwords.hash_password("una contraseña larga")
    assert not passwords.verify_password("x" * (passwords.MAX_LENGTH + 1), stored)


def test_parametros_antiguos_piden_rehash():
    assert passwords.needs_rehash("scrypt$14$8$1$c2Fs$aGFzaA==")


@pytest.mark.parametrize("password, message", [
    ("corta", "al menos"),
    ("a" * 300, "más de"),
    ("abababababababab", "repite"),
    ("ana.garcia-2026!", "correo"),
])
def test_politica_de_contrasenas(password, message):
    with pytest.raises(passwords.WeakPassword, match=message):
        passwords.check_policy(password, email="ana.garcia@cotecnia.es")


def test_politica_acepta_una_buena():
    passwords.check_policy("Tres palabras al azar", email="ana@cotecnia.es")


# RFC 6238, anexo B: secreto ASCII "12345678901234567890", SHA-1, t=59 → 94287082 (8 cifras)
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def test_totp_vector_del_rfc():
    assert totp.code_at(RFC_SECRET, 59 // 30) == "287082"
    assert totp.code_at(RFC_SECRET, 1111111109 // 30) == "081804"


def test_totp_acepta_el_paso_de_al_lado_y_no_repite():
    now = 1_700_000_000.0
    step = totp.current_step(now)
    assert totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, step), now=now) == step
    assert totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, step - 1), now=now) == step - 1
    assert totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, step + 1), now=now) == step + 1
    assert totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, step + 2), now=now) is None
    # Ya usado: el mismo paso (o uno anterior) no vale otra vez
    assert totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, step), after_step=step, now=now) is None
    # Con espacios, como lo enseñan algunas apps
    code = totp.code_at(RFC_SECRET, step)
    assert totp.verify(RFC_SECRET, f"{code[:3]} {code[3:]}", now=now) == step


@pytest.mark.parametrize("code", ["", "12345", "abcdef", "1234567"])
def test_totp_rechaza_formatos_raros(code):
    assert totp.verify(RFC_SECRET, code) is None
    assert totp.verify("", "123456") is None


def test_totp_secreto_y_enlace_para_la_app():
    secret = totp.new_secret()
    assert len(secret) == 32 and secret.isalnum()
    uri = totp.provisioning_uri(secret, "ana@cotecnia.es")
    assert uri.startswith("otpauth://totp/PLATIA%20Gateway%3Aana%40cotecnia.es?")
    assert f"secret={secret}" in uri and "issuer=PLATIA+Gateway" in uri


def test_clave_personal_no_lleva_el_nombre():
    new = new_user_key()
    assert new.raw.startswith(USER_PREFIX) and len(new.raw) == len(USER_PREFIX) + 48
    assert new.prefix == new.raw[:len(USER_PREFIX) + 8]
    assert new.key_hash == hash_key(new.raw)
    assert kind_of(new.raw) == KeyKind.USER


def test_cookie_segura_segun_el_entorno():
    assert Settings(env="production").cookie_secure
    assert not Settings(env="development").cookie_secure
    assert Settings(env="development", auth_cookie_secure="true").cookie_secure
    assert not Settings(env="production", auth_cookie_secure="false").cookie_secure
