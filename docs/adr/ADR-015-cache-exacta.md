# ADR-015: Caché exacta por aplicación y persona, cifrada en Redis; sin compresión de prompts

**Fecha:** 2026-11-01 · **Estado:** Aceptado

## Contexto
G11 es la fase opcional de eficiencia. Los capítulos 8 y 9 del libro describen:
- una caché exacta por persona, en Redis y cifrada si se quiere;
- una caché en memoria delante de Redis;
- una caché semántica con embeddings para propósitos "seguros";
- claves de caché de prompts de OpenAI;
- compresión de prompts: espacios, BM25 sobre el historial y resúmenes.

PLATIA ya usa la caché de prompts de Anthropic (`cache_control`), que el gateway conserva
porque reenvía el cuerpo tal cual (ADR-002). Sus tareas (propuestas, análisis de pliegos,
copiloto) casi nunca repiten la misma petición literal, salvo reintentos y consultas
idénticas.

## Decisión

**Caché exacta, desactivada por defecto**
- Cada aplicación decide con `cli set-cache`: segundos que se guarda cada respuesta (0 = sin
  caché, lo de por defecto) y, si quiere, para qué propósitos.
- El cliente puede saltársela en una petición con `X-Platia-Cache: no`. La respuesta dice
  `X-Platia-Cache: miss` o `hit` cuando la caché está activa.

**Qué se guarda y quién lo ve**
- La clave cubre la organización, la aplicación y la persona final, además del propósito,
  el contrato, las cabeceras que se reenvían al proveedor y el cuerpo ya filtrado. Una
  respuesta nunca se sirve a otra persona.
- Solo sin streaming, y solo respuestas terminadas con normalidad (`end_turn`,
  `stop_sequence` o `stop`). No se guardan las cortadas por `max_tokens`, las que piden usar
  una herramienta ni las de más de `CACHE_MAX_BYTES`.
- En Redis la respuesta va cifrada con AES-256-GCM (`app/crypto.py`), con la clave como dato
  asociado. Sin `ENCRYPTION_KEY` no se guarda nada. RNF-05 prohíbe guardar el texto en
  claro, y así se cumple también en la caché.

**Dónde va en el pipeline**
- La etapa `cache` va después de `policy` y antes de `quota`:
  - lo que la política ya no permite (un modelo retirado de la lista, una entrada
    bloqueada) no sale de la caché;
  - un acierto no gasta cuota ni límites de ritmo, porque no cuesta nada.
- En cada acierto se vuelve a aplicar el filtro de salida con la política vigente: se
  guarda la respuesta del proveedor, no la filtrada.
- Un acierto queda en la auditoría como una petición más, con proveedor `cache`, 0 intentos,
  0 tokens y coste 0. Así `cli usage` y la auditoría cuentan los aciertos sin cambiar la
  cadena de G9 (no hace falta otra `hash_version`).

**Si Redis falla**: se sigue sin caché. No es un control sino un ahorro, así que no se falla
cerrado.

**Vaciarla**: `cli cache-clear --org … [--app …]`, por ejemplo tras cambiar un prompt o un
modelo.

## Lo que no entra ahora
- **Caché semántica**: con los documentos y propuestas de PLATIA, una pregunta parecida no
  merece la misma respuesta (el libro avisa del cruce entre secciones similares). Además
  necesita embeddings y otra base de vectores.
- **Compresión de prompts**: cambiar el texto rompería la caché de prompts de Anthropic, que
  ahorra más (hasta el 90 % de la entrada repetida), y el reenvío tal cual del cuerpo
  (ADR-002). Recortar el historial es decisión de quien lo construye: PLATIA ya lo hace con
  sus presupuestos por tarea.
- **Caché en memoria delante de Redis**: con varias réplicas del gateway daría respuestas
  distintas a cada una. Redis ya responde en milisegundos frente a segundos del modelo.
- **Respuestas en streaming desde la caché**: habría que inventar los eventos SSE. Se hará si
  algún caso lo pide.

## Consecuencias
- Cero coste y cero riesgo para quien no la activa.
- Una aplicación con la caché activa puede recibir la misma respuesta a la misma pregunta
  aunque el modelo hubiera contestado otra cosa (temperatura). Por eso se elige por
  propósito.
