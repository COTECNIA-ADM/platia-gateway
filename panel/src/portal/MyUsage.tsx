// Mi consumo (cap. 30): totales, evolución diaria, cuotas y últimas peticiones propias.
import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { fmtDate, fmtMs, fmtNum, fmtUsd, METRIC_LABEL, PERIOD_LABEL, STATUS_LABEL } from '../lib/format'
import type { Page } from '../lib/types'
import { useUrlState } from '../lib/useUrlState'
import { BarList, ChartCard, ColumnChart, Meter } from '../components/charts'
import { Badge, Empty, ErrorBox, Spinner, Stat } from '../components/ui'
import type { MyRequest, PortalInfo, Usage } from './types'

const RANGES = [
  { days: '7', label: 'Últimos 7 días' },
  { days: '30', label: 'Últimos 30 días' },
  { days: '90', label: 'Últimos 90 días' },
]

const SERIES = [
  { key: 'ok', label: 'Correctas', color: 'var(--series-1)' },
  { key: 'errors', label: 'Errores', color: 'var(--series-2)' },
]

const dayLabel = (day: string) => new Date(`${day}T00:00:00Z`).toLocaleDateString('es-ES', { day: 'numeric', month: 'short', timeZone: 'UTC' })
const statusTone = (status: string) => (status === 'success' ? 'success' : status === 'blocked' ? 'warning' : 'danger')

