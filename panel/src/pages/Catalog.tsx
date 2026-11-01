// Catálogo común: proveedores, modelos con su precio y despliegues con su estado (G6).
// Lo ven los administradores; lo cambia solo quien opera el gateway (ADR-017).
import { useState, type FormEvent, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import { fmtUsd } from '../lib/format'
import { joinList, splitList } from '../lib/lists'
import type { Catalog as CatalogData, Deployment, Model, Provider } from '../lib/types'
import { Badge, ErrorBox, Field, Modal, Spinner, useToast } from '../components/ui'

type Dialog = { kind: 'provider'; provider?: Provider } | { kind: 'model' } | { kind: 'price'; model: Model } | { kind: 'fallbacks'; model: Model } | { kind: 'deployment' }

export function Catalog() {
  const client = useQueryClient()
  const toast = useToast()
  const catalog = useQuery({ queryKey: ['catalog'], queryFn: () => api<CatalogData>('/admin/catalog') })
  const [dialog, setDialog] = useState<Dialog | null>(null)
  const refresh = () => void client.invalidateQueries({ queryKey: ['catalog'] })
  const patch = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown }) => api(path, body ? { method: 'PATCH', body } : { method: 'DELETE' }),
    onSuccess: () => {
      refresh()
      toast('Catálogo actualizado')
    },
  })
  const data = catalog.data
  const edit = data?.can_edit ?? false
  const providerName = (id: number) => data?.providers.find((p) => p.id === id)?.slug ?? `#${id}`
  const modelKey = (id: number) => data?.models.find((m) => m.id === id)?.model_key ?? `#${id}`

  return (
    <>
      <header className="page-head">
        <h1>Catálogo de modelos</h1>
        {data && !edit && <span className="muted">Solo lectura: lo cambia quien opera el gateway</span>}
      </header>
      {catalog.isError && <ErrorBox error={catalog.error} />}
      {patch.isError && <ErrorBox error={patch.error} />}
      {catalog.isPending && <Spinner />}
      {data && (
        <>
          <Section title="Proveedores" action={edit && <button className="btn btn-small" onClick={() => setDialog({ kind: 'provider' })}>Nuevo proveedor</button>}>
            <table>
              <thead>
                <tr>
                  <th>Proveedor</th>
                  <th>Tipo</th>
                  <th>URL</th>
                  <th>Clave</th>
                  <th>Estado</th>
                  {edit && <th />}
                </tr>
              </thead>
              <tbody>
                {data.providers.map((p) => (
                  <tr key={p.id}>
                    <td>
                      {p.name} <span className="muted small">{p.slug}</span>
                    </td>
                    <td>{p.kind === 'anthropic' ? 'Anthropic' : 'Compatible con OpenAI'}</td>
                    <td className="small">{p.base_url ?? 'por defecto'}</td>
                    <td>{p.has_api_key ? <Badge tone="success">Guardada</Badge> : <Badge tone="warning">Sin clave</Badge>}</td>
                    <td>{p.is_active ? 'Activo' : 'Inactivo'}</td>
                    {edit && (
                      <td className="btn-row">
                        <button className="btn btn-small" onClick={() => setDialog({ kind: 'provider', provider: p })}>
                          Editar
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>
          <Section title="Modelos" action={edit && <button className="btn btn-small" onClick={() => setDialog({ kind: 'model' })}>Nuevo modelo</button>}>
            <table>
              <thead>
                <tr>
                  <th>Modelo</th>
                  <th>Familia</th>
                  <th>Precio (por millón de tokens)</th>
                  <th>Reserva</th>
                  <th>Estado</th>
                  {edit && <th />}
                </tr>
              </thead>
              <tbody>
                {data.models.map((m) => (
                  <tr key={m.id}>
                    <td>
                      {m.display_name} <code className="small">{m.model_key}</code>
                    </td>
                    <td>{m.family}</td>
                    <td className="small">
                      {m.price ? `entrada ${fmtUsd(m.price.input_per_mtok)} · salida ${fmtUsd(m.price.output_per_mtok)}` : <Badge tone="warning">Sin precio</Badge>}
                    </td>
                    <td className="small">{m.fallback_models.join(' → ') || '—'}</td>
                    <td>{m.is_active ? 'Activo' : 'Inactivo'}</td>
                    {edit && (
                      <td className="btn-row">
                        <button className="btn btn-small" onClick={() => setDialog({ kind: 'price', model: m })}>
                          Precio
                        </button>
                        <button className="btn btn-small" onClick={() => setDialog({ kind: 'fallbacks', model: m })}>
                          Reserva
                        </button>
                        <button className="btn btn-small" onClick={() => patch.mutate({ path: `/admin/models/${m.id}`, body: { is_active: !m.is_active } })}>
                          {m.is_active ? 'Desactivar' : 'Activar'}
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>
          <Section title="Despliegues" action={edit && <button className="btn btn-small" onClick={() => setDialog({ kind: 'deployment' })}>Nuevo despliegue</button>}>
            <table>
              <thead>
                <tr>
                  <th>Modelo</th>
                  <th>Proveedor</th>
                  <th>Modelo del proveedor</th>
                  <th>Prioridad</th>
                  <th>Peso</th>
                  <th>Estado</th>
                  {edit && <th />}
                </tr>
              </thead>
              <tbody>
                {data.deployments.map((d) => (
                  <DeploymentRow key={d.id} d={d} edit={edit} model={modelKey(d.model_id)} provider={providerName(d.provider_id)} onChange={(path, body) => patch.mutate({ path, body })} />
                ))}
              </tbody>
            </table>
          </Section>
        </>
      )}
      {dialog && data && <CatalogDialog dialog={dialog} data={data} onClose={() => setDialog(null)} onDone={refresh} />}
    </>
  )
}

function Section({ title, action, children }: { title: string; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="card">
      <header className="card-head">
        <h2>{title}</h2>
        {action}
      </header>
      <div className="table-wrap">{children}</div>
    </section>
  )
}

function DeploymentRow({ d, edit, model, provider, onChange }: { d: Deployment; edit: boolean; model: string; provider: string; onChange: (path: string, body?: unknown) => void }) {
  const [priority, setPriority] = useState(String(d.priority))
  const [weight, setWeight] = useState(String(d.weight))
  const dirty = priority !== String(d.priority) || weight !== String(d.weight)
  return (
    <tr className={d.is_active ? undefined : 'row-muted'}>
      <td>{model}</td>
      <td>{provider}</td>
      <td>
        <code className="small">{d.upstream_model}</code>
      </td>
      <td>{edit ? <input aria-label={`Prioridad del despliegue ${d.id}`} className="input-narrow" type="number" min={0} max={1000} value={priority} onChange={(e) => setPriority(e.target.value)} /> : d.priority}</td>
      <td>{edit ? <input aria-label={`Peso del despliegue ${d.id}`} className="input-narrow" type="number" min={0} max={10000} value={weight} onChange={(e) => setWeight(e.target.value)} /> : d.weight}</td>
      <td>
        {d.cooled ? <Badge tone="warning">Enfriado</Badge> : d.is_active ? <Badge tone="success">Disponible</Badge> : <Badge>Inactivo</Badge>}
      </td>
      {edit && (
        <td className="btn-row">
          {dirty && (
            <button className="btn btn-small btn-primary" onClick={() => onChange(`/admin/deployments/${d.id}`, { priority: Number(priority), weight: Number(weight) })}>
              Guardar
            </button>
          )}
          <button className="btn btn-small" onClick={() => onChange(`/admin/deployments/${d.id}`, { is_active: !d.is_active })}>
            {d.is_active ? 'Desactivar' : 'Activar'}
          </button>
          {d.cooled && (
            <button className="btn btn-small" onClick={() => onChange(`/admin/deployments/${d.id}/cooldown`)}>
              Quitar enfriamiento
            </button>
          )}
        </td>
      )}
    </tr>
  )
}

function CatalogDialog({ dialog, data, onClose, onDone }: { dialog: Dialog; data: CatalogData; onClose: () => void; onDone: () => void }) {
  const toast = useToast()
  const [form, setForm] = useState<Record<string, string>>((): Record<string, string> => {
    if (dialog.kind === 'provider' && dialog.provider)
      return { name: dialog.provider.name, base_url: dialog.provider.base_url ?? '', api_key: '', allow_http: '' }
    if (dialog.kind === 'fallbacks') return { fallbacks: joinList(dialog.model.fallback_models) }
    if (dialog.kind === 'provider') return { slug: '', name: '', kind: 'anthropic', base_url: '', api_key: '', allow_http: '' }
    if (dialog.kind === 'model') return { model_key: '', display_name: '', family: 'anthropic', max_output_tokens: '' }
    if (dialog.kind === 'price') return { input: '', output: '', cache_read: '', cache_write: '', since: '' }
    return { model_id: String(data.models[0]?.id ?? ''), provider_id: String(data.providers[0]?.id ?? ''), upstream_model: '', priority: '0', weight: '100' }
  })
  const set = (key: string, value: string) => setForm((f) => ({ ...f, [key]: value }))
  const num = (v: string) => (v === '' ? null : Number(v))
  const save = useMutation({
    mutationFn: () => {
      switch (dialog.kind) {
        case 'provider':
          return dialog.provider
            ? api(`/admin/providers/${dialog.provider.id}`, {
                method: 'PATCH',
                body: { name: form.name, base_url: form.base_url || null, allow_http: !!form.allow_http, ...(form.api_key ? { api_key: form.api_key } : {}) },
              })
            : api('/admin/providers', {
                body: { slug: form.slug, name: form.name, kind: form.kind, base_url: form.base_url || null, api_key: form.api_key || null, allow_http: !!form.allow_http },
              })
        case 'model':
          return api('/admin/models', { body: { model_key: form.model_key, display_name: form.display_name, family: form.family, max_output_tokens: num(form.max_output_tokens) } })
        case 'price':
          return api(`/admin/models/${dialog.model.id}/prices`, {
            body: {
              input_per_mtok: Number(form.input),
              output_per_mtok: Number(form.output),
              cache_read_per_mtok: num(form.cache_read),
              cache_write_per_mtok: num(form.cache_write),
              effective_from: form.since ? `${form.since}T00:00:00Z` : null,
            },
          })
        case 'fallbacks':
          return api(`/admin/models/${dialog.model.id}`, { method: 'PATCH', body: { fallback_models: splitList(form.fallbacks) } })
        case 'deployment':
          return api('/admin/deployments', {
            body: { model_id: Number(form.model_id), provider_id: Number(form.provider_id), upstream_model: form.upstream_model || null, priority: Number(form.priority), weight: Number(form.weight) },
          })
      }
    },
    onSuccess: () => {
      onDone()
      toast('Catálogo actualizado')
      onClose()
    },
  })
  const titles: Record<Dialog['kind'], string> = {
    provider: dialog.kind === 'provider' && dialog.provider ? `Proveedor ${dialog.provider.slug}` : 'Nuevo proveedor',
    model: 'Nuevo modelo',
    price: dialog.kind === 'price' ? `Precio de ${dialog.model.model_key}` : '',
    fallbacks: dialog.kind === 'fallbacks' ? `Reserva de ${dialog.model.model_key}` : '',
    deployment: 'Nuevo despliegue',
  }
  const input = (key: string, label: string, props: Record<string, unknown> = {}, hint?: string) => (
    <Field label={label} hint={hint}>
      <input value={form[key]} onChange={(e) => set(key, e.target.value)} {...props} />
    </Field>
  )
  return (
    <Modal title={titles[dialog.kind]} onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault()
          save.mutate()
        }}
      >
        {save.isError && <ErrorBox error={save.error} />}
        {dialog.kind === 'provider' && (
          <>
            {!dialog.provider && input('slug', 'Identificador', { required: true, pattern: '[a-z0-9][a-z0-9\\-]{1,62}' })}
            {input('name', 'Nombre', { required: true })}
            {!dialog.provider && (
              <Field label="Tipo">
                <select value={form.kind} onChange={(e) => set('kind', e.target.value)}>
                  <option value="anthropic">Anthropic</option>
                  <option value="openai_compatible">Compatible con OpenAI</option>
                </select>
              </Field>
            )}
            {input('base_url', 'URL base', {}, 'Vacío: la del proveedor (obligatoria en los compatibles con OpenAI)')}
            {input('api_key', dialog.provider ? 'Clave nueva' : 'Clave', { type: 'password', autoComplete: 'off' }, 'Se guarda cifrada y nunca se vuelve a mostrar')}
            <label className="check">
              <input type="checkbox" checked={!!form.allow_http} onChange={(e) => set('allow_http', e.target.checked ? '1' : '')} /> Permitir http (solo un servicio
              de la red interna)
            </label>
          </>
        )}
        {dialog.kind === 'model' && (
          <>
            {input('model_key', 'Nombre que usan las aplicaciones', { required: true })}
            {input('display_name', 'Nombre para mostrar', { required: true })}
            <Field label="Familia">
              <select value={form.family} onChange={(e) => set('family', e.target.value)}>
                <option value="anthropic">Anthropic</option>
                <option value="openai">OpenAI</option>
              </select>
            </Field>
            {input('max_output_tokens', 'Máximo de tokens de salida', { type: 'number', min: 1 })}
          </>
        )}
        {dialog.kind === 'price' && (
          <>
            {input('input', 'Entrada (US$ por millón)', { type: 'number', min: 0, step: 'any', required: true })}
            {input('output', 'Salida (US$ por millón)', { type: 'number', min: 0, step: 'any', required: true })}
            {input('cache_read', 'Lectura de caché', { type: 'number', min: 0, step: 'any' })}
            {input('cache_write', 'Escritura de caché', { type: 'number', min: 0, step: 'any' })}
            {input('since', 'Desde', { type: 'date' }, 'Vacío: desde ahora. Los precios anteriores se conservan')}
          </>
        )}
        {dialog.kind === 'fallbacks' && input('fallbacks', 'Modelos de reserva, en orden', {}, 'Separados por comas; vacío: sin reserva')}
        {dialog.kind === 'deployment' && (
          <>
            <Field label="Modelo">
              <select value={form.model_id} onChange={(e) => set('model_id', e.target.value)}>
                {data.models.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.model_key}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Proveedor">
              <select value={form.provider_id} onChange={(e) => set('provider_id', e.target.value)}>
                {data.providers.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.slug}
                  </option>
                ))}
              </select>
            </Field>
            {input('upstream_model', 'Modelo en el proveedor', {}, 'Vacío: el mismo nombre')}
            {input('priority', 'Prioridad', { type: 'number', min: 0, max: 1000 }, '0 primero; los siguientes, si el anterior falla')}
            {input('weight', 'Peso', { type: 'number', min: 0, max: 10000 }, 'Reparto entre despliegues de la misma prioridad')}
          </>
        )}
        <button className="btn btn-primary" disabled={save.isPending}>
          Guardar
        </button>
      </form>
    </Modal>
  )
}
