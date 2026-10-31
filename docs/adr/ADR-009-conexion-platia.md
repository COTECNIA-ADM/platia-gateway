# ADR-009: Conexión con PLATIA por red interna, con tarea y persona en cada llamada

**Fecha:** 2026-10-31 · **Estado:** Aceptado

## Contexto
PLATIA es la primera aplicación que usa el gateway (cap. 26). Su `ai_service` llama a
Claude con el SDK de Anthropic, siempre en streaming, y PLATIA ya lleva su propio registro
de uso (`llm_usage_log`) y sus presupuestos por tarea. La conexión tiene que poder
deshacerse sin desplegar código (RNF-07) y no debe abrir a PLATIA nada más que el gateway.
En PLATIA la decisión complementaria es su ADR-019.

## Decisión

**Red compartida con nombre fijo**
- El `docker-compose.yml` del gateway crea la red `platia_llm_net` y une a ella el
  gateway con el alias `platia-gateway`. PLATIA la declara como externa y solo une su
  `ai_service`. La llamada no pasa por Nginx ni sale del servidor; MySQL y Redis del
  gateway siguen solo en `data_net`.
- Hay que levantar el gateway antes que PLATIA cuando el interruptor está encendido.

**Aplicación de confianza con propósito libre**
- PLATIA se da de alta como aplicación `platia`, de confianza (puede declarar la persona
  con `X-Platia-User-Id`) y con el comodín `*` en `allowed_purposes`: el catálogo de tareas
  y su modelo los gobierna PLATIA, y el gateway registra la tarea de cada llamada. El
  comodín solo se usa para aplicaciones que gobiernan sus propias tareas.
- `scripts/alta_platia.sh` da de alta organización, equipo, aplicación, proveedor, los tres
  modelos de PLATIA con los mismos precios que su tabla de modelos y una clave. Se puede
  repetir (`cli --exists-ok`).

**Correlación**
- `ai_service` envía el `correlation_id` de PLATIA como `X-Request-ID`. Las llamadas de una
  misma tarea comparten ese id en los dos registros, y `cli usage --request-id` resume la
  auditoría del gateway para compararla con `llm_usage_log`.

**Fallar cerrado**
- Si el gateway no responde, PLATIA no salta a la llamada directa ni a su proveedor de
  reserva: la llamada falla. Volver a la llamada directa es una decisión manual (apagar el
  interruptor), porque saltarse el gateway deja la llamada sin auditoría central.

## Consecuencias
- PLATIA depende del gateway para toda su IA generativa mientras el interruptor está
  encendido; la reserva entre proveedores pasa al gateway (G6).
- Los precios están en dos sitios hasta que PLATIA los lea del gateway; si difieren, el
  coste de los dos registros no coincide, aunque los tokens sí.
- `scripts/prueba_platia.sh` prueba en Docker la conexión y la vuelta atrás con el
  `ai_service` real de PLATIA y un proveedor simulado.
