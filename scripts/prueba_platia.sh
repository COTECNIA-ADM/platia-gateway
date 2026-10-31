#!/usr/bin/env bash
# Prueba en Docker de la conexión con PLATIA (G5), repetible y sin coste.
#
# Requisitos: el gateway levantado (docker compose up -d --build --wait) y una copia del
# repositorio de PLATIA con la conexión al gateway (ADR-019 de PLATIA).
# Uso:
#     PLATIA_DIR=../platia-dev bash scripts/prueba_platia.sh
#     AI_IMAGE=platia-dev-ai_service bash scripts/prueba_platia.sh   # imagen ya construida
#
# Arranca, en la red compartida platia_llm_net, un proveedor simulado y un ai_service de
# PLATIA con LLM_GATEWAY_ENABLED=true (sin Qdrant y con embeddings de prueba: solo se
# prueba la generación). Da de alta una aplicación «platia» con nombres nuevos, llama a
# /v1/complete y /v1/stream de ai_service como lo hace el backend y comprueba en la
# auditoría del gateway la tarea, la persona, el correlation_id y los tokens. Después
# comprueba la vuelta atrás: con LLM_GATEWAY_ENABLED=false, ai_service llama directamente
# al proveedor y el gateway no ve la llamada. No toca la configuración real: cada
# ejecución usa su propia organización, proveedor y modelo.
set -euo pipefail
cd "$(dirname "$0")/.."

RUN="pp$(date +%s)"
NETWORK=platia_llm_net
FAKE_KEY="fake-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
AI_KEY="ai-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
FAKE_NAME="platia-fake-provider-$RUN"
AI_NAME="platia-ai-service-$RUN"
DIRECT_NAME="platia-ai-direct-$RUN"

gateway_id="$(docker compose ps -q gateway)"
[ -n "$gateway_id" ] || { echo "El gateway no está levantado: docker compose up -d --build --wait"; exit 1; }
docker network inspect "$NETWORK" >/dev/null 2>&1 \
  || { echo "No existe la red $NETWORK: vuelve a levantar el gateway con su docker-compose.yml actual"; exit 1; }
image="$(docker inspect -f '{{.Config.Image}}' "$gateway_id")"

if [ -z "${AI_IMAGE:-}" ]; then
  PLATIA_DIR="${PLATIA_DIR:-../platia-dev}"
  [ -f "$PLATIA_DIR/ai_service/Dockerfile" ] || { echo "No encuentro $PLATIA_DIR/ai_service: usa PLATIA_DIR o AI_IMAGE"; exit 1; }
  echo "== Construyendo el ai_service de PLATIA ($PLATIA_DIR)"
  AI_IMAGE=platia-ai-service-prueba
  docker build -q -t "$AI_IMAGE" "$PLATIA_DIR/ai_service" >/dev/null
fi

