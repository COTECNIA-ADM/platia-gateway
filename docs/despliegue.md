# Despliegue en producción

El gateway va en el **mismo servidor que PLATIA** (ADR-019). PLATIA ya ocupa los puertos 80 y
443, así que el gateway publica solo HTTPS en el **8443**, con su propio nombre (por ejemplo
`gateway.cotecnia.es`). PLATIA no pasa por ahí: llama al gateway por la red interna
`platia_llm_net` ([conexion-platia.md](conexion-platia.md)).

| Pieza | Qué es |
| --- | --- |
| `docker-compose.prod.yml` | Se usa siempre con el base: imágenes de GHCR, `GATEWAY_ENV=production`, solo HTTPS y logs con rotación |
| `nginx/prod.conf.template` | Nginx con TLS 1.2/1.3 y HSTS; rutas, límites y registro en `nginx/snippets/` (los mismos que en desarrollo) |
| `worker` | Sella la auditoría y entrega los webhooks; la API no lo hace (`BACKGROUND_TASKS=false`) |
| `scripts/deploy.sh` | Copia, imágenes nuevas, migraciones, arranque y vuelta atrás si no queda sano |
| `scripts/backup.sh` / `restore.sh` | Copias de MySQL (con los disparadores de la auditoría) y del `.env` |
| `.github/workflows/release.yml` | Publica las imágenes `gateway` y `nginx` (con el panel y el portal) al pasar la CI de `main` |
| `.github/workflows/deploy.yml` | Despliegue desde GitHub, con aprobación |

En los comandos de esta guía:

```bash
gw()    { docker compose -f docker-compose.yml -f docker-compose.prod.yml "$@"; }
gwcli() { gw run --rm -T migrations python -m app.cli "$@"; }
```

## 1. Antes de empezar

- Un nombre DNS para el gateway (`gateway.cotecnia.es`) que apunte al servidor de PLATIA.
- El puerto **8443** abierto en el cortafuegos para quien vaya a usar el panel, el portal o
  Claude Code. Si la red de la empresa no deja salir a ese puerto, ábrelo o elige otro con
  `HTTPS_PORT`.
- Un certificado para ese nombre (apartado 3).
- Acceso a las imágenes: si el repositorio es privado, crea en GitHub un *personal access
  token (classic)* solo con `read:packages` y, en el servidor:
  `docker login ghcr.io -u TU_USUARIO_DE_GITHUB` (pega el token como contraseña).

## 2. Primera instalación

```bash
sudo mkdir -p /opt/platia-gateway && sudo chown "$USER" /opt/platia-gateway
git clone https://github.com/cotecnia-adm/platia-gateway.git /opt/platia-gateway
cd /opt/platia-gateway
python3 scripts/generate_env.py         # secretos aleatorios en .env
```

En `.env`, cambia o comprueba esto:

| Variable | Valor |
| --- | --- |
| `GATEWAY_ENV` | `production` (el compose de producción lo fuerza igualmente) |
| `SERVER_NAME` | `gateway.cotecnia.es` |
| `HTTPS_PORT` | `8443` |
| `CERTS_DIR` | `./certs` (con `fullchain.pem` y `privkey.pem`) |
| `REGISTRY` | `ghcr.io/cotecnia-adm/platia-gateway` (en minúsculas) |
| `METRICS_TOKEN` | Déjalo generado, o vacío si no vas a usar Prometheus |

En producción, el gateway **no arranca** si algún secreto falta o es débil, si la cookie no
va solo por HTTPS o si el MFA de administradores está apagado. El mensaje dice qué variable
corregir.

Pon el certificado (apartado 3) y despliega la última versión de `main`; la etiqueta es
`sha-` más los 7 primeros caracteres del commit:

```bash
./scripts/deploy.sh sha-$(git rev-parse --short=7 origin/main)
```

Después, la organización y el primer administrador (activará el MFA al entrar en el panel):

```bash
gwcli create-org --slug cotecnia --name Cotecnia
gwcli create-user --org cotecnia --email ana@cotecnia.es --name Ana --role admin
gwcli set-password --org cotecnia --email ana@cotecnia.es
gwcli set-user --org cotecnia --email ana@cotecnia.es --operator si   # cambia el catálogo
```

El panel queda en `https://gateway.cotecnia.es:8443/panel/` y el portal, en `/portal/`.
Para conectar PLATIA: `bash scripts/alta_platia.sh` y [conexion-platia.md](conexion-platia.md).

## 3. Certificado

**Con el certificado de la empresa** (lo más sencillo; uno comodín `*.cotecnia.es` sirve):
copia la cadena completa a `certs/fullchain.pem` y la clave a `certs/privkey.pem`
(`chmod 600 certs/privkey.pem`). Al renovarlo, sustituye los ficheros: Nginx los vuelve a
leer cada 6 horas (o al momento con `gw exec nginx nginx -s reload`).

