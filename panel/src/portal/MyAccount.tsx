// Mi cuenta (cap. 31): datos, contraseña, segundo factor y sesiones abiertas.
import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import { useAuth } from '../lib/auth'
import { fmtDate } from '../lib/format'
import type { List } from '../lib/types'
import { Badge, ConfirmButton, ErrorBox, Field, Spinner, useToast } from '../components/ui'
import type { MySession, PortalInfo } from './types'

export function MyAccount() {
  const info = useQuery({ queryKey: ['me-portal'], queryFn: () => api<PortalInfo>('/me/portal') })
  const user = info.data?.user
  return (
    <>
      <header className="page-head">
        <h1>Mi cuenta</h1>
      </header>
      {info.isError && <ErrorBox error={info.error} onRetry={() => void info.refetch()} />}
      {info.isPending && <Spinner />}
      {user && (
        <section className="card">
          <h2>Datos</h2>
          <dl className="details">
            {(
              [
                ['Nombre', user.name],
                ['Correo', user.email],
                ['Organización', user.organization],
                ['Equipo', user.team ?? '—'],
                ['Rol', user.role === 'admin' ? 'Administración' : 'Miembro'],
                ['Último acceso', fmtDate(user.last_login_at)],
              ] as const
            ).map(([k, v]) => (
              <div key={k}>
                <dt>{k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
          <p className="muted small">Para cambiar el nombre, el equipo o el rol, habla con un administrador.</p>
        </section>
      )}
      <PasswordForm changedAt={user?.password_changed_at ?? null} />
      {user && <MfaCard enabled={user.mfa_enabled} admin={user.role === 'admin'} />}
      <Sessions />
    </>
  )
}

function PasswordForm({ changedAt }: { changedAt: string | null }) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [repeat, setRepeat] = useState('')
  const toast = useToast()
  const client = useQueryClient()
  const change = useMutation({
    mutationFn: () => api<{ other_sessions_closed: number }>('/auth/password', { body: { current_password: current, new_password: next } }),
    onSuccess: (r) => {
      setCurrent('')
      setNext('')
      setRepeat('')
      toast(r.other_sessions_closed ? `Contraseña cambiada; se han cerrado ${r.other_sessions_closed} sesiones más` : 'Contraseña cambiada')
      void client.invalidateQueries({ queryKey: ['me-portal'] })
      void client.invalidateQueries({ queryKey: ['me-sessions'] })
    },
  })
  const mismatch = repeat !== '' && repeat !== next
  function submit(e: FormEvent) {
    e.preventDefault()
    if (!mismatch) change.mutate()
  }
  return (
    <section className="card">
      <h2>Contraseña</h2>
      {changedAt && <p className="muted">Último cambio: {fmtDate(changedAt)}. Al cambiarla se cierran tus otras sesiones.</p>}
      <form onSubmit={submit}>
        <Field label="Contraseña actual">
          <input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required />
        </Field>
        <Field label="Contraseña nueva">
          <input type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} required />
        </Field>
        <Field label="Repite la nueva">
          <input type="password" autoComplete="new-password" value={repeat} onChange={(e) => setRepeat(e.target.value)} required />
        </Field>
        {mismatch && <p className="notice notice-warning">Las dos contraseñas nuevas no coinciden.</p>}
        {change.isError && <ErrorBox error={change.error} />}
        <button className="btn btn-primary" disabled={change.isPending || mismatch}>
          Cambiar contraseña
        </button>
      </form>
    </section>
  )
}

type Setup = { secret: string; otpauth_uri: string }

function MfaCard({ enabled, admin }: { enabled: boolean; admin: boolean }) {
  const { enableMfa } = useAuth()
  const client = useQueryClient()
  const toast = useToast()
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const setup = useMutation({ mutationFn: () => api<Setup>('/auth/mfa/setup', { method: 'POST' }) })
  const done = (text: string) => {
    setCode('')
    setPassword('')
    setup.reset()
    toast(text)
    void client.invalidateQueries({ queryKey: ['me-portal'] })
  }
  const enable = useMutation({ mutationFn: () => enableMfa(code.trim()), onSuccess: () => done('Segundo factor activado') })
  const disable = useMutation({
    mutationFn: () => api('/auth/mfa/disable', { body: { password, code: code.trim() } }),
    onSuccess: () => done('Segundo factor desactivado'),
  })

  const codeField = (
    <Field label="Código de la app autenticadora">
      <input value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" required />
    </Field>
  )

  if (enabled)
    return (
      <section className="card">
        <h2>
          Segundo factor <Badge tone="success">Activado</Badge>
        </h2>
        {admin ? (
          <p className="muted">Los administradores no pueden desactivarlo. Si pierdes el móvil, otro administrador puede reiniciarlo.</p>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault()
              disable.mutate()
            }}
          >
            <p>Para desactivarlo hacen falta tu contraseña y un código.</p>
            <Field label="Contraseña">
              <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
            </Field>
            {codeField}
            {disable.isError && <ErrorBox error={disable.error} />}
            <button className="btn btn-danger" disabled={disable.isPending}>
              Desactivar
            </button>
          </form>
        )}
      </section>
    )

  return (
    <section className="card">
      <h2>
        Segundo factor <Badge tone="warning">Sin activar</Badge>
      </h2>
      <p>Protege tu cuenta y tus claves con una app autenticadora (Google Authenticator, Microsoft Authenticator, 1Password…).</p>
      {!setup.data ? (
        <>
          {setup.isError && <ErrorBox error={setup.error} />}
          <button className="btn btn-primary" onClick={() => setup.mutate()} disabled={setup.isPending}>
            Activar
          </button>
        </>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            enable.mutate()
          }}
        >
          <p>Añade esta clave en la app (o abre el enlace desde el móvil):</p>
          <code className="secret-value">{setup.data.secret}</code>
          <p>
            <a href={setup.data.otpauth_uri}>Abrir en la app autenticadora</a>
          </p>
          {codeField}
          {enable.isError && <ErrorBox error={enable.error} />}
          <button className="btn btn-primary" disabled={enable.isPending}>
            Confirmar
          </button>
        </form>
      )}
    </section>
  )
}

