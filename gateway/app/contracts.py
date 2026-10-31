"""Los dos contratos de entrada (Anthropic Messages y OpenAI Chat Completions) y el paso
de uno a otro (cap. 1 y 7, ADR-002).

- **Misma familia** (cliente y proveedor hablan el mismo contrato): el cuerpo original se
  reenvía con el modelo cambiado por el del proveedor. No se pierde ninguna función.
- **Familias distintas**: se traduce solo lo que los dos contratos expresan igual: texto
  de sistema, de persona y de asistente, `max_tokens`, `temperature`, `top_p` y las
  secuencias de parada. Si la petición usa algo más (herramientas, imágenes,
  razonamiento, salidas estructuradas...), `Unsupported`: ese destino no es elegible. Las
  herramientas entre familias se valorarán con la reserva entre proveedores (G6).
"""
import time
from dataclasses import dataclass, field

from .errors import InvalidRequest
from .models import Contract, ProviderKind

ANTHROPIC_VERSION = "2023-06-01"
# max_tokens es obligatorio en Anthropic; si el cliente de OpenAI no lo manda y el modelo no
# declara su máximo, se usa este valor
DEFAULT_MAX_TOKENS = 4096


class Unsupported(Exception):
    """La petición usa algo que no se puede traducir al otro contrato."""


def native_contract(kind: ProviderKind) -> Contract:
    return Contract.ANTHROPIC if kind == ProviderKind.ANTHROPIC else Contract.OPENAI


@dataclass
class Usage:
    """Tokens de una respuesta. `input` no incluye los leídos o escritos en caché."""
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass
class Neutral:
    """Petición de solo texto, común a los dos contratos."""
    messages: list[dict]  # [{"role": "user" | "assistant", "content": str}]
    system: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: list[str] = field(default_factory=list)


# --- Validación mínima común (el proveedor valida el resto)

def check_basics(body: dict) -> str:
    """Comprueba la forma mínima y devuelve el modelo pedido."""
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise InvalidRequest("Falta el campo 'model'")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise InvalidRequest("El campo 'messages' debe ser una lista no vacía")
    if not isinstance(body.get("stream", False), bool):
        raise InvalidRequest("El campo 'stream' debe ser true o false")
    return model.strip()


# --- De cada contrato a la forma común

def _texts(content, *, allowed_extra: frozenset[str] = frozenset()) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise Unsupported("contenido no textual")
    parts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
            raise Unsupported(f"bloque de tipo {block.get('type') if isinstance(block, dict) else '?'}")
        if set(block) - {"type", "text"} - allowed_extra:
            raise Unsupported("bloque de texto con campos adicionales")
        parts.append(block["text"])
    return "\n\n".join(parts)


_ANTHROPIC_PORTABLE = {"model", "messages", "system", "max_tokens", "temperature", "top_p", "stop_sequences",
                       "stream", "metadata"}
# cache_control es una pista de caché: perderla no cambia la respuesta
_CACHE_HINT = frozenset({"cache_control"})


def from_anthropic(body: dict) -> Neutral:
    extra = set(body) - _ANTHROPIC_PORTABLE
    if extra:
        raise Unsupported(", ".join(sorted(extra)))
    system = body.get("system")
    messages = []
    for message in body["messages"]:
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            raise Unsupported("rol de mensaje")
        messages.append({"role": message["role"], "content": _texts(message.get("content"),
                                                                    allowed_extra=_CACHE_HINT)})
    stop = body.get("stop_sequences") or []
    return Neutral(messages=messages,
                   system=_texts(system, allowed_extra=_CACHE_HINT) if system is not None else None,
                   max_tokens=body.get("max_tokens"), temperature=body.get("temperature"),
                   top_p=body.get("top_p"), stop=list(stop))


_OPENAI_PORTABLE = {"model", "messages", "max_tokens", "max_completion_tokens", "temperature", "top_p", "stop",
                    "stream", "user", "n"}


