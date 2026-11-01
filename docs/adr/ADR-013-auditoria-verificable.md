# ADR-013: Auditoría verificable con sellado posterior, disparadores y seudonimización por huella

**Fecha:** 2026-11-01 · **Estado:** Aceptado

## Contexto
RF-14 pide una auditoría que no se pueda modificar, encadenada con hash y verificable, con
exportación y seudonimización (RGPD). RNF-05 prohíbe guardar el texto de las
conversaciones. Desde G3 cada petición deja una fila en `audit_logs`, escrita en dos pasos
(apertura pendiente y cierre, ADR-007), y G6 a G8 le añadieron intentos, coste y hallazgos
de contenido. El capítulo 21 del libro describe disparadores que impiden cambiar y borrar,
una cadena SHA-256 calculada al insertar, un escritor por lotes y la verificación por
tramos. El 22 describe un motor de gobierno con controles, revisiones e incidentes.

## Decisión

**Huellas con sal, nunca el texto**
- `prompt_hash`: HMAC-SHA256 del cuerpo que sale hacia el proveedor (ya filtrado por G8), en
  JSON canónico, con la sal de la organización (`organizations.audit_salt`). Permite
  comprobar que un texto concreto se envió sin guardarlo, y el mismo texto da huellas
  distintas en dos organizaciones.
- `end_user_digest`: la misma huella de la persona (`X-Platia-User-Id`).

**Sellar después de cerrar, no al insertar**
- Con la escritura en dos pasos, el contenido final de una fila no se conoce al insertarla.
  Un proceso en segundo plano del gateway (cada `AUDIT_SEAL_INTERVAL_SECONDS`, 10 por defecto)
  sella por orden de id las filas cerradas: `chain_hash = sha256(hash anterior | contenido
  canónico)`. `cli audit-seal` hace lo mismo a mano.
- El último eslabón vive en `audit_chain_state`, leído con bloqueo: dos procesos no sellan a
  la vez y se detecta que falten filas del final.
- Una fila pendiente detiene el sellado: la cadena va en orden y sin huecos. Si sigue
  pendiente pasado `AUDIT_PENDING_MAX_SECONDS` (2400: más que un streaming máximo), se
  **concilia**: queda como `error` / `audit_unclosed`. Un cierre que llegue después ya no
  la cambia (`audit.close_late` en el log, con los tokens).
- Cada sellado deja `audit.sealed` con el último id y hash en el log: si los logs se guardan
  fuera, sirven de ancla independiente de la base.

**Disparadores en MySQL** (migración 0005)
- Una fila sin sellar se puede actualizar (así se cierra). Una sellada no: el disparador lo
  rechaza, salvo para cambiar `end_user_ref` por un seudónimo (`seud:…`) o vaciarlo.
- Ninguna fila se puede borrar. `TRUNCATE` no dispara los disparadores; es una orden de
  administrador (los tests la usan).
- Con el log binario activo (lo normal en MySQL 8), crear disparadores sin el privilegio SUPER
  exige `log_bin_trust_function_creators=1`. `docker-compose.yml` arranca MySQL así; en un
  MySQL gestionado va en su grupo de parámetros. La migración lo comprueba antes de tocar nada
  y, si falta, se detiene con un mensaje que lo explica.
- Los disparadores frenan errores y cambios casuales. Contra alguien con permisos de
  administrador que los quite, está la cadena: `cli audit-verify` recalcula todos los hashes
  y dice la primera fila que no cuadra.

**RGPD**
- La cadena cubre la huella de la persona, no su referencia. Así `cli audit-pseudonymize`
  cambia la referencia por un seudónimo en filas ya selladas sin romper la cadena.
- Es seudonimización, no anonimización: con la sal de la organización se puede volver a
  relacionar. Por eso la sal nunca sale del gateway.
- `cli audit-export` saca las filas en JSON (una por línea) con su hash, para un auditor o
  para una petición de acceso de una persona (`--user`, que la encuentra también después de
  seudonimizarla). `--pseudonymize` quita la referencia y deja solo la huella.

## Lo que no entra ahora
- **Escritura por lotes**: la apertura tiene que estar escrita antes de llamar al proveedor
  (fallar cerrado, ADR-005). Con el volumen de PLATIA, una escritura por petición no es un
  problema. Se medirá en G12.
- **Cifrado del contenido de la conversación para investigaciones**: no se guarda el
  contenido.
- **Motor de gobierno** (controles, revisiones, incidentes, permisos de herramientas):
  necesita el panel (G13) para tener sentido; la evidencia que necesitará ya está en la
  auditoría y en los ADR.
- **Retención y borrado programado**: con el disparador que impide borrar, una purga por
  antigüedad necesitará una orden de administrador con su propio registro. Se decidirá
  cuando haya un plazo de retención acordado.

## Consecuencias
- La auditoría se puede presentar como prueba: cualquier cambio o borrado posterior se
  detecta.
- Las filas tardan unos segundos en quedar selladas. Si un proceso muere a mitad de una
  petición, el sellado espera hasta la conciliación (40 minutos por defecto).
- Añadir una columna a lo que cubre la cadena exige una versión nueva de `hash_version`.
