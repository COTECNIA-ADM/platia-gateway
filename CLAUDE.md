# PLATIA Gateway — Instrucciones del proyecto para Claude Code

Gateway LLM corporativo: una sola puerta hacia los modelos de lenguaje para las
aplicaciones internas (la primera, PLATIA, repo `cotecnia-adm/platia-dev`). Se construye
por fases pequeñas siguiendo *Anatomía de una Plataforma IA Corporativa*; el plan está en
`docs/plan-fases.md`. Antes de diseñar algo nuevo, lee los capítulos de la fase y los ADR de
`docs/adr/`. Una decisión de arquitectura nueva se documenta con un ADR.

## Stack
- **Gateway**: FastAPI asíncrono + SQLAlchemy 2 (aiomysql) — `gateway/`
- **Datos**: MySQL 8.0 (base `platia_gateway`) y Redis 7
- **Proxy**: Nginx, único servicio con puerto publicado (8090) — `nginx/nginx.conf`
- **Redes Docker**: `edge_net` (Nginx ↔ gateway) y `data_net` (gateway ↔ MySQL/Redis)

## Comandos
- Crear `.env`: `python scripts/generate_env.py` (variables nuevas: `--actualizar`)
- Levantar: `docker compose up -d --build --wait` → http://localhost:8090/health
- Tests: `cd gateway && pip install -r requirements-dev.txt && pytest`
- Estilo: `ruff check gateway` (configuración en `ruff.toml`); la CI exige además una
  cobertura mínima (`--cov-fail-under`, ver `.github/workflows/ci.yml`)

## Convenciones obligatorias
- Textos, comentarios y documentación en español de España.
- Logs con `structlog`, nunca `print()`. Nada de datos personales ni secretos en los logs.
- Toda configuración y secreto sale de variables de entorno (`app/settings.py`); plantilla
  en `.env.example`. Una variable nueva va en los dos sitios y en `docker-compose.yml`.
- Los errores nunca devuelven detalles internos: el detalle va al log.
- Fallar cerrado: si no se puede resolver la identidad, la cuota o una política, se
  rechaza la petición.
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
- No hacer `ALTER TABLE` a mano: los cambios de esquema irán con Alembic (desde G2).
