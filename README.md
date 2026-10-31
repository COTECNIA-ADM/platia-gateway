# PLATIA Gateway

Gateway LLM corporativo de PLATIA: una sola puerta hacia los modelos de lenguaje para
todas las aplicaciones internas, con claves por aplicación y persona, cuotas, filtros de
seguridad, auditoría encadenada, varios proveedores con reserva, panel de administración
y portal de usuario.

Se construye por fases; el plan está en [`docs/plan-fases.md`](docs/plan-fases.md).

## Puesta en marcha

```bash
python scripts/generate_env.py          # crea .env con secretos aleatorios
docker compose up -d --build --wait     # gateway, MySQL, Redis y Nginx
curl http://localhost:8090/health       # {"status":"ok","service":"platia-gateway",...}
```

El puerto se cambia con `HTTP_PORT` en `.env` (PLATIA usa el 8080).

## Desarrollo

```bash
cd gateway
pip install -r requirements-dev.txt
pytest
ruff check .
```

## Créditos

La arquitectura sigue *Anatomía de una Plataforma IA Corporativa* (Carlos Pérez González y
Juan Carlos Montes Senra). Los fragmentos de su código compañero se reutilizan con licencia
MIT; ver [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
