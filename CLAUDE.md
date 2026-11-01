# PLATIA Gateway — Instrucciones del proyecto para Claude Code

Gateway LLM corporativo: una sola puerta hacia los modelos de lenguaje para las
aplicaciones internas (la primera, PLATIA, repo `cotecnia-adm/platia-dev`). Se construye
por fases pequeñas siguiendo *Anatomía de una Plataforma IA Corporativa*; el plan está en
`docs/plan-fases.md`. Antes de diseñar algo nuevo, lee los capítulos de la fase,
`docs/requisitos.md`, `docs/arquitectura.md` y los ADR de `docs/adr/`. Una decisión de arquitectura nueva se documenta con un ADR.

## Stack
- **Gateway**: FastAPI asíncrono + SQLAlchemy 2 (aiomysql) — `gateway/`
- **Datos**: MySQL 8.0 (base `platia_gateway`) y Redis 7
- **Panel y portal**: React 18 + TypeScript + Vite — `panel/` (portal en `src/portal/`), compilado en la imagen de Nginx y servido en `/panel/` y `/portal/`
- **Tareas de fondo**: servicio `worker` (`python -m app.worker`): sellado y webhooks; la API va con `BACKGROUND_TASKS=false`
- **Proxy**: Nginx, único servicio con puerto publicado (8090; en producción, HTTPS en el 8443) —
  `nginx/nginx.conf` (desarrollo), `nginx/prod.conf.template` (producción), `nginx/snippets/` (comunes) y `nginx/Dockerfile`
- **Redes Docker**: `edge_net` (Nginx ↔ gateway) y `data_net` (gateway ↔ MySQL/Redis)

## Comandos
- Crear `.env`: `python scripts/generate_env.py` (variables nuevas: `--actualizar`)
- Levantar: `docker compose up -d --build --wait` → http://localhost:8090/health
- Tests: `cd gateway && pip install -r requirements-dev.txt && pytest`
- Migraciones: las aplica el contenedor `migrations` al levantar. Nueva migración:
  `cd gateway && alembic revision --autogenerate -m "..."`, revisarla a mano y probar
  upgrade/downgrade/upgrade
- Tests con base de datos: `TEST_DATABASE_URL=mysql+pymysql://usuario:clave@host:puerto/base pytest`
  (vacía y recrea la base: nunca una con datos; el MySQL necesita
  `log_bin_trust_function_creators=1` para los disparadores de la auditoría)
- Alta de configuración (hasta el panel, G13): `docker compose run --rm migrations python -m app.cli <orden>`
  (`create-org`, `create-app`, `create-key`, `create-provider`, `create-model`, `create-deployment`,
  `set-price`, `set-fallbacks`, `set-deployment`, `set-limit`, `remove-limit`, `limits`,
  `set-content-policy`, `content-policies`, `audit-seal`, `audit-verify`, `audit-export`,
  `audit-pseudonymize`, `create-user`, `set-user`, `set-password`, `reset-mfa`,
  `revoke-sessions`, `set-cache`, `cache-clear`, `revoke-key`, `create-webhook`, `webhooks`,
  `set-webhook`, `webhook-test`, `latency`, `set-user --operator`,
  `show`, `usage`; `--exists-ok` para
  guiones que se repiten)
- Prueba en Docker sin coste (con la plataforma levantada): `bash scripts/prueba_docker.sh`
  (con `pip install anthropic openai`, prueba también los SDK oficiales)
- Conexión con PLATIA (G5): alta con `bash scripts/alta_platia.sh`; prueba conjunta con
  `PLATIA_DIR=../platia-dev bash scripts/prueba_platia.sh`; guía en `docs/conexion-platia.md`
- Producción (guía en `docs/despliegue.md`): `docker compose -f docker-compose.yml -f docker-compose.prod.yml ...`;
  desplegar con `scripts/deploy.sh sha-xxxxxxx`, copias con `scripts/backup.sh --prod` y `scripts/restore.sh`;
  prueba local de la configuración de producción: `bash scripts/prueba_produccion.sh`
- Panel: `cd panel && npm ci && npm test && npm run build` (desarrollo: `npm run dev`, con el gateway levantado)
- Estilo: `ruff check gateway` (configuración en `ruff.toml`); la CI exige además una
  cobertura mínima (`--cov-fail-under`, ver `.github/workflows/ci.yml`)

## Convenciones obligatorias
- Textos, comentarios y documentación en español de España.
- Logs con `structlog`, nunca `print()`. Nada de datos personales ni secretos en los logs.
- Toda configuración y secreto sale de variables de entorno (`app/settings.py`); plantilla
  en `.env.example`. Una variable nueva va en los dos sitios y en `docker-compose.yml`.
- Los errores nunca devuelven detalles internos: el detalle va al log.
- Cada modelo nuevo se importa en `app/models/__init__.py`. Fechas por defecto con
  `default=utcnow`; configuración con borrado lógico (`deleted_at`); la auditoría guarda ids
  como enteros, sin claves foráneas, y nunca texto de prompts ni respuestas.
- Secretos de proveedor siempre cifrados con `app/crypto.py`; claves de cliente solo como hash.
- Fallar cerrado: si no se puede resolver la identidad, la cuota o una política, se
  rechaza la petición.
- Pipeline (ADR-003, ADR-007): una etapa por módulo en `app/pipeline/`, con firma
  `run(ctx, deps)`; corta lanzando un error de `app/errors.py` y nunca llama a otra etapa.
  El orden está en `runner.py`. Entre familias iguales el cuerpo se reenvía tal cual; la
  traducción entre familias está en `contracts.py` y nunca degrada sin avisar.