function Sessions() {
  const { logout } = useAuth()
  const client = useQueryClient()
  const toast = useToast()
  const sessions = useQuery({ queryKey: ['me-sessions'], queryFn: () => api<List<MySession>>('/auth/sessions') })
  const refresh = () => void client.invalidateQueries({ queryKey: ['me-sessions'] })
  const revoke = useMutation({
    mutationFn: (id: number) => api(`/auth/sessions/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      toast('Sesión cerrada')
      refresh()
    },
    onError: (e) => toast(e instanceof Error ? e.message : 'No se pudo cerrar', 'danger'),
  })
  const others = useMutation({
    mutationFn: () => api<{ sessions_closed: number }>('/auth/sessions/revoke-others', { method: 'POST' }),
    onSuccess: (r) => {
      toast(`Sesiones cerradas: ${r.sessions_closed}`)
      refresh()
    },
    onError: (e) => toast(e instanceof Error ? e.message : 'No se pudieron cerrar', 'danger'),
  })
  const rows = sessions.data?.data ?? []
  return (
    <section className="card">
      <header className="card-head">
        <h2>Sesiones abiertas</h2>
        <div className="btn-row">
          {rows.length > 1 && (
            <ConfirmButton question="¿Cerrar todas las demás sesiones?" onConfirm={() => others.mutate()}>
              Cerrar las demás
            </ConfirmButton>
          )}
          <button className="btn btn-small" onClick={() => void logout()}>
            Salir
          </button>
        </div>
      </header>
      {sessions.isError && <ErrorBox error={sessions.error} onRetry={() => void sessions.refetch()} />}
      {sessions.isPending && <Spinner />}
      {rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Navegador</th>
                <th>Dirección</th>
                <th>Inicio</th>
                <th>Último uso</th>
                <th>
                  <span className="sr-only">Acciones</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((s) => (
                <tr key={s.id}>
                  <td className="truncate" title={s.user_agent ?? undefined}>
                    {s.user_agent ?? '—'}
                  </td>
                  <td>{s.ip_address ?? '—'}</td>
                  <td>{fmtDate(s.created_at)}</td>
                  <td>{fmtDate(s.last_used_at)}</td>
                  <td>
                    {s.current ? (
                      <Badge tone="info">Esta sesión</Badge>
                    ) : (
                      <button className="btn btn-small" onClick={() => revoke.mutate(s.id)} aria-label={`Cerrar la sesión iniciada el ${fmtDate(s.created_at)}`}>
                        Cerrar
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