cleanup() { docker rm -f "$FAKE_NAME" "$AI_NAME" "$DIRECT_NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run -d --rm --name "$FAKE_NAME" --network "$NETWORK" --network-alias "fake-$RUN" \
  -e FAKE_PROVIDER_KEY="$FAKE_KEY" -v "$PWD/scripts/fake_provider.py:/fake.py:ro" \
  --entrypoint python "$image" /fake.py >/dev/null

cli() { docker compose run --rm -T -e FAKE_KEY="$FAKE_KEY" migrations python -m app.cli "$@"; }
echo "== Alta de PLATIA en el gateway ($RUN)"
cli create-org --slug "$RUN" --name "Prueba $RUN"
cli create-team --org "$RUN" --slug plataforma --name Plataforma
cli create-app --org "$RUN" --team plataforma --slug platia --name PLATIA --trusted --purpose '*'
cli create-provider --slug "anthropic-$RUN" --name "Anthropic simulado" --kind anthropic \
  --base-url "http://fake-$RUN:8080" --allow-http --api-key-env FAKE_KEY
cli create-model --key "claude-$RUN" --name "Claude simulado" --family anthropic
cli create-deployment --model "claude-$RUN" --provider "anthropic-$RUN" --upstream claude-real
cli set-price --model "claude-$RUN" --input 2 --output 10 --since 2020-01-01
KEY="$(cli create-key --org "$RUN" --app platia --label "ai_service de PLATIA" | tail -n 1)"

start_ai() { # nombre, variables de entorno (-e ...)
  local name="$1"; shift
  docker run -d --rm --name "$name" --network "$NETWORK" \
    -e AI_SERVICE_API_KEY="$AI_KEY" -e EMBEDDING_PROVIDER=hash -e QDRANT_HOST=localhost "$@" \
    "$AI_IMAGE" uvicorn app.main:app --host 0.0.0.0 --port 8000 >/dev/null
  for _ in $(seq 30); do
    [ "$(AI="$name" ai /health '' | head -n 1)" = 200 ] && return 0
    sleep 1
  done
  echo "FALLO: ai_service ($name) no arranca"; docker logs "$name" | tail -20; exit 1
}

# El backend de PLATIA llama así a ai_service; aquí se hace desde dentro del contenedor
ai() { # ruta, cuerpo JSON → código HTTP en la primera línea y cuerpo después
  docker exec -i -e AI_KEY="$AI_KEY" "${AI:-$AI_NAME}" python - "$1" "$2" <<'PY'
import os, sys, urllib.error, urllib.request
request = urllib.request.Request(f"http://127.0.0.1:8000{sys.argv[1]}", data=sys.argv[2].encode() or None,
                                 headers={"X-API-Key": os.environ["AI_KEY"], "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(request, timeout=60) as response:
        print(response.status); print(response.read().decode())
except urllib.error.HTTPError as exc:
    print(exc.code); print(exc.read().decode())
except OSError as exc:
    print(0); print(exc)
PY
}

echo "== ai_service de PLATIA por el gateway"
start_ai "$AI_NAME" -e LLM_GATEWAY_ENABLED=true -e LLM_GATEWAY_URL=http://platia-gateway:8000 \
  -e LLM_GATEWAY_API_KEY="$KEY"
out="$(ai /health '')"
grep -q '"llm_gateway":true' <<<"$out" && grep -q '"llm_gateway":"configured"' <<<"$out" \
  || { echo "FALLO: /health de ai_service no ve el gateway"; echo "$out"; exit 1; }
echo "ok: /health de ai_service con el gateway activo"

body() { # tarea, correlation_id
  printf '{"service_type":"%s","targets":[{"provider":"anthropic","model":"claude-%s"}],"system":"Eres breve.",' "$1" "$RUN"
  printf '"messages":[{"role":"user","content":"Resume el pliego"}],"max_tokens":300,"correlation_id":"%s","user_ref":"7"}' "$2"
}
check() { # descripción, respuesta, texto que debe contener
  if [ "$(head -n 1 <<<"$2")" != 200 ] || ! grep -q "$3" <<<"$2"; then
    echo "FALLO: $1"; echo "$2"; exit 1
  fi
  echo "ok: $1"
}

out="$(ai /v1/complete "$(body document_summary "$RUN-1")")"
check "/v1/complete (documento)" "$out" "Respuesta simulada"
grep -q '"input_tokens":1000' <<<"$out" && grep -q '"output_tokens":200' <<<"$out" \
  || { echo "FALLO: tokens de la respuesta de ai_service"; echo "$out"; exit 1; }
out="$(ai /v1/stream "$(body copilot "$RUN-2")")"
check "/v1/stream (copiloto)" "$out" "event: final"
test "$(grep -c '^event: delta' <<<"$out")" -ge 1

echo "== Vuelta atrás: LLM_GATEWAY_ENABLED=false"
# Sin el gateway, el SDK de Anthropic va a ANTHROPIC_BASE_URL (aquí, el proveedor simulado)
start_ai "$DIRECT_NAME" -e LLM_GATEWAY_ENABLED=false -e ANTHROPIC_API_KEY="$FAKE_KEY" \
  -e ANTHROPIC_BASE_URL="http://fake-$RUN:8080"
out="$(AI="$DIRECT_NAME" ai /v1/complete "$(body document_summary "$RUN-3")")"
check "/v1/complete directo, sin el gateway" "$out" "Respuesta simulada"

echo "== Auditoría del gateway"
summary="$(docker compose exec -T gateway python - "$RUN" <<'PY'
import asyncio, sys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from app.settings import Settings

async def main():
    engine = create_async_engine(Settings().database_url)
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT request_id, purpose, end_user_ref, status, stream, input_tokens, output_tokens, cost_usd"
            " FROM audit_logs WHERE request_id LIKE :r ORDER BY request_id"), {"r": sys.argv[1] + "-%"})).all()
    await engine.dispose()
    for row in rows:
        print(" ".join(str(v) for v in row))

asyncio.run(main())
PY
)"
echo "$summary"
expected="$RUN-1 document_summary 7 success 1 1000 200 0.00400000
$RUN-2 copilot 7 success 1 1000 200 0.00400000"
if [ "$summary" != "$expected" ]; then
  echo "FALLO: se esperaba en la auditoría:"; echo "$expected"; exit 1
fi
echo "ok: tarea, persona, correlation_id y tokens iguales en PLATIA y en el gateway"
echo "ok: la llamada directa ($RUN-3) no pasa por el gateway"

echo "== Consumo por tarea (cli usage)"
cli usage --request-id "$RUN-1"
echo "== Prueba superada"
