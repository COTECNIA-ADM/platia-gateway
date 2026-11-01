// Peticiones y avisos en vivo (WebSocket /events/stream, G12).
import { useLiveEvents, type LiveState } from '../lib/realtime'
import { fmtMs, fmtUsd, STATUS_LABEL } from '../lib/format'
import type { LiveEvent } from '../lib/types'
import { Badge, Empty, type Tone } from './ui'

const STATE_LABEL: Record<LiveState, [string, Tone]> = {
  connecting: ['Conectando…', 'neutral'],
  open: ['En vivo', 'success'],
  closed: ['Reconectando…', 'warning'],
  denied: ['Sin permiso', 'danger'],
}

const EVENT_LABEL: Record<string, string> = {
  'quota.alert': 'Aviso de cuota',
  'quota.exhausted': 'Cuota agotada',
  'security.blocked': 'Bloqueo de seguridad',
  'deployment.cooled': 'Despliegue enfriado',
  'webhook.test': 'Prueba de webhook',
}

const OUTCOME_TONE: Record<string, Tone> = { success: 'success', error: 'danger', blocked: 'warning' }

function describe(event: LiveEvent) {
  const d = event.data
  if (event.type === 'request.finished') {
    const outcome = String(d.outcome)
    return (
      <>
        <Badge tone={OUTCOME_TONE[outcome] ?? 'neutral'}>{STATUS_LABEL[outcome] ?? outcome}</Badge>{' '}
        <strong>{String(d.application ?? '—')}</strong> · {String(d.model ?? '—')} · {fmtMs(d.latency_ms as number)} ·{' '}
        {fmtUsd(d.cost_usd as string)}
        {d.cache === 'hit' && ' · caché'}
      </>
    )
  }
  return (
    <>
      <Badge tone="accent">{EVENT_LABEL[event.type] ?? event.type}</Badge>{' '}
      <span className="muted">{Object.entries(d).map(([k, v]) => `${k}: ${String(v)}`).join(' · ')}</span>
    </>
  )
}

export function LiveFeed() {
  const { events, state } = useLiveEvents()
  const [label, tone] = STATE_LABEL[state]
  return (
    <section className="card">
      <header className="card-head">
        <h2>En vivo</h2>
        <Badge tone={tone}>{label}</Badge>
      </header>
      {events.length === 0 ? (
        <Empty title="Sin actividad todavía">Cada petición y cada aviso aparecerán aquí al momento.</Empty>
      ) : (
        <ul className="feed" aria-live="polite">
          {events.map((e) => (
            <li key={e.id}>
              <time className="muted small">{new Date(e.created_at).toLocaleTimeString('es-ES')}</time> {describe(e)}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
