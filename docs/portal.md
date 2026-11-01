# Portal de usuario

El portal está en **http://localhost:8090/portal/** (en producción, en `/portal/` del dominio
del gateway). Entra cualquier persona dada de alta con su correo y su contraseña (y el código
de la app autenticadora si tiene MFA; un administrador tiene que activarlo antes). Las
decisiones están en [ADR-018](adr/ADR-018-portal-de-usuario.md).

| Sección | Qué tiene |
| --- | --- |
| Mi consumo | Peticiones, coste, errores y tokens de los últimos 7, 30 o 90 días; por día y por modelo (con vista de tabla); sus cuotas con lo gastado; y sus últimas peticiones, sin texto |
| Mis claves | Sus claves personales: crear (se enseña una vez, con cómo configurar Claude Code y los SDK), rotar con margen y revocar; y los modelos que puede usar con su precio |
| Mi cuenta | Sus datos, cambiar la contraseña, activar o desactivar el MFA (los administradores no pueden desactivarlo) y sus sesiones abiertas |

Las claves personales necesitan una aplicación con `--personal-keys` en la organización (ver
el README). Sin ella, el portal lo dice y no deja crear claves.

Es el mismo proyecto que el panel (`panel/`, con el portal en `src/portal/` y su entrada en
`portal.html`). En desarrollo, `npm run dev` lo sirve en http://localhost:5173/portal/.

## API

Todo pide `Authorization: Bearer <token de acceso>` de una sesión (no vale una clave de API)
y responde solo con datos de la persona de esa sesión: ninguna ruta acepta otro id de persona.

| Ruta | Qué hace |
| --- | --- |
| `GET /me/portal` | Su cuenta, si hay claves personales (con sus máximos), los modelos que puede usar con su precio y sus cuotas por persona con lo gastado |
| `GET /me/usage?days=1..90` | Totales, por modelo y serie por día (UTC) de sus peticiones |
| `GET /me/requests?before_id=&limit=` | Sus últimas peticiones (`{"data": [...], "next": …}`), sin texto |
| `GET/POST /me/keys`, `DELETE /me/keys/{id}` | Sus claves personales (G10) |
| `POST /me/keys/{id}/rotate` | Clave nueva con el mismo nombre; la anterior vale `grace_hours` más (24 por defecto, máximo 72, 0 la revoca ya). Cuerpo: `{"expires_days": 90, "grace_hours": 24}` |
| `POST /auth/password`, `/auth/mfa/setup`, `/auth/mfa/enable`, `/auth/mfa/disable` | Contraseña y MFA (G10) |
| `GET /auth/sessions`, `DELETE /auth/sessions/{id}`, `POST /auth/sessions/revoke-others` | Sesiones abiertas (G10) |

```bash
curl -s http://localhost:8090/me/usage?days=30 -H "Authorization: Bearer $TOKEN"
curl -s http://localhost:8090/me/keys/12/rotate -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"grace_hours": 4}'
```

El consumo cuenta lo que la persona hace con sus claves y lo que una aplicación (PLATIA, por
ejemplo) hace en su nombre con `X-Platia-User-Id`. Las cuotas que se enseñan son solo las de
cada persona en la aplicación de claves personales: las de equipo u organización dirían
cuánto gastan los demás.
