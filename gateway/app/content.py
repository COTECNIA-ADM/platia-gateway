"""Detectores de contenido: manipulación (jailbreak), marcas de clasificación, secretos y datos personales (cap. 19 y 20, ADR-012).

Todo es local y determinista (expresiones regulares con validación), sin llamadas a otro
modelo: cuesta milisegundos y da el mismo resultado cada vez.

- **Secretos**: claves de API (incluidas las `plgw-` del propio gateway), claves privadas,
  tokens JWT, credenciales escritas como `password=...`.
- **Datos personales**: correo, DNI y NIE (con su letra de control), IBAN (con su dígito de
  control), tarjeta (con Luhn) y teléfono español.
- **Marcas de clasificación**: CONFIDENCIAL, SECRETO, DIFUSIÓN LIMITADA... en mayúsculas,
  como aparecen en la cabecera de un documento (en minúsculas suelen ser texto normal).
- **Manipulación**: frases típicas para saltarse las instrucciones, en español y en inglés,
  con un peso cada una; se suman por categoría y la petición se marca a partir de un umbral.

Nunca se guarda ni se registra el texto encontrado: solo la categoría, el tipo y cuántos.
Los patrones de secretos y datos personales están adaptados de
`machinebooks.ai/ES/aigateway/cap-19/block-14.py` y `cap-20/block-14.py` (MIT); el
cálculo de la puntuación de manipulación, de `cap-19/block-01.py` a `block-04.py`.
"""
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass

# --- Validaciones que evitan falsos positivos

_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"


def _dni_ok(value: str) -> bool:
    value = re.sub(r"[\s-]", "", value).upper()
    number, letter = value[:-1], value[-1]
    if number[0] in "XYZ":
        number = str("XYZ".index(number[0])) + number[1:]
    return number.isdigit() and _DNI_LETTERS[int(number) % 23] == letter


def _iban_ok(value: str) -> bool:
    value = value.replace(" ", "").upper()
    if not 15 <= len(value) <= 34:
        return False
    digits = "".join(str(int(c, 36)) for c in value[4:] + value[:4])
    return int(digits) % 97 == 1


