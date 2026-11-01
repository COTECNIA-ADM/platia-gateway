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

## Primera llamada a Claude

Hasta que llegue el panel de administración (G13), la configuración se da de alta con la
línea de comandos. La clave de Anthropic no va en `.env`: se guarda cifrada en la base.

```bash
cli() { docker compose run --rm migrations python -m app.cli "$@"; }
cli create-org --slug cotecnia --name Cotecnia
cli create-team --org cotecnia --slug plataforma --name Plataforma
cli create-app --org cotecnia --team plataforma --slug platia --name PLATIA --trusted
docker compose run --rm -e ANTHROPIC_API_KEY migrations python -m app.cli create-provider \
    --slug anthropic --name Anthropic --kind anthropic --api-key-env ANTHROPIC_API_KEY
cli create-model --key claude-sonnet-5-5 --name "Claude Sonnet 5.5" --family anthropic
cli create-deployment --model claude-sonnet-5-5 --provider anthropic
cli set-price --model claude-sonnet-5-5 --input 3 --output 15 --cache-read 0.3 --cache-write 3.75
cli create-key --org cotecnia --app platia     # muestra la clave plgw-app-... una sola vez
```

(Para `create-provider`, exporta antes `ANTHROPIC_API_KEY` en tu terminal; comprueba los
precios vigentes en la web de Anthropic.) Después, con el SDK de Anthropic o con `curl`:

```bash
curl http://localhost:8090/v1/messages -H "x-api-key: plgw-app-platia-..." \
    -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
    -d '{"model":"claude-sonnet-5-5","max_tokens":200,"messages":[{"role":"user","content":"Hola"}]}'
```

La respuesta trae las cabeceras `X-Platia-Model-Used`, `X-Platia-Cost-Usd` y
`X-Platia-Latency-Ms`, y cada llamada queda en la tabla `audit_logs` (sin el texto).
`cli show` resume lo dado de alta y `cli revoke-key --prefix ...` revoca una clave.

Las mismas rutas aceptan el contrato de OpenAI (`POST /v1/chat/completions`, clave en
`Authorization: Bearer`). Las dos admiten streaming (`"stream": true`), así que funcionan
`client.messages.stream(...)` del SDK de Anthropic y `stream=True` del de OpenAI. En
streaming, el coste no va en una cabecera, pero queda en la auditoría.

## Reserva entre proveedores

Un modelo puede tener varios despliegues (en el mismo proveedor o en otros) y una cadena de
modelos de reserva. Si un destino falla por algo suyo (saturado, caído, sin respuesta), la
petición pasa al siguiente sin que el cliente lo note; los que fallan se enfrían un rato.

```bash
cli create-deployment --model claude-sonnet-5-5 --provider anthropic-2 --priority 1   # segundo destino
cli set-fallbacks --model claude-sonnet-5-5 --to claude-haiku-5-5                    # modelo de reserva
cli set-deployment --id 3 --inactive                                                 # retirar un destino
cli show                                                                             # ids, prioridades y cadenas
```

La respuesta dice el modelo que respondió (`X-Platia-Model-Used`) y cuántos destinos se
probaron (`X-Platia-Attempts`). Detalles y límites en [ADR-010](docs/adr/ADR-010-reserva-y-enfriamiento.md).

## Cuotas y límites de ritmo

Sin límites, el gateway no corta por gasto ni por ritmo. Cada límite se fija para la
organización, un equipo, una aplicación o las personas de una aplicación:

```bash
cli set-limit --org cotecnia --metric cost_usd --period month --value 200            # techo mensual (USD)
cli set-limit --org cotecnia --app platia --metric requests --period minute --value 120
cli set-limit --org cotecnia --app platia --metric concurrent --value 10            # peticiones a la vez
cli set-limit --org cotecnia --app platia --user '' --metric cost_usd --period day --value 5   # cada persona
cli limits                                                                           # límites y consumo del periodo
```

Una cuota de día o de mes agotada devuelve 402 (`X-Platia-Quota-Reset` dice cuándo se
renueva); un límite por minuto o de simultáneas, 429 con `Retry-After`. Al pasar el 80 %
queda un aviso `quota.alert` en el log. Con límites, si Redis no responde, el gateway
rechaza la petición (503). Detalles en [ADR-011](docs/adr/ADR-011-cuotas-y-limites.md).

## Seguridad de contenido

El gateway revisa el texto de cada petición y de cada respuesta. Por defecto bloquea los
intentos de saltarse las instrucciones del modelo (403), tacha los secretos (claves de API,
contraseñas) antes de enviarlos al proveedor y solo anota los datos personales y las marcas
de clasificación. Cada organización o aplicación lo cambia:

```bash
cli content-policies --org cotecnia --app platia                                     # política efectiva
cli set-content-policy --org cotecnia --direction input --check pii --action redact   # tachar datos personales
cli set-content-policy --org cotecnia --app platia --direction input --check pii --action default
```

Lo encontrado (tipo y número, nunca el dato) queda en `security_flags` de la auditoría y
en la cabecera `X-Platia-Content-Flags`. Detalles en [ADR-012](docs/adr/ADR-012-seguridad-de-contenido.md).

## Auditoría verificable