- Streaming (ADR-008): el código del proveedor se comprueba antes de responder; entre
  familias iguales los eventos SSE se reenvían tal cual; la auditoría se cierra en el
  `finally` del generador dentro de `anyio.CancelScope(shield=True)`.
- Las cabeceras propias (`X-Platia-*`) y la clave del cliente nunca se reenvían al proveedor.
- Reserva (ADR-010): `route` ordena los destinos y `execute` los recorre. Un error del adaptador
  que deba cambiar de destino lleva `fallback=True`; los de la petición (400) nunca. La reserva
  solo usa modelos que la aplicación tiene permitidos.
- Cuotas (ADR-011): la etapa `quota` admite con un guion Lua (todo o nada) y `quota.settle`
  ajusta con el uso real allí donde se cierra la auditoría. Los contadores de día y mes se
  reconstruyen desde `audit_logs`; con límites y sin Redis, se rechaza (503).
- Contenido (ADR-012): detectores en `app/content.py` (deterministas, sin llamar a otro modelo)
  y acción por casilla en `content_policies`. Nunca se guarda ni se registra el texto
  encontrado; solo categoría, tipo y recuento en `security_flags`.
- Auditoría (ADR-013): una fila sellada (`chain_hash`) no se cambia ni se borra (disparadores);
  solo se puede seudonimizar `end_user_ref`. Una columna nueva en la cadena (`CHAIN_FIELDS`)
  exige otra `hash_version`. Los tests vacían las tablas con TRUNCATE, nunca con DELETE.
- Personas (ADR-014): contraseñas solo con `app/passwords.py` (scrypt) y MFA con `app/totp.py`;
  el JWT de acceso es HS256 fijo y cada petición comprueba la sesión en MySQL. La renovación
  va en cookie HttpOnly y las rutas que la usan exigen `X-Requested-With`. Una clave personal
  sigue las reglas de la aplicación con `personal_keys` y nunca declara otra persona.
- Caché (ADR-015): opcional por aplicación, la clave incluye la persona final y el valor va
  cifrado; un acierto pasa por `security_output` y la auditoría (proveedor `cache`). Si Redis
  falla, se sigue sin caché.
- Telemetría (ADR-016): métricas en `app/telemetry.py` con etiquetas de cardinalidad baja
  (nunca persona, clave ni id de petición); `/metrics` exige `METRICS_TOKEN` y Nginx no la
  publica. Los avisos salen con `events.emit` (nunca rompe la petición; lo repetible lleva
  `dedupe`), van a Redis y a la cola `webhook_deliveries`, y se firman con
  `X-Platia-Signature: t=…,v1=…`. Un destino de webhook pasa por la comprobación SSRF de
  `app/webhooks.py` (https, dirección pública fijada, sin redirecciones). Ningún evento lleva
  texto de prompts o respuestas ni claves.
- Panel (ADR-017): rutas en `app/admin/`, solo administradores con MFA y de su organización
  (lo ajeno, 404); el catálogo común, webhooks globales y sellado, solo `platform_operator`,
  que se da con la CLI. Cada cambio llama a `record(...)` antes de `commit` (misma
  transacción) y los secretos se registran como `(cambiado)`. Un secreto se enseña una vez,
  con `no-store`. Una ruta nueva va también en `docs/panel.md`.
- Portal (ADR-018): rutas en `app/portal.py` (y `account_api.py`), siempre filtradas por la
  persona de la sesión y sin aceptar otro id de persona; nunca cuotas ni consumo de equipo u
  organización. Una ruta nueva va también en `docs/portal.md`.
- Panel web y portal (`panel/`): llamadas solo con `src/lib/api.ts`, datos con React Query, token en
  memoria (nunca `localStorage` ni `sessionStorage`), nada de `dangerouslySetInnerHTML`,
  gráficos con vista de tabla, filtros en la URL y textos en español. Pruebas `*.test.tsx`
  junto a la pantalla con `mockApi` y `renderWithProviders`; `src/test/conventions.test.ts`
  comprueba las reglas que se pueden leer en el código.
- Cada fase llega en un PR con tests, `ruff` y cobertura en verde, y una prueba en Docker
  que se pueda repetir.
- Despliegue (ADR-019): una tarea de fondo nueva va en `app/background.py` (la arranca el
  worker) y debe ser segura con varios procesos. Una comprobación de producción nueva va en
  `Settings.production_problems` (nombra la variable, nunca su valor). Una ruta nueva se publica
  en `nginx/snippets/locations.conf` con su zona de límite. Las migraciones, compatibles hacia
  atrás (primero se añade, lo que sobra se borra después), para que `deploy.sh` pueda volver atrás.
- No exponer MySQL ni Redis fuera de `data_net`. Nginx solo publica las rutas que tocan.

## Licencia y libro
- El código de https://github.com/machinebooks/machinebooks.ai (carpeta `ES/aigateway`)
  es MIT: se puede adaptar citando en la cabecera del fichero la ruta del fragmento
  (`machinebooks.ai/ES/aigateway/cap-XX/block-NN`). El aviso está en `THIRD_PARTY_NOTICES.md`.
- El texto del libro tiene todos los derechos reservados: no se copia al repositorio, ni en
  código, comentarios o documentación. Se redacta de nuevo y se cita el capítulo.
- Nombres propios, no los del caso del libro (N7x): cabeceras `X-Platia-*`, claves `plgw-`.

## Lo que NO hacer
- No hardcodear API keys, contraseñas ni secretos. No subir `.env`.
- No hacer commit sin que pasen los tests.
- No hacer `ALTER TABLE` a mano: los cambios de esquema van con Alembic.
