# ADR-006: Modelo de datos, claves con hash y secretos cifrados con AES-256-GCM

**Fecha:** 2026-10-31 · **Estado:** Aceptado

## Contexto
G2 fija las tablas sobre las que se construyen G3 a G9 (libro, cap. 2, 14, 18 y 32). Hay
que decidir cómo se guardan las claves de los clientes y las de los proveedores, y cómo se
relaciona la auditoría con la configuración.

## Decisión
- **Una base MySQL** (`platia_gateway`) con 10 tablas: `organizations`, `teams`, `users`,
  `applications`, `api_keys`, `providers`, `llm_models`, `deployments`, `model_prices` y
  `audit_logs`. Cambios siempre con migraciones Alembic (`gateway/migrations/`), que aplica el
  contenedor `migrations` antes de arrancar el gateway.
- **Claves de clientes**: solo el hash SHA-256 de la clave completa y un prefijo visible. Un
  hash rápido basta porque el secreto es aleatorio de 128 bits o más (no es una contraseña).
  Una restricción `CHECK` obliga a que una clave sea de una aplicación o de una persona, nunca
  de las dos.
- **Claves de proveedor**: cifradas con AES-256-GCM (`app/crypto.py`), formato
  `enc:v1:<base64>`, con el slug del proveedor como dato asociado para que un valor copiado a
  otra fila no se descifre. La clave maestra es `ENCRYPTION_KEY` (32 bytes, en `.env`).
- **Precios por modelo** con fecha de inicio; un cambio de precio es una fila nueva, así el
  coste de una llamada antigua se puede recalcular.
- **Auditoría sin claves foráneas**: guarda los ids como enteros para sobrevivir a borrados
  de configuración. Fechas con microsegundos (`DATETIME(6)`). Guarda hashes con la sal de la
  organización, nunca el texto. La escritura en dos pasos, la cadena de hashes y el trigger
  que impide modificarla llegan en G9.
- **Borrado lógico** (`deleted_at`) en la configuración; estados explícitos donde importan
  (aplicación activa, suspendida o revocada).
- Las cuotas y los límites tendrán sus tablas en G7, con su propia migración.

## Alternativas
- **bcrypt o Argon2 para las claves de cliente:** pensados para contraseñas de poca
  entropía; con secretos aleatorios solo añaden latencia en cada petición.
- **Claves de proveedor en variables de entorno:** no permite varias cuentas por proveedor
  ni cambiarlas desde el panel (G13).
- **Fernet:** válido, pero AES-GCM con dato asociado ata cada valor a su fila y es lo que
  describe el libro.
- **Precios por despliegue:** más flexible (Azure frente a OpenAI), pero hoy cada modelo tiene
  un solo precio. Se añadirá si aparece el caso.

## Consecuencias
- Si se pierde `ENCRYPTION_KEY`, hay que volver a introducir las claves de proveedor.
  Rotarla exige recifrar (una orden para eso llegará con el panel).
- Los tests de base de datos necesitan un MySQL real (`TEST_DATABASE_URL`); la CI lo levanta.
