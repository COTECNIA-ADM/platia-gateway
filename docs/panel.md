# Panel de administración

El panel web está en **http://localhost:8090/panel/** (en producción, en `/panel/` del
dominio del gateway). Entra un administrador con su correo, su contraseña y el código de la
app autenticadora; la primera vez, el panel le pide activar el MFA. Secciones: resumen con
peticiones en vivo, aplicaciones y claves, personas, cuotas, seguridad de contenido,
auditoría (con exportación), webhooks, catálogo de modelos y registro de cambios.

Desarrollo del panel (`panel/`, React + TypeScript + Vite), con el gateway levantado:

```bash
cd panel && npm ci
npm run dev        # http://localhost:5173/panel/ (llama al gateway de :8090)
npm test           # pruebas de pantallas (Vitest + Testing Library)
npm run build      # lo que se copia en la imagen de Nginx
```

## API

Todo cuelga de `/admin/` y pide `Authorization: Bearer <token de acceso>` de una persona con
rol `admin` y MFA activo (G10). Cada administrador ve y cambia solo su organización; lo de
otra responde 404. Las decisiones están en [ADR-017](adr/ADR-017-api-del-panel.md).

```bash
# Entrar (con MFA: /auth/login devuelve mfa_ticket y se completa en /auth/login/mfa)
curl -s http://localhost:8090/auth/login -H 'Content-Type: application/json' \
     -d '{"email":"eva@cotecnia.es","password":"…"}'
curl -s http://localhost:8090/admin/overview?hours=24 -H "Authorization: Bearer $TOKEN"
```

### Rutas

| Ruta | Qué hace |
| --- | --- |
| `GET/POST /admin/teams`, `PATCH /admin/teams/{id}` | Equipos |
| `GET/POST /admin/applications`, `GET/PATCH /admin/applications/{id}` | Aplicaciones: propósitos, modelos permitidos, confianza, claves personales y caché |
| `GET/POST /admin/applications/{id}/keys` | Claves de una aplicación (la clave en claro sale una vez) |
| `GET /admin/keys?kind=&state=active\|all`, `DELETE /admin/keys/{id}` | Todas las claves de la organización y revocar una |
| `GET/POST /admin/users`, `PATCH/DELETE /admin/users/{id}` | Personas: alta, rol, equipo, baja |
| `POST /admin/users/{id}/password`, `/reset-mfa`, `/revoke-sessions` | Contraseña, MFA perdido y cerrar sesiones (y claves personales con `{"keys": true}`) |
| `GET /admin/limits`, `PUT /admin/limits`, `PATCH/DELETE /admin/limits/{id}` | Cuotas con su uso actual; `PUT` crea o cambia por ámbito, métrica y periodo |
| `GET /admin/content-policies?application_id=`, `PUT /admin/content-policies` | Seguridad de contenido: matriz efectiva y de dónde sale; `"action": "default"` vuelve a lo heredado |
| `GET /admin/overview?hours=` | Resumen: totales, por modelo, por aplicación, serie por hora o día y latencias |
| `GET /admin/audit`, `GET /admin/audit/{id}` | Auditoría con filtros (`since`, `until`, `application_id`, `status`, `model`, `purpose`, `request_id`, `end_user`) y cursor `before_id` |
| `GET /admin/audit/export?since=` | Exportación en JSON Lines con el hash de cadena |
| `GET /admin/audit/chain` | Estado del sellado (operadores) |
| `GET /admin/actions` | Registro de cambios hechos desde el panel |
| `GET/POST /admin/webhooks`, `PATCH/DELETE /admin/webhooks/{id}` | Webhooks; `"global": true` solo operadores |
| `POST /admin/webhooks/{id}/rotate-secret`, `/test`, `GET …/deliveries` | Secreto nuevo, prueba al momento y entregas |
| `GET /admin/catalog` | Proveedores (sin claves), modelos con su precio vigente y despliegues con su enfriamiento |
| `POST /admin/providers`, `PATCH /admin/providers/{id}` | Proveedores (operadores); `api_key` solo se escribe |
| `POST /admin/models`, `PATCH /admin/models/{id}` | Modelos y su cadena de reserva (operadores) |
| `GET/POST /admin/models/{id}/prices` | Precios desde una fecha (operadores escriben) |
| `POST /admin/deployments`, `PATCH /admin/deployments/{id}`, `DELETE …/{id}/cooldown` | Despliegues, peso, prioridad y quitar el enfriamiento (operadores) |

### Convenciones
- Campos desconocidos: 400. Duplicados: 409. Sin permiso: 403. De otra organización: 404.
- Listados largos: `{"data": [...], "next": <id o null>}`; la página siguiente con `before_id=<next>`.
- Importes en USD como texto decimal (`"0.0125"`) y fechas ISO en UTC.
- Cada cambio queda en `admin_actions`; los secretos, como `(cambiado)`.

## Operadores
Quien opera el gateway cambia el catálogo común. Se marca con la CLI y solo a un administrador:

```bash
cli set-user --org cotecnia --email eva@cotecnia.es --operator si
```
