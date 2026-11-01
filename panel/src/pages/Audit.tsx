// Auditoría de la organización (ADR-013): filtros en la URL, páginas con cursor, detalle de
// una petición y exportación en JSON Lines para un auditor. Nunca hay texto de prompts.
import { useState, type FormEvent } from 'react'
import { useInfiniteQuery, useMutation, useQuery } from '@tanstack/react-query'
import { api, download } from '../lib/api'
import { fmtDate, fmtMs, fmtNum, fmtUsd, STATUS_LABEL } from '../lib/format'
import type { Application, AuditRow, List, Page } from '../lib/types'
import { useUrlState } from '../lib/useUrlState'
import { Badge, Empty, ErrorBox, Field, Modal, Spinner, type Tone } from '../components/ui'

const FILTERS = ['since', 'until', 'application_id', 'status', 'model', 'purpose', 'request_id', 'end_user'] as const
const TONE: Record<string, Tone> = { success: 'success', error: 'danger', blocked: 'warning' }

export function Audit() {
  const [filters, setFilters] = useUrlState(FILTERS)
  const [draft, setDraft] = useState(filters)
  const [detail, setDetail] = useState<AuditRow | null>(null)
  const apps = useQuery({ queryKey: ['applications'], queryFn: () => api<List<Application>>('/admin/applications') })
  const rows = useInfiniteQuery({
    queryKey: ['audit', filters],
    queryFn: ({ pageParam }) => api<Page<AuditRow>>('/admin/audit', { query: { ...filters, before_id: pageParam, limit: 50 } }),
    initialPageParam: null as number | null,
    getNextPageParam: (last) => last.next,
  })
  const exporter = useMutation({
    mutationFn: () =>
      download(
        '/admin/audit/export',
        { since: filters.since || new Date(Date.now() - 30 * 86400_000).toISOString().slice(0, 10), until: filters.until, application_id: filters.application_id },
        'auditoria.jsonl',
      ),
  })

  function apply(e: FormEvent) {
    e.preventDefault()
    setFilters(draft)
  }
  const set = (changes: Partial<typeof draft>) => setDraft((d) => ({ ...d, ...changes }))
  const all = rows.data?.pages.flatMap((p) => p.data) ?? []

  return (
    <>
      <header className="page-head">
        <h1>Auditoría</h1>
        <button className="btn" onClick={() => exporter.mutate()} disabled={exporter.isPending}>
          Exportar (JSON Lines)
        </button>
      </header>
      <form className="filter-bar card" onSubmit={apply} aria-label="Filtros">
        <Field label="Desde">
          <input type="date" value={draft.since} onChange={(e) => set({ since: e.target.value })} />
        </Field>
        <Field label="Hasta">
          <input type="date" value={draft.until} onChange={(e) => set({ until: e.target.value })} />
        </Field>
        <Field label="Aplicación">
          <select value={draft.application_id} onChange={(e) => set({ application_id: e.target.value })}>
            <option value="">Todas</option>
            {apps.data?.data.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Resultado">
          <select value={draft.status} onChange={(e) => set({ status: e.target.value })}>
            <option value="">Todos</option>
            {Object.entries(STATUS_LABEL).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Modelo">
          <input value={draft.model} onChange={(e) => set({ model: e.target.value })} />
        </Field>
        <Field label="Propósito">
          <input value={draft.purpose} onChange={(e) => set({ purpose: e.target.value })} />
        </Field>
        <Field label="Id de petición">
          <input value={draft.request_id} onChange={(e) => set({ request_id: e.target.value })} />
        </Field>
        <Field label="Persona final">
          <input value={draft.end_user} onChange={(e) => set({ end_user: e.target.value })} />
        </Field>
        <div className="btn-row">
          <button className="btn btn-primary">Filtrar</button>
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => {
              const empty = Object.fromEntries(FILTERS.map((k) => [k, ''])) as typeof draft
              setDraft(empty)
              setFilters(empty)
            }}
          >
            Limpiar
          </button>
        </div>
      </form>
      {exporter.isError && <ErrorBox error={exporter.error} />}
      {rows.isError && <ErrorBox error={rows.error} onRetry={() => void rows.refetch()} />}
      {rows.isPending && <Spinner />}
      {rows.data && all.length === 0 && <Empty title="No hay peticiones con estos filtros" />}
      {all.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Momento</th>
                <th>Aplicación</th>
                <th>Modelo</th>
                <th>Resultado</th>
                <th>Tokens</th>
                <th>Coste</th>
                <th>Latencia</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {all.map((r) => (
                <tr key={r.id}>
                  <td>{fmtDate(r.created_at)}</td>
                  <td>
                    {r.application ?? '—'}
                    {r.purpose && <div className="muted small">{r.purpose}</div>}
                  </td>
                  <td>
                    {r.model ?? r.requested_model ?? '—'}
                    {r.provider && <div className="muted small">{r.provider}</div>}
                  </td>
                  <td>
                    <Badge tone={TONE[r.status] ?? 'neutral'}>{STATUS_LABEL[r.status] ?? r.status}</Badge>
                    {r.error_type && <div className="muted small">{r.error_type}</div>}
                  </td>
                  <td>{fmtNum((r.input_tokens ?? 0) + (r.output_tokens ?? 0))}</td>
                  <td>{fmtUsd(r.cost_usd)}</td>
                  <td>{fmtMs(r.latency_ms)}</td>
                  <td>
                    <button className="btn btn-small btn-ghost" onClick={() => setDetail(r)}>
                      Detalle
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {rows.hasNextPage && (
        <button className="btn" onClick={() => void rows.fetchNextPage()} disabled={rows.isFetchingNextPage}>
          Cargar más
        </button>
      )}
      {detail && <AuditDetail row={detail} onClose={() => setDetail(null)} />}
    </>
  )
}

function AuditDetail({ row, onClose }: { row: AuditRow; onClose: () => void }) {
  const fields: [string, string][] = [
    ['Id de petición', row.request_id],
    ['Inicio', fmtDate(row.created_at)],
    ['Fin', fmtDate(row.completed_at)],
    ['Aplicación', row.application ?? '—'],
    ['Propósito', row.purpose ?? '—'],
    ['Persona final', row.end_user_ref ?? '—'],
    ['Clave', row.api_key_prefix ? `${row.api_key_prefix}…` : '—'],
    ['Contrato', `${row.contract}${row.stream ? ' (streaming)' : ''}`],
    ['Modelo pedido', row.requested_model ?? '—'],
    ['Modelo usado', row.model ?? '—'],
    ['Proveedor', row.provider ?? '—'],
    ['Intentos', fmtNum(row.attempts)],
    ['Resultado', `${STATUS_LABEL[row.status] ?? row.status} (${row.http_status ?? '—'})${row.error_type ? ` · ${row.error_type}` : ''}`],
    ['Tokens de entrada', fmtNum(row.input_tokens)],
    ['Tokens de salida', fmtNum(row.output_tokens)],
    ['Caché leída / escrita', `${fmtNum(row.cache_read_tokens)} / ${fmtNum(row.cache_write_tokens)}`],
    ['Coste', fmtUsd(row.cost_usd)],
    ['Latencia', fmtMs(row.latency_ms)],
    ['Tiempo propio del gateway', fmtMs(row.overhead_ms)],
    ['Huella del prompt', row.prompt_hash ?? '—'],
    ['Sellada en la cadena', row.sealed ? 'Sí' : 'Todavía no'],
  ]
  return (
    <Modal title="Detalle de la petición" onClose={onClose} wide>
      <dl className="details">
        {fields.map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
      {row.security_flags !== null && row.security_flags !== undefined && (
        <>
          <h3>Seguridad de contenido</h3>
          <pre className="code">{JSON.stringify(row.security_flags, null, 2)}</pre>
        </>
      )}
    </Modal>
  )
}
