#!/usr/bin/env bash
# Prueba en Docker del contrato de inferencia (G3) y del streaming (G4), repetible y sin coste.
#
# Requisito: la plataforma levantada (docker compose up -d --build --wait).
# Uso:       bash scripts/prueba_docker.sh
#
# Arranca un proveedor simulado en la red edge_net, da de alta con la línea de comandos una
# organización, una aplicación, una clave, el proveedor y dos modelos (uno de cada familia)
# y llama al gateway a través de Nginx por los dos contratos, con y sin streaming. Si los SDK
# de Anthropic y OpenAI están instalados (pip install anthropic openai), los prueba también
# (scripts/prueba_sdk.py). Cada ejecución usa nombres nuevos: se puede repetir sin limpiar.
set -euo pipefail
cd "$(dirname "$0")/.."

BASE="http://localhost:${HTTP_PORT:-8090}"
RUN="p$(date +%s)"
FAKE_KEY="fake-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
FAKE_NAME="platia-fake-provider-$RUN"

gateway_id="$(docker compose ps -q gateway)"
[ -n "$gateway_id" ] || { echo "El gateway no está levantado: docker compose up -d --build --wait"; exit 1; }
image="$(docker inspect -f '{{.Config.Image}}' "$gateway_id")"
network="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$gateway_id" \
  | tr ' ' '\n' | grep 'edge_net$')"

