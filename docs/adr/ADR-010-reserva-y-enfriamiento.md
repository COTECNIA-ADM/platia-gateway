# ADR-010: Reserva entre despliegues y modelos, con enfriamiento pasivo en Redis

**Fecha:** 2026-10-31 · **Estado:** Aceptado

## Contexto
Hasta G5 cada petición iba a un solo despliegue: si el proveedor respondía 429, 503 o no
contestaba, el cliente recibía el error. Desde G5 PLATIA no tiene reserva propia mientras
usa el gateway (su ADR-019), así que la reserva tiene que estar aquí (RF-08). Los capítulos
5 y 6 del libro describen la selección por propósito, varias estrategias de reparto
(peso, latencia, menos ocupado), comprobaciones activas de salud, enfriamiento tras fallos
y cadenas de escalado entre modelos.

## Decisión

**Una lista ordenada de destinos por petición**
- `route` prepara la lista y `execute` la recorre; ninguna llama a la otra (ADR-003).
- Primero, los despliegues del modelo pedido: prioridad más baja antes y, dentro de cada
  prioridad, sorteo por peso sin repetir. Peso 0 = solo como reserva dentro de su prioridad.
- Después, los de cada modelo de su cadena de reserva (`llm_models.fallback_models`), en
  orden. Solo un nivel: la reserva de una reserva no se sigue, para que la cadena sea la
  que se ve en `cli show`.
- Un modelo de reserva solo entra si la aplicación lo tiene permitido, y un destino de la
  otra familia solo si la petición se traduce sin perder nada (ADR-002). La reserva nunca
  da a una aplicación algo que no podía pedir ni degrada la petición sin avisar.
- Como mucho `ROUTE_MAX_ATTEMPTS` destinos (3), y no se empieza un intento nuevo pasados
  `ROUTE_RETRY_WINDOW_SECONDS` (120): Nginx corta a los 330 s sin datos.

**Qué errores cambian de destino**
- Sí: 429, 529, 503, 5xx, tiempo agotado, sin conexión, respuesta ilegible y 401/403/404
  del proveedor (credencial o modelo mal configurados en ese destino).
- No: 400, 413 y 422. Son de la petición y cualquier otro destino la rechazaría igual.
- Lo marca el adaptador (`GatewayError.fallback`), no el texto del error.
- En streaming solo antes de responder: el código del proveedor se comprueba al abrir
  (ADR-008), así que un 503 inicial también cambia de destino. Un corte a mitad no se
  reintenta, porque el cliente ya ha recibido parte del texto.

**Enfriamiento pasivo, compartido en Redis**
- Se aprende de las llamadas reales, sin pings: un ping a un modelo de pago cuesta dinero.
- `COOLDOWN_FAILURES` (3) fallos seguidos en `COOLDOWN_WINDOW_SECONDS` (60) enfrían el
  despliegue `COOLDOWN_SECONDS` (60). Un 429/529 lo enfría a la primera, el tiempo que pide
  `Retry-After` (hasta `COOLDOWN_MAX_SECONDS`). Un éxito reinicia la cuenta.
- Un despliegue enfriado pasa al final de la lista, no se descarta: con un solo destino
  (el caso de PLATIA hoy), un 429 aislado no deja el modelo sin servicio un minuto.
- Claves con caducidad (`gw:cooldown:<id>`, `gw:failures:<id>`), compartidas por todos
  los procesos del gateway. Si Redis no responde, se sigue sin enfriamiento y se avisa en el
  log: es una optimización, no un control. Las cuotas (G7) sí fallarán cerradas (ADR-005).

**Trazabilidad**
- `audit_logs.attempts` guarda cuántos destinos se probaron. `model_key`, `deployment_id`
  y `provider_slug` son los del que respondió (o del último que falló), y `requested_model`
  el que pidió el cliente. El coste se calcula con el precio del modelo que respondió.
- Las respuestas llevan `X-Platia-Attempts` y `X-Platia-Model-Used` con el modelo real.
- Cada cambio de destino deja `route.fallback` en el log y cada enfriamiento,
  `deployment.cooled`.

## Lo que no entra ahora
- **Selección de modelo por propósito**: PLATIA, el único cliente, elige el modelo de cada
  tarea en su propia configuración. Se añadirá cuando una aplicación quiera delegar esa
  elección en el gateway.
- **Estrategias por latencia o por ocupación**: con uno o dos destinos por modelo no
  cambian nada; el peso basta. Los datos de latencia ya están en la auditoría.
- **Comprobaciones activas de salud** con llamadas de prueba: tienen coste y la salud
  pasiva detecta lo mismo con el tráfico real.
- **Límite de tokens por minuto de cada despliegue**: va con las cuotas (G7).
- **Tabla propia de escaladas**: `attempts` y el log bastan hasta la auditoría de G9.

## Consecuencias
- Si el proveedor principal cae, PLATIA sigue funcionando sin cambiar nada, siempre que
  el modelo tenga otro despliegue o un modelo de reserva.
- Una petición con reserva tarda más: suma el tiempo del intento fallido.
- Una reserva a un modelo más caro sube el coste sin que el cliente lo pida. Por eso solo
  se usan reservas configuradas a propósito, y el coste real queda en la auditoría.
