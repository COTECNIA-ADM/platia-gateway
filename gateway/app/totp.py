"""Códigos de un solo uso por tiempo (TOTP, RFC 6238) para el segundo factor (G10, ADR-014).

Compatible con cualquier app autenticadora (Google Authenticator, Microsoft Authenticator,
1Password...): SHA-1, 6 dígitos y pasos de 30 segundos. Se acepta el paso anterior y el
siguiente por desajustes de reloj, y `verify` devuelve el paso usado para que el mismo
código no sirva dos veces.

La idea sigue machinebooks.ai/ES/aigateway/cap-17/block-12.py (MIT), sin la dependencia
pyotp: el algoritmo cabe en unas líneas de la biblioteca estándar.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

DIGITS = 6
PERIOD = 30
WINDOW = 1
ISSUER = "PLATIA Gateway"


def new_secret() -> str:
    """20 bytes aleatorios en base32, sin relleno: lo que esperan las apps."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10 ** DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // PERIOD)


def verify(secret: str, code: str, *, after_step: int | None = None, now: float | None = None) -> int | None:
    """Paso del código si es válido y posterior a `after_step` (el último usado); si no, None."""
    code = (code or "").strip().replace(" ", "")
    if not secret or len(code) != DIGITS or not code.isdigit():
        return None
    step = current_step(now)
    for candidate in range(step - WINDOW, step + WINDOW + 1):
        if after_step is not None and candidate <= after_step:
            continue
        if hmac.compare_digest(code_at(secret, candidate), code):
            return candidate
    return None


def provisioning_uri(secret: str, account: str) -> str:
    """Enlace otpauth:// que la app lee con un código QR."""
    label = quote(f"{ISSUER}:{account}")
    query = urlencode({"secret": secret, "issuer": ISSUER, "digits": DIGITS, "period": PERIOD})
    return f"otpauth://totp/{label}?{query}"
