# Arquitectura

El gateway es un servicio FastAPI asíncrono detrás de Nginx, con MySQL para la
configuración y la auditoría y Redis para lo que cambia en cada petición (cuotas, límites,
salud de despliegues). Por dentro, cada petición recorre un **pipeline de etapas** que leen
y escriben un contexto compartido (libro, cap. 1 y 3). Las razones de cada elección están
en [`adr/`](adr/).

## Contenedores y redes

```mermaid
flowchart LR
    subgraph clientes
        P[PLATIA ai_service]
        A[Otras aplicaciones]
        H[Personas: Claude Code, scripts]
    end
    subgraph edge_net
        N[nginx :8090]
        G[gateway FastAPI]
    end
    subgraph data_net
        M[(MySQL platia_gateway)]
        R[(Redis)]
    end
    subgraph llm_net
        G2[gateway]
    end
    A --> N --> G
    H --> N
    P -. red interna G5 .-> G2
    G --> M
    G --> R
    G --> X[Anthropic]
    G --> Y[Proveedor compatible con OpenAI]
```

- **edge_net**: Nginx y el gateway. Nginx es el único con puerto publicado.
- **data_net**: el gateway, MySQL y Redis. Ninguno de los dos publica puertos.
- **llm_net** (G5): red Docker compartida con PLATIA para que `ai_service` llame al
  gateway sin salir del servidor. Es una red externa que crean los dos `docker-compose`.

## Contratos de entrada

| Ruta | Contrato | Para quién | Fase |
| --- | --- | --- | --- |
| `POST /v1/messages` | Anthropic Messages | PLATIA y clientes del SDK de Anthropic | G3 (G4 streaming) |
| `POST /v1/chat/completions` | OpenAI Chat Completions | Clientes compatibles con OpenAI | G3 (G4 streaming) |
| `GET /v1/models` | OpenAI | Saber qué modelos puede usar una clave | G3 |
| `GET /health` | Propio | Nginx y monitorización | G0 |

Los dos contratos convergen en el mismo pipeline: la diferencia está solo en cómo se lee la
entrada y cómo se da forma a la salida. Cuando la petición y el proveedor son de la misma
familia (Anthropic → Anthropic), el cuerpo original viaja casi intacto después de los
controles, para no perder funciones que una traducción rompería
([ADR-002](adr/ADR-002-doble-contrato.md)).

**Cabeceras propias** (nunca se reenvían al proveedor):

| Cabecera | Para qué | Quién puede enviarla |
| --- | --- | --- |
| `Authorization: Bearer plgw-…` o `x-api-key: plgw-…` | Clave de aplicación o personal | Todos |
| `X-Platia-User-Id` | Persona final de la aplicación | Solo aplicaciones marcadas como de confianza |
| `X-Platia-Purpose` | Tarea o propósito (por ejemplo `proposal_writer`), para cuotas, routing y auditoría | Todos; se valida contra la lista de la aplicación |
| `X-Request-ID` | Correlación de extremo a extremo; si falta, la pone Nginx | Todos |

**Cabeceras de respuesta**: `X-Request-ID`, `X-Platia-Model-Used`, `X-Platia-Cost-Usd`,
`X-Platia-Latency-Ms` (G3 y G7).

## El pipeline

Cada etapa recibe el contexto de la petición, lo lee, escribe su resultado y devuelve. Una
etapa no llama a otra: así cada una se prueba sola y el orden se puede cambiar. El libro
describe 14 etapas; este gateway empieza con 8 y las demás se añaden si hacen falta
([ADR-003](adr/ADR-003-pipeline-de-etapas.md)).

| # | Etapa | Qué resuelve | Si falla su dependencia | Fase |
| --- | --- | --- | --- | --- |
| 1 | `auth` | Clave → aplicación o persona → equipo → organización activa; persona y propósito declarados | Se rechaza (401) | G3 |
| 2 | `security_input` | Jailbreak, marcas de clasificación, secretos | Reglas por defecto en memoria; nunca se salta | G8 |
| 3 | `filter` | Datos personales: detectar y tachar según la política | Reglas por defecto en memoria | G8 |
| 4 | `policy` | Modelo final permitido para esa clave y propósito; cuota y límites | Se rechaza (402, 403 o 429) | G3, G7 |
| 5 | `route` | Despliegue y adaptador del proveedor | Se rechaza (503) | G3, G6 |
| 6 | `execute` | Llamada al proveedor con tiempo máximo y cadena de reserva | Siguiente de la cadena; si no queda, 502 | G3, G6 |
| 7 | `security_output` | Filtro de salida, también por trozos en streaming | Se corta la respuesta | G8 |
| 8 | `audit` | Registro, coste y cabeceras de respuesta | Ver política de fallo de auditoría | G3, G9 |

Atajos: una etapa puede cortar el pipeline (identidad inválida, bloqueo de seguridad, cuota
agotada). La auditoría se ejecuta siempre, también en los cortes y cuando el cliente se
desconecta a mitad de un streaming (bloque `finally`).

**En el código** (G3, [ADR-007](adr/ADR-007-contrato-y-pipeline-minimo.md)): cada etapa es
un módulo de `gateway/app/pipeline/` y `runner.py` fija el orden: `auth` → `policy` →
`route` → apertura de `audit` → `execute` → cierre de `audit`. Los contratos y su
traducción están en `contracts.py`, los adaptadores en `providers.py` y las rutas en
`api.py`. Los 401 no se registran en `audit_logs` (solo en el log), para que una clave
falsa no pueda escribir en la base.

