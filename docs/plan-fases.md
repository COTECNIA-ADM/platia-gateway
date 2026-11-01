# Plan por fases

Cada fase es un PR revisable con tests en la CI y una prueba en Docker. El bloque A
termina con PLATIA usando el gateway; los bloques B a D añaden control sin tocar PLATIA.
Alcance acordado: primero los bloques A y B (G0–G9).

| # | Fase | Qué se construye | Capítulos | Estado |
| --- | --- | --- | --- | --- |
| G0 | Preparación | Estructura, FastAPI con `/health`, docker-compose (gateway, MySQL, Redis, Nginx), CI, aviso de licencia | 33–34 | Hecha |
| G1 | Requisitos y arquitectura | `docs/requisitos.md`, `docs/arquitectura.md`, ADR de alcance y del reparto con PLATIA | 1–2, Ap. B y C | Hecha |
| G2 | Modelo de datos | Organización, equipo, persona, aplicación, claves, proveedores, despliegues, precios y auditoría; Alembic | 2, 32 | Hecha |
| G3 | Contrato y pipeline mínimo | `/v1/messages` (Anthropic) y `/v1/chat/completions` (OpenAI) sin streaming; pipeline auth → route → execute → audit; adaptadores; claves API opacas | 1, 3, 7, 18 | Hecha |
| G4 | Streaming SSE | Streaming en los dos contratos, auditoría en `finally`, uso real del último fragmento | 4 | Hecha |
| G5 | Conexión con PLATIA | Red `llm_net`, proveedor «gateway» en `ai_service` tras `LLM_GATEWAY_ENABLED`, cabeceras de persona y tarea, comparación de costes | 26 | Hecha |
| G6 | Routing y reserva | Selección por propósito, despliegues con salud, enfriamiento y cadenas de reserva | 5–6 | Hecha |
| G7 | Cuotas, coste y rate limiting | Precios, coste por llamada, cuotas por organización, equipo, aplicación y persona; límites por minuto y de simultáneas (ventanas fijas, ADR-011) | 13–15 | Hecha |
| G8 | Seguridad de contenido | Jailbreak, PII, DLP, firewall y filtro de salida también en streaming | 19–20 | En curso |
| G9 | Auditoría y gobierno | Registro append-only con cadena SHA-256, escritura por lotes, exportación y seudonimización RGPD | 21–22 | Pendiente |
| G10 | Identidad de personas | JWT con refresh, MFA, revocación, claves personales; SSO/OIDC si hay proveedor | 16–18 | Más adelante |
| G11 | Eficiencia (opcional) | Caché exacta por organización y compresión ligera | 8–9 | Más adelante |
| G12 | Telemetría | Métricas, logs, tiempo real por WebSocket y webhooks firmados | 24 | Más adelante |
| G13 | Panel de administración | App React: operaciones, proveedores y routing, cuotas, firewall, auditoría | 27–29 | Más adelante |
| G14 | Portal de usuario | Cuenta, seguridad, claves y consumo propios | 30–31 | Más adelante |
| G15 | Despliegue y operación | Compose de producción, Nginx endurecido, Celery, GHCR, copias y restauración | 33–34 | Más adelante |

Fuera del núcleo, para más adelante si hacen falta: embeddings, imágenes y audio (cap. 10),
búsqueda web y extracción de documentos (cap. 11), cola y RAG propios (cap. 12; PLATIA ya
tiene los suyos), federación MCP (cap. 25–26), SCIM, device binding, cifrado en cliente y
claves maestras con Shamir (cap. 16–18) y el portal de cumplimiento del AI Act (cap. 23 y 31).

## Integración con PLATIA

El gateway envuelve a `ai_service`, no lo sustituye. PLATIA sigue decidiendo prompts,
modelo y presupuesto por tarea, RAG y ROI (su ADR-011); solo cambia a quién llama
`ai_service` para generar texto. Un interruptor (`LLM_GATEWAY_ENABLED`) permite volver a la
llamada directa. El gateway expone el contrato de Anthropic además del de OpenAI para no
perder salidas estructuradas, razonamiento, herramientas ni caché de prompts.