cleanup() { docker rm -f "$FAKE_NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run -d --rm --name "$FAKE_NAME" --network "$network" --network-alias "fake-$RUN" \
  -e FAKE_PROVIDER_KEY="$FAKE_KEY" -v "$PWD/scripts/fake_provider.py:/fake.py:ro" \
  --entrypoint python "$image" /fake.py >/dev/null

cli() { docker compose run --rm -T -e FAKE_KEY="$FAKE_KEY" migrations python -m app.cli "$@"; }
echo "== Alta de la configuración ($RUN)"
cli create-org --slug "$RUN" --name "Prueba $RUN"
cli create-team --org "$RUN" --slug equipo --name Equipo
cli create-app --org "$RUN" --team equipo --slug platia --name PLATIA --trusted --purpose proposal_writer
cli create-provider --slug "anthropic-$RUN" --name "Anthropic simulado" --kind anthropic \
  --base-url "http://fake-$RUN:8080" --allow-http --api-key-env FAKE_KEY
cli create-provider --slug "openai-$RUN" --name "OpenAI simulado" --kind openai_compatible \
  --base-url "http://fake-$RUN:8080/v1" --allow-http --api-key-env FAKE_KEY
cli create-model --key "claude-$RUN" --name "Claude simulado" --family anthropic --max-output-tokens 1000
cli create-model --key "gpt-$RUN" --name "GPT simulado" --family openai
cli create-deployment --model "claude-$RUN" --provider "anthropic-$RUN" --upstream claude-real
cli create-deployment --model "gpt-$RUN" --provider "openai-$RUN" --upstream gpt-real
cli set-price --model "claude-$RUN" --input 3 --output 15 --since 2020-01-01
KEY="$(cli create-key --org "$RUN" --app platia --label prueba | tail -n 1)"

check() { # descripción, código esperado, respuesta (código en la última línea)
  local code="${3##*$'\n'}"
  if [ "$code" != "$2" ]; then echo "FALLO: $1 (esperado $2, recibido $code)"; echo "$3"; exit 1; fi
  echo "ok: $1"
}

echo "== Llamadas a través de Nginx"
out="$(curl -s -w '\n%{http_code}' "$BASE/v1/messages" -H "x-api-key: $KEY" -H 'anthropic-version: 2023-06-01' \
  -H 'X-Platia-Purpose: proposal_writer' -H 'X-Platia-User-Id: 42' -H 'X-Request-ID: prueba-docker-1' \
  -D /tmp/platia-headers -d "{\"model\":\"claude-$RUN\",\"max_tokens\":50,\"messages\":[{\"role\":\"user\",\"content\":\"Hola\"}]}")"
check "Anthropic → Anthropic" 200 "$out"
grep -q "Respuesta simulada de Anthropic" <<<"$out"
grep -qi "x-platia-model-used: claude-$RUN" /tmp/platia-headers
# 1000 × 3 + 200 × 15 = 6000 por millón
grep -qi "x-platia-cost-usd: 0.00600000" /tmp/platia-headers
echo "ok: cabeceras X-Platia-* y coste"

out="$(curl -s -w '\n%{http_code}' "$BASE/v1/chat/completions" -H "Authorization: Bearer $KEY" \
  -d "{\"model\":\"claude-$RUN\",\"messages\":[{\"role\":\"system\",\"content\":\"Breve\"},{\"role\":\"user\",\"content\":\"Hola\"}]}")"
check "OpenAI → Anthropic (traducido)" 200 "$out"
grep -q '"object":"chat.completion"' <<<"$out"

out="$(curl -s -w '\n%{http_code}' "$BASE/v1/messages" -H "x-api-key: $KEY" \
  -d "{\"model\":\"gpt-$RUN\",\"max_tokens\":50,\"messages\":[{\"role\":\"user\",\"content\":\"Hola\"}]}")"
check "Anthropic → OpenAI (traducido)" 200 "$out"
grep -q "Respuesta simulada de OpenAI" <<<"$out"

out="$(curl -s -w '\n%{http_code}' "$BASE/v1/models" -H "Authorization: Bearer $KEY")"
check "Lista de modelos" 200 "$out"
grep -q "gpt-$RUN" <<<"$out"

echo "== Streaming"
out="$(curl -sN -w '\n%{http_code}' "$BASE/v1/messages" -H "x-api-key: $KEY" -D /tmp/platia-headers \
  -d "{\"model\":\"claude-$RUN\",\"max_tokens\":50,\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Hola\"}]}")"
check "Streaming Anthropic → Anthropic" 200 "$out"
grep -q "event: message_stop" <<<"$out"
test "$(grep -c '"text_delta"' <<<"$out")" = "4"
grep -qi "content-type: text/event-stream" /tmp/platia-headers
echo "ok: eventos SSE completos (4 trozos de texto y message_stop)"

out="$(curl -sN -w '\n%{http_code}' "$BASE/v1/chat/completions" -H "Authorization: Bearer $KEY" \
  -d "{\"model\":\"claude-$RUN\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Hola\"}]}")"
check "Streaming OpenAI → Anthropic (traducido)" 200 "$out"
grep -q "data: \[DONE\]" <<<"$out"

out="$(curl -sN -w '\n%{http_code}' "$BASE/v1/chat/completions" -H "Authorization: Bearer $KEY" \
  -d "{\"model\":\"gpt-$RUN\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Hola\"}]}")"
check "Streaming OpenAI → OpenAI" 200 "$out"
if grep -q '"usage"' <<<"$out"; then echo "FALLO: se reenvió el uso que el cliente no pidió"; exit 1; fi

PY="${PYTHON:-python3}"
if "$PY" -c "import anthropic, openai" 2>/dev/null; then
  echo "== SDK oficiales"
  "$PY" scripts/prueba_sdk.py "$BASE" "$KEY" "claude-$RUN" "gpt-$RUN"
  SDK_CALLS=4
else
  echo "== SDK oficiales: no instalados (pip install anthropic openai); se omiten"
  SDK_CALLS=0
fi

echo "== Errores"
out="$(curl -s -w '\n%{http_code}' "$BASE/v1/messages" -d '{}')"
check "Sin clave → 401" 401 "$out"
out="$(curl -s -w '\n%{http_code}' "$BASE/v1/messages" -H "x-api-key: $KEY" -H 'X-Platia-Purpose: otro' \
  -d "{\"model\":\"claude-$RUN\",\"max_tokens\":5,\"messages\":[{\"role\":\"user\",\"content\":\"x\"}]}")"
check "Propósito no permitido → 403" 403 "$out"
out="$(curl -s -w '\n%{http_code}' "$BASE/docs")"
check "La documentación no se publica" 404 "$out"

echo "== Auditoría"
prefix="$(cut -c1-24 <<<"$KEY")"
summary="$(docker compose exec -T gateway python - "$prefix" <<'PY'
import asyncio, sys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from app.settings import Settings

async def main():
    engine = create_async_engine(Settings().database_url)
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT SUM(status = 'success'), SUM(status = 'success' AND stream), SUM(status = 'blocked'),"
            " SUM(status NOT IN ('success', 'blocked')), SUM(end_user_ref = '42'), SUM(output_tokens)"
            " FROM audit_logs WHERE api_key_prefix = :p"), {"p": sys.argv[1]})).one()
    await engine.dispose()
    print(" ".join(str(int(v or 0)) for v in row))

asyncio.run(main())
PY
)"
# correctas, de ellas en streaming, bloqueadas, otras, con persona 42, tokens de salida
read -r ok streamed blocked other person tokens <<<"$summary"
expected_ok=$((6 + SDK_CALLS))
echo "correctas=$ok streaming=$streamed bloqueadas=$blocked otras=$other persona42=$person"
if [ "$ok" != "$expected_ok" ] || [ "$blocked" != 1 ] || [ "$other" != 0 ] || [ "$person" != 1 ] \
   || [ "$streamed" -lt 3 ] || [ "$tokens" -le 0 ]; then
  echo "FALLO: auditoría (se esperaban $expected_ok correctas, 1 bloqueada y ninguna pendiente)"; exit 1
fi
echo "ok: cada llamada auditada, también las de streaming; sin texto de la conversación"
echo "== Prueba superada"
