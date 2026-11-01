# ADR-019: Despliegue y operación: worker propio sin Celery, solo HTTPS en el servidor de PLATIA y copias de MySQL

**Fecha:** 2026-11-01 · **Estado:** Aceptado

## Contexto
G15 cierra el plan con la puesta en producción: compose de producción, Nginx endurecido,
tareas de fondo, imágenes publicadas, despliegue con vuelta atrás y copias. Los capítulos 33 y
34 del libro proponen un stack con Nginx (TLS, zonas de límite, plazos contra conexiones
lentas, registro en JSON) y Celery con Redis para las tareas de fondo, y un arranque que se
niega a seguir con secretos de ejemplo o débiles en producción.

Partimos de esto:
- Las únicas tareas de fondo son dos bucles asíncronos que corrían en cada proceso de la API:
  - el sellado de la auditoría (G9);
  - la entrega de webhooks (G12), que ya tiene su propia cola en MySQL con `SKIP LOCKED`.
- PLATIA ya está en producción con su propio Nginx en los puertos 80 y 443, y llama al
  gateway por la red interna `platia_llm_net`, así que los dos van en el mismo servidor.
- PLATIA ya resolvió el despliegue: imágenes en GHCR, `deploy.sh` con vuelta atrás y copias
  con `mysqldump`.

## Decisión

**Un proceso `worker`, sin Celery.**
- El sellado y las entregas pasan a un servicio aparte, con la misma imagen y la misma
  configuración que la API: `python -m app.worker`. La API arranca con `BACKGROUND_TASKS=false`.
- Así un reinicio o un pico de la API no corta una entrega a medias, y el trabajo no se
  repite en cada proceso de uvicorn.
- El worker toca un fichero de latido cada 15 s; su healthcheck falla si pasan 2 minutos sin
  latido.
- Celery no aporta nada aquí: son dos tareas periódicas, idempotentes y seguras con varios
  procesos, y la cola de webhooks ya vive en MySQL. Traerlo supondría:
  - otro broker y otra configuración;
  - mezclar el código asíncrono con los workers síncronos de Celery;
  - mantener otra pieza.
- Si mañana hay tareas largas bajo demanda (por ejemplo, exportaciones grandes), se
  reconsidera.

**Producción en el servidor de PLATIA** (decisión de la persona responsable).
- El Nginx del gateway publica solo HTTPS, en `HTTPS_PORT` (8443 por defecto), con su propio
  nombre (`SERVER_NAME`) y su certificado.
- No publica HTTP. Una petición HTTP a ese puerto se redirige a HTTPS (código 497 de Nginx).
- Un nombre que no es el suyo no recibe ni certificado (`ssl_reject_handshake`).
- PLATIA sigue sin pasar por Nginx.
- El certificado es el de la empresa (uno comodín sirve). Let's Encrypt necesitaría el
  puerto 80, que es de PLATIA; la guía explica cómo pedirlo con el certbot de PLATIA.

**Nginx endurecido, igual en desarrollo y en producción.** Las rutas y los ajustes van en
`nginx/snippets/`, que usan las dos configuraciones. Lo que se prueba en la CI es lo que
corre en producción. Incluye:
- **TLS**: 1.2 y 1.3 con los cifrados «intermedios» de Mozilla, sin tickets de sesión, y HSTS
  de un año.
- **Límites por dirección IP** (inicio de sesión, cuenta y panel, API y conexiones
  simultáneas), con 429 en JSON.
  - Son holgados a propósito: frenan avalanchas y fuerza bruta, porque las cuotas de verdad
    son las del gateway (ADR-011) y una oficina entera puede salir por una sola IP.
- **Plazos contra conexiones lentas**: `send_timeout` mide el tiempo entre dos escrituras, así
  que no corta un streaming largo.
- **Registro de accesos en JSON y sin datos personales**:
  - la dirección sin su último octeto;
  - la ruta sin la consulta, que puede llevar una persona o un filtro;
  - sin navegador ni cabeceras.

**Arranque seguro en producción.**
- Con `GATEWAY_ENV=production`, la API y el worker no arrancan si:
  - falta `ENCRYPTION_KEY`, `DB_PASSWORD` o `REDIS_PASSWORD`;
  - `JWT_SECRET` (o `METRICS_TOKEN`, si se usa) es corto o es un valor de ejemplo;
  - la base se usa como root;
  - la cookie no va solo por HTTPS;
  - el MFA de administradores está apagado;
  - los logs no son JSON.
- El mensaje nombra la variable, nunca su valor.

**Imágenes y despliegue, como en PLATIA.**
- GitHub Actions publica `gateway` y `nginx` (con el panel y el portal) en GHCR con la
  etiqueta `sha-xxxxxxx` cuando la CI de `main` pasa.
- `scripts/deploy.sh sha-xxxxxxx` hace lo siguiente:
  - una copia;
  - descarga las imágenes;
  - las migraciones van antes que la API;
  - espera a los healthchecks y a `/health` por HTTPS;
  - si algo falla en 3 minutos, vuelve a la versión anterior.
- Se lanza a mano o con el flujo «Despliegue», que pide aprobación.

**Copias: MySQL y `.env`, nunca Redis.**
- `scripts/backup.sh` vuelca la base con sus disparadores (sin ellos, la auditoría sellada
  dejaría de estar protegida) y comprueba que el volcado está completo.
- Guarda también el `.env`: sin la `ENCRYPTION_KEY` no se descifran las claves de los
  proveedores ni los secretos MFA.
- Redis no se copia: los contadores de cuota se reconstruyen desde la auditoría, y la caché
  y los enfriamientos son temporales.
- `scripts/restore.sh` hace lo siguiente:
  - pide confirmación;
  - carga la copia;
  - vacía Redis;
  - arranca;
  - comprueba la cadena de la auditoría (`audit-verify`).

## Alternativas descartadas
- **Celery con beat** (lo que propone el libro): explicado arriba. El coste es real y no hay
  tareas que lo pidan.
- **Dejar las tareas en la API**: funciona (son seguras con varios procesos), pero repite el
  trabajo en cada proceso y ata las entregas a los reinicios de la API.
- **Pasar por el Nginx de PLATIA** (un `server` más en su configuración): ahorra el puerto
  8443, pero ata el despliegue del gateway al de PLATIA y mezcla sus límites y sus registros.
- **Copiar también Redis**: daría la falsa idea de que hace falta. Restaurar contadores
  viejos sería peor que reconstruirlos.

## Consecuencias
- Cinco contenedores en lugar de cuatro; el worker usa poca memoria (256 MB de límite).
- Las personas y Claude Code usan `https://<nombre del gateway>:8443`. Si la red de la
  empresa no deja salir a ese puerto, hay que abrirlo o cambiar `HTTPS_PORT`.
- `scripts/prueba_produccion.sh` prueba en la CI la configuración de producción con un
  certificado autofirmado:
  - HTTPS y HSTS;
  - rechazo de TLS 1.1;
  - arranque bloqueado con un secreto débil;
  - copia y restauración.
- Las migraciones deben seguir siendo compatibles hacia atrás para que la vuelta atrás
  automática sirva, como en PLATIA.
