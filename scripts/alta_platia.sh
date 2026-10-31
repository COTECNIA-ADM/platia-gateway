#!/usr/bin/env bash
# Da de alta PLATIA en el gateway (G5): organización, equipo, aplicación de confianza,
# proveedor Anthropic con su clave cifrada, los tres modelos que usa PLATIA con sus precios
# y una clave plgw-app-platia-... para el .env de PLATIA.
#
# Requisitos: el gateway levantado y la clave de Anthropic exportada en la terminal:
#     export ANTHROPIC_API_KEY=sk-ant-...
#     bash scripts/alta_platia.sh
#
# Se puede repetir: lo que ya existe se deja como está (la clave del proveedor no se
# cambia) y cada ejecución crea una clave nueva para PLATIA. Las claves anteriores siguen
# valiendo hasta que se revoquen (cli revoke-key --prefix ...).
#
# Variables opcionales: ORG (cotecnia), PROVIDER_SLUG (anthropic) y PROVIDER_BASE_URL
# (vacía = la API de Anthropic; solo para pruebas con un proveedor simulado).
set -euo pipefail
cd "$(dirname "$0")/.."

: "${ANTHROPIC_API_KEY:?Exporta antes ANTHROPIC_API_KEY (no se guarda en .env: va cifrada a la base)}"
ORG="${ORG:-cotecnia}"
PROVIDER_SLUG="${PROVIDER_SLUG:-anthropic}"

# </dev/null: docker compose run leería si no la lista de modelos del bucle de abajo
cli() { docker compose run --rm -T -e ANTHROPIC_API_KEY migrations python -m app.cli --exists-ok "$@" </dev/null; }

cli create-org --slug "$ORG" --name "Cotecnia"
cli create-team --org "$ORG" --slug plataforma --name "Plataforma"
# De confianza: puede declarar la persona (X-Platia-User-Id). Propósito '*': PLATIA decide
# qué tarea usa qué modelo y presupuesto; el gateway registra la tarea de cada llamada
cli create-app --org "$ORG" --team plataforma --slug platia --name "PLATIA" --trusted --purpose '*'

provider_args=(--slug "$PROVIDER_SLUG" --name "Anthropic" --kind anthropic --api-key-env ANTHROPIC_API_KEY)
if [ -n "${PROVIDER_BASE_URL:-}" ]; then
  provider_args+=(--base-url "$PROVIDER_BASE_URL" --allow-http)
fi
cli create-provider "${provider_args[@]}"

# Los mismos modelos y precios con los que PLATIA calcula su coste (backend/app/seeds.py,
# precios del 2026-10-23): así los dos registros se pueden comparar
while IFS='|' read -r key name input output cache_read cache_write; do
  cli create-model --key "$key" --name "$name" --family anthropic \
    --capability tools --capability thinking --capability structured_output --capability prompt_cache
  cli create-deployment --model "$key" --provider "$PROVIDER_SLUG"
  cli set-price --model "$key" --input "$input" --output "$output" --cache-read "$cache_read" \
    --cache-write "$cache_write" --since 2026-10-23
done <<'MODELS'
claude-opus-5-5|Claude Opus 5.5|4.00|20.00|0.20|5.00
claude-sonnet-5-5|Claude Sonnet 5.5|2.00|10.00|0.20|2.50
claude-haiku-5-5|Claude Haiku 5.5|0.10|0.50|0.01|0.125
MODELS

KEY="$(docker compose run --rm -T migrations python -m app.cli create-key --org "$ORG" --app platia \
  --label "ai_service de PLATIA" | tail -n 1)"
cat <<EOM

PLATIA dado de alta en el gateway. En el .env de PLATIA pon:

    LLM_GATEWAY_ENABLED=true
    LLM_GATEWAY_API_KEY=$KEY

y levanta PLATIA con la red del gateway:

    docker compose -f docker-compose.yml -f docker-compose.gateway.yml up -d

La clave no se vuelve a mostrar. Guía completa: docs/conexion-platia.md
EOM
