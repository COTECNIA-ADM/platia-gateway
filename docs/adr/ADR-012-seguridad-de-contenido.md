# ADR-012: Seguridad de contenido con detectores locales y una política por organización y aplicación

**Fecha:** 2026-11-01 · **Estado:** Aceptado

## Contexto
RF-12 pide detectar en la entrada intentos de manipulación del modelo (jailbreak), datos
personales, secretos y marcas de clasificación, con una acción configurable. RF-13 pide
filtrar también la salida, incluido el streaming. Los capítulos 19 y 20 del libro describen
detectores por patrones con pesos, normalización Unicode contra ofuscaciones, tachado en
cascada de secretos y datos personales, clasificadores por categorías en base de datos,
detección de fugas en la salida, filtrado por trozos y marcas de agua invisibles.

El primer cliente, PLATIA, trabaja con pliegos y contratos: llevan correos, teléfonos e
IBAN por necesidad, y ya tiene sus propias defensas (`guardrails` y prompts que tratan los
documentos como datos). La política por defecto no puede romperle el trabajo.

## Decisión

**Detectores locales y deterministas** (`app/content.py`)
- Expresiones regulares con validación, sin llamar a otro modelo: milisegundos, sin coste y
  el mismo resultado cada vez (RNF-06).
- Secretos: claves de Anthropic, OpenAI, AWS, GitHub, Google y Slack, las `plgw-` del
  propio gateway, JWT, claves privadas y credenciales escritas como `password=...` (de
  estas solo se tacha el valor).
- Datos personales: correo, DNI y NIE con su letra de control, IBAN con su dígito de
  control, tarjeta con Luhn y teléfono español. La validación quita la mayoría de los
  falsos positivos (importes, expedientes, referencias).
- Marcas de clasificación en mayúsculas (CONFIDENCIAL, DIFUSIÓN LIMITADA, USO INTERNO...):
  en minúsculas suelen ser texto normal de un contrato.
- Manipulación: frases típicas en español y en inglés con un peso. Por categoría cuenta el
  mayor peso; se suman y, desde 60, la petición se marca. Tres señales débiles juntas
  multiplican por 1,5. Más de tres mensajes de asistente seguidos (técnica de *prefill*)
  suman 40. Antes de buscar, el texto se normaliza (NFKC) y se le quitan los caracteres
  invisibles. Solo se puntúa el texto de la persona y de las herramientas, no el del sistema.
- No se leen imágenes, documentos en base64 ni bloques de razonamiento (llevan firma: no se
  pueden tocar sin invalidar la petición).

**Cuatro acciones y una política por casillas**
- `block` (403, `permission_error`), `redact` (se tacha con `[OCULTO:tipo]` y sigue), `log`
  (solo se anota) y `off`.
- Tabla `content_policies`: una fila por organización o aplicación, sentido (entrada o
  salida) y categoría. La de la aplicación manda sobre la de la organización, y esta sobre
  la de por defecto. `cli set-content-policy` y `content-policies`.
- Por defecto: manipulación `block`, secretos de entrada `redact`, datos personales y marcas
  `log`, y en la salida todo `log`. Así PLATIA funciona como antes y los secretos no salen
  hacia el proveedor por un descuido.
- La manipulación y las marcas no se pueden tachar (no tiene sentido); en la salida solo se
  revisan secretos y datos personales.

**Tres etapas** (ADR-003): `security_input` (manipulación, marcas y secretos) y `filter`
(datos personales) antes de `policy`; `security_output` tras `execute` en las respuestas sin
streaming.

**Streaming**
- Con `log` (lo de por defecto) los eventos salen tal cual, byte a byte (ADR-008), y el texto
  se revisa al terminar.
- Con `redact` o `block`, el texto de cada bloque pasa por `TextGuard`: retiene la cola (64
  caracteres hasta el último espacio, y entero lo que no tiene espacios) para que ningún
  dato llegue partido y sin tachar, y se reescriben los eventos de texto. Con `block` se
  corta la respuesta con un evento de error antes de enviar el dato.
- Funciona en los cuatro caminos (mismo contrato o traducido), porque se aplica a los
  eventos ya en el contrato del cliente.

**Trazabilidad sin guardar el dato**
- Cada hallazgo va a `audit_logs.security_flags`: sentido, categoría, acción, recuento por
  tipo y, en manipulación, la puntuación. Nunca el texto encontrado (RNF-05).
- Log `content.flagged` con lo mismo y cabecera `X-Platia-Content-Flags`
  (`input.pii=log,input.secret=redact`). En streaming la cabecera solo puede llevar lo de la
  entrada; lo de la salida queda en la auditoría.
- Si la política no se puede leer de MySQL, la petición se rechaza (503): fallar cerrado.

## Lo que no entra ahora
- **Patrones propios de cada organización** (códigos de cliente, nombres de proyecto): se
  añadirán como filas de la política cuando haya un caso concreto.
- **Clasificador con un modelo** o por categorías temáticas (violencia, etc.): cuesta una
  llamada más por petición y el proveedor ya modera lo suyo.
- **Detección de fugas del prompt de sistema** en la salida y **marcas de agua invisibles**:
  el gateway no conoce el prompt de PLATIA como tal, y las marcas cambian el texto que
  recibe el cliente.
- **Escalado por reincidencia** (avisos, bajada de límites, suspensión): con G12 y G13.
- **Datos personales en la salida tachados por defecto**: rompería el paso directo del
  streaming y PLATIA necesita ver los datos de sus documentos.

## Consecuencias
- Una frase de manipulación dentro de un documento que la persona sube también bloquea la
  petición. Si PLATIA la necesitara, su aplicación puede bajar la manipulación a `log`.
- Las expresiones regulares tienen falsos negativos (un dato escrito de forma rara) y algún
  falso positivo; por eso los datos personales están en `log` por defecto y cada
  organización decide.
- Con `redact` en la salida, el streaming ya no es byte a byte: los trozos de texto se
  reagrupan y llegan con un poco de retraso.
