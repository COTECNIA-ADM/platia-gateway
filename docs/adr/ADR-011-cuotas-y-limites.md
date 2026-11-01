# ADR-011: Cuotas de gasto y límites de ritmo con contadores en Redis y la auditoría como respaldo

**Fecha:** 2026-10-31 · **Estado:** Aceptado

## Contexto
Hasta G6 el gateway calculaba el coste de cada llamada, pero no lo limitaba: una aplicación
con un bucle mal hecho podía gastar sin tope. RF-10 pide cuotas por organización, equipo,
aplicación y persona, con aviso al 80 % y bloqueo al 100 % (402), y RF-11, límites de
peticiones por minuto y de peticiones simultáneas (429). Los capítulos 13 a 15 del libro
describen presupuestos por periodo con conciliación entre coste estimado y real, límites
por minuto con contadores atómicos en Redis, un contador de peticiones en paralelo con
caducidad de seguridad y un *token bucket* por despliegue.

## Decisión

**Una tabla de límites, no columnas en cada entidad**
- `usage_limits`: ámbito (`organization`, `team`, `application`, `end_user`), id del ámbito,
  métrica (`cost_usd`, `requests`, `tokens`, `concurrent`), periodo (`minute`, `day`,
  `month`; las simultáneas no llevan), valor y porcentaje de aviso (80 por defecto).
- En `end_user`, sin persona concreta el límite vale para **cada** persona de la
  aplicación por separado; con persona (`subject`), solo para ella y sustituye al general.
  La persona es la que declara una aplicación de confianza con `X-Platia-User-Id`.
- Se aplican todos los que tocan a la petición; el primero agotado la corta.
- Se configuran con `cli set-limit`, `remove-limit` y `limits` hasta el panel (G13).

**Una etapa propia, `quota`, entre `policy` y `route`** (ADR-003)
- Antes de gastar nada: una petición rechazada no llega al proveedor ni ocupa un destino.
- 402 (`billing_error`) si se agota una cuota de día o de mes: reintentar no sirve hasta
  que se renueve, y la cabecera `X-Platia-Quota-Reset` dice cuándo.
- 429 (`rate_limit_error`) con `Retry-After` en los límites por minuto y de simultáneas.
- Los rechazos quedan en la auditoría como `blocked`, con su código.

**Ventanas fijas naturales en UTC, no deslizantes**
- Minuto, día y mes de calendario. La clave del contador lleva el inicio de la ventana
  (`gw:q:<métrica>:<ámbito>:<id>[:<persona>]:<ventana>`) y caduca poco después del final,
  así que no hay que borrar nada al cambiar de periodo.
- Un presupuesto mensual se entiende (y se cruza con la factura) por mes natural; por
  minuto, una ventana fija puede dejar pasar hasta el doble en el cambio de minuto, que con
  los volúmenes de PLATIA no importa. Se cambiará si un cliente lo necesita.
- UTC también para día y mes: es lo que usa la auditoría. En España, el día empieza a las
  01:00 o 02:00 de la hora local.

**Admitir mientras no se haya llegado al límite**
- Un guion Lua comprueba todos los contadores de la petición y, solo si ninguno está al
  tope, los sube todos (todo o nada, sin carreras entre procesos). Adapta el INCR con
  caducidad del libro (`machinebooks.ai/ES/aigateway/cap-15/block-13.lua`).
- Al admitir suben 1 petición, 1 simultánea y los **tokens estimados** (tamaño del texto
  entre 4 más `max_tokens`). Al terminar (`quota.settle`) se corrigen los tokens con el uso
  real, se suma el **coste real** y se libera la simultánea. Se ajusta también en errores,
  en cortes y en streaming (en el `finally` del generador, junto a la auditoría).
- El gasto no se reserva por adelantado: reservar `max_tokens` al precio de salida
  bloquearía peticiones que luego cuestan céntimos. La consecuencia es que la última
  petición admitida puede pasarse del límite; el exceso está acotado por lo que cuesten
  las peticiones en vuelo, y se puede limitar con `concurrent`.
- El gasto se cuenta en millonésimas de dólar (enteros) para no acumular redondeos.

**La auditoría es la fuente de verdad**
- Si falta un contador de día o de mes (Redis reiniciado, límite recién creado), se
  reconstruye sumando `audit_logs` de la ventana (sin las bloqueadas) antes de comprobarlo.
  Los de minuto empiezan en cero. Se añade un índice `(team_id, created_at)` para que la
  suma por equipo no recorra la tabla.
- `cli limits` enseña el consumo del periodo según la auditoría, no según Redis.

**Fallar cerrado** (ADR-005)
- Si la petición tiene límites y Redis no responde (o no está configurado), 503 y no sale.
- Sin límites que comprobar no se consulta Redis: no hay nada que decidir. En producción
  conviene tener siempre al menos el presupuesto mensual de la organización.
- Un fallo al ajustar después de responder no cambia la respuesta: va al log
  (`quota.settle_failed`) y la reconstrucción desde la auditoría lo corrige al cambiar de periodo
  o al perderse el contador.

**Avisos y cabeceras**
- Al pasar el porcentaje de aviso, y al agotar el límite, una línea en el log
  (`quota.alert`, `quota.exhausted`) una sola vez por ventana. El aviso por correo o webhook
  llega con la telemetría (G12).
- Las respuestas admitidas llevan `X-Platia-Budget-Remaining-Usd` (lo que quedaba antes de
  esta petición del presupuesto más justo) y `X-Platia-RateLimit-Limit` y `-Remaining` (del
  límite de peticiones por minuto más estricto).

## Lo que no entra ahora
- **Límite por clave**: RF-11 habla de clave; aquí el ámbito más fino es la aplicación,
  que en la práctica tiene una clave activa (dos solo durante una rotación).
- **Token bucket por despliegue**: el límite real de cada cuenta de proveedor ya lo
  marca su 429, que enfría el despliegue y activa la reserva (ADR-010).
- **Presupuesto por sesión o por tarea**: PLATIA ya controla el gasto de cada tarea en su
  propia configuración (su ADR-011).
- **Cuotas semanales** y periodos configurables: día y mes cubren lo que se ha pedido.
- **Caché de la lista de límites**: se lee de MySQL en cada petición (una consulta por
  índice). Se medirá en G12.

## Consecuencias
- Una organización puede fijar un techo de gasto y repartirlo entre equipos, aplicaciones
  y personas sin tocar el código de PLATIA.
- Un SDK que reciba 429 reintenta solo (respeta `Retry-After`); con 402 no reintenta, que
  es lo que se quiere.
- Con límites, Redis pasa a ser imprescindible: si cae, las aplicaciones con límites dejan
  de recibir respuesta hasta que vuelva.
