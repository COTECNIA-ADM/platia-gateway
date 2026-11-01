# ADR-016: Telemetría con Prometheus, avisos por webhook firmado y eventos en tiempo real

**Fecha:** 2026-11-01 · **Estado:** Aceptado

## Contexto
G12 cubre RF-18 (avisos por webhook firmado y métricas) y la medida de RNF-06 (p95 del tiempo
propio del gateway < 50 ms). El capítulo 24 del libro describe:
- métricas de Prometheus sin la organización como etiqueta;
- logs en JSON sin datos personales y con id de petición;
- un WebSocket autenticado que reenvía un canal de Redis;
- webhooks firmados con HMAC, con un registro de entregas, protección SSRF y desactivación
  tras fallos seguidos.

Ya teníamos logs JSON con `request_id` (G0 y G3) y avisos de cuota en el log (G7). Faltaba
sacarlos del log.

## Decisión

**Métricas**
- `/metrics` en formato Prometheus. Las etiquetas son de cardinalidad baja y conocida:
  aplicación, modelo, proveedor, contrato y resultado. Nunca la persona, la clave ni el id
  de la petición.
- Nginx no la publica y además exige `Authorization: Bearer <METRICS_TOKEN>`. Sin token
  configurado responde 404: fallar cerrado.
- Con dos procesos de uvicorn se usa el modo multiproceso de `prometheus_client`
  (`PROMETHEUS_MULTIPROC_DIR`, que se vacía al arrancar). Sin él, cada lectura vería solo
  uno de los dos procesos.

**Tiempo propio del gateway (RNF-06)**
- Se mide en cada petición el tiempo desde que llega hasta que la respuesta está lista para
  salir, menos lo que se espera al proveedor (todos los intentos).
  - En streaming, hasta que el proveedor responde con su cabecera: lo que viene después es
    el modelo escribiendo.
  - No incluye el cierre de la auditoría, que es una escritura de milisegundos.
- Va en el histograma `platia_gateway_overhead_seconds` y en `audit_logs.overhead_ms`.
  - Esa columna queda fuera de la cadena de G9: no cambia `hash_version`.
  - Así `cli latency` da p50, p95 y p99 de cualquier periodo, sin Prometheus, y el panel
    (G13) puede enseñarlo.
- Los aciertos de caché y los rechazos no cuentan: no llaman al proveedor.

**Eventos**
- Un evento es un sobre JSON: id, tipo, fecha, organización y datos.
- Nunca lleva texto de prompts ni respuestas, ni claves. La persona solo aparece como su
  referencia, igual que en la auditoría.
- Hay cinco tipos:
  - `quota.alert` y `quota.exhausted`: al pasar el umbral de aviso de un límite y al
    agotarlo, una vez por ventana (lo de G7, ahora fuera del log);
  - `security.blocked`: una petición bloqueada por la seguridad de contenido;
  - `deployment.cooled`: un despliegue enfriado por fallos del proveedor. Es global, sin
    organización, para quien opera el gateway;
  - `webhook.test`: la prueba manual.
- Lo que se puede repetir mucho (`security.blocked` y `deployment.cooled`) se envía como
  mucho una vez cada `EVENT_DEDUP_SECONDS` (5 minutos) por aplicación y tipo, o por
  despliegue. Un atacante que insiste no genera una tormenta de avisos. Sin Redis no se
  puede deduplicar y esos avisos se quedan en el log.
- Emitir nunca rompe ni retrasa una petición más allá de una escritura: un fallo va al log.

**Webhooks firmados**
- Cada webhook es de una organización o global (todas, más los eventos del gateway) y elige
  sus eventos. Su secreto (`plgw-whsec-…`) se muestra una vez y se guarda cifrado con
  `app/crypto.py`. Se gestionan con la CLI hasta el panel.
