"""Contraseñas de las personas (G10, ADR-014).

Se guardan con scrypt (biblioteca estándar): lento a propósito y con mucha memoria, para
que probar contraseñas robadas cueste. Formato: ``scrypt$<log2 N>$<r>$<p>$<sal>$<hash>``,
con la sal y el hash en base64. Si un día suben los parámetros, `needs_rehash` lo dice y
la contraseña se vuelve a guardar en el siguiente inicio de sesión.

El cap. 16 del libro usa bcrypt; scrypt evita una dependencia más y resiste mejor el
ataque con tarjetas gráficas.
"""
import base64
import hashlib
import hmac
import secrets

LOG2_N, R, P = 15, 8, 1
_MAXMEM = 64 * 1024 * 1024
MIN_LENGTH = 12
MAX_LENGTH = 256


class WeakPassword(ValueError):
    """La contraseña no cumple la política. El mensaje se puede enseñar a la persona."""


def _derive(password: str, salt: bytes, log2_n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** log2_n, r=r, p=p, maxmem=_MAXMEM, dklen=32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _derive(password, salt, LOG2_N, R, P)
    b64 = base64.b64encode
    return f"scrypt${LOG2_N}${R}${P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Compara en tiempo constante. Sin contraseña guardada (o con un formato raro), False."""
    if not stored or len(password) > MAX_LENGTH:
        return False
    try:
        scheme, log2_n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = _derive(password, base64.b64decode(salt), int(log2_n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str) -> bool:
    return not stored.startswith(f"scrypt${LOG2_N}${R}${P}$")


# Se usa cuando la persona no existe: el tiempo de respuesta no dice si el correo está dado de alta
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def check_policy(password: str, *, email: str = "") -> None:
    if len(password) < MIN_LENGTH:
        raise WeakPassword(f"La contraseña debe tener al menos {MIN_LENGTH} caracteres")
    if len(password) > MAX_LENGTH:
        raise WeakPassword(f"La contraseña no puede tener más de {MAX_LENGTH} caracteres")
    if len(set(password)) < 5:
        raise WeakPassword("La contraseña repite demasiado los mismos caracteres")
    local = email.split("@", 1)[0].lower()
    if local and len(local) >= 4 and local in password.lower():
        raise WeakPassword("La contraseña no puede contener tu correo")