Cada petición deja una fila en `audit_logs` con quién, qué modelo, resultado, tokens, coste y
lo que encontró la seguridad de contenido, y la huella del prompt (nunca el texto). El
gateway sella las filas en una cadena SHA-256 y MySQL impide cambiarlas o borrarlas.

```bash
cli audit-verify                                   # ¿está íntegra la cadena?
cli audit-export --org cotecnia --since 2026-11-01 > auditoria.jsonl
cli audit-export --org cotecnia --user 7           # petición de acceso de una persona (RGPD)
cli audit-pseudonymize --org cotecnia --user 7     # seudonimizarla sin romper la cadena
```

Detalles en [ADR-013](docs/adr/ADR-013-auditoria-verificable.md).

## Caché de respuestas

Una aplicación puede guardar sus respuestas para no pagar dos veces la misma petición (misma
persona, mismo propósito, mismo cuerpo). Está desactivada por defecto, solo funciona sin
streaming y la respuesta se guarda cifrada en Redis.

```bash
cli set-cache --org cotecnia --app platia --ttl 3600 --purpose document_summary
cli set-cache --org cotecnia --app platia --ttl 0          # desactivarla
cli cache-clear --org cotecnia --app platia                # vaciarla
```

Cada respuesta lleva `X-Platia-Cache: miss` o `hit`, y una petición se la salta con
`X-Platia-Cache: no`. Detalles en [ADR-015](docs/adr/ADR-015-cache-exacta.md).

## Métricas y avisos

`/metrics` (Prometheus, con `METRICS_TOKEN`) da peticiones, tokens, coste, errores de
proveedor y el tiempo propio del gateway. Los avisos de cuota, los bloqueos de seguridad y
los despliegues enfriados llegan firmados a los webhooks que se den de alta, y el panel los
verá en tiempo real por WebSocket.

```bash
cli create-webhook --org cotecnia --name "Avisos IA" --url https://avisos.cotecnia.es/platia \
    --event quota.alert --event quota.exhausted --event security.blocked
cli webhook-test --id 1
cli latency --since 2026-11-01        # p50, p95 y p99 del tiempo propio (objetivo: p95 < 50 ms)
```

Cómo comprobar la firma en el receptor, la lista de métricas y el WebSocket:
[docs/telemetria.md](docs/telemetria.md) y [ADR-016](docs/adr/ADR-016-telemetria.md).

## Personas y claves personales

Las personas entran con contraseña y una app autenticadora (TOTP) en `/auth/login`; el panel
y el portal llegan en G13 y G14. Un administrador tiene que activar el MFA antes de hacer
nada más. Las claves personales (`plgw-usr-…`, para Claude Code o scripts) siguen las reglas
de la aplicación marcada con `--personal-keys`.

```bash
cli create-app --org cotecnia --team plataforma --slug herramientas --name "Herramientas" \
    --personal-keys --model claude-sonnet
cli create-user --org cotecnia --email ana@cotecnia.es --name "Ana" --role admin
cli set-password --org cotecnia --email ana@cotecnia.es   # la pide sin mostrarla
cli create-key --org cotecnia --user ana@cotecnia.es --label "Claude Code"
cli reset-mfa --org cotecnia --email ana@cotecnia.es       # si pierde el móvil
cli revoke-sessions --org cotecnia --email ana@cotecnia.es --keys
```

En PowerShell, con `gwcli` (que usa `-T`), la contraseña se pasa por la entrada:
`"la contraseña" | gwcli set-password --org cotecnia --email ana@cotecnia.es`. Detalles en
[ADR-014](docs/adr/ADR-014-identidad-de-personas.md).

## Conexión con PLATIA

`bash scripts/alta_platia.sh` (con `ANTHROPIC_API_KEY` exportada) da de alta PLATIA con sus
modelos y precios y muestra la clave para su `.env`. PLATIA llama al gateway por la red
interna `platia_llm_net` cuando tiene `LLM_GATEWAY_ENABLED=true`. Guía completa, comparación
de costes y vuelta atrás en [`docs/conexion-platia.md`](docs/conexion-platia.md).

`cli usage` resume la auditoría por tarea y modelo (`--app`, `--since`, `--until`,
`--request-id`).

## Prueba en Docker sin coste

```bash
bash scripts/prueba_docker.sh
```

Arranca un proveedor simulado, da de alta una configuración nueva y comprueba los dos
contratos con y sin streaming, los errores de identidad y permisos, las cabeceras y la
auditoría. Si tienes instalados los SDK (`pip install anthropic openai`), los prueba también
como los usará PLATIA. Se puede repetir: cada ejecución usa nombres nuevos.

`PLATIA_DIR=../platia-dev bash scripts/prueba_platia.sh` prueba además la conexión con el
`ai_service` real de PLATIA, por el gateway y con el interruptor apagado.

## Desarrollo

```bash
cd gateway
pip install -r requirements-dev.txt
pytest                                  # los tests con MySQL necesitan TEST_DATABASE_URL
ruff check .
```

## Créditos

La arquitectura sigue *Anatomía de una Plataforma IA Corporativa* (Carlos Pérez González y
Juan Carlos Montes Senra). Los fragmentos de su código compañero se reutilizan con licencia
MIT; ver [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