export function MyUsage() {
  const [filters, setFilters] = useUrlState(['days'] as const)
  const days = Number(filters.days || 30)
  const usage = useQuery({ queryKey: ['me-usage', days], queryFn: () => api<Usage>('/me/usage', { query: { days } }) })
  const info = useQuery({ queryKey: ['me-portal'], queryFn: () => api<PortalInfo>('/me/portal') })
  const data = usage.data

  return (
    <>
      <header className="page-head">
        <h1>Mi consumo</h1>
        <select aria-label="Periodo" value={String(days)} onChange={(e) => setFilters({ days: e.target.value })}>
          {RANGES.map((r) => (
            <option key={r.days} value={r.days}>
              {r.label}
            </option>
          ))}
        </select>
      </header>
      <p className="muted">Solo tus peticiones: las que haces con tus claves personales y las que las aplicaciones hacen en tu nombre.</p>
      {usage.isError && <ErrorBox error={usage.error} onRetry={() => void usage.refetch()} />}
      {usage.isPending && <Spinner />}
      {data && (
        <>
          <div className="stats">
            <Stat label="Peticiones" value={fmtNum(data.totals.requests)} />
            <Stat label="Coste" value={fmtUsd(data.totals.cost_usd)} />
            <Stat
              label="Errores y bloqueos"
              value={fmtNum(data.totals.errors + data.totals.blocked)}
              hint={`${fmtNum(data.totals.errors)} errores · ${fmtNum(data.totals.blocked)} bloqueadas`}
            />
            <Stat
              label="Tokens"
              value={fmtNum(data.totals.input_tokens + data.totals.output_tokens)}
              hint={`${fmtNum(data.totals.input_tokens)} de entrada · ${fmtNum(data.totals.output_tokens)} de salida`}
            />
          </div>
          {data.series.length === 0 ? (
            <Empty title="Sin peticiones en este periodo" />
          ) : (
            <div className="grid-2">
              <ChartCard
                title="Peticiones por día"
                subtitle="UTC"
                legend={SERIES}
                table={
                  <table>
                    <thead>
                      <tr>
                        <th>Día</th>
                        <th>Peticiones</th>
                        <th>Errores</th>
                        <th>Coste</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.series.map((p) => (
                        <tr key={p.day}>
                          <td>{dayLabel(p.day)}</td>
                          <td>{fmtNum(p.requests)}</td>
                          <td>{fmtNum(p.errors)}</td>
                          <td>{fmtUsd(p.cost_usd)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                }
              >
                <ColumnChart
                  categories={data.series.map((p) => ({ key: p.day, label: dayLabel(p.day) }))}
                  series={SERIES}
                  values={Object.fromEntries(data.series.map((p) => [p.day, { ok: p.requests - p.errors, errors: p.errors }]))}
                />
              </ChartCard>
              <ChartCard
                title="Por modelo"
                subtitle="Peticiones"
                table={
                  <table>
                    <thead>
                      <tr>
                        <th>Modelo</th>
                        <th>Peticiones</th>
                        <th>Tokens</th>
                        <th>Coste</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.by_model.map((m) => (
                        <tr key={m.model}>
                          <td>{m.model}</td>
                          <td>{fmtNum(m.requests)}</td>
                          <td>{fmtNum(m.tokens)}</td>
                          <td>{fmtUsd(m.cost_usd)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                }
              >
                <BarList
                  items={data.by_model.map((m) => ({ key: m.model, label: m.model, value: m.requests }))}
                  detail={(key) => {
                    const m = data.by_model.find((x) => x.model === key)
                    return m ? [{ label: 'coste', value: fmtUsd(m.cost_usd) }] : []
                  }}
                />
              </ChartCard>
            </div>
          )}
        </>
      )}
      <Quotas info={info.data} />
      <Requests />
    </>
  )
}

function Quotas({ info }: { info: PortalInfo | undefined }) {
  if (!info || info.limits.length === 0) return null
  return (
    <section className="card">
      <h2>Mis cuotas</h2>
      <p className="muted">Límites de tus claves personales. Al llegar al 100 %, el gateway rechaza las peticiones hasta que empieza el periodo siguiente.</p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Límite</th>
              <th>Usado</th>
              <th>Consumo</th>
            </tr>
          </thead>
          <tbody>
            {info.limits.map((l, i) => {
              const usd = l.metric === 'cost_usd'
              const show = (v: string | null) => (usd ? fmtUsd(v) : fmtNum(v))
              return (
                <tr key={i}>
                  <td>
                    {METRIC_LABEL[l.metric] ?? l.metric} {PERIOD_LABEL[l.period] ?? l.period}: {show(l.value)}
                    {l.only_you && (
                      <>
                        {' '}
                        <Badge tone="info">Solo para ti</Badge>
                      </>
                    )}
                  </td>
                  <td>{l.used === null ? '—' : show(l.used)}</td>
                  <td>
                    {l.used === null ? <span className="muted">No se mide aquí</span> : <Meter value={Number(l.used)} max={Number(l.value)} warnAt={l.alert_pct / 100} />}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function Requests() {
  const query = useInfiniteQuery({
    queryKey: ['me-requests'],
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) => api<Page<MyRequest>>('/me/requests', { query: { before_id: pageParam, limit: 50 } }),
    getNextPageParam: (last) => last.next,
  })
  const rows = query.data?.pages.flatMap((p) => p.data) ?? []
  return (
    <section className="card">
      <h2>Últimas peticiones</h2>
      <p className="muted">El gateway no guarda el texto de las peticiones ni de las respuestas: solo estos datos.</p>
      {query.isError && <ErrorBox error={query.error} onRetry={() => void query.refetch()} />}
      {query.isPending && <Spinner />}
      {query.isSuccess && rows.length === 0 && <Empty title="Todavía no has hecho ninguna petición" />}
      {rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Fecha</th>
                <th>Aplicación</th>
                <th>Modelo</th>
                <th>Estado</th>
                <th>Tokens</th>
                <th>Coste</th>
                <th>Latencia</th>
                <th>Clave</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td title={r.request_id}>{fmtDate(r.created_at)}</td>
                  <td>
                    {r.application ?? '—'}
                    {r.purpose && <div className="small muted">{r.purpose}</div>}
                  </td>
                  <td>{r.model ?? '—'}</td>
                  <td>
                    <Badge tone={statusTone(r.status)} title={r.error_type ?? undefined}>
                      {STATUS_LABEL[r.status] ?? r.status}
                    </Badge>
                  </td>
                  <td>{r.input_tokens === null ? '—' : `${fmtNum(r.input_tokens)} / ${fmtNum(r.output_tokens)}`}</td>
                  <td>{fmtUsd(r.cost_usd)}</td>
                  <td>{fmtMs(r.latency_ms)}</td>
                  <td>{r.api_key_prefix ? <code>{r.api_key_prefix}…</code> : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {query.hasNextPage && (
        <button className="btn" onClick={() => void query.fetchNextPage()} disabled={query.isFetchingNextPage}>
          Cargar más
        </button>
      )}
    </section>
  )
}
