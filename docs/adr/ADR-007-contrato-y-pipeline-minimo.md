# ADR-007: Contrato y pipeline mínimo sin streaming

**Fecha:** 2026-10-31 · **Estado:** Aceptado

## Contexto
G3 es la primera fase que llama a un modelo. Hay que decidir cómo se reenvía el cuerpo,
qué se traduce entre familias, qué se registra y qué ve el cliente cuando algo falla,
antes de añadir streaming (G4), reserva entre proveedores (G6) y cuotas (G7). El libro
traduce siempre a una forma común con adaptadores por proveedor (cap. 7).

## Decisión
- **Cuerpo sin esquema.** Las rutas leen el JSON a mano y solo comprueban `model`,
  `messages` y `stream`. Entre familias iguales el cuerpo se reenvía entero con el modelo
  del proveedor: lo nuevo de la API de Anthropic funciona sin tocar el gateway (ADR-002).
- **Traducción solo de texto.** Entre familias distintas se traducen sistema, mensajes de
  texto, `max_tokens`, `temperature` (0-1), `top_p` y paradas. Cualquier otra cosa
  (herramientas, imágenes, razonamiento, formato de salida) hace que ese destino no sea
  elegible: 400 con el motivo, nunca una respuesta peor sin avisar.
- **Identidad en una consulta.** Clave, aplicación, equipo y organización se comprueban
  juntos; cualquier fallo da el mismo 401. Las claves personales (`plgw-usr-`) se
  rechazan hasta G10.
- **Auditoría en dos pasos.** Fila `pending` antes de llamar al proveedor (si no se puede
  escribir, 503 y no sale nada) y cierre con tokens y coste. Los rechazos posteriores a la
  identificación se registran como `blocked` (403) o `error`; los 401 solo van al log, para
  que una clave falsa no pueda llenar la tabla.
- **Coste** con el precio vigente del modelo (`model_prices`); sin precio, 0 y se registran
  los tokens igualmente.
- **Errores** con la forma de cada contrato y los tipos de Anthropic. Del proveedor se
  reenvía, limpio, solo el mensaje de un 400 (es un error de la petición); un 401/403/404
  del proveedor es un fallo de configuración nuestro (502), y 429/529 se convierten en 503
  para que los SDK reintenten.
- **Alta por línea de comandos** (`python -m app.cli`) hasta que llegue el panel (G13). La
  clave de un proveedor se lee de una variable de entorno o de la entrada, nunca de un
  argumento.

## Alternativas
- **Esquema Pydantic completo de cada contrato:** rechazaría campos nuevos de los
  proveedores y obligaría a mantener dos copias de su API.
- **Traducir herramientas ya en G3:** solo sirve cuando hay reserva entre familias (G6);
  hasta entonces, cada modelo tiene su proveedor natural.
- **Auditar también los 401:** daría visibilidad de ataques, pero cualquiera sin clave
  podría escribir en la base. Se cubrirá con el registro de eventos de seguridad (G8).

## Consecuencias
- El cliente ve los mismos errores que vería con el proveedor directo, con el gateway como
  origen de los 401/403/503 propios.
- Un modelo de otra familia solo sirve para texto mientras no se amplíe la traducción.
