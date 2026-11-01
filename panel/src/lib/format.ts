// Formato de cifras y fechas en español. Los importes son USD (como los guarda el gateway).

const num = new Intl.NumberFormat('es-ES')

export function fmtNum(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  return num.format(Number(value))
}

export function fmtUsd(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  const n = Number(value)
  const digits = n !== 0 && Math.abs(n) < 0.01 ? 4 : 2
  return `${n.toLocaleString('es-ES', { minimumFractionDigits: 2, maximumFractionDigits: digits })} US$`
}

export function fmtMs(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${fmtNum(value)} ms`
}

export function fmtDate(value: string | null | undefined): string {
  if (!value) return '—'
  return new Date(value).toLocaleString('es-ES', { dateStyle: 'short', timeStyle: 'medium' })
}

export function fmtDay(value: string | null | undefined): string {
  if (!value) return '—'
  return new Date(value).toLocaleDateString('es-ES', { dateStyle: 'medium' })
}

export const STATUS_LABEL: Record<string, string> = { success: 'Correcta', error: 'Error', blocked: 'Bloqueada' }
export const METRIC_LABEL: Record<string, string> = {
  cost_usd: 'Coste (US$)',
  requests: 'Peticiones',
  tokens: 'Tokens',
  concurrent: 'Simultáneas',
}
export const PERIOD_LABEL: Record<string, string> = { minute: 'por minuto', day: 'al día', month: 'al mes', none: '—' }
export const CHECK_LABEL: Record<string, string> = {
  jailbreak: 'Manipulación del modelo',
  classified: 'Marcas de clasificación',
  secret: 'Secretos',
  pii: 'Datos personales',
}
export const ACTION_LABEL: Record<string, string> = { block: 'Bloquear', redact: 'Tachar', log: 'Anotar', off: 'No comprobar' }
