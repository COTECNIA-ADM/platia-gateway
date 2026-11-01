// Una aplicación: reglas, caché y claves. La clave nueva se enseña una sola vez.
import { useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import { fmtDate } from '../lib/format'
import { joinList, splitList } from '../lib/lists'
import type { ApiKey, Application, List, NewKey } from '../lib/types'
import { ConfirmButton, ErrorBox, Field, Modal, OneTimeSecret, Spinner, useToast } from '../components/ui'
import { KeyState } from './Keys'

export function ApplicationDetail() {
  const id = Number(useParams().id)
  const app = useQuery({ queryKey: ['application', id], queryFn: () => api<Application>(`/admin/applications/${id}`) })
  return (
    <>
      <p>
        <Link to="/applications">← Aplicaciones</Link>
      </p>
      {app.isError && <ErrorBox error={app.error} />}
      {app.isPending && <Spinner />}
      {app.data && (
        <>
          <header className="page-head">
            <h1>{app.data.name}</h1>
            <span className="muted">{app.data.slug}</span>
          </header>
          <Settings app={app.data} />
          <Keys app={app.data} />
        </>
      )}
    </>
  )
}

function Settings({ app }: { app: Application }) {
  const client = useQueryClient()
  const toast = useToast()
  const [form, setForm] = useState({
    name: app.name,
    purposes: joinList(app.allowed_purposes),
    models: joinList(app.allowed_models),
    trusted: app.trusted,
    cache_ttl_seconds: String(app.cache_ttl_seconds),
    cache_purposes: joinList(app.cache_purposes),
  })
  const save = useMutation({
    mutationFn: () =>
      api<Application>(`/admin/applications/${app.id}`, {
        method: 'PATCH',
        body: {
          name: form.name,
          allowed_purposes: splitList(form.purposes),
          allowed_models: form.models.trim() ? splitList(form.models) : null,
          trusted: form.trusted,
          cache_ttl_seconds: Number(form.cache_ttl_seconds),
          cache_purposes: form.cache_purposes.trim() ? splitList(form.cache_purposes) : null,
        },
      }),
    onSuccess: (updated) => {
      client.setQueryData(['application', app.id], updated)
      void client.invalidateQueries({ queryKey: ['applications'] })
      toast('Cambios guardados')
    },
  })
  const set = (changes: Partial<typeof form>) => setForm((f) => ({ ...f, ...changes }))
  return (
    <section className="card">
      <h2>Reglas</h2>
      <form
        className="form-grid"
        onSubmit={(e: FormEvent) => {
          e.preventDefault()
          save.mutate()
        }}
      >
        {save.isError && <ErrorBox error={save.error} />}
        <Field label="Nombre">
          <input value={form.name} onChange={(e) => set({ name: e.target.value })} required />
        </Field>
        <Field label="Propósitos permitidos" hint="Separados por comas; vacío: cualquiera">
          <input value={form.purposes} onChange={(e) => set({ purposes: e.target.value })} />
        </Field>
        <Field label="Modelos permitidos" hint="Vacío: todos los del catálogo">
          <input value={form.models} onChange={(e) => set({ models: e.target.value })} />
        </Field>
        <Field label="Caché (segundos)" hint="0: sin caché; máximo 30 días">
          <input type="number" min={0} max={2592000} value={form.cache_ttl_seconds} onChange={(e) => set({ cache_ttl_seconds: e.target.value })} />
        </Field>
        <Field label="Propósitos con caché" hint="Vacío: todos">
          <input value={form.cache_purposes} onChange={(e) => set({ cache_purposes: e.target.value })} />
        </Field>
        <label className="check">
          <input type="checkbox" checked={form.trusted} onChange={(e) => set({ trusted: e.target.checked })} /> De confianza
        </label>
        <div>
          <button className="btn btn-primary" disabled={save.isPending}>
            Guardar
          </button>
        </div>
      </form>
    </section>
  )
}

function Keys({ app }: { app: Application }) {
  const client = useQueryClient()
  const toast = useToast()
  const keys = useQuery({ queryKey: ['keys', app.id], queryFn: () => api<List<ApiKey>>(`/admin/applications/${app.id}/keys`) })
  const [label, setLabel] = useState('')
  const [days, setDays] = useState('')
  const [created, setCreated] = useState<NewKey | null>(null)
  const create = useMutation({
    mutationFn: () => api<NewKey>(`/admin/applications/${app.id}/keys`, { body: { label, expires_days: days ? Number(days) : null } }),
    onSuccess: (key) => {
      setCreated(key)
      setLabel('')
      setDays('')
      void client.invalidateQueries({ queryKey: ['keys'] })
    },
  })
  const revoke = useMutation({
    mutationFn: (id: number) => api(`/admin/keys/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['keys'] })
      toast('Clave revocada')
    },
  })
  return (
    <section className="card">
      <h2>Claves</h2>
      {app.personal_keys ? (
        <p className="muted">Las claves de esta aplicación las crea cada persona en su portal.</p>
      ) : (
        <form
          className="inline-form"
          onSubmit={(e: FormEvent) => {
            e.preventDefault()
            create.mutate()
          }}
        >
          <Field label="Etiqueta">
            <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="producción" />
          </Field>
          <Field label="Caduca en (días)" hint="Vacío: no caduca">
            <input type="number" min={1} max={3650} value={days} onChange={(e) => setDays(e.target.value)} />
          </Field>
          <button className="btn btn-primary" disabled={create.isPending}>
            Crear clave
          </button>
        </form>
      )}
      {create.isError && <ErrorBox error={create.error} />}
      {revoke.isError && <ErrorBox error={revoke.error} />}
      {keys.isPending && <Spinner />}
      {keys.data && keys.data.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Prefijo</th>
                <th>Etiqueta</th>
                <th>Estado</th>
                <th>Creada</th>
                <th>Caduca</th>
                <th>Último uso</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {keys.data.data.map((k) => (
                <tr key={k.id}>
                  <td>
                    <code>{k.prefix}…</code>
                  </td>
                  <td>{k.label || '—'}</td>
                  <td>
                    <KeyState state={k.state} />
                  </td>
                  <td>{fmtDate(k.created_at)}</td>
                  <td>{fmtDate(k.expires_at)}</td>
                  <td>{fmtDate(k.last_used_at)}</td>
                  <td>
                    {k.state === 'active' && (
                      <ConfirmButton question="¿Revocarla? Dejará de funcionar ya." onConfirm={() => revoke.mutate(k.id)}>
                        Revocar
                      </ConfirmButton>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {created && (
        <Modal title="Clave creada" onClose={() => setCreated(null)}>
          <OneTimeSecret label="Clave de la aplicación" value={created.key} />
        </Modal>
      )}
    </section>
  )
}
