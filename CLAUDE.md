# PLATIA Gateway — Instrucciones del proyecto para Claude Code

Gateway LLM corporativo: una sola puerta hacia los modelos de lenguaje para las
aplicaciones internas (la primera, PLATIA, repo `cotecnia-adm/platia-dev`). Se construye
por fases pequeñas siguiendo *Anatomía de una Plataforma IA Corporativa*; el plan está en
`docs/plan-fases.md`. Antes de diseñar algo nuevo, lee los capítulos de la fase,
`docs/requisitos.md`, `docs/arquitectura.md` y los ADR de `docs/adr/`. Una decisión de arquitectura nueva se documenta con un ADR.

## Stack
- **Gateway**: FastAPI asíncrono + SQLAlchemy 2 (aiomysql) — `gateway/`
- **Datos**: MySQL 8.0 (base `platia_gateway`) y Redis 7
- **Proxy**: Nginx, único servicio con puerto publicado (8090) — `nginx/nginx.conf`
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
  `audit-pseudonymize`, `revoke-key`,
  `show`, `usage`; `--exists-ok` para
  guiones que se repiten)
- Prueba en Docker sin coste (con la plataforma levantada): `bash scripts/prueba_docker.sh`
  (con `pip install anthropic openai`, prueba también los SDK oficiales)
- Conexión con PLATIA (G5): alta con `bash scripts/alta_platia.sh`; prueba conjunta con
  `PLATIA_DIR=../platia-dev bash scripts/prueba_platia.sh`; guía en `docs/conexion-platia.md`
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
- Cada fase llega en un PR con tests, `ruff` y cobertura en verde, y una prueba en Docker
  que se pueda repetir.
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
