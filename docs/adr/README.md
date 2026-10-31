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
