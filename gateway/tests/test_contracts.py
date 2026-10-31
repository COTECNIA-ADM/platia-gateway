import pytest

from app import contracts as c
from app.errors import InvalidRequest
from app.models import Contract, ProviderKind


def test_contrato_nativo_de_cada_proveedor():
    assert c.native_contract(ProviderKind.ANTHROPIC) == Contract.ANTHROPIC
    assert c.native_contract(ProviderKind.OPENAI_COMPATIBLE) == Contract.OPENAI


@pytest.mark.parametrize("body, message", [
    ({"messages": [{"role": "user", "content": "x"}]}, "model"),
    ({"model": "  ", "messages": [{"role": "user", "content": "x"}]}, "model"),
    ({"model": "m", "messages": []}, "messages"),
    ({"model": "m", "messages": "hola"}, "messages"),
    ({"model": "m", "messages": [{"role": "user", "content": "x"}], "stream": "yes"}, "stream"),
])
def test_forma_minima(body, message):
    with pytest.raises(InvalidRequest, match=message):
        c.check_basics(body)


def test_forma_minima_devuelve_el_modelo():
    assert c.check_basics({"model": " claude ", "messages": [{"role": "user", "content": "x"}]}) == "claude"


def test_anthropic_a_openai_solo_texto():
    neutral = c.from_anthropic({
        "model": "m", "max_tokens": 100, "temperature": 0.2, "top_p": 0.9, "stop_sequences": ["FIN"],
        "metadata": {"user_id": "u"},
        "system": [{"type": "text", "text": "Eres útil", "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hola"}, {"type": "text", "text": "¿qué tal?"}]},
                     {"role": "assistant", "content": "Bien"}, {"role": "user", "content": "Sigue"}],
    })
    body = c.to_openai(neutral, "gpt-x")
    assert body == {"model": "gpt-x", "max_tokens": 100, "temperature": 0.2, "top_p": 0.9, "stop": ["FIN"],
                    "messages": [{"role": "system", "content": "Eres útil"},
                                 {"role": "user", "content": "Hola\n\n¿qué tal?"},
                                 {"role": "assistant", "content": "Bien"}, {"role": "user", "content": "Sigue"}]}


def test_openai_a_anthropic_solo_texto():
    neutral = c.from_openai({
        "model": "m", "n": 1, "user": "x", "stop": "FIN", "temperature": 0.5,
        "messages": [{"role": "system", "content": "A"}, {"role": "developer", "content": "B"},
                     {"role": "user", "content": [{"type": "text", "text": "Hola"}], "name": "ana"}],
    })
    body = c.to_anthropic(neutral, "claude-x", default_max_tokens=None)
    assert body == {"model": "claude-x", "max_tokens": c.DEFAULT_MAX_TOKENS, "system": "A\n\nB", "temperature": 0.5,
                    "stop_sequences": ["FIN"], "messages": [{"role": "user", "content": "Hola"}]}
    assert c.to_anthropic(c.from_openai({"model": "m", "max_completion_tokens": 7, "messages": [
        {"role": "user", "content": "x"}]}), "c", 2000)["max_tokens"] == 7
    assert c.to_anthropic(c.from_openai({"model": "m", "messages": [{"role": "user", "content": "x"}]}),
                          "c", 2000)["max_tokens"] == 2000


@pytest.mark.parametrize("body", [
    {"model": "m", "messages": [{"role": "user", "content": "x"}], "tools": []},
    {"model": "m", "messages": [{"role": "user", "content": "x"}], "thinking": {"type": "enabled"}},
    {"model": "m", "messages": [{"role": "user", "content": [{"type": "image", "source": {}}]}]},
    {"model": "m", "messages": [{"role": "user", "content": [{"type": "text", "text": "x", "citations": []}]}]},
    {"model": "m", "messages": [{"role": "tool", "content": "x"}]},
    {"model": "m", "messages": [{"role": "user", "content": 3}]},
    {"model": "m", "messages": ["texto"]},
])
def test_anthropic_no_traducible(body):
    with pytest.raises(c.Unsupported):
        c.from_anthropic(body)


@pytest.mark.parametrize("body", [
    {"model": "m", "messages": [{"role": "user", "content": "x"}], "tools": []},
    {"model": "m", "messages": [{"role": "user", "content": "x"}], "n": 2},
    {"model": "m", "messages": [{"role": "tool", "content": "x", "tool_call_id": "1"}]},
    {"model": "m", "messages": [{"role": "function", "content": "x"}]},
    {"model": "m", "messages": [{"role": "assistant", "content": None, "tool_calls": []}]},
    {"model": "m", "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}]},
    {"model": "m", "messages": [7]},
])
def test_openai_no_traducible(body):
    with pytest.raises(c.Unsupported):
        c.from_openai(body)


def test_temperatura_fuera_de_rango_para_anthropic():
    neutral = c.from_openai({"model": "m", "temperature": 1.5, "messages": [{"role": "user", "content": "x"}]})
    with pytest.raises(c.Unsupported, match="temperature"):
        c.to_anthropic(neutral, "c", None)


def test_uso_de_cada_contrato():
    assert c.usage_of({"usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100,
                                 "cache_creation_input_tokens": 20}}, Contract.ANTHROPIC) == c.Usage(10, 5, 100, 20)
    assert c.usage_of({"usage": {"prompt_tokens": 110, "completion_tokens": 5,
                                 "prompt_tokens_details": {"cached_tokens": 100}}}, Contract.OPENAI) == c.Usage(10, 5, 100)
    # Valores raros o ausentes cuentan como 0
    assert c.usage_of({"usage": {"input_tokens": "x", "output_tokens": -3}}, Contract.ANTHROPIC) == c.Usage()
    assert c.usage_of({}, Contract.OPENAI) == c.Usage()


def test_respuesta_anthropic_en_formato_openai():
    out = c.anthropic_to_openai_response({
        "content": [{"type": "text", "text": "Hola"}, {"type": "text", "text": " mundo"}], "stop_reason": "max_tokens",
        "usage": {"input_tokens": 3, "output_tokens": 2, "cache_read_input_tokens": 4}}, model="claude", request_id="r1")
    assert out["id"] == "chatcmpl-r1" and out["model"] == "claude" and out["object"] == "chat.completion"
    assert out["choices"][0] == {"index": 0, "message": {"role": "assistant", "content": "Hola mundo"},
                                 "finish_reason": "length"}
    assert out["usage"]["prompt_tokens"] == 7 and out["usage"]["total_tokens"] == 9
    assert out["usage"]["prompt_tokens_details"] == {"cached_tokens": 4}


def test_respuesta_openai_en_formato_anthropic():
    out = c.openai_to_anthropic_response({
        "choices": [{"message": {"role": "assistant", "content": "Hola"}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2}}, model="gpt", request_id="r1")
    assert out["id"] == "msg_r1" and out["type"] == "message" and out["model"] == "gpt"
    assert out["content"] == [{"type": "text", "text": "Hola"}] and out["stop_reason"] == "max_tokens"
    assert out["usage"]["input_tokens"] == 3 and out["usage"]["output_tokens"] == 2
    empty = c.openai_to_anthropic_response({}, model="gpt", request_id="r2")
    assert empty["content"] == [] and empty["stop_reason"] == "end_turn"
