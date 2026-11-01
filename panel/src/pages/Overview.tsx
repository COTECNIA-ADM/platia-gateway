// Resumen de uso de la organización y peticiones en vivo (capítulos 27 y 28).
import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { fmtMs, fmtNum, fmtUsd } from '../lib/format'
import type { Overview as OverviewData } from '../lib/types'
import { useUrlState } from '../lib/useUrlState'
import { BarList, ChartCard, ColumnChart } from '../components/charts'
import { Empty, ErrorBox, Spinner, Stat } from '../components/ui'
import { LiveFeed } from '../components/LiveFeed'

const RANGES = [
  { hours: '24', label: 'Últimas 24 horas' },
  { hours: '168', label: 'Últimos 7 días' },
  { hours: '720', label: 'Últimos 30 días' },
]

const SERIES = [
  { key: 'success', label: 'Correctas', color: 'var(--series-1)' },
  { key: 'errors', label: 'Errores', color: 'var(--series-2)' },
  { key: 'blocked', label: 'Bloqueadas', color: 'var(--series-3)' },
]

function bucketLabel(at: string, hours: number) {
  const date = new Date(at)
  return hours <= 72
    ? date.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' })
    : date.toLocaleDateString('es-ES', { day: 'numeric', month: 'short' })
}

export function Overview() {
  const [filters, setFilters] = useUrlState(['hours'] as const)
  const hours = Number(filters.hours || 24)
  const query = useQuery({
    queryKey: ['overview', hours],
    queryFn: () => api<OverviewData>('/admin/overview', { query: { hours } }),
    refetchInterval: 60_000,
  })
  const data = query.data

  return (
    <>
      <header className="page-head">
        <h1>Resumen</h1>
        <select aria-label="Periodo" value={String(hours)} onChange={(e) => setFilters({ hours: e.target.value })}>
          {RANGES.map((r) => (
            <option key={r.hours} value={r.hours}>
              {r.label}
            </option>
          ))}
        </select>
      </header>
      {query.isError && <ErrorBox error={query.error} onRetry={() => void query.refetch()} />}
      {query.isPending && <Spinner />}
      {data && (
        <>
          <div className="stats">
            <Stat label="Peticiones" value={fmtNum(data.totals.requests)} hint={`${fmtNum(data.totals.cache_hits)} desde la caché`} />
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
            <Stat label="Latencia p95" value={fmtMs(data.latency_ms.p95)} hint={`p50 ${fmtMs(data.latency_ms.p50)}`} />
            <Stat
              label="Tiempo propio p95"
              value={fmtMs(data.overhead_ms.p95)}
              hint={
                data.overhead_ms.p95 !== null && data.overhead_ms.p95 > data.overhead_ms.target_p95
                  ? `Por encima del objetivo (${data.overhead_ms.target_p95} ms)`
                  : `Objetivo: menos de ${data.overhead_ms.target_p95} ms`
              }
            />
          </div>
          <ChartCard
            title="Peticiones"
            subtitle={hours <= 72 ? 'Por hora (UTC)' : 'Por día (UTC)'}
            legend={SERIES}
            table={
              <table>
                <thead>
                  <tr>
                    <th>Momento</th>
                    <th>Peticiones</th>
                    <th>Errores</th>
                    <th>Bloqueadas</th>
                    <th>Coste</th>
                  </tr>
                </thead>
                <tbody>
                  {data.series.map((p) => (
                    <tr key={p.at}>
                      <td>{bucketLabel(p.at, hours)}</td>
                      <td>{fmtNum(p.requests)}</td>
                      <td>{fmtNum(p.errors)}</td>
                      <td>{fmtNum(p.blocked)}</td>
                      <td>{fmtUsd(p.cost_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            }
          >
            <ColumnChart
              categories={data.series.map((p) => ({ key: p.at, label: bucketLabel(p.at, hours) }))}
              series={SERIES}
              values={Object.fromEntries(
                data.series.map((p) => [p.at, { success: p.requests - p.errors - p.blocked, errors: p.errors, blocked: p.blocked }]),
              )}
            />
          </ChartCard>
          <div className="grid-2">
            <ChartCard
              title="Por modelo"
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
              {data.by_model.length ? (
                <BarList items={data.by_model.map((m) => ({ key: m.model, label: m.model, value: m.requests }))} />
              ) : (
                <Empty title="Sin peticiones en este periodo" />
              )}
            </ChartCard>
            <ChartCard
              title="Por aplicación"
              table={
                <table>
                  <thead>
                    <tr>
                      <th>Aplicación</th>
                      <th>Peticiones</th>
                      <th>Fallidas</th>
                      <th>Coste</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.by_application.map((a) => (
                      <tr key={a.application_id}>
                        <td>{a.application}</td>
                        <td>{fmtNum(a.requests)}</td>
                        <td>{fmtNum(a.failed)}</td>
                        <td>{fmtUsd(a.cost_usd)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              }
            >
              {data.by_application.length ? (
                <BarList
                  color="var(--series-2)"
                  items={data.by_application.map((a) => ({ key: String(a.application_id), label: a.application, value: a.requests }))}
                />
              ) : (
                <Empty title="Sin peticiones en este periodo" />
              )}
            </ChartCard>
          </div>
        </>
      )}
      <LiveFeed />
    </>
  )
}
