// Seguridad de contenido (ADR-012): qué se hace con cada hallazgo, en la entrada y en la salida,
// para toda la organización o para una aplicación.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import { ACTION_LABEL, CHECK_LABEL } from '../lib/format'
import type { Application, List, PolicyCell } from '../lib/types'
import { useUrlState } from '../lib/useUrlState'
import { Badge, ErrorBox, Spinner, useToast } from '../components/ui'

const ORIGIN_LABEL: Record<PolicyCell['origin'], string> = {
  default: 'por defecto',
  organization: 'de la organización',
  application: 'de la aplicación',
}

export function ContentPolicies() {
  const client = useQueryClient()
  const toast = useToast()
  const [filters, setFilters] = useUrlState(['application_id'] as const)
  const appId = filters.application_id ? Number(filters.application_id) : null
  const apps = useQuery({ queryKey: ['applications'], queryFn: () => api<List<Application>>('/admin/applications') })
  const cells = useQuery({
    queryKey: ['policies', appId],
    queryFn: () => api<{ data: PolicyCell[] }>('/admin/content-policies', { query: { application_id: appId } }),
  })
  const save = useMutation({
    mutationFn: (body: { direction: string; check: string; action: string }) =>
      api('/admin/content-policies', { method: 'PUT', body: { ...body, application_id: appId } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['policies'] })
      toast('Política guardada')
    },
  })
  return (
    <>
      <header className="page-head">
        <h1>Seguridad de contenido</h1>
        <select aria-label="Ámbito" value={filters.application_id} onChange={(e) => setFilters({ application_id: e.target.value })}>
          <option value="">Toda la organización</option>
          {apps.data?.data.map((a) => (
            <option key={a.id} value={a.id}>
              Aplicación {a.name}
            </option>
          ))}
        </select>
      </header>
      <p className="muted">
        Los detectores son locales: nunca se manda el texto a otro modelo ni se guarda lo encontrado, solo la categoría y el recuento.
      </p>
      {cells.isError && <ErrorBox error={cells.error} />}
      {save.isError && <ErrorBox error={save.error} />}
      {cells.isPending && <Spinner />}
      {cells.data && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Comprobación</th>
                <th>Sentido</th>
                <th>Acción</th>
                <th>De dónde sale</th>
              </tr>
            </thead>
            <tbody>
              {cells.data.data.map((c) => {
                const own = appId ? c.application : c.organization
                return (
                  <tr key={`${c.direction}-${c.check}`}>
                    <td>{CHECK_LABEL[c.check] ?? c.check}</td>
                    <td>{c.direction === 'input' ? 'Entrada' : 'Salida'}</td>
                    <td>
                      <select
                        aria-label={`${CHECK_LABEL[c.check]} en la ${c.direction === 'input' ? 'entrada' : 'salida'}`}
                        value={own ?? 'default'}
                        onChange={(e) => save.mutate({ direction: c.direction, check: c.check, action: e.target.value })}
                      >
                        <option value="default">
                          Heredada ({ACTION_LABEL[appId ? (c.organization ?? c.default) : c.default]})
                        </option>
                        {c.allowed.map((a) => (
                          <option key={a} value={a}>
                            {ACTION_LABEL[a] ?? a}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>
                      <Badge tone={c.origin === 'default' ? 'neutral' : 'info'}>{ORIGIN_LABEL[c.origin]}</Badge>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}
