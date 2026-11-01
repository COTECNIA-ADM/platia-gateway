// Todas las claves de la organización, también las personales, sin el secreto.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import { fmtDate } from '../lib/format'
import type { ApiKey, List } from '../lib/types'
import { useUrlState } from '../lib/useUrlState'
import { Badge, ConfirmButton, Empty, ErrorBox, Spinner, useToast } from '../components/ui'

const STATE: Record<ApiKey['state'], [string, 'success' | 'warning' | 'neutral']> = {
  active: ['Activa', 'success'],
  expired: ['Caducada', 'warning'],
  revoked: ['Revocada', 'neutral'],
}

export function KeyState({ state }: { state: ApiKey['state'] }) {
  const [label, tone] = STATE[state]
  return <Badge tone={tone}>{label}</Badge>
}

export function Keys() {
  const client = useQueryClient()
  const toast = useToast()
  const [filters, setFilters] = useUrlState(['kind', 'state'] as const)
  const keys = useQuery({
    queryKey: ['keys', 'all', filters],
    queryFn: () => api<List<ApiKey>>('/admin/keys', { query: { kind: filters.kind, state: filters.state || 'active' } }),
  })
  const revoke = useMutation({
    mutationFn: (id: number) => api(`/admin/keys/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['keys'] })
      toast('Clave revocada')
    },
  })
  return (
    <>
      <header className="page-head">
        <h1>Claves</h1>
        <div className="filters">
          <select aria-label="Tipo" value={filters.kind} onChange={(e) => setFilters({ kind: e.target.value })}>
            <option value="">Todas</option>
            <option value="application">De aplicación</option>
            <option value="user">Personales</option>
          </select>
          <select aria-label="Estado" value={filters.state} onChange={(e) => setFilters({ state: e.target.value })}>
            <option value="">Activas</option>
            <option value="all">Todas, también revocadas y caducadas</option>
          </select>
        </div>
      </header>
      {keys.isError && <ErrorBox error={keys.error} />}
      {revoke.isError && <ErrorBox error={revoke.error} />}
      {keys.isPending && <Spinner />}
      {keys.data && keys.data.data.length === 0 && <Empty title="No hay claves con estos filtros" />}
      {keys.data && keys.data.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Prefijo</th>
                <th>De</th>
                <th>Etiqueta</th>
                <th>Estado</th>
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
                  <td>
                    {k.owner ?? '—'} <span className="muted small">{k.kind === 'user' ? 'persona' : 'aplicación'}</span>
                  </td>
                  <td>{k.label || '—'}</td>
                  <td>
                    <KeyState state={k.state} />
                  </td>
                  <td>{fmtDate(k.expires_at)}</td>
                  <td>{fmtDate(k.last_used_at)}</td>
                  <td>
                    {k.state === 'active' && (
                      <ConfirmButton question="¿Revocarla?" onConfirm={() => revoke.mutate(k.id)}>
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
    </>
  )
}
