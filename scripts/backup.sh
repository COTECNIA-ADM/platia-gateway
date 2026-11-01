#!/usr/bin/env bash
# Copia de seguridad del gateway (G15, ADR-019): base de datos y .env.
#
#   ./scripts/backup.sh            # desarrollo (docker-compose.yml)
#   ./scripts/backup.sh --prod     # producción (añade docker-compose.prod.yml)
#
# Deja una carpeta backups/AAAAMMDD-HHMMSS con:
#   mysql.sql.gz   la base platia_gateway entera, con los disparadores de la auditoría
#                  (volcado coherente, sin parar el gateway)
#   env            el .env: sin su ENCRYPTION_KEY no se pueden descifrar las claves de los
#                  proveedores ni los secretos MFA
# Redis no se copia: los contadores de cuota del día y del mes se reconstruyen desde la
# auditoría (ADR-011), y la caché y los enfriamientos son temporales.
# Borra las copias de más de BACKUP_KEEP_DAYS días (14 por defecto).
#
# La carpeta backups/ tiene secretos y datos personales (auditoría): cópiala fuera del
# servidor (otro disco, almacenamiento de objetos cifrado) y restringe quién puede leerla.
# Adaptado de cotecnia-adm/platia-dev (scripts/backup.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f docker-compose.yml)
[ "${1:-}" = "--prod" ] && COMPOSE+=(-f docker-compose.prod.yml)
KEEP_DAYS="${BACKUP_KEEP_DAYS:-$(sed -n 's/^BACKUP_KEEP_DAYS=//p' .env 2>/dev/null)}"
KEEP_DAYS="${KEEP_DAYS:-14}"

umask 077
dest="backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$dest"

echo "Base de datos..."
"${COMPOSE[@]}" exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump -uroot \
  --single-transaction --routines --triggers --set-gtid-purged=OFF --add-drop-database \
  --databases "$MYSQL_DATABASE"' | gzip > "$dest/mysql.sql.gz"

cp .env "$dest/env"

# Comprobación mínima: un volcado vacío o cortado no es una copia
gzip -t "$dest/mysql.sql.gz"
zcat "$dest/mysql.sql.gz" | tail -n 1 | grep -q "Dump completed" || { echo "El volcado de MySQL está incompleto"; exit 1; }
zgrep -q 'TRIGGER `trg_audit_logs_no_delete`' "$dest/mysql.sql.gz" || { echo "El volcado no trae los disparadores de la auditoría"; exit 1; }

find backups -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" -exec rm -rf {} +
echo "Copia en $dest ($(du -sh "$dest" | cut -f1))"
