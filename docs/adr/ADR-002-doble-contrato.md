# ADR-002: Contratos de Anthropic y de OpenAI, con paso directo entre familias iguales

**Fecha:** 2026-10-31 · **Estado:** Aceptado

## Contexto
El libro toma como contrato principal el de OpenAI (`/v1/chat/completions`) y ofrece
también el de Anthropic (`/v1/messages`) (cap. 1). PLATIA usa funciones propias de la API de
Anthropic: salidas estructuradas (`output_config.format`), `effort`, bloques de razonamiento
con firma que se devuelven en el turno siguiente, herramientas y `cache_control`. Una
traducción a un formato común las pierde o las deforma.

## Decisión
- El gateway expone los dos contratos y los dos convergen en el mismo pipeline.
- Internamente hay una petición normalizada para lo que necesitan los controles (mensajes,
  modelo, herramientas, `stream`, límites de tokens).
- Si la petición y el proveedor son de la misma familia, se reenvía el **cuerpo original**
  después de los controles (con el modelo resuelto por el gateway y, si toca, el texto
  tachado), en lugar de reconstruirlo desde la forma normalizada.
- Entre familias distintas solo se traduce lo que ambas admiten. Si la petición usa una
  función que el destino no tiene, ese destino no es elegible: no hay degradación silenciosa,
  tampoco en las cadenas de reserva.
- La clave se acepta en `Authorization: Bearer` y en `x-api-key`, como envía el SDK de
  Anthropic. Las cabeceras `anthropic-version` y `anthropic-beta` se reenvían a Anthropic.

## Alternativas
- **Solo OpenAI:** PLATIA tendría que cambiar su cliente y perdería funciones.
- **Solo Anthropic:** cerraría la puerta a clientes y proveedores compatibles con OpenAI.
- **Traducir siempre a un formato propio:** cada función nueva del proveedor obligaría a
  tocar el gateway antes de poder usarla.

## Consecuencias
- Hay que probar cada contrato por operación (mensajes, herramientas, streaming, uso,
  errores), no dar la compatibilidad por supuesta.
- Los controles de contenido (G8) trabajan sobre la forma normalizada y aplican los
  cambios (tachados) también al cuerpo original.
