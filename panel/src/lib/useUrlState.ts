// Filtros de los listados en la URL: se pueden compartir, recargar y volver atrás.
import { useSearchParams } from 'react-router'

export function useUrlState<K extends string>(keys: readonly K[]) {
  const [params, setParams] = useSearchParams()
  const values = Object.fromEntries(keys.map((k) => [k, params.get(k) ?? ''])) as Record<K, string>
  function set(changes: Partial<Record<K, string>>) {
    const next = new URLSearchParams(params)
    for (const [key, value] of Object.entries(changes) as [K, string | undefined][]) {
      if (value) next.set(key, value)
      else next.delete(key)
    }
    setParams(next, { replace: true })
  }
  return [values, set] as const
}
