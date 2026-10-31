"""Cifrado en reposo de secretos (claves de proveedor) con AES-256-GCM (cap. 18 y 32).

Formato guardado: ``enc:v1:<base64(nonce[12] + texto cifrado + etiqueta)>``.

Cada valor se cifra con un *contexto* (por ejemplo ``provider:anthropic``) como dato
asociado: un valor copiado a otra fila no se puede descifrar allí.

Adaptado de machinebooks.ai/ES/aigateway/cap-32/block-07.py (MIT, ver THIRD_PARTY_NOTICES.md).
"""
import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "enc:v1:"
_NONCE_BYTES = 12


class CryptoError(Exception):
    """Clave mal configurada o valor que no se puede descifrar. El mensaje no lleva secretos."""


def load_key(encoded: str) -> bytes:
    if not encoded:
        raise CryptoError("ENCRYPTION_KEY no está configurada")
    try:
        key = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise CryptoError("ENCRYPTION_KEY no es base64 válido") from exc
    if len(key) != 32:
        raise CryptoError("ENCRYPTION_KEY debe tener 32 bytes (AES-256)")
    return key


def encrypt(plaintext: str, *, key: bytes, context: str) -> str:
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), context.encode("utf-8"))
    return PREFIX + base64.b64encode(nonce + sealed).decode("ascii")


def decrypt(value: str, *, key: bytes, context: str) -> str:
    if not value.startswith(PREFIX):
        raise CryptoError("Formato de valor cifrado desconocido")
    try:
        raw = base64.b64decode(value[len(PREFIX):], validate=True)
        nonce, sealed = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        return AESGCM(key).decrypt(nonce, sealed, context.encode("utf-8")).decode("utf-8")
    except (binascii.Error, ValueError, InvalidTag) as exc:
        raise CryptoError("No se puede descifrar el valor (clave o contexto incorrectos)") from exc
