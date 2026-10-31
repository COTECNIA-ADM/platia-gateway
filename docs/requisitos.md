# Requisitos

Referencia funcional del gateway. Cada componente tiene que poder justificarse con uno de
los requisitos de abajo; la fase indica cuándo se construye (`docs/plan-fases.md`).

## 1. Problema

Hoy cada aplicación interna habla con el proveedor de IA por su cuenta. PLATIA lo hace
bien (presupuestos por tarea, prompts versionados, registro de cada llamada), pero esos
controles viven dentro de PLATIA y no protegen a ninguna otra herramienta. No hay una
respuesta única a cuatro preguntas:

1. **Quién** ha llamado al modelo (aplicación y persona).
2. **Cuánto** ha costado, por aplicación, equipo y persona, y si se ha pasado del límite.
3. **Qué datos** han salido hacia el proveedor (datos personales, secretos, documentos
   clasificados) y si alguien ha intentado manipular al modelo.
4. **Qué pasa** si el proveedor principal cae o un modelo deja de estar disponible.

El gateway es una sola puerta hacia los modelos que responde a las cuatro para cualquier
aplicación, sin que cada una tenga que reimplementarlo (libro, cap. 1).

## 2. Clientes y personas

| Quién | Cómo llega | Qué necesita | Desde |
| --- | --- | --- | --- |
| PLATIA (`ai_service`) | Clave de aplicación por red interna, contrato de Anthropic, siempre en streaming | Las mismas funciones que hoy (salidas estructuradas, razonamiento, herramientas, caché de prompts) y atribución por persona y tarea | G5 |
| Otras aplicaciones internas | Clave de aplicación, contrato de OpenAI o de Anthropic | Una URL y una clave; no saber qué proveedor hay detrás | G3 |
| Personas con herramientas propias (Claude Code, scripts) | Clave personal | Usar los modelos permitidos dentro de su cuota | G10 |
| Administración | Panel de administración | Proveedores, modelos, cuotas, reglas de seguridad, auditoría y costes | G13 |
| Personas usuarias | Portal | Sus claves, su consumo y su seguridad | G14 |

## 3. Requisitos funcionales

| Id | Requisito | Fase |
| --- | --- | --- |
| RF-01 | Aceptar peticiones con el contrato de Anthropic (`POST /v1/messages`) y con el de OpenAI (`POST /v1/chat/completions`) | G3 |
| RF-02 | Con el contrato de Anthropic hacia un modelo de Anthropic, conservar todas las funciones de la petición: herramientas, razonamiento con firma, salidas estructuradas, `effort` y `cache_control` | G3 |
| RF-03 | Respuesta en streaming (SSE) en los dos contratos, con el uso real de tokens al final | G4 |
| RF-04 | Autenticar cada petición con una clave opaca de aplicación (`plgw-app-…`) o personal (`plgw-usr-…`), guardada solo como hash; sin clave válida, 401 | G3, G10 |
| RF-05 | Aceptar la clave también en la cabecera `x-api-key`, porque así la envía el SDK oficial de Anthropic | G3 |
| RF-06 | Atribuir cada llamada a organización, equipo, aplicación y persona final; una aplicación de confianza declara la persona y la tarea con cabeceras `X-Platia-*` | G3, G5 |
| RF-07 | Listar los modelos que cada clave puede usar (`GET /v1/models`) | G3 |
| RF-08 | Elegir el despliegue de cada modelo según salud, peso y enfriamiento tras fallos, con cadenas de reserva que respeten lo que la petición tenía permitido | G6 |
| RF-09 | Calcular el coste de cada llamada con precios editables (USD), incluidos los tokens de caché | G7 |
| RF-10 | Cuotas por organización, equipo, aplicación y persona, por periodo, con aviso al 80 % y bloqueo al 100 % (402) | G7 |
| RF-11 | Límite de peticiones por minuto y de peticiones simultáneas por clave (429) | G7 |
| RF-12 | Detectar intentos de manipulación del modelo (jailbreak), datos personales, secretos y marcas de clasificación en la entrada, con acción configurable: bloquear, avisar, tachar o permitir | G8 |
| RF-13 | Filtrar la salida del modelo, también en streaming, antes de que llegue al cliente | G8 |
| RF-14 | Registrar cada petición en una auditoría que no se puede modificar, encadenada con hash y verificable, con exportación y seudonimización (RGPD) | G3, G9 |
| RF-15 | Devolver en cabeceras de respuesta el modelo usado, el coste, la latencia y el id de petición | G3, G7 |
| RF-16 | Administrar proveedores, modelos, despliegues, precios, cuotas, reglas y aplicaciones desde un panel | G13 |
| RF-17 | Portal para que cada persona vea y gestione sus claves y su consumo, sin ver datos de otros | G14 |
| RF-18 | Avisos por webhook firmado (cuota, bloqueos de seguridad) y métricas | G12 |

## 4. Requisitos no funcionales

| Id | Requisito | Cómo se comprueba |
| --- | --- | --- |
| RNF-01 | **Fallar cerrado**: sin identidad, cuota o política resueltas, la petición se rechaza | Tests de cada etapa con su dependencia caída |
| RNF-02 | Ningún error devuelve detalles internos ni claves de proveedor; el detalle va al log, con las claves tachadas | Tests de errores y de logs |
| RNF-03 | Las claves de proveedor se guardan cifradas en reposo; las de clientes, solo como hash | Revisión del modelo de datos (G2) y tests |
| RNF-04 | MySQL y Redis no son accesibles fuera de la red de datos; Nginx solo publica las rutas que tocan | `docker compose` y CI |
| RNF-05 | El gateway no guarda el texto de los prompts ni de las respuestas en claro; la auditoría guarda hashes y metadatos | Revisión del modelo de datos (G2, G9) |
| RNF-06 | Sobrecoste de latencia del gateway sin detectores pesados: objetivo p95 < 50 ms (sin contar el proveedor) | Se mide en G12; es un objetivo, no un dato |
| RNF-07 | Vuelta atrás en PLATIA sin desplegar código: un interruptor devuelve `ai_service` a la llamada directa | Prueba de G5 con el interruptor encendido y apagado |
| RNF-08 | Toda configuración y secreto por variables de entorno; nada en el código | `.env.example` y revisión |
| RNF-09 | Cada fase con tests, `ruff` y cobertura mínima en la CI, y una prueba en Docker repetible | CI |

## 5. Fuera de alcance (por ahora)

Embeddings, imágenes y audio; búsqueda web y extracción de documentos; RAG propio (PLATIA
ya tiene el suyo); federación MCP; SCIM, device binding, cifrado en cliente y claves
maestras con Shamir; portal de cumplimiento del AI Act. El motivo de cada uno está en
[ADR-005](adr/ADR-005-alcance-y-politica-de-fallo.md).