**Streaming** (G4, [ADR-008](adr/ADR-008-streaming.md)): con `stream: true`, `execute` abre
la respuesta del proveedor y comprueba su código. Desde ahí, `streaming.py` reenvía los
eventos SSE tal cual entre familias iguales, o traducidos si son de familias distintas
(solo texto). También lee el uso de tokens y cierra la auditoría en el `finally` del
generador, aunque el cliente se desconecte.

**Etapas del libro que no entran ahora**: ganchos corporativos antes y después del chat
(con los webhooks de G12), caché exacta y semántica (G11, opcional), contexto RAG (PLATIA
ya lo tiene), recorte del historial y control previo de ventana (se reconsideran en G7).

## Identidad

```mermaid
flowchart TD
    O[Organización] --> T[Equipo]
    T --> U[Persona]
    T --> AP[Aplicación]
    U --> KU[Claves personales plgw-usr-]
    AP --> KA[Claves de aplicación plgw-app-]
```

- Una **aplicación** pertenece a un equipo y tiene su propio presupuesto, su lista de
  propósitos permitidos y, si se quiere, una lista cerrada de modelos. Estados: activa,
  suspendida (reversible) y revocada (definitiva).
- Las claves son opacas, con prefijo visible y secreto aleatorio; se guarda solo su hash.
  Una aplicación puede tener una clave secundaria con caducidad para rotar sin cortes.
- Las claves de aplicación y las personales no se confunden: un tipo no puede hacerse pasar
  por el otro.
- Solo una aplicación **de confianza** (PLATIA) puede declarar la persona final con
  `X-Platia-User-Id`. El gateway no conoce las cuentas de PLATIA: guarda ese id como dato
  de atribución, no como identidad autenticada.
- Políticas en cascada (persona → equipo → organización): se toma el valor más específico
  configurado; si una política exige un techo del nivel superior, se comprueba aparte.
- Sin dispositivos ni device binding en este alcance ([ADR-004](adr/ADR-004-identidad.md)).

## Reparto con PLATIA

PLATIA sigue gobernando lo que es de su negocio y el gateway lo que es corporativo
([ADR-001](adr/ADR-001-servicio-aparte.md)):

| Responsabilidad | PLATIA | Gateway |
| --- | --- | --- |
| Prompts versionados y modelo por tarea | Sí | No |
| Presupuesto por tarea y por persona de PLATIA, ROI | Sí | No |
| Cuota por organización, equipo, aplicación y persona | No | Sí |
| Guardarraíles de negocio | Sí | No |
| Jailbreak, datos personales, secretos y filtro de salida corporativos | No | Sí |
| Embeddings, Qdrant y RAG con permisos | Sí | No |
| Reserva entre proveedores y salud de despliegues | Su cadena actual, como respaldo | Sí |
| Registro de cada llamada | `llm_usage_log` | Auditoría encadenada |

`ai_service` llama siempre en streaming (`messages.stream`), así que PLATIA solo puede
pasar por el gateway cuando G4 esté hecho. En G5 se cambian solo la URL base y la clave del
SDK de Anthropic, detrás de `LLM_GATEWAY_ENABLED`. Si el gateway no responde, por defecto
se falla cerrado; volver a la llamada directa es una decisión manual.

## Datos

MySQL guarda la configuración y la auditoría; Redis, los contadores de cuota y de límites,
el estado de salud y enfriamiento de los despliegues y la revocación de claves. Las tablas
se crean con migraciones Alembic ([ADR-006](adr/ADR-006-modelo-de-datos.md)).

```mermaid
erDiagram
    organizations ||--o{ teams : tiene
    organizations ||--o{ users : tiene
    teams ||--o{ users : agrupa
    teams ||--o{ applications : posee
    applications ||--o{ api_keys : "claves plgw-app-"
    users ||--o{ api_keys : "claves plgw-usr-"
    providers ||--o{ deployments : sirve
    llm_models ||--o{ deployments : "se despliega en"
    llm_models ||--o{ model_prices : "precio desde"
```

| Tabla | Qué guarda | Fase en que se usa |
| --- | --- | --- |
| `organizations`, `teams`, `users` | Jerarquía de identidad; sal de auditoría por organización | G3, G10 |
| `applications` | Clientes registrados: estado, confianza, propósitos y modelos permitidos | G3 |
| `api_keys` | Hash y prefijo de cada clave, caducidad y revocación | G3 |
| `providers` | Cuentas de proveedor con la clave cifrada (AES-256-GCM) | G3 |
| `llm_models`, `deployments` | Modelos, sus funciones y dónde se sirven, con peso y prioridad | G3, G6 |
| `model_prices` | USD por millón de tokens desde una fecha, incluida la caché | G7 |
| `audit_logs` | Una fila por petición: quién, qué, resultado, tokens, coste, hashes | G3, G9 |

`audit_logs` no tiene claves foráneas, para sobrevivir a cambios en la configuración.

El texto de prompts y respuestas no se guarda en claro: la auditoría guarda hashes (con sal
por organización), tokens, coste, latencia, modelo y decisiones de seguridad (G9).