- Firma `X-Platia-Signature: t=<unix>,v1=<HMAC-SHA256 de "t.cuerpo">`.
  - El libro firma solo el cuerpo. Con la marca de tiempo el receptor puede rechazar
    entregas viejas: una copia no se puede reenviar más tarde.
  - `X-Platia-Delivery` y el `id` del evento permiten descartar duplicados (se entrega al
    menos una vez).
- **Cola en MySQL, no en memoria**:
  - cada evento deja una fila por webhook en `webhook_deliveries`;
  - un bucle en segundo plano, como el sellado de G9, las reclama con
    `FOR UPDATE SKIP LOCKED` (dos procesos no envían la misma), las envía y reintenta
    a los 10 s, 1 min, 5 min, 30 min y 2 h;
  - un receptor caído no frena ninguna petición y un reinicio no pierde avisos;
  - tras `WEBHOOK_MAX_ATTEMPTS` intentos, la entrega queda fallida;
  - con 5 entregas fallidas seguidas, el webhook se desactiva solo;
  - las entregas terminadas se borran a los `WEBHOOK_RETENTION_DAYS`.
- **SSRF**:
  - solo `https` y solo direcciones públicas;
  - se resuelve el nombre y basta una dirección privada, local, de enlace (metadatos de la
    nube), CGNAT, reservada o de multidifusión para rechazarlo, también escrita como IPv6;
  - se conecta a la misma dirección comprobada, con el nombre en `Host` y en SNI para
    validar el certificado. Así un DNS que cambia entre comprobar y conectar no lleva a la
    red interna;
  - no se siguen redirecciones ni se usa el proxy del entorno;
  - un receptor interno (n8n, la intranet) se permite por nombre con
    `WEBHOOK_PRIVATE_HOSTS`, y solo él puede usar `http`.

**Tiempo real**
- `/events/stream` es un WebSocket que reenvía el canal de Redis a los administradores:
  - los eventos de su organización y los del gateway;
  - un resumen `request.finished` por petición (aplicación, modelo, resultado, tokens,
    coste, latencia), sin persona ni texto.
  Lo usará el panel (G13).
- El token de acceso (G10) va en el subprotocolo (`platia.v1, bearer.<token>`), nunca en la
  URL, porque la URL acaba en los logs de Nginx. Como no hay cookies, otra web no puede abrir
  la conexión en nombre de nadie.
- Cada `REALTIME_RECHECK_SECONDS` se vuelve a comprobar la sesión en MySQL. Si se ha
  revocado o el token ha caducado, se cierra con 4401 y el panel se vuelve a conectar con un
  token nuevo.

## Lo que no entra ahora
- **Grafana, Prometheus y alertas**: son de quien opera la infraestructura. G15 dirá cómo
  conectarlos. El gateway solo expone las métricas.
- **Trazas distribuidas (OpenTelemetry)**: con un salto (PLATIA → gateway → proveedor) basta
  con el `X-Request-ID` que ya se propaga y se guarda en la auditoría.
- **Middleware de log de acceso del libro**: los logs `request.completed` y `request.failed`
  ya tienen id, aplicación, modelo, estado, latencia y ahora el tiempo propio. Las cabeceras
  nunca se registran, así que no hace falta taparlas.
- **Webhooks por aplicación o por equipo**: con organización y global basta para RF-18.
  Las cuotas de equipo y de aplicación avisan al webhook de su organización.
- **Eventos de cambios de configuración**: llegarán con el panel (G13), que es quien los
  hace.

## Consecuencias
- Hace falta `METRICS_TOKEN` en `.env` (lo crea `generate_env.py --actualizar`) y dar ese
  token al Prometheus que lea las métricas.
- RNF-06 se mide con datos reales en cada despliegue: `cli latency` dice el p95 y si está
  dentro del objetivo.
- Un receptor de webhooks debe comprobar la firma y la fecha (ver `docs/telemetria.md`).
  Si no responde 2xx, se reintenta y al final se desactiva: hay que reactivarlo con
  `cli set-webhook --enable`.
