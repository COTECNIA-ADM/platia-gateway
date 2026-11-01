# Registros de decisiones de arquitectura (ADR)

Cada ADR cuenta en menos de una página qué se decidió, por qué y qué alternativas se
descartaron. Para cambiar una decisión se escribe un ADR nuevo que sustituya al anterior.

| ADR | Decisión | Estado |
| --- | --- | --- |
| [001](ADR-001-servicio-aparte.md) | Gateway como servicio y repositorio aparte que envuelve el `ai_service` de PLATIA | Aceptado |
| [002](ADR-002-doble-contrato.md) | Contratos de Anthropic y de OpenAI, con paso directo entre familias iguales | Aceptado |
| [003](ADR-003-pipeline-de-etapas.md) | Pipeline de 8 etapas sobre un contexto compartido | Aceptado |
| [004](ADR-004-identidad.md) | Organización, equipo, persona y aplicación con claves opacas; sin dispositivos | Aceptado |
| [005](ADR-005-alcance-y-politica-de-fallo.md) | Alcance por bloques y fallar cerrado por defecto | Aceptado |
| [006](ADR-006-modelo-de-datos.md) | Modelo de datos, claves con hash y secretos cifrados con AES-256-GCM | Aceptado |
| [007](ADR-007-contrato-y-pipeline-minimo.md) | Contrato y pipeline mínimo: cuerpo sin esquema, traducción solo de texto, auditoría en dos pasos | Aceptado |
| [008](ADR-008-streaming.md) | Streaming SSE con paso directo de eventos y auditoría al cerrar | Aceptado |
| [009](ADR-009-conexion-platia.md) | Conexión con PLATIA por red interna, propósito libre, correlación por `X-Request-ID` y sin salto a la llamada directa | Aceptado |
| [010](ADR-010-reserva-y-enfriamiento.md) | Reserva entre despliegues y modelos permitidos, con enfriamiento pasivo en Redis | Aceptado |
| [011](ADR-011-cuotas-y-limites.md) | Cuotas y límites de ritmo con contadores en Redis, ventanas naturales y la auditoría como respaldo | Aceptado |
| [012](ADR-012-seguridad-de-contenido.md) | Seguridad de contenido con detectores locales y política por organización y aplicación | Aceptado |
| [013](ADR-013-auditoria-verificable.md) | Auditoría verificable: sellado posterior en cadena SHA-256, disparadores y seudonimización por huella | Aceptado |
