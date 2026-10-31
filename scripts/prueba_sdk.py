#!/usr/bin/env python3
"""Prueba el gateway con los SDK oficiales de Anthropic y OpenAI, como lo hará PLATIA.

Uso (lo llama scripts/prueba_docker.sh si los SDK están instalados):

    pip install anthropic openai
    python scripts/prueba_sdk.py <url_base> <clave_plgw> <modelo_claude> <modelo_gpt>

Comprueba las llamadas normales, `messages.stream` (lo que usa `ai_service`), el
streaming del contrato de OpenAI y la lista de modelos.
"""
import sys

import anthropic
import openai


def check(description: str, condition: bool) -> None:
    if not condition:
        print(f"FALLO: {description}")
        sys.exit(1)
    print(f"ok: {description}")


def main(base: str, key: str, claude: str, gpt: str) -> None:
    claude_client = anthropic.Anthropic(base_url=base, api_key=key, max_retries=0,
                                        default_headers={"X-Platia-Purpose": "proposal_writer"})
    message = claude_client.messages.create(model=claude, max_tokens=50,
                                            messages=[{"role": "user", "content": "Hola"}])
    check("SDK de Anthropic: messages.create", message.content[0].text.startswith("Respuesta simulada"))

    with claude_client.messages.stream(model=claude, max_tokens=50,
                                       messages=[{"role": "user", "content": "Hola"}]) as stream:
        pieces = list(stream.text_stream)
        final = stream.get_final_message()
    check("SDK de Anthropic: messages.stream llega por partes", len(pieces) >= 4)
    check("SDK de Anthropic: texto y uso del mensaje final",
          "".join(pieces) == "Respuesta simulada en streaming" and final.usage.output_tokens == 200)

    check("SDK de Anthropic: models.list", claude in [m.id for m in claude_client.models.list()])

    gpt_client = openai.OpenAI(base_url=f"{base}/v1", api_key=key, max_retries=0)
    stream = gpt_client.chat.completions.create(model=gpt, stream=True,
                                                messages=[{"role": "user", "content": "Hola"}])
    text = "".join(chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices)
    check("SDK de OpenAI: streaming", text == "Respuesta simulada en streaming")

    stream = gpt_client.chat.completions.create(model=claude, stream=True,
                                                messages=[{"role": "user", "content": "Hola"}])
    text = "".join(chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices)
    check("SDK de OpenAI contra un modelo Claude (traducido)", text == "Respuesta simulada en streaming")

    try:
        claude_client.with_options(api_key="plgw-app-falsa-0000").messages.create(
            model=claude, max_tokens=5, messages=[{"role": "user", "content": "x"}])
        check("SDK de Anthropic: clave falsa rechazada", False)
    except anthropic.AuthenticationError:
        check("SDK de Anthropic: clave falsa rechazada", True)


if __name__ == "__main__":
    main(*sys.argv[1:5])
