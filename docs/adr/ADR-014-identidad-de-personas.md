# ADR-014: Identidad de personas con contraseña, TOTP, sesiones en MySQL y claves personales

**Fecha:** 2026-11-01 · **Estado:** Aceptado

## Contexto
Hasta G9 solo entran aplicaciones, con su clave. El panel (G13) y el portal (G14) necesitan
que entren personas, y RF-04 pide claves personales (`plgw-usr-`) para quien usa
herramientas propias (Claude Code, scripts). Los capítulos 16 a 18 del libro describen:
- JWT con device binding y revocación en Redis por token, persona y dispositivo;
- renovación con rotación y bloqueo por intentos;
- SSO por OIDC, SCIM y MFA por correo o TOTP;
- claves personales con rotación doble, cifrado en cliente y claves maestras repartidas con
  Shamir.

ADR-004 ya descartó los dispositivos.

## Decisión

**Contraseña y MFA**
- Contraseñas con scrypt (biblioteca estándar, 32 MB por cálculo), de 12 caracteres como
  mínimo y sin el correo dentro. Si la persona no existe, se calcula igual un hash: el
  tiempo de respuesta no dice qué correos están dados de alta.
- MFA con TOTP (RFC 6238) y cualquier app autenticadora. El secreto se guarda cifrado con
  `app/crypto.py`. Se acepta el paso anterior y el siguiente, y nunca dos veces el mismo
  (`mfa_last_step`).
- Con la contraseña correcta y MFA activado, se entrega un vale de 5 minutos que solo sirve
  para enviar el código: 5 intentos y un solo uso, contados en Redis.
- Con `REQUIRE_MFA_FOR_ADMINS` (activado por defecto), un administrador sin MFA recibe un
  acceso que solo deja configurarlo. Para un MFA perdido está `cli reset-mfa`.
- Tras 5 contraseñas fallidas por persona y dirección, 15 minutos de espera. Sin Redis no se
  pueden contar los intentos, así que no se puede entrar (503): fallar cerrado.

**Tokens y sesiones**
- El acceso es un JWT HS256 de 15 minutos. El algoritmo está fijo en el código: nunca se lee
  del token ni de la configuración. Lleva la persona, la organización, la sesión y el alcance.
- **Cada petición comprueba la sesión en MySQL** (viva, sin caducar, persona y organización
  activas). Revocar surte efecto al momento y no hace falta la lista negra de Redis del
  libro. Cuesta una consulta por petición, que con el tráfico del panel es nada. Las
  peticiones de inferencia no usan JWT.
- La renovación es opaca (`<sesión>.<secreto>`) y en `user_sessions` solo se guarda su hash.
  Cada uso la cambia. Si llega una ya usada, alguien tiene una copia y la sesión entera se
  revoca.
- Una sesión caduca a los 7 días sin uso y siempre a los 30.
- Cambiar la contraseña o activar o quitar el MFA cierra las demás sesiones. La persona ve
  sus sesiones y las puede cerrar una a una o todas.

**Navegador**
- El acceso vive en memoria: nunca en `localStorage` (regla que también sigue PLATIA).
- La renovación va en una cookie `HttpOnly`, `SameSite=Strict`, solo para `/auth`, y `Secure`
  en producción.
- Las rutas que usan la cookie exigen además `X-Requested-With`: un formulario de otra web no
  puede enviarla.

**Claves personales**
- `plgw-usr-<secreto>`, solo como hash. El prefijo visible no lleva el nombre de la persona.
- Como mucho 5 activas por persona, y siempre con caducidad (90 días por defecto, como mucho
  365). Se crean y se revocan en `/me/keys` o con `cli create-key --user`.
- **Siguen las reglas de una aplicación de la organización marcada con `--personal-keys`**:
  modelos, propósitos, límites y política de contenido. Así no hay un segundo juego de reglas.
- La persona de la clave es la persona final (`usr:<id>`), para las cuotas por persona y la
  auditoría (`user_id`), y no puede declarar otra con `X-Platia-User-Id`. Las cuotas por
  equipo usan su equipo si lo tiene.
- Si la organización no tiene esa aplicación, las claves personales no funcionan (403).

## Lo que no entra ahora
- **SSO por OIDC (Microsoft o Google)**: se añade cuando se elija el proveedor, sobre estas
  mismas sesiones. Una persona de SSO no tendrá contraseña (`password_hash` nulo).
- **SCIM**: sin SSO no tiene sentido. Las altas y bajas siguen en la CLI y en el panel.
- **MFA por correo**: necesita un servidor de correo. TOTP no.
- **Device binding y huella del cliente**: descartados en ADR-004.
- **Rotación doble de claves personales**: una persona crea otra clave y revoca la vieja.
- **Cifrado en cliente y claves maestras con Shamir**: el gateway no guarda conversaciones
  (RNF-05), así que no hay nada que cifrar para investigaciones.

## Consecuencias
- Para que entren personas hacen falta `JWT_SECRET` (lo crea `generate_env.py --actualizar`)
  y Redis.
- Si se pierde `ENCRYPTION_KEY`, también se pierden los MFA: hay que quitarlos con
  `cli reset-mfa` y volver a configurarlos.
- Cambiar `JWT_SECRET` invalida todos los accesos en curso. Las sesiones siguen y se
  renuevan con la cookie.
