# ADR-008: Streaming SSE con paso directo de eventos y auditoría al cerrar

**Fecha:** 2026-10-31 · **Estado:** Aceptado

## Contexto
`ai_service` de PLATIA llama siempre con `messages.stream` del SDK de Anthropic, así que
PLATIA no puede pasar por el gateway sin streaming (ADR-001). En streaming el código HTTP
se envía antes de saber cómo acaba la respuesta, el uso de tokens llega al final y el
cliente puede irse a mitad (cap. 4).

## Decisión
- **Abrir antes de responder.** El gateway abre la respuesta del proveedor y comprueba su
  código antes de enviar nada. Un 400, 429 o 529 del proveedor llega al cliente como un
  error HTTP normal, igual que sin streaming.
- **Misma familia: eventos tal cual.** Cada bloque SSE se reenvía byte a byte en cuanto
  llega (razonamiento, firmas, herramientas y tipos de evento futuros incluidos). El
  gateway solo lee `message_start` y `message_delta` (Anthropic) o el fragmento con
  `usage` (OpenAI) para la auditoría.
- **OpenAI sin uso pedido.** El gateway pide siempre `stream_options.include_usage` al
  proveedor; si el cliente no lo pidió, ese fragmento final no se le reenvía.
- **Familias distintas: solo texto**, con los eventos reconstruidos en el contrato del
  cliente (ADR-007).
- **Errores a mitad.** Si el proveedor envía un error, se reenvía. Si corta, deja de
  enviar datos (`PROVIDER_TIMEOUT_SECONDS` entre fragmentos) o se pasa de
  `STREAM_MAX_SECONDS`, el gateway añade un evento de error en el formato del cliente.
- **Auditoría en el `finally` del generador**, protegida frente a la cancelación: se
  registra también cuando el cliente se desconecta (`client_disconnected`, 499), con los
  tokens leídos hasta ese momento.
- **Sin búfer en Nginx** (`proxy_buffering off` y `X-Accel-Buffering: no`).
- Las cabeceras de coste y latencia no existen en streaming, porque se envían antes de
  conocerlos. El coste queda en la auditoría.

## Alternativas
- **Esperar a la respuesta entera y simular el streaming:** rompe el primer token rápido,
  que es la razón del streaming.
- **Reconstruir siempre los eventos desde una forma común:** perdería los bloques de
  razonamiento con firma que PLATIA devuelve en el turno siguiente.
- **Cerrar la auditoría en una tarea aparte:** se perdería si el proceso se reinicia
  justo después. Hacerlo en el `finally` es más simple y basta mientras no haya cola (G9).

## Consecuencias
- Un error a mitad no puede cambiar el código HTTP (ya es 200). El cliente lo ve como
  evento y la auditoría lo guarda como `error`.
- Sin `message_delta` (el cliente se fue antes), los tokens de salida quedan en lo último
  que informó el proveedor.
