"""Claves API opacas del gateway (cap. 18, ADR-004).

Formato: ``plgw-app-<slug>-<secreto>``, con 192 bits de secreto aleatorio. Se guarda solo
el SHA-256 de la clave completa y un prefijo visible (slug y 8 caracteres del secreto) que
permite reconocerla en listados y logs. La clave en claro se muestra una única vez, al
crearla.

Basado en machinebooks.ai/ES/aigateway/cap-18/block-01.py (MIT, ver THIRD_PARTY_NOTICES.md).
"""
import hashlib
import re
import secrets
from dataclasses import dataclass

from .models import KeyKind

APP_PREFIX = "plgw-app-"
USER_PREFIX = "plgw-usr-"
# Una clave más larga no es nuestra: se rechaza sin calcular el hash
MAX_KEY_LENGTH = 128
# En la clave va el slug recortado: el prefijo visible cabe en la columna (48)
_SLUG_IN_KEY = 24
_VISIBLE_SECRET = 8
_SAFE_SLUG = re.compile(r"[^a-z0-9-]")


@dataclass(frozen=True)
class NewKey:
    raw: str  # se enseña una vez y no se guarda
    prefix: str
    key_hash: str


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def kind_of(raw: str) -> KeyKind | None:
    """Tipo de clave por su prefijo, o None si no tiene el formato del gateway."""
    if not raw or len(raw) > MAX_KEY_LENGTH:
        return None
    if raw.startswith(APP_PREFIX):
        return KeyKind.APPLICATION
    if raw.startswith(USER_PREFIX):
        return KeyKind.USER
    return None


def new_application_key(slug: str) -> NewKey:
    clean = _SAFE_SLUG.sub("-", slug.lower())[:_SLUG_IN_KEY].strip("-") or "app"
    head = f"{APP_PREFIX}{clean}-"
    raw = head + secrets.token_hex(24)
    return NewKey(raw=raw, prefix=raw[:len(head) + _VISIBLE_SECRET], key_hash=hash_key(raw))


def new_user_key() -> NewKey:
    """Clave personal (G10): ``plgw-usr-<secreto>``. No lleva el nombre de la persona: el
    prefijo visible identifica la clave sin decir de quién es a quien vea un log."""
    raw = USER_PREFIX + secrets.token_hex(24)
    return NewKey(raw=raw, prefix=raw[:len(USER_PREFIX) + _VISIBLE_SECRET], key_hash=hash_key(raw))
