// Cuotas y límites de ritmo (ADR-011) con su uso actual.
import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import { fmtNum, fmtUsd, METRIC_LABEL, PERIOD_LABEL } from '../lib/format'
import type { Application, Limit, List } from '../lib/types'
import { Meter } from '../components/charts'
import { ConfirmButton, Empty, ErrorBox, Field, Modal, Spinner, useToast } from '../components/ui'
import { useTeams } from './Applications'

const SCOPE_LABEL: Record<Limit['scope'], string> = {
  organization: 'Organización',
  team: 'Equipo',
  application: 'Aplicación',
  end_user: 'Persona final',
}

function amount(limit: Pick<Limit, 'metric'>, value: string | null) {
  return limit.metric === 'cost_usd' ? fmtUsd(value) : fmtNum(value)
}

export function Limits() {
  const client = useQueryClient()
  const toast = useToast()
  const limits = useQuery({ queryKey: ['limits'], queryFn: () => api<List<Limit>>('/admin/limits') })
  const [creating, setCreating] = useState(false)
  const change = useMutation({
    mutationFn: ({ id, body }: { id: number; body?: unknown }) =>
      api(`/admin/limits/${id}`, body ? { method: 'PATCH', body } : { method: 'DELETE' }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['limits'] })
      toast('Cuota actualizada')
    },
  })
  return (
    <>
      <header className="page-head">
        <h1>Cuotas</h1>
        <button className="btn btn-primary" onClick={() => setCreating(true)}>
          Nueva cuota
        </button>
      </header>
      <p className="muted">
        Al llegar al porcentaje de aviso se manda un aviso; al 100 % el gateway rechaza las peticiones (402, o 429 en los límites por
        minuto y de simultáneas).
      </p>
      {limits.isError && <ErrorBox error={limits.error} />}
      {change.isError && <ErrorBox error={change.error} />}
      {limits.isPending && <Spinner />}
      {limits.data && limits.data.data.length === 0 && <Empty title="No hay cuotas: el uso no tiene tope" />}
      {limits.data && limits.data.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Ámbito</th>
                <th>Qué</th>
                <th>Uso</th>
                <th>Límite</th>
                <th>Aviso</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {limits.data.data.map((l) => (
                <tr key={l.id} className={l.is_active ? undefined : 'row-muted'}>
                  <td>
                    {SCOPE_LABEL[l.scope]} <span className="muted small">{l.scope_label}</span>
                    {l.subject && <div className="small">{l.subject}</div>}
                  </td>
                  <td>
                    {METRIC_LABEL[l.metric]} {PERIOD_LABEL[l.period]}
                  </td>
                  <td>
                    {l.used !== null ? (
                      <>
                        <Meter value={Number(l.used)} max={Number(l.value)} warnAt={l.alert_pct / 100} /> {amount(l, l.used)}
                      </>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>{amount(l, l.value)}</td>
                  <td>{l.alert_pct} %</td>
                  <td className="btn-row">
                    <button className="btn btn-small" onClick={() => change.mutate({ id: l.id, body: { is_active: !l.is_active } })}>
                      {l.is_active ? 'Pausar' : 'Activar'}
                    </button>
                    <ConfirmButton question="¿Borrar la cuota?" onConfirm={() => change.mutate({ id: l.id })}>
                      Borrar
                    </ConfirmButton>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {creating && <NewLimit onClose={() => setCreating(false)} />}
    </>
  )
}

function NewLimit({ onClose }: { onClose: () => void }) {
  const client = useQueryClient()
  const toast = useToast()
  const teams = useTeams()
  const apps = useQuery({ queryKey: ['applications'], queryFn: () => api<List<Application>>('/admin/applications') })
  const [form, setForm] = useState({ scope: 'application', scope_id: '', subject: '', metric: 'cost_usd', period: 'month', value: '', alert_pct: '80' })
  const set = (changes: Partial<typeof form>) => setForm((f) => ({ ...f, ...changes }))
  const concurrent = form.metric === 'concurrent'
  const save = useMutation({
    mutationFn: () =>
      api<Limit>('/admin/limits', {
        method: 'PUT',
        body: {
          scope: form.scope,
          scope_id: form.scope === 'organization' ? null : Number(form.scope_id),
          subject: form.scope === 'end_user' && form.subject ? form.subject : null,
          metric: form.metric,
          period: concurrent ? 'none' : form.period,
          value: form.value,
          alert_pct: Number(form.alert_pct),
        },
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['limits'] })
      toast('Cuota guardada')
      onClose()
    },
  })
  const targets = form.scope === 'team' ? teams.data?.data : form.scope === 'organization' ? [] : apps.data?.data
  return (
    <Modal title="Nueva cuota" onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault()
          save.mutate()
        }}
      >
        {save.isError && <ErrorBox error={save.error} />}
        <Field label="Ámbito">
          <select value={form.scope} onChange={(e) => set({ scope: e.target.value, scope_id: '' })}>
            {Object.entries(SCOPE_LABEL).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </Field>
        {form.scope !== 'organization' && (
          <Field label={form.scope === 'team' ? 'Equipo' : 'Aplicación'}>
            <select value={form.scope_id} onChange={(e) => set({ scope_id: e.target.value })} required>
              <option value="">Elige…</option>
              {targets?.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </Field>
        )}
        {form.scope === 'end_user' && (
          <Field label="Persona concreta" hint="Su X-Platia-User-Id; vacío: cada persona por separado">
            <input value={form.subject} onChange={(e) => set({ subject: e.target.value })} />
          </Field>
        )}
        <Field label="Métrica">
          <select value={form.metric} onChange={(e) => set({ metric: e.target.value })}>
            {Object.entries(METRIC_LABEL).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </Field>
        {!concurrent && (
          <Field label="Periodo">
            <select value={form.period} onChange={(e) => set({ period: e.target.value })}>
              <option value="minute">Por minuto</option>
              <option value="day">Al día</option>
              <option value="month">Al mes</option>
            </select>
          </Field>
        )}
        <Field label="Límite">
          <input type="number" min={0} step="any" value={form.value} onChange={(e) => set({ value: e.target.value })} required />
        </Field>
        <Field label="Aviso al (%)">
          <input type="number" min={1} max={100} value={form.alert_pct} onChange={(e) => set({ alert_pct: e.target.value })} />
        </Field>
        <button className="btn btn-primary" disabled={save.isPending}>
          Guardar
        </button>
      </form>
    </Modal>
  )
}