def _luhn_ok(value: str) -> bool:
    digits = [int(c) for c in value if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


@dataclass(frozen=True)
class Detector:
    check: str  # secret, pii o classified
    kind: str  # tipo concreto, para el recuento (email, dni, aws_key...)
    pattern: re.Pattern
    valid: Callable[[str], bool] | None = None
    group: int = 0  # parte del texto que se tacha (p. ej. solo el valor de una credencial)


def _re(pattern: str, flags: int = 0) -> re.Pattern:
    return re.compile(pattern, flags)


# Orden: de lo más específico a lo más general. Un trozo ya marcado no se vuelve a marcar.
DETECTORS: tuple[Detector, ...] = (
    Detector("secret", "private_key", _re(
        r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----(?:[\s\S]{0,8000}?-----END (?:[A-Z]+ )*PRIVATE KEY-----)?")),
    Detector("secret", "anthropic_key", _re(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    Detector("secret", "openai_key", _re(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    Detector("secret", "gateway_key", _re(r"\bplgw-(?:app|usr)-[A-Za-z0-9_-]{16,}")),
    Detector("secret", "aws_key", _re(r"\bAKIA[0-9A-Z]{16}\b")),
    Detector("secret", "github_token", _re(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    Detector("secret", "google_key", _re(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    Detector("secret", "slack_token", _re(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    Detector("secret", "jwt", _re(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    Detector("secret", "credential", _re(
        r"\b(?:password|passwd|pwd|contraseña|secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
        r"[\"']?\s*[:=]\s*[\"']?([^\s\"',;}]{8,})", re.IGNORECASE), group=1),
    Detector("pii", "email", _re(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")),
    Detector("pii", "iban", _re(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b"), _iban_ok),
    Detector("pii", "dni", _re(r"\b\d{8}[ -]?[A-Za-z]\b"), _dni_ok),
    Detector("pii", "nie", _re(r"\b[XYZxyz][ -]?\d{7}[ -]?[A-Za-z]\b"), _dni_ok),
    Detector("pii", "card", _re(r"\b[3-6]\d{3}(?:[ -]?\d{4}){2}[ -]?\d{1,7}\b"), _luhn_ok),
    Detector("pii", "phone", _re(r"(?<![\w+])(?:(?:\+|00)34[ -]?)?[6789](?:[ -]?\d){8}(?![\w])")),
    Detector("classified", "mark", _re(
        r"\b(?:(?:ESTRICTAMENTE )?CONFIDENCIAL|SECRETO|RESERVADO|DIFUSIÓN LIMITADA|USO INTERNO"
        r"|(?:STRICTLY )?CONFIDENTIAL|TOP SECRET|INTERNAL USE ONLY)\b")),
)


@dataclass(frozen=True)
class Finding:
    check: str
    kind: str
    start: int
    end: int


def find(text: str, checks: set[str]) -> list[Finding]:
    """Lo encontrado de las categorías pedidas, sin solapes y en orden de aparición."""
    taken: list[Finding] = []
    for detector in DETECTORS:
        if detector.check not in checks:
            continue
        for match in detector.pattern.finditer(text):
            if detector.valid is not None and not detector.valid(match.group(0)):
                continue
            start, end = match.span(detector.group)
            if any(start < f.end and f.start < end for f in taken):
                continue
            taken.append(Finding(detector.check, detector.kind, start, end))
    return sorted(taken, key=lambda f: f.start)


def placeholder(finding: Finding) -> str:
    return f"[OCULTO:{finding.kind}]"


def redact(text: str, findings: list[Finding]) -> str:
    out, last = [], 0
    for finding in findings:
        out += [text[last:finding.start], placeholder(finding)]
        last = finding.end
    return "".join(out) + text[last:]


# --- Manipulación del modelo (jailbreak)

@dataclass(frozen=True)
class _Rule:
    category: str
    weight: int
    pattern: re.Pattern


_I = re.IGNORECASE
JAILBREAK_RULES: tuple[_Rule, ...] = (
    _Rule("prompt_override", 70, _re(
        r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:of\s+)?(?:the\s+|your\s+)?"
        r"(?:previous|prior|above|earlier|original)\s+(?:instructions?|rules|prompts?|directions)", _I)),
    _Rule("prompt_override", 70, _re(
        r"\b(?:ignora|olvida|descarta|omite|olvídate\s+de)\s+(?:todas?\s+)?(?:las\s+|tus\s+)?"
        r"(?:instrucciones|reglas|indicaciones|normas)(?:\s+(?:anteriores|previas|de\s+arriba|originales|iniciales))?\b", _I)),
    _Rule("role_bypass", 90, _re(
        r"\byou\s+are\s+now\s+(?:a\s+|an\s+)?(?:DAN|STAN|DUDE|unrestricted|jailbroken|evil|unfiltered)\b", _I)),
    _Rule("role_bypass", 90, _re(
        r"\b(?:enter|switch\s+to|activate|enable)\s+(?:the\s+)?(?:DAN|developer|god|jailbreak)\s+mode\b", _I)),
    _Rule("role_bypass", 90, _re(
        r"\b(?:activa|entra\s+en|cambia\s+a|pasa\s+a)\s+(?:el\s+)?modo\s+(?:DAN|desarrollador|dios|jailbreak"
        r"|sin\s+restricciones|sin\s+censura|sin\s+filtros)\b", _I)),
    _Rule("role_bypass", 90, _re(r"\bahora\s+eres\s+(?:DAN|una?\s+IA\s+sin\s+(?:restricciones|filtros|censura))", _I)),
    _Rule("system_leak", 85, _re(
        r"\b(?:show|tell|reveal|print|output|repeat|leak)\s+(?:me\s+)?(?:your\s+(?:system\s+|initial\s+|original\s+)?"
        r"(?:prompt|instructions)|the\s+system\s+prompt)\b", _I)),
    _Rule("system_leak", 85, _re(
        r"\b(?:muestra|muéstrame|dime|revela|revélame|repite|imprime|escribe)\s+(?:tu|tus|el|las)\s+"
        r"(?:prompt|instrucciones)\s+(?:de\s+sistema|del\s+sistema|iniciales|originales|ocultas)\b", _I)),
    _Rule("system_leak", 85, _re(r"\b(?:muestra|revela|repite)(?:me)?\s+tu\s+prompt\b", _I)),
    _Rule("persistent_rule", 40, _re(r"\bfrom\s+now\s+on,?\s+you\s+(?:will|must)\s+(?:always|never)\b", _I)),
    _Rule("persistent_rule", 40, _re(r"\ba\s+partir\s+de\s+ahora,?\s+(?:siempre|nunca)\s+(?:responderás|dirás|harás)\b", _I)),
    _Rule("pretend", 30, _re(r"\b(?:pretend\s+(?:you\s+are|to\s+be)|finge\s+(?:que\s+eres|ser))\b", _I)),
    _Rule("no_limits", 30, _re(
        r"\b(?:without\s+(?:any\s+)?(?:restrictions|filters|censorship)|sin\s+(?:ninguna\s+)?(?:censura|filtros))\b", _I)),
)
JAILBREAK_THRESHOLD = 60
# Mensajes de asistente seguidos que el cliente escribe por el modelo: técnica de "prefill"
_PREFILL_RUN = 3
_PREFILL_WEIGHT = 40
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿"))


def normalize(text: str) -> str:
    """NFKC (letras de ancho completo y similares a su forma normal) y sin caracteres invisibles."""
    return unicodedata.normalize("NFKC", text).translate(_INVISIBLE)


def jailbreak_score(texts: list[str], assistant_run: int = 0) -> tuple[int, list[str]]:
    """Puntuación de 0 a 100 y categorías encontradas. Dentro de una categoría cuenta el mayor peso."""
    best: dict[str, int] = {}
    weak = 0
    for text in texts:
        scan = normalize(text)
        for rule in JAILBREAK_RULES:
            if rule.pattern.search(scan):
                best[rule.category] = max(best.get(rule.category, 0), rule.weight)
                weak += rule.weight < 60
    if assistant_run > _PREFILL_RUN:
        best["prefill"] = _PREFILL_WEIGHT
    score = sum(best.values())
    if weak >= 3:  # muchas señales débiles juntas
        score = int(score * 1.5)
    return min(100, score), sorted(best)


# --- Recorrido del texto de una petición o de una respuesta

@dataclass
class TextSlot:
    """Un campo de texto dentro del cuerpo: se lee con `value` y se cambia con `set`."""
    role: str  # system, user, assistant o tool
    container: dict | list
    key: str | int

    @property
    def value(self) -> str:
        return self.container[self.key]

    def set(self, text: str) -> None:
        self.container[self.key] = text


def _content_slots(content, role: str) -> Iterator[TextSlot]:
    if isinstance(content, str):
        return
    if not isinstance(content, list):
        return
    for index, part in enumerate(content):
        if isinstance(part, str):
            yield TextSlot(role, content, index)
        elif isinstance(part, dict):
            # Los bloques de razonamiento llevan firma: no se tocan (ni se leen)
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                yield TextSlot(role, part, "text")
            elif part.get("type") == "tool_result":
                yield from _field_slots(part, "content", "tool")


def _field_slots(container: dict, key: str, role: str) -> Iterator[TextSlot]:
    value = container.get(key)
    if isinstance(value, str):
        yield TextSlot(role, container, key)
    else:
        yield from _content_slots(value, role)


def request_slots(body: dict) -> Iterator[TextSlot]:
    """Los textos de una petición en cualquiera de los dos contratos (sistema, mensajes y
    resultados de herramientas); imágenes, documentos y razonamiento se quedan fuera."""
    if "system" in body:
        yield from _field_slots(body, "system", "system")
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role") if isinstance(message.get("role"), str) else "user"
        role = "system" if role == "developer" else role
        yield from _field_slots(message, "content", role)


def response_slots(body: dict) -> Iterator[TextSlot]:
    """Los textos de una respuesta sin streaming (Anthropic: `content`; OpenAI: `choices`)."""
    for index, block in enumerate(body.get("content") or []):
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            yield TextSlot("assistant", body["content"][index], "text")
    for choice in body.get("choices") or []:
        message = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            yield TextSlot("assistant", message, "content")


def assistant_run(body: dict) -> int:
    """La racha más larga de mensajes de asistente seguidos que manda el cliente."""
    longest = run = 0
    for message in body.get("messages") or []:
        run = run + 1 if isinstance(message, dict) and message.get("role") == "assistant" else 0
        longest = max(longest, run)
    return longest


def tally(findings: list[Finding]) -> dict[str, Counter]:
    """Recuento por categoría y tipo: {"pii": Counter({"email": 2})}."""
    result: dict[str, Counter] = {}
    for finding in findings:
        result.setdefault(finding.check, Counter())[finding.kind] += 1
    return result


# --- Filtro de la salida en streaming

# Texto que se retiene al final: lo bastante para que no se corte por la mitad un dato con
# espacios (un IBAN agrupado, un teléfono, la cabecera de una clave privada)
HOLD_CHARS = 64


class TextGuard:
    """Filtra un texto que llega por trozos: tacha lo que encuentra y retiene la cola.

    `feed` devuelve lo que ya se puede enviar; `flush`, el resto al terminar el bloque. Solo
    se corta en un espacio y nunca dentro de algo encontrado: así un dato no llega partido
    al cliente medio sin tachar. Un dato sin espacios se retiene entero hasta que termina.
    """

    def __init__(self, checks: set[str], redact_checks: set[str]):
        self.checks, self.redact_checks = checks, redact_checks
        self.pending = ""
        self.found: list[Finding] = []

    def _emit(self, text: str) -> str:
        findings = find(text, self.checks)
        self.found += findings
        return redact(text, [f for f in findings if f.check in self.redact_checks])

    def feed(self, text: str) -> str:
        self.pending += text
        limit = len(self.pending) - HOLD_CHARS
        if limit <= 0:
            return ""
        cut = max(self.pending.rfind(" ", 0, limit), self.pending.rfind("\n", 0, limit))
        if cut <= 0:
            return ""
        for finding in find(self.pending, self.checks):
            if finding.start < cut < finding.end:
                cut = finding.start
        if cut <= 0:
            return ""
        ready, self.pending = self.pending[:cut], self.pending[cut:]
        return self._emit(ready)

    def flush(self) -> str:
        ready, self.pending = self.pending, ""
        return self._emit(ready) if ready else ""
