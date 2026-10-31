# ADR-001: Gateway como servicio y repositorio aparte que envuelve el `ai_service` de PLATIA

**Fecha:** 2026-10-31 · **Estado:** Aceptado

## Contexto
PLATIA ya tiene una pasarela de IA (`ai_service`) sin estado, y su backend gobierna
prompts, modelo por tarea, presupuestos, guardarraíles y ROI (ADR-011 de PLATIA). Se quiere
un gateway corporativo que sirva también a otras aplicaciones y a personas, siguiendo
*Anatomía de una Plataforma IA Corporativa*.

## Decisión
- El gateway vive en su propio repositorio (`cotecnia-adm/platia-gateway`), con su CI, sus
  versiones y su despliegue.
- **Envuelve** a `ai_service`, no lo sustituye: `ai_service` sigue haciendo embeddings y
  Qdrant y solo cambia a quién llama para generar texto (URL base y clave del SDK).
- PLATIA conserva lo que es de su negocio (prompts, modelo y presupuesto por tarea, RAG,
  ROI). El gateway hace lo corporativo (claves, cuotas transversales, seguridad de
  contenido, reserva entre proveedores, auditoría encadenada).
- El cambio en PLATIA va detrás de `LLM_GATEWAY_ENABLED`, para volver atrás sin desplegar.

## Alternativas
- **Carpeta dentro de `platia-dev`:** un solo repo, pero el gateway sirve a más clientes y
  las convenciones de PLATIA (Flask, tres esquemas, `platform_guard`) no le encajan.
- **Sustituir `ai_service` por el gateway:** obligaría a mover embeddings y Qdrant, que no
  son del gateway, y a reescribir el cliente de PLATIA de una vez.
- **Llevar el gobierno de PLATIA al gateway:** duplicaría usuarios y permisos de PLATIA en
  otro servicio.

## Consecuencias
- Algunos controles existen en los dos sitios (coste, guardarraíles). Se acepta: miran
  cosas distintas. El coste se compara cada noche desde G5 hasta que coincida.
- Probar los dos juntos exige una red Docker compartida (`llm_net`, G5).
