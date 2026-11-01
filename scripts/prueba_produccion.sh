#!/usr/bin/env bash
# Prueba en Docker de la configuración de producción (G15, ADR-019), sin servidor ni dominio:
# docker-compose.prod.yml con las imágenes compiladas en local, un certificado autofirmado
# para gateway.prueba.test y HTTPS en un puerto de prueba.
#
# Requisito: las imágenes de desarrollo compiladas (docker compose build) y un .env.
# Uso:       bash scripts/prueba_produccion.sh
#
# Comprueba que una configuración insegura no arranca, que solo se habla HTTPS (TLS 1.2 o
# más, HSTS, redirección desde HTTP y nada para otros nombres), que el gateway está en modo
# producción y que una copia de seguridad se restaura (con la auditoría íntegra). Al terminar
# deja la plataforma en desarrollo.
set -euo pipefail
cd "$(dirname "$0")/.."

NAME=gateway.prueba.test
PORT="${PRUEBA_HTTPS_PORT:-18443}"
CERTS="$(mktemp -d)"
export REGISTRY=platia-prueba GATEWAY_VERSION=sha-0000000 SERVER_NAME="$NAME" HTTPS_PORT="$PORT" CERTS_DIR="$CERTS"
DEV=(docker compose)
PROD=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)
URL="https://$NAME:$PORT"
tls() { curl -s --noproxy '*' --cacert "$CERTS/fullchain.pem" --resolve "$NAME:$PORT:127.0.0.1" "$@"; }

back_to_dev() {
  echo "== Vuelta a desarrollo"
  "${DEV[@]}" up -d --no-build --wait --pull never --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$CERTS"
}
[ -n "${PRUEBA_SIN_VUELTA:-}" ] || trap back_to_dev EXIT

echo "== Imágenes locales con los nombres del registro ($REGISTRY, $GATEWAY_VERSION)"
docker tag platia-gateway-gateway "$REGISTRY/gateway:$GATEWAY_VERSION"
docker tag platia-gateway-nginx "$REGISTRY/nginx:$GATEWAY_VERSION"
openssl req -x509 -nodes -newkey rsa:2048 -days 1 -subj "/CN=$NAME" -addext "subjectAltName=DNS:$NAME" \
  -keyout "$CERTS/privkey.pem" -out "$CERTS/fullchain.pem" >/dev/null 2>&1
chmod 644 "$CERTS"/*.pem
"${PROD[@]}" config -q

echo "== Una configuración insegura no arranca"
if out="$("${PROD[@]}" run --rm --no-deps -T -e JWT_SECRET=corta worker 2>&1)"; then
  echo "FALLO: el worker arrancó con un JWT_SECRET corto"; exit 1
fi
grep -q "JWT_SECRET debe ser aleatoria" <<<"$out" || { echo "FALLO: no explicaba el motivo: $out"; exit 1; }
! grep -q "corta" <<<"$(grep 'JWT_SECRET' <<<"$out")" || { echo "FALLO: el mensaje mostraba el secreto"; exit 1; }
echo "ok: JWT_SECRET corto → no arranca, y el mensaje nombra la variable sin enseñarla"

echo "== Arranque en producción"
"${PROD[@]}" up -d --no-build --wait --pull never --wait-timeout 180
[ "$("${PROD[@]}" exec -T gateway printenv GATEWAY_ENV)" = production ] || { echo "FALLO: el gateway no está en producción"; exit 1; }
echo "ok: gateway, worker y nginx sanos con GATEWAY_ENV=production"

headers="$(tls -D - -o /dev/null "$URL/health")"
grep -q "^HTTP/2 200" <<<"$headers" || { echo "FALLO: /health por HTTPS: $headers"; exit 1; }
grep -qi "^strict-transport-security: max-age=31536000" <<<"$headers" || { echo "FALLO: falta HSTS"; exit 1; }
echo "ok: /health por HTTPS (HTTP/2) con HSTS"
portal="$(tls -D - -o /dev/null "$URL/portal/")"
grep -qi "^strict-transport-security" <<<"$portal" && grep -qi "^content-security-policy: default-src 'none'; script-src 'self'" <<<"$portal" \
  || { echo "FALLO: el portal debía llevar HSTS y su política de contenido"; exit 1; }
echo "ok: el portal también lleva HSTS y su política de contenido"
redirect="$(curl -s --noproxy '*' -o /dev/null -w '%{http_code} %{redirect_url}' "http://127.0.0.1:$PORT/health" -H "Host: $NAME")"
[ "$redirect" = "301 https://$NAME:$PORT/health" ] || { echo "FALLO: HTTP al puerto HTTPS debía redirigir: $redirect"; exit 1; }
echo "ok: HTTP en el puerto HTTPS → 301 a https"
if tls --tls-max 1.1 -o /dev/null "$URL/health" 2>/dev/null; then echo "FALLO: aceptó TLS 1.1"; exit 1; fi
echo "ok: TLS 1.1 rechazado"
if curl -s --noproxy '*' -k -o /dev/null --resolve "otro.prueba.test:$PORT:127.0.0.1" "https://otro.prueba.test:$PORT/health"; then
  echo "FALLO: respondió a un nombre que no es el del gateway"; exit 1
fi
echo "ok: otro nombre no recibe respuesta (ni certificado)"
docs="$("${PROD[@]}" exec -T gateway python -c "import urllib.request as u
try: u.urlopen('http://127.0.0.1:8000/openapi.json'); print('publicada')
except Exception as e: print(getattr(e, 'code', e))")"
[ "$docs" = 404 ] || { echo "FALLO: la documentación OpenAPI se publicaba en producción ($docs)"; exit 1; }
echo "ok: sin documentación OpenAPI en producción"

echo "== Copia de seguridad y restauración"
cli() { "${PROD[@]}" run --rm -T migrations python -m app.cli "$@" 2>/dev/null; }
./scripts/backup.sh --prod
copy="$(ls -d backups/* | tail -n 1)"
SLUG="tras-copia-$(date +%s)"
cli create-org --slug "$SLUG" --name "Creada tras la copia"
echo RESTAURAR | ./scripts/restore.sh "$copy" --prod | grep -v "^ *Container" || { echo "FALLO: la restauración"; exit 1; }
! cli show | grep -q "$SLUG" || { echo "FALLO: la restauración debía quitar lo creado después de la copia"; exit 1; }
triggers="$("${PROD[@]}" exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -N -e "SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE() OR TRIGGER_SCHEMA = \"$MYSQL_DATABASE\""')"
[ "$triggers" -ge 2 ] || { echo "FALLO: tras restaurar faltan los disparadores de la auditoría ($triggers)"; exit 1; }
tls -f -o /dev/null "$URL/health" || { echo "FALLO: el gateway no responde tras restaurar"; exit 1; }
echo "ok: copia y restauración; la cadena de la auditoría sigue íntegra y con sus disparadores"

echo "== Prueba de producción superada"
