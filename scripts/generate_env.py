#!/usr/bin/env python3
"""Crea el fichero .env a partir de .env.example con secretos aleatorios.

Uso: python scripts/generate_env.py [--force | --actualizar]

- Sustituye cada <GENERAR> por un secreto aleatorio distinto y cada
  <GENERAR_FERNET> por una clave Fernet válida.
- --actualizar añade a un .env existente las variables nuevas de .env.example
  (por ejemplo, al pasar de fase) sin tocar las que ya tienes.
- Las claves de los proveedores (Anthropic...) no van en .env: se guardan
  cifradas en la base con `python -m app.cli create-provider` (ver README).
- No sobrescribe un .env existente salvo con --force (perderías los secretos
  con los que se creó la base de datos).
"""
import base64
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"
TARGET = ROOT / ".env"
PLACEHOLDER = "<GENERAR>"
FERNET_PLACEHOLDER = "<GENERAR_FERNET>"


def fill(line: str) -> str:
    if FERNET_PLACEHOLDER in line:
        line = line.replace(FERNET_PLACEHOLDER, base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
    if PLACEHOLDER in line:
        # token_urlsafe: sin caracteres especiales de shell ni de URL
        line = line.replace(PLACEHOLDER, secrets.token_urlsafe(32))
    return line


def _key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


# Valores de fases anteriores que ya no son válidos: (variable, valores viejos, valor nuevo)
OBSOLETE: list[tuple[str, set[str], str]] = []


def _migrate_obsolete(text: str) -> tuple[str, list[str]]:
    lines, changed = text.splitlines(), []
    for i, line in enumerate(lines):
        key = _key(line)
        for name, old_values, new_value in OBSOLETE:
            if key == name:
                value = line.split("=", 1)[1].split("#", 1)[0].strip()
                if value in old_values:
                    lines[i] = f"{name}={new_value}"
                    changed.append(f"{name}: {value} -> {new_value}")
    return "\n".join(lines) + "\n", changed


def update() -> int:
    if not TARGET.exists():
        print(f"No existe {TARGET}. Ejecuta primero: python scripts/generate_env.py")
        return 1
    current, migrated = _migrate_obsolete(TARGET.read_text(encoding="utf-8"))
    for change in migrated:
        print(f"Actualizado {change}")
    if migrated:
        TARGET.write_text(current, encoding="utf-8", newline="\n")
    existing = {_key(line) for line in current.splitlines()}
    added = [fill(line) for line in EXAMPLE.read_text(encoding="utf-8").splitlines()
             if _key(line) and _key(line) not in existing]
    if not added:
        print("Tu .env ya tiene todas las variables.")
        return 0
    block = "\n# --- Añadido por generate_env.py --actualizar ---\n" + "\n".join(added) + "\n"
    TARGET.write_text(current.rstrip("\n") + "\n" + block, encoding="utf-8", newline="\n")
    names = ", ".join(_key(line) for line in added)
    print(f"Añadidas {len(added)} variables a {TARGET}: {names}")
    return 0


def main() -> int:
    if "--actualizar" in sys.argv:
        return update()
    if TARGET.exists() and "--force" not in sys.argv:
        print(f"{TARGET} ya existe. Usa --force para regenerarlo (cambiará los secretos)")
        print("o --actualizar para añadir solo las variables nuevas.")
        return 1

    lines = [fill(line) for line in EXAMPLE.read_text(encoding="utf-8").splitlines()]

    # newline="\n": en Windows también LF, como espera Docker Compose
    TARGET.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    try:
        TARGET.chmod(0o600)
    except OSError:
        pass
    print(f"Creado {TARGET}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
