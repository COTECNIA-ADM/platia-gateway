// Webhooks firmados (ADR-016): alta, eventos, prueba al momento, entregas y secreto nuevo.
import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import { useAuth } from '../lib/auth'
import { fmtDate, fmtMs } from '../lib/format'
import type { Delivery, List, Webhook } from '../lib/types'
import { Badge, ConfirmButton, Empty, ErrorBox, Field, Modal, OneTimeSecret, Spinner, useToast } from '../components/ui'

type Hooks = List<Webhook> & { events: string[] }
type TestResult = { ok: boolean; status_code: number | null; error: string | null; duration_ms: number | null }

export function Webhooks() {
  const client = useQueryClient()
  const toast = useToast()
  const hooks = useQuery({ queryKey: ['webhooks'], queryFn: () => api<Hooks>('/admin/webhooks') })
  const [creating, setCreating] = useState(false)
  const [secret, setSecret] = useState<string | null>(null)
  const [deliveriesOf, setDeliveriesOf] = useState<Webhook | null>(null)
  const refresh = () => void client.invalidateQueries({ queryKey: ['webhooks'] })
  const toggle = useMutation({
    mutationFn: (hook: Webhook) => api(`/admin/webhooks/${hook.id}`, { method: 'PATCH', body: { is_active: !hook.is_active } }),
    onSuccess: refresh,
  })
  const remove = useMutation({
    mutationFn: (id: number) => api(`/admin/webhooks/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      refresh()
      toast('Webhook borrado')
    },
  })
  const rotate = useMutation({
    mutationFn: (id: number) => api<{ secret: string }>(`/admin/webhooks/${id}/rotate-secret`, { method: 'POST' }),
    onSuccess: (r) => setSecret(r.secret),
  })
  const test = useMutation({
    mutationFn: (id: number) => api<TestResult>(`/admin/webhooks/${id}/test`, { method: 'POST' }),
    onSuccess: (r) => {
      refresh()
      toast(
        r.ok ? `El receptor respondió ${r.status_code} en ${fmtMs(r.duration_ms)}` : `Falló: ${r.error ?? r.status_code}`,
        r.ok ? 'success' : 'danger',
      )
    },
  })
  const error = [toggle, remove, rotate, test].find((m) => m.isError)?.error

  return (
    <>
      <header className="page-head">
        <h1>Webhooks</h1>
        <button className="btn btn-primary" onClick={() => setCreating(true)}>
          Nuevo webhook
        </button>
      </header>
      <p className="muted">
        Cada aviso va firmado con <code>X-Platia-Signature</code>. Cómo comprobarla: docs/telemetria.md. Tras 5 entregas fallidas
        seguidas el webhook se desactiva.
      </p>
      {hooks.isError && <ErrorBox error={hooks.error} />}
      {error !== undefined && <ErrorBox error={error} />}
      {hooks.isPending && <Spinner />}
      {hooks.data && hooks.data.data.length === 0 && <Empty title="No hay webhooks" />}
      {hooks.data && hooks.data.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Webhook</th>
                <th>Eventos</th>
                <th>Estado</th>
                <th>Entregas</th>
                <th>Acciones</th>
              </tr>
            </thead>
            <tbody>
              {hooks.data.data.map((h) => (
                <tr key={h.id}>
                  <td>
                    {h.name} {h.global && <Badge tone="accent">Global</Badge>}
                    <div className="muted small">{h.url}</div>
                  </td>
                  <td className="small">{h.events.join(', ')}</td>
                  <td>
                    {h.is_active ? <Badge tone="success">Activo</Badge> : <Badge tone="warning">Inactivo</Badge>}
                    {h.disabled_reason && <div className="muted small">{h.disabled_reason}</div>}
                    {h.consecutive_failures > 0 && <div className="small">{h.consecutive_failures} fallos seguidos</div>}
                  </td>
                  <td className="small">
                    {h.deliveries.delivered ?? 0} entregadas · {h.deliveries.pending ?? 0} pendientes · {h.deliveries.failed ?? 0} fallidas
                  </td>
                  <td className="btn-row">
                    <button className="btn btn-small" onClick={() => test.mutate(h.id)} disabled={test.isPending}>
                      Probar
                    </button>
                    <button className="btn btn-small" onClick={() => setDeliveriesOf(h)}>
                      Entregas
                    </button>
                    <button className="btn btn-small" onClick={() => toggle.mutate(h)}>
                      {h.is_active ? 'Desactivar' : 'Activar'}
                    </button>
                    <ConfirmButton className="btn btn-small" question="¿Secreto nuevo? El anterior deja de valer." onConfirm={() => rotate.mutate(h.id)}>
                      Secreto nuevo
                    </ConfirmButton>
                    <ConfirmButton question="¿Borrarlo?" onConfirm={() => remove.mutate(h.id)}>
                      Borrar
                    </ConfirmButton>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {creating && hooks.data && <NewWebhook events={hooks.data.events} onClose={() => setCreating(false)} onSecret={setSecret} />}
      {secret && (
        <Modal title="Secreto de firma" onClose={() => setSecret(null)}>
          <OneTimeSecret label="Secreto de firma" value={secret} />
        </Modal>
      )}
      {deliveriesOf && <Deliveries hook={deliveriesOf} onClose={() => setDeliveriesOf(null)} />}
    </>
  )
}

function NewWebhook({ events, onClose, onSecret }: { events: string[]; onClose: () => void; onSecret: (s: string) => void }) {
  const { me } = useAuth()
  const client = useQueryClient()
  const [form, setForm] = useState({ name: '', url: 'https://', events: [] as string[], global: false })
  const create = useMutation({
    mutationFn: () => api<Webhook & { secret: string }>('/admin/webhooks', { body: form }),
    onSuccess: (hook) => {
      void client.invalidateQueries({ queryKey: ['webhooks'] })
      onClose()
      onSecret(hook.secret)
    },
  })
  const toggleEvent = (event: string) =>
    setForm((f) => ({ ...f, events: f.events.includes(event) ? f.events.filter((e) => e !== event) : [...f.events, event] }))
  return (
    <Modal title="Nuevo webhook" onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault()
          create.mutate()
        }}
      >
        {create.isError && <ErrorBox error={create.error} />}
        <Field label="Nombre">
          <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
        </Field>
        <Field label="URL" hint="https y una dirección pública">
          <input type="url" value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} required />
        </Field>
        <fieldset className="checks">
          <legend>Eventos</legend>
          {events.map((event) => (
            <label key={event} className="check">
              <input type="checkbox" checked={form.events.includes(event)} onChange={() => toggleEvent(event)} /> {event}
            </label>
          ))}
        </fieldset>
        {me?.platform_operator && (
          <label className="check">
            <input type="checkbox" checked={form.global} onChange={(e) => setForm({ ...form, global: e.target.checked })} /> Global (todas
            las organizaciones y los eventos del gateway)
          </label>
        )}
        <button className="btn btn-primary" disabled={create.isPending || form.events.length === 0}>
          Crear
        </button>
      </form>
    </Modal>
  )
}

function Deliveries({ hook, onClose }: { hook: Webhook; onClose: () => void }) {
  const rows = useQuery({ queryKey: ['deliveries', hook.id], queryFn: () => api<List<Delivery>>(`/admin/webhooks/${hook.id}/deliveries`) })
  return (
    <Modal title={`Entregas de ${hook.name}`} onClose={onClose} wide>
      {rows.isError && <ErrorBox error={rows.error} />}
      {rows.isPending && <Spinner />}
      {rows.data && rows.data.data.length === 0 && <Empty title="Sin entregas" />}
      {rows.data && rows.data.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Evento</th>
                <th>Estado</th>
                <th>Intentos</th>
                <th>Última respuesta</th>
                <th>Creada</th>
                <th>Siguiente intento</th>
              </tr>
            </thead>
            <tbody>
              {rows.data.data.map((d) => (
                <tr key={d.id}>
                  <td>{d.event}</td>
                  <td>{d.status}</td>
                  <td>{d.attempts}</td>
                  <td>
                    {d.last_status_code ?? d.last_error ?? '—'} {d.last_duration_ms !== null && <span className="muted small">{fmtMs(d.last_duration_ms)}</span>}
                  </td>
                  <td>{fmtDate(d.created_at)}</td>
                  <td>{d.status === 'pending' ? fmtDate(d.next_attempt_at) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  )
}
