# ADR-004: Organización, equipo, persona y aplicación con claves opacas; sin dispositivos

**Fecha:** 2026-10-31 · **Estado:** Aceptado

## Contexto
El libro usa la jerarquía Organización → Equipo → Persona → Dispositivo, con las
aplicaciones como clientes registrados en paralelo, y resuelve la organización de cada
petición por varias vías, siempre denegando si no lo consigue (cap. 2). El device binding y
las licencias por dispositivo responden a un despliegue con aplicación de escritorio que
aquí no existe.

## Decisión
- Jerarquía **Organización → Equipo → Persona**, y **Aplicación** colgando de un equipo.
  Modelo multi-organización desde el principio, aunque al arrancar solo haya una.
- Claves opacas con prefijo por tipo: `plgw-app-<slug>-<secreto>` (aplicación) y
  `plgw-usr-<secreto>` (persona, G10). Solo se guarda el hash; el prefijo permite saber de
  quién es una clave en un log sin exponer el secreto.
- La resolución de identidad es una sola vía por tipo de clave y falla cerrada: clave
  desconocida, revocada, de una aplicación suspendida o de una organización inactiva → 401.
- Una aplicación marcada como de confianza puede declarar la persona final
  (`X-Platia-User-Id`). Ese id se usa para cuotas por persona y auditoría, no como
  identidad autenticada. Una aplicación sin esa marca que envíe la cabecera recibe 400.
- Sin dispositivos, licencias por dispositivo ni device binding.

## Alternativas
- **Solo un `user_id`:** no permite cuotas por equipo ni aplicaciones sin persona.
- **Dispositivos como en el libro:** sin aplicación de escritorio no aportan control y sí
  tablas y comprobaciones.
- **JWT para aplicaciones:** una clave opaca se revoca al instante y no lleva datos dentro.

## Consecuencias
- Toda consulta incluye la organización y el propietario: el ORM no lo hace solo.
- Si un día hay aplicación de escritorio, los dispositivos se añaden con un ADR nuevo.
