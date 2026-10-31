# ADR-005: Alcance por bloques y fallar cerrado por defecto

**Fecha:** 2026-10-31 · **Estado:** Aceptado

## Contexto
El libro describe una plataforma de unos 590 endpoints y 127 tablas. Su código compañero
son fragmentos didácticos, no una aplicación ejecutable. Además, su caso mezcla ramas que
fallan abiertas (siguen si una dependencia cae) y cerradas (cap. 3).

## Decisión
**Alcance.** Primero los bloques A y B del plan (G0–G9): contrato, streaming, conexión con
PLATIA, routing con reserva, cuotas, seguridad de contenido y auditoría. Después, si el uso
lo pide, identidad de personas, panel, portal, telemetría y producción (G10–G15).

Fuera por ahora, con su motivo:

| Capítulos | Qué | Motivo |
| --- | --- | --- |
| 8–9 | Caché semántica y compresión | Riesgo de servir respuestas de otro contexto; PLATIA ya usa la caché de prompts del proveedor. La caché exacta es opcional (G11) |
| 10–11 | Embeddings, imágenes, audio, búsqueda web, extracción de documentos | Ningún cliente lo necesita hoy; PLATIA hace sus embeddings en local |
| 12 | Cola y RAG propios | PLATIA ya tiene ambos, con control de acceso por documento |
| 16–18 (parte) | SCIM, device binding, cifrado en cliente, claves maestras con Shamir | Sin aplicación de escritorio ni proveedor de identidad conectado |
| 23, 31 | Portal de cumplimiento del AI Act | Se valorará con el panel (G13) |
| 25–26 | Federación MCP | Ningún cliente lo necesita hoy |

**Política de fallo.** Por defecto, si una etapa no puede decidir, la petición se rechaza:

| Etapa | Si falla su dependencia |
| --- | --- |
| `auth`, `policy` | Se rechaza (401, 402, 403, 429) |
| `security_input`, `filter` | Se usan las reglas por defecto en memoria, se anota el modo degradado y se sigue; nunca se salta el control |
| `route`, `execute` | Siguiente destino elegible; si no queda ninguno, 502 o 503 |
| `security_output` | Se corta la respuesta |
| `audit` | Antes de llamar al proveedor se registra la petición; si no se puede registrar, se rechaza. El cierre (tokens, coste) se intenta siempre y se concilia si falla. Se detalla en G9 |

## Alternativas
- **Construir los 34 capítulos:** meses sin nada usable y mucho código sin cliente.
- **Fallar abierto como en partes del caso del libro:** más disponibilidad, pero un fallo de
  Redis o de la BD dejaría pasar peticiones sin control.

## Consecuencias
- Si cae Redis, el gateway deja de atender (las cuotas no se pueden comprobar). Se acepta:
  Redis es local y vigilado; la alternativa es gasto o fugas sin control.
- Cada etapa nueva declara su comportamiento ante fallo en esta tabla.
