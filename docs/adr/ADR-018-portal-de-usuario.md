# ADR-018: Portal de usuario: solo lo propio, rotación con margen y un solo build con el panel

**Fecha:** 2026-11-01 · **Estado:** Aceptado

## Contexto
G14 cubre RF-17: que cada persona vea y gestione sus claves personales y su consumo sin pedir
nada a un administrador y sin ver datos de nadie más. Los capítulos 30 y 31 del libro
proponen un portal de autoservicio separado del panel, con el consumo propio, la gestión de
claves (incluida la rotación sin cortes) y la seguridad de la cuenta.

Ya teníamos casi todo debajo: sesión, contraseña, MFA y sesiones abiertas en `/auth/*`, y
crear, listar y revocar claves personales en `/me/keys` (G10, ADR-014). La auditoría guarda la
persona en `user_id` (ADR-013) y las cuotas por persona usan `usr:<id>` (ADR-011).

## Decisión

**Qué ve cada persona.** Las rutas nuevas (`/me/portal`, `/me/usage`, `/me/requests` y
`/me/keys/{id}/rotate`) filtran siempre por la persona de la sesión, nunca por un parámetro.
- El consumo sale de `audit_logs` con `user_id` de la persona: incluye lo que hace con sus
  claves y lo que una aplicación hace en su nombre (`X-Platia-User-Id`).
- Las peticiones se listan sin texto, que el gateway no guarda.
- Solo se enseñan las cuotas por persona de la aplicación de claves personales (las comunes y
  las que son solo suyas), con lo gastado por ella. Las cuotas de equipo, aplicación u
  organización no se enseñan: lo gastado diría cuánto consumen los demás.
- Los modelos son los permitidos en la aplicación de claves personales, con su precio.

**Rotación con margen.** Rotar crea una clave nueva con el mismo nombre y deja la anterior
viva unas horas (24 por defecto, 72 como máximo; 0 la revoca al momento). La anterior nunca
gana validez: caduca en lo que antes ocurra, su caducidad o el margen. La clave nueva no
cuenta para el máximo de 5 mientras dura el margen, porque si no una persona con 5 claves no
podría rotar. Todo va en una transacción con la fila de la persona bloqueada.

**Quién entra.** Cualquier persona con sesión completa, también los miembros (que no
necesitan MFA). Un administrador sin MFA tiene que activarlo antes, como en el panel. Las
claves de aplicación o personales no abren el portal: solo el token de sesión.

**Un solo build.** El portal es otra página de entrada (`portal.html`) del mismo proyecto
Vite que el panel: comparte la sesión, la API, los componentes y las pruebas. Nginx lo sirve
en `/portal/` con la misma política de contenido estricta; el código está en `/panel/assets/`.
Ambos enlazan entre sí (el enlace al panel solo sale a quien administra).

## Alternativas descartadas
- **Un proyecto aparte para el portal**: duplicaría la sesión, los componentes y la CI sin
  ninguna ventaja; la separación que importa (qué datos se ven) está en la API.
- **Reutilizar `/admin/*` con un filtro por persona**: mezclaría los papeles y bastaría un
  fallo en el filtro para ver datos de otros. `/me/*` no acepta un id de persona.
- **Rotar revocando la anterior al momento**: obliga a cambiar la configuración en el mismo
  segundo; con margen se cambia sin cortes y se puede seguir eligiendo 0.
- **Enseñar las cuotas de equipo**: útil para saber cuánto queda, pero revela el consumo de
  otras personas. Queda para cuando haya un papel de responsable de equipo.

## Consecuencias
- Una persona deja de pedir a un administrador sus claves o cuánto lleva gastado.
- Durante el margen de una rotación hay dos claves válidas; el portal dice hasta cuándo.
- El portal no muestra las cuotas de equipo ni de organización: si una de ellas corta las
  peticiones, la persona ve el error 429 pero no el límite.
