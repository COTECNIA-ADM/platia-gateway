# Telemetría: métricas, avisos por webhook y tiempo real

Guía de G12. La decisión y sus motivos están en [ADR-016](adr/ADR-016-telemetria.md).

En los ejemplos, `gwcli` es `docker compose run --rm -T migrations python -m app.cli`.

## Métricas para Prometheus

`/metrics` solo se ve desde la red interna de Docker: Nginx no la publica. Además exige el
token de `METRICS_TOKEN` (`generate_env.py --actualizar` lo crea). Sin token configurado
responde 404.

Prometheus debe estar en la red `edge_net` del gateway. La configuración de lectura queda así:

```yaml
scrape_configs:
  - job_name: platia-gateway
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/platia_gateway_token   # el valor de METRICS_TOKEN
    static_configs:
      - targets: ["gateway:8000"]
```

| Métrica | Tipo | Etiquetas |
| --- | --- | --- |
| `platia_gateway_requests_total` | contador | `contract`, `application`, `model`, `provider`, `outcome` (success, blocked, error, cancelled, unauthenticated) |
| `platia_gateway_request_duration_seconds` | histograma | `contract`, `stream` |
| `platia_gateway_overhead_seconds` | histograma | `contract`, `stream`: tiempo propio del gateway (RNF-06) |
| `platia_gateway_tokens_total` | contador | `model`, `kind` (input, output, cache_read, cache_write) |
| `platia_gateway_cost_usd_total` | contador | `application`, `model` |
| `platia_gateway_upstream_errors_total` | contador | `provider`, `error_type`: cada intento fallido |
| `platia_gateway_security_flags_total` | contador | `direction`, `check`, `action` |
| `platia_gateway_cache_lookups_total` | contador | `result` (hit, miss) |
| `platia_gateway_quota_rejections_total` | contador | `scope`, `metric`, `period` |
| `platia_gateway_events_total` | contador | `event` |
| `platia_gateway_webhook_deliveries_total` | contador | `result` (ok, error) |

Ejemplos de consulta:
- p95 del tiempo propio:
  `histogram_quantile(0.95, sum by (le) (rate(platia_gateway_overhead_seconds_bucket[5m])))`
- Gasto por aplicación en la última hora:
  `sum by (application) (increase(platia_gateway_cost_usd_total[1h]))`

## Tiempo propio del gateway (RNF-06)

El objetivo es un p95 por debajo de 50 ms sin contar al proveedor. Cada petición lo guarda en
la auditoría, así que se puede medir sin Prometheus:

```bash
gwcli latency                       # últimas 24 horas
gwcli latency --since 2026-11-01 --app platia
```

La orden separa las peticiones con y sin streaming y dice si el p95 está dentro del
objetivo. No cuenta los aciertos de caché ni los rechazos, porque no llaman al proveedor.

## Avisos por webhook

| Evento | Cuándo | Organización |
| --- | --- | --- |
| `quota.alert` | Un límite de día o mes pasa su umbral de aviso (`alert_pct`, 80 % por defecto) | La del límite |
| `quota.exhausted` | Un límite de día o mes se agota | La del límite |
| `security.blocked` | La seguridad de contenido bloquea una petición. Como mucho uno cada 5 min por aplicación y tipo | La de la aplicación |
| `deployment.cooled` | Un despliegue se enfría por fallos del proveedor. Como mucho uno cada 5 min por despliegue | Ninguna: solo webhooks globales |

Alta, prueba y estado:

```bash
gwcli create-webhook --org cotecnia --name "Avisos IA" --url https://avisos.cotecnia.es/platia \
  --event quota.alert --event quota.exhausted --event security.blocked
# Para quien opera el gateway, todos los eventos de todas las organizaciones:
gwcli create-webhook --global --name Operación --url https://ops.cotecnia.es/hook --event deployment.cooled
gwcli webhook-test --id 1          # envía ahora un evento webhook.test
gwcli webhooks                     # estado y últimas entregas
gwcli set-webhook --id 1 --enable  # reactivar tras desactivarse solo
gwcli set-webhook --id 1 --rotate-secret
```

El secreto de firma (`plgw-whsec-…`) se muestra una sola vez.

La URL tiene que ser `https` y pública. Un receptor de la red interna (por ejemplo n8n en
`http://n8n:5678`) se permite añadiendo su nombre a `WEBHOOK_PRIVATE_HOSTS` en `.env`.

### Qué llega

```http
POST /platia HTTP/1.1
Content-Type: application/json
User-Agent: PLATIA-Gateway-Webhook/1
X-Platia-Event: quota.alert
X-Platia-Delivery: 42
X-Platia-Signature: t=1793520000,v1=5f2b…

{"id":"9c1e…","type":"quota.alert","created_at":"2026-11-01T09:20:00.123456Z","organization_id":1,
 "data":{"limit_id":3,"scope":"application","scope_id":2,"subject":null,"metric":"cost_usd",
         "period":"month","used":"80.12","limit":"100","threshold_pct":80,
         "window_end":"2026-12-01T00:00:00Z","application":"platia"}}
```

### Cómo comprobarlo en el receptor

1. Lee el cuerpo **tal cual llega**, sin volver a serializarlo.
2. Calcula HMAC-SHA256 con el secreto sobre `<t>.<cuerpo>` y compáralo en tiempo constante
   con `v1`.
3. Rechaza si `t` está a más de 5 minutos de la hora actual.
4. Responde 2xx en menos de 10 segundos y procesa después. Cualquier otro código, una
   redirección o el tiempo agotado cuentan como fallo y se reintenta.
5. Guarda el `id` del evento para descartar un duplicado.

```python
import hashlib, hmac, time

def firma_valida(secreto: str, cabecera: str, cuerpo: bytes, tolerancia: int = 300) -> bool:
    partes = dict(p.split("=", 1) for p in cabecera.split(","))
    t = int(partes["t"])
    if abs(time.time() - t) > tolerancia:
        return False
    esperada = hmac.new(secreto.encode(), f"{t}.".encode() + cuerpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperada, partes.get("v1", ""))
```

### Reintentos y desactivación

- Se reintenta a los 10 s, 1 min, 5 min, 30 min y 2 h.
- Tras `WEBHOOK_MAX_ATTEMPTS` intentos (6), la entrega queda como `failed`.
- Con 5 entregas fallidas seguidas, el webhook se desactiva solo (`webhooks` lo muestra como
  `desactivado (entregas_fallidas)`).
- Las entregas se guardan `WEBHOOK_RETENTION_DAYS` días.

## Tiempo real por WebSocket

`wss://<gateway>/events/stream` lo usará el panel (G13). Solo entran administradores con la
sesión completa (MFA hecho). El token de acceso va como subprotocolo, nunca en la URL:

```js
const ws = new WebSocket("wss://gateway.cotecnia.es/events/stream", ["platia.v1", `bearer.${accessToken}`]);
ws.onmessage = (m) => console.log(JSON.parse(m.data));   // {type, organization_id, data, ...}
ws.onclose = (e) => { if (e.code === 4401) { /* renovar el token y volver a conectar */ } };
```

Llegan los eventos de la tabla de arriba y un `request.finished` por petición:
aplicación, propósito, modelo, proveedor, resultado, tokens, coste, latencia y tiempo propio.
No llevan la persona ni el texto.

Códigos de cierre:
- 4401: token no válido, sesión cerrada o token caducado.
- 4403: no es administrador, o falta `platia.v1`.
- 1013: Redis no está disponible; vuelve a intentarlo más tarde.
