#!/usr/bin/env bash
# Restaura una copia de scripts/backup.sh. SUSTITUYE los datos actuales.
#
#   ./scripts/restore.sh backups/20261101-030000            # desarrollo
#   ./scripts/restore.sh backups/20261101-030000 --prod     # producción
#
# Pide escribir RESTAURAR para seguir. El .env de la copia no se restaura solo: si el
# servidor es nuevo, cópialo antes a mano (cp backups/.../env .env), porque las claves de los
# proveedores y los secretos MFA se cifraron con su ENCRYPTION_KEY.
# Al terminar comprueba la cadena de la auditoría (cli audit-verify) y vacía los contadores de
# Redis, que se reconstruyen desde la auditoría restaurada.
# Adaptado de cotecnia-adm/platia-dev (scripts/restore.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

src="${1:?Uso: $0 backups/AAAAMMDD-HHMMSS [--prod]}"
COMPOSE=(docker compose -f docker-compose.yml)
[ "${2:-}" = "--prod" ] && COMPOSE+=(-f docker-compose.prod.yml)
[ -f "$src/mysql.sql.gz" ] || { echo "No encuentro $src/mysql.sql.gz"; exit 1; }
gzip -t "$src/mysql.sql.gz"

read -r -p "Se borrarán los datos actuales del gateway y se cargará $src. Escribe RESTAURAR para seguir: " answer
[ "$answer" = "RESTAURAR" ] || { echo "Cancelado."; exit 1; }

echo "Parando el gateway (MySQL y Redis siguen arriba)..."
"${COMPOSE[@]}" stop nginx gateway worker

echo "Base de datos (se borra y se recrea)..."
gunzip -c "$src/mysql.sql.gz" | "${COMPOSE[@]}" exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot'

echo "Contadores de Redis (cuotas, caché, enfriamientos): se vacían y se reconstruyen solos..."
"${COMPOSE[@]}" exec -T redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli FLUSHDB' >/dev/null

echo "Arrancando (las migraciones se aplican si la copia es de una versión anterior)..."
"${COMPOSE[@]}" up -d --no-build --wait
"${COMPOSE[@]}" run --rm -T migrations python -m app.cli audit-verify
echo "Restaurado desde $src."
