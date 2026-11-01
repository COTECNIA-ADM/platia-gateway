// Registro de cambios hechos desde el panel (ADR-017). Los secretos salen como «(cambiado)».
import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { fmtDate } from '../lib/format'
import type { AdminAction, List, Page, User } from '../lib/types'
import { Empty, ErrorBox, Spinner } from '../components/ui'

export function Actions() {
  const users = useQuery({ queryKey: ['users'], queryFn: () => api<List<User>>('/admin/users') })
  const rows = useInfiniteQuery({
    queryKey: ['actions'],
    queryFn: ({ pageParam }) => api<Page<AdminAction>>('/admin/actions', { query: { before_id: pageParam, limit: 50 } }),
    initialPageParam: null as number | null,
    getNextPageParam: (last) => last.next,
  })
  const who = (id: number | null) => users.data?.data.find((u) => u.id === id)?.email ?? (id ? `#${id}` : '—')
  const all = rows.data?.pages.flatMap((p) => p.data) ?? []
  return (
    <>
      <header className="page-head">
        <h1>Registro de cambios</h1>
      </header>
      {rows.isError && <ErrorBox error={rows.error} />}
      {rows.isPending && <Spinner />}
      {rows.data && all.length === 0 && <Empty title="Todavía no hay cambios hechos desde el panel" />}
      {all.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Momento</th>
                <th>Quién</th>
                <th>Acción</th>
                <th>Sobre</th>
                <th>Cambios</th>
                <th>IP</th>
              </tr>
            </thead>
            <tbody>
              {all.map((a) => (
                <tr key={a.id}>
                  <td>{fmtDate(a.created_at)}</td>
                  <td>{who(a.user_id)}</td>
                  <td>
                    <code>{a.action}</code>
                    {a.organization_id === null && <div className="muted small">catálogo común</div>}
                  </td>
                  <td>
                    {a.target_type} {a.target_id}
                  </td>
                  <td className="small">
                    {a.changes
                      ? Object.entries(a.changes)
                          .map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
                          .join(' · ')
                      : '—'}
                  </td>
                  <td>{a.ip_address ?? '—'}</td>
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
    </>
  )
}