**Con Let's Encrypt, usando el certbot de PLATIA.** Let's Encrypt comprueba el dominio por
el puerto 80, que atiende el Nginx de PLATIA. Ese Nginx sirve el reto a cualquier nombre que
apunte al servidor, así que el certbot de PLATIA puede pedir también el del gateway:

```bash
cd /opt/platia
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile letsencrypt \
  run --rm --entrypoint certbot certbot certonly --webroot -w /var/www/certbot \
  -d gateway.cotecnia.es --email avisos@cotecnia.es --agree-tos --no-eff-email --non-interactive
```

El certbot de PLATIA lo renueva solo, junto con el suyo. Para copiarlo al gateway, añade al
`crontab` del servidor (el volumen se llama `<proyecto de PLATIA>_letsencrypt`; míralo con
`docker volume ls`):

```
30 4 * * * docker run --rm -v platia_letsencrypt:/le:ro -v /opt/platia-gateway/certs:/certs alpine sh -c 'cp -L /le/live/gateway.cotecnia.es/fullchain.pem /le/live/gateway.cotecnia.es/privkey.pem /certs/'
```

## 4. Despliegues siguientes

Cada cambio en `main` con la CI en verde publica las imágenes con su etiqueta `sha-xxxxxxx`
(flujo «Imágenes»). Para desplegar:

- **Desde GitHub**: Actions → «Despliegue» → *Run workflow* (vacío = el último commit de
  `main`). Necesita el entorno `production` con revisores y los secretos `DEPLOY_HOST`,
  `DEPLOY_USER`, `DEPLOY_SSH_KEY` y `DEPLOY_KNOWN_HOSTS` (los mismos que PLATIA si es el
  mismo servidor).
- **A mano**: `cd /opt/platia-gateway && git fetch && git checkout <commit> && ./scripts/deploy.sh sha-xxxxxxx`.

`deploy.sh` hace una copia, descarga las imágenes, aplica las migraciones, espera a que
gateway, worker y Nginx estén sanos y a que `/health` responda por HTTPS. Si en 3 minutos no
lo consigue, vuelve a la versión anterior (`.deploy/previous`). Volver atrás a mano:
`./scripts/deploy.sh $(cat .deploy/previous)`.

## 5. Copias de seguridad

```bash
./scripts/backup.sh --prod        # backups/AAAAMMDD-HHMMSS: mysql.sql.gz y env
```

Programa una al día en el `crontab` y lleva la carpeta fuera del servidor (tiene secretos y la
auditoría, que contiene datos personales):

```
0 3 * * * cd /opt/platia-gateway && ./scripts/backup.sh --prod >> backups/backup.log 2>&1
```

Se borran solas las de más de `BACKUP_KEEP_DAYS` días (14). Redis no se copia: los contadores
de cuota se reconstruyen desde la auditoría.

Restaurar (sustituye los datos; pide escribir `RESTAURAR`):

```bash
./scripts/restore.sh backups/20261101-030000 --prod
```

Al terminar comprueba la cadena de la auditoría. En un servidor nuevo, copia antes
`backups/.../env` a `.env`: las claves de los proveedores y los secretos MFA se cifraron con
su `ENCRYPTION_KEY`.

## 6. Operación

| Para | Comando |
| --- | --- |
| Estado | `gw ps` (los cinco con `healthy`, salvo `migrations`, que termina) |
| Registros | `gw logs -f --tail 100 gateway worker nginx` (JSON; Nginx sin datos personales) |
| Cadena de la auditoría | `gwcli audit-verify` |
| Consumo | `gwcli usage --since 2026-11-01` o el panel |
| Alguien sin acceso | `gwcli set-password`, `reset-mfa` o `revoke-sessions` |

Límites de Nginx por dirección IP (aparte de las cuotas del gateway). Pasarse da 429 en JSON:

| Rutas | Límite |
| --- | --- |
| `/auth/login` y `/auth/login/mfa` | 30 por minuto (ráfaga de 20) |
| `/auth/*`, `/me/*` y `/admin/*` | 300 por minuto (ráfaga de 50) |
| `/v1/*` | 600 por minuto (ráfaga de 100) y 100 conexiones a la vez |

PLATIA no cuenta para estos límites: no pasa por Nginx.

## 7. Probarlo sin servidor

`bash scripts/prueba_produccion.sh` (con las imágenes de desarrollo compiladas) levanta la
configuración de producción con un certificado autofirmado en el puerto 18443 y comprueba:
- HTTPS con HSTS, y que rechaza TLS 1.1 y otros nombres;
- que un secreto débil impide arrancar;
- una copia y su restauración.

Al terminar deja la plataforma en desarrollo. La CI la ejecuta en cada PR.
