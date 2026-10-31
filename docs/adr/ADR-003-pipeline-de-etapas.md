# ADR-003: Pipeline de 8 etapas sobre un contexto compartido

**Fecha:** 2026-10-31 · **Estado:** Aceptado

## Contexto
El libro organiza el motor como un `PipelineRunner` que ejecuta unas 14 etapas en serie
sobre un `PipelineContext` (cap. 3). El endpoint no tiene lógica: construye el contexto y
llama al runner. Muchas de esas etapas cubren funciones que este gateway no tendrá al
principio (ganchos, caché, RAG, recorte de historial).

## Decisión
- Mismo patrón: un contexto por petición, etapas con la firma `async def run(ctx)`, sin
  llamadas entre etapas, y un runner que las ejecuta en orden y para en el primer corte.
- Ocho etapas: `auth`, `security_input`, `filter`, `policy`, `route`, `execute`,
  `security_output` y `audit` (detalle en `docs/arquitectura.md`). Se añade una etapa nueva
  cuando una fase la necesita, con su test.
- `audit` va en un `finally`: se ejecuta también en cortes, errores y desconexiones del
  cliente durante el streaming.
- El streaming usa el mismo pipeline hasta `execute`; a partir de ahí, `security_output`
  trabaja por trozos.

## Alternativas
- **Endpoint con toda la lógica:** más rápido de escribir, imposible de probar por partes y
  de reordenar.
- **Las 14 etapas desde el principio:** etapas vacías o a medias que hay que mantener.
- **Middlewares de FastAPI para los controles:** no ven el cuerpo ya interpretado ni el
  streaming por trozos con comodidad.

## Consecuencias
- Cada etapa declara qué hace si falla su dependencia (ver ADR-005).
- Los tests de una etapa construyen un contexto a mano, sin levantar el resto.