def from_openai(body: dict) -> Neutral:
    extra = set(body) - _OPENAI_PORTABLE
    if extra:
        raise Unsupported(", ".join(sorted(extra)))
    if body.get("n", 1) != 1:
        raise Unsupported("n distinto de 1")
    system_parts, messages = [], []
    for message in body["messages"]:
        if not isinstance(message, dict):
            raise Unsupported("mensaje")
        role = message.get("role")
        if set(message) - {"role", "content", "name"}:
            raise Unsupported("mensaje con campos adicionales")
        text = _texts(message.get("content"))
        if role in ("system", "developer"):
            system_parts.append(text)
        elif role in ("user", "assistant"):
            messages.append({"role": role, "content": text})
        else:
            raise Unsupported(f"rol {role}")
    stop = body.get("stop")
    temperature = body.get("temperature")
    return Neutral(messages=messages, system="\n\n".join(system_parts) or None,
                   max_tokens=body.get("max_completion_tokens") or body.get("max_tokens"),
                   temperature=temperature, top_p=body.get("top_p"),
                   stop=[stop] if isinstance(stop, str) else list(stop or []))


# --- De la forma común al contrato del proveedor

def to_anthropic(neutral: Neutral, model: str, default_max_tokens: int | None) -> dict:
    if neutral.temperature is not None and neutral.temperature > 1:
        # Anthropic acepta 0-1; recortar cambiaría la petición sin avisar
        raise Unsupported("temperature mayor que 1")
    body: dict = {"model": model, "messages": neutral.messages,
                  "max_tokens": neutral.max_tokens or default_max_tokens or DEFAULT_MAX_TOKENS}
    if neutral.system:
        body["system"] = neutral.system
    if neutral.temperature is not None:
        body["temperature"] = neutral.temperature
    if neutral.top_p is not None:
        body["top_p"] = neutral.top_p
    if neutral.stop:
        body["stop_sequences"] = neutral.stop
    return body


def to_openai(neutral: Neutral, model: str) -> dict:
    messages = ([{"role": "system", "content": neutral.system}] if neutral.system else []) + neutral.messages
    body: dict = {"model": model, "messages": messages}
    if neutral.max_tokens is not None:
        body["max_tokens"] = neutral.max_tokens
    if neutral.temperature is not None:
        body["temperature"] = neutral.temperature
    if neutral.top_p is not None:
        body["top_p"] = neutral.top_p
    if neutral.stop:
        body["stop"] = neutral.stop
    return body


# --- Uso (tokens) de cada respuesta

def _int(value) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def usage_from_anthropic(response: dict) -> Usage:
    usage = response.get("usage") or {}
    return Usage(input=_int(usage.get("input_tokens")), output=_int(usage.get("output_tokens")),
                 cache_read=_int(usage.get("cache_read_input_tokens")),
                 cache_write=_int(usage.get("cache_creation_input_tokens")))


def usage_from_openai(response: dict) -> Usage:
    usage = response.get("usage") or {}
    cached = _int((usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
    # En OpenAI los tokens de caché van dentro de prompt_tokens
    return Usage(input=max(_int(usage.get("prompt_tokens")) - cached, 0),
                 output=_int(usage.get("completion_tokens")), cache_read=cached)


def usage_of(response: dict, contract: Contract) -> Usage:
    return usage_from_anthropic(response) if contract == Contract.ANTHROPIC else usage_from_openai(response)


# --- Respuestas traducidas (solo texto)

_STOP_TO_OPENAI = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "refusal": "content_filter"}
_STOP_TO_ANTHROPIC = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}


def anthropic_to_openai_response(response: dict, *, model: str, request_id: str) -> dict:
    content = response.get("content") or []
    text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    usage = usage_from_anthropic(response)
    prompt = usage.input + usage.cache_read + usage.cache_write
    return {
        "id": f"chatcmpl-{request_id}", "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": _STOP_TO_OPENAI.get(response.get("stop_reason"), "stop")}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": usage.output,
                  "total_tokens": prompt + usage.output,
                  "prompt_tokens_details": {"cached_tokens": usage.cache_read}},
    }


def openai_to_anthropic_response(response: dict, *, model: str, request_id: str) -> dict:
    choices = response.get("choices") or [{}]
    message = choices[0].get("message") or {}
    text = message.get("content") or ""
    usage = usage_from_openai(response)
    return {
        "id": f"msg_{request_id}", "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": _STOP_TO_ANTHROPIC.get(choices[0].get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": {"input_tokens": usage.input, "output_tokens": usage.output,
                  "cache_read_input_tokens": usage.cache_read, "cache_creation_input_tokens": 0},
    }
