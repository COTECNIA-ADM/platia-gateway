// Personas de la organización: alta, rol, equipo, contraseña, MFA, sesiones y bajas.
import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import { useAuth } from '../lib/auth'
import { fmtDate } from '../lib/format'
import type { List, User } from '../lib/types'
import { Badge, ConfirmButton, ErrorBox, Field, Modal, Spinner, useToast } from '../components/ui'
import { useTeams } from './Applications'

export function Users() {
  const { me } = useAuth()
  const client = useQueryClient()
  const toast = useToast()
  const users = useQuery({ queryKey: ['users'], queryFn: () => api<List<User>>('/admin/users') })
  const teams = useTeams()
  const [creating, setCreating] = useState(false)
  const [passwordFor, setPasswordFor] = useState<User | null>(null)
  const refresh = () => void client.invalidateQueries({ queryKey: ['users'] })

  const action = useMutation({
    mutationFn: ({ path, method = 'POST', body }: { path: string; method?: string; body?: unknown; done: string }) =>
      api(path, { method, body }),
    onSuccess: (_, vars) => {
      refresh()
      toast(vars.done)
    },
  })

  return (
    <>
      <header className="page-head">
        <h1>Personas</h1>
        <button className="btn btn-primary" onClick={() => setCreating(true)}>
          Nueva persona
        </button>
      </header>
      {users.isError && <ErrorBox error={users.error} />}
      {action.isError && <ErrorBox error={action.error} />}
      {users.isPending && <Spinner />}
      {users.data && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Persona</th>
                <th>Rol</th>
                <th>Equipo</th>
                <th>Seguridad</th>
                <th>Último acceso</th>
                <th>Acciones</th>
              </tr>
            </thead>
            <tbody>
              {users.data.data.map((u) => {
                const self = u.id === me?.id
                return (
                  <tr key={u.id} className={u.is_active ? undefined : 'row-muted'}>
                    <td>
                      {u.name}
                      <div className="muted small">{u.email}</div>
                    </td>
                    <td>
                      <select
                        aria-label={`Rol de ${u.name}`}
                        value={u.role}
                        disabled={self}
                        onChange={(e) =>
                          action.mutate({ path: `/admin/users/${u.id}`, method: 'PATCH', body: { role: e.target.value }, done: 'Rol cambiado' })
                        }
                      >
                        <option value="member">Miembro</option>
                        <option value="admin">Administrador</option>
                      </select>
                      {u.platform_operator && <div className="small">Opera el gateway</div>}
                    </td>
                    <td>
                      <select
                        aria-label={`Equipo de ${u.name}`}
                        value={u.team_id ?? ''}
                        onChange={(e) =>
                          action.mutate({
                            path: `/admin/users/${u.id}`,
                            method: 'PATCH',
                            body: { team_id: e.target.value ? Number(e.target.value) : null },
                            done: 'Equipo cambiado',
                          })
                        }
                      >
                        <option value="">Sin equipo</option>
                        {teams.data?.data.map((t) => (
                          <option key={t.id} value={t.id}>
                            {t.name}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>
                      {!u.is_active && <Badge tone="neutral">Inactiva</Badge>}{' '}
                      {u.mfa_enabled ? <Badge tone="success">MFA</Badge> : <Badge tone="warning">Sin MFA</Badge>}{' '}
                      {!u.has_password && <Badge tone="warning">Sin contraseña</Badge>}
                      <div className="muted small">
                        {u.active_sessions} sesiones · {u.active_keys} claves
                      </div>
                    </td>
                    <td>{fmtDate(u.last_login_at)}</td>
                    <td className="btn-row">
                      {!self && (
                        <>
                          <button className="btn btn-small" onClick={() => setPasswordFor(u)}>
                            Contraseña
                          </button>
                          {u.mfa_enabled && (
                            <ConfirmButton
                              className="btn btn-small"
                              question="¿Quitar su MFA?"
                              onConfirm={() => action.mutate({ path: `/admin/users/${u.id}/reset-mfa`, done: 'MFA quitado: lo configurará al entrar' })}
                            >
                              Quitar MFA
                            </ConfirmButton>
                          )}
                          <ConfirmButton
                            className="btn btn-small"
                            question="¿Cerrar sesiones y revocar sus claves personales?"
                            onConfirm={() =>
                              action.mutate({ path: `/admin/users/${u.id}/revoke-sessions`, body: { keys: true }, done: 'Sesiones cerradas y claves revocadas' })
                            }
                          >
                            Cerrar sesiones
                          </ConfirmButton>
                          <ConfirmButton
                            className="btn btn-small"
                            question={u.is_active ? '¿Desactivarla?' : '¿Reactivarla?'}
                            onConfirm={() =>
                              action.mutate({
                                path: `/admin/users/${u.id}`,
                                method: 'PATCH',
                                body: { is_active: !u.is_active },
                                done: u.is_active ? 'Persona desactivada' : 'Persona reactivada',
                              })
                            }
                          >
                            {u.is_active ? 'Desactivar' : 'Reactivar'}
                          </ConfirmButton>
                          <ConfirmButton
                            question="¿Darla de baja? Se conserva su auditoría."
                            onConfirm={() => action.mutate({ path: `/admin/users/${u.id}`, method: 'DELETE', done: 'Persona dada de baja' })}
                          >
                            Baja
                          </ConfirmButton>
                        </>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      {creating && <NewUser onClose={() => setCreating(false)} />}
      {passwordFor && <SetPassword user={passwordFor} onClose={() => setPasswordFor(null)} />}
    </>
  )
}

function NewUser({ onClose }: { onClose: () => void }) {
  const client = useQueryClient()
  const toast = useToast()
  const teams = useTeams()
  const [form, setForm] = useState({ email: '', name: '', role: 'member', team_id: '', password: '' })
  const create = useMutation({
    mutationFn: () =>
      api<User>('/admin/users', {
        body: {
          email: form.email,
          name: form.name,
          role: form.role,
          team_id: form.team_id ? Number(form.team_id) : null,
          password: form.password || null,
        },
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['users'] })
      toast('Persona creada')
      onClose()
    },
  })
  const set = (changes: Partial<typeof form>) => setForm((f) => ({ ...f, ...changes }))
  return (
    <Modal title="Nueva persona" onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault()
          create.mutate()
        }}
      >
        {create.isError && <ErrorBox error={create.error} />}
        <Field label="Nombre">
          <input value={form.name} onChange={(e) => set({ name: e.target.value })} required />
        </Field>
        <Field label="Correo">
          <input type="email" value={form.email} onChange={(e) => set({ email: e.target.value })} required />
        </Field>
        <Field label="Rol">
          <select value={form.role} onChange={(e) => set({ role: e.target.value })}>
            <option value="member">Miembro</option>
            <option value="admin">Administrador</option>
          </select>
        </Field>
        <Field label="Equipo">
          <select value={form.team_id} onChange={(e) => set({ team_id: e.target.value })}>
            <option value="">Sin equipo</option>
            {teams.data?.data.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Contraseña inicial" hint="Opcional; se la das por un canal seguro y la cambia al entrar">
          <input type="password" value={form.password} onChange={(e) => set({ password: e.target.value })} autoComplete="new-password" />
        </Field>
        <button className="btn btn-primary" disabled={create.isPending}>
          Crear
        </button>
      </form>
    </Modal>
  )
}

function SetPassword({ user, onClose }: { user: User; onClose: () => void }) {
  const toast = useToast()
  const [password, setPassword] = useState('')
  const save = useMutation({
    mutationFn: () => api<{ sessions_closed: number }>(`/admin/users/${user.id}/password`, { body: { password } }),
    onSuccess: (r) => {
      toast(`Contraseña cambiada; ${r.sessions_closed} sesiones cerradas`)
      onClose()
    },
  })
  return (
    <Modal title={`Contraseña de ${user.name}`} onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault()
          save.mutate()
        }}
      >
        {save.isError && <ErrorBox error={save.error} />}
        <Field label="Contraseña nueva" hint="Al menos 12 caracteres; cierra sus sesiones">
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" required />
        </Field>
        <button className="btn btn-primary" disabled={save.isPending}>
          Guardar
        </button>
      </form>
    </Modal>
  )
}
