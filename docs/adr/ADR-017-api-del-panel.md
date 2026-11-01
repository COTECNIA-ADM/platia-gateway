# ADR-017: API del panel de administración, papeles y registro de cambios

**Fecha:** 2026-11-01 · **Estado:** Aceptado

## Contexto
G13 cubre RF-16: administrar desde un panel lo que hasta ahora solo se hacía con la CLI
(aplicaciones, claves, personas, cuotas, reglas de contenido, webhooks, proveedores, modelos,
despliegues y precios) y ver la operación y la auditoría. Los capítulos 27 a 29 del libro
proponen una aplicación React que habla con una API de administración y recibe eventos en
tiempo real; separan quien administra una organización de quien opera la plataforma.

Ya teníamos las piezas de debajo: personas con contraseña, MFA y sesiones en MySQL (G10,
ADR-014), eventos por WebSocket (G12, ADR-016) y la CLI con todas las órdenes.

## Decisión

**Dos entregas.** G13a es la API (`/admin/*`) con sus pruebas; G13b, la aplicación web que
la usa. Así cada PR se puede revisar y probar por separado, y la API sirve también para
automatizar sin la CLI.

**Quién entra.**
- Solo personas con rol `admin`, sesión de alcance completo y, por tanto, MFA (G10). Una
  clave de aplicación o personal nunca abre `/admin/*`.
- Cada administrador actúa solo sobre su organización. Un objeto de otra organización
  responde 404, como si no existiera, para no revelar qué hay.
- El catálogo de proveedores, modelos, despliegues y precios es común a todas las
  organizaciones. Los administradores lo ven (sin claves) y solo quien **opera el gateway**
  (`users.platform_operator`) lo cambia. Lo mismo vale para los webhooks globales y el estado
  del sellado de la auditoría.
- Ser operador solo se da con la CLI (`set-user --operator si`), y solo a administradores.
  Desde el panel no se puede conceder: nadie se sube de nivel a sí mismo.

**Reglas que evitan cerrarse la puerta.** Un administrador no se quita el rol, no se da de
baja ni se cambia aquí la contraseña o el MFA (eso va en su cuenta, con la contraseña
actual). La organización nunca se queda sin administradores activos. Quitar el rol o dar de
baja a alguien cierra sus sesiones en el momento.

**Registro de cambios.** Cada cambio del panel escribe una fila en `admin_actions` en la
misma transacción: quién, qué acción, sobre qué objeto, los campos cambiados, la IP y el id
de petición. Los secretos (claves de proveedor, contraseñas, secretos de webhook) solo
aparecen como `(cambiado)`. No se encadena como la auditoría de peticiones (ADR-013): lo
protege que solo lo escribe el gateway y que nadie tiene una ruta para borrarlo.

**Secretos.** Una clave de aplicación o un secreto de webhook se enseñan una sola vez, con
`Cache-Control: no-store`. La clave de un proveedor se puede poner o cambiar, pero la API
solo dice si existe (`has_api_key`).

**Contrato.** Entrada con Pydantic y campos desconocidos rechazados (400). Un duplicado es
409. Los listados largos (auditoría, registro de cambios) se paginan con cursor
(`data` + `next`, que se pasa como `before_id`). Los importes van como texto decimal para no
perder precisión. Los errores siguen el formato del gateway y nunca llevan detalles internos.

**Auditoría.** El panel consulta la auditoría de su organización con filtros, la exporta en
JSON Lines con el hash de cadena de cada fila (máximo 100 000 filas por exportación) y da
un resumen por modelo, aplicación y hora con los percentiles de latencia y de tiempo propio.
Nunca hay texto de prompts ni respuestas, porque no se guarda.

## Lo que no entra ahora
- **Portal de cumplimiento del AI Act**: el registro de usos, propósitos y la exportación de
  la auditoría ya dan la base. Un portal específico (inventario de sistemas, evaluaciones de
  riesgo) se valorará en el bloque D.
- **Inicio de sesión único (SSO)**: pendiente de decidir; si se elige, entrará como otra
  forma de entrar en G10, sin cambiar esta API.
- **Papeles más finos** (solo lectura, auditor): con administrador y operador basta para una
  organización. Se añadirán si aparece la necesidad.
- **Probar un prompt desde el panel** (el «playground» del libro): pide guardar o mostrar
  texto de prompts; el portal de usuario (G14) lo hará con la clave de la persona.

## Consecuencias
- Tabla nueva `admin_actions` y columna `users.platform_operator` (migración 0009).
- Nginx publica `/admin/` con `X-Real-IP` para el registro de cambios.
- Hay que marcar al menos un operador con la CLI para cambiar el catálogo desde el panel.
- La CLI sigue sirviendo para todo, también sin panel; sus cambios no pasan por
  `admin_actions`.
