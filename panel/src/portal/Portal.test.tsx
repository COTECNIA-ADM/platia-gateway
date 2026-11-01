import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { PortalApp } from './PortalApp'
import { MyAccount } from './MyAccount'
import { MyKeys } from './MyKeys'
import { MyUsage } from './MyUsage'
import { mockApi, renderWithProviders, sessionRoutes } from '../test/utils'
import type { PortalInfo } from './types'

const MEMBER = { id: 7, email: 'luis@cotecnia.es', name: 'Luis', role: 'member' as const, mfa_enabled: false }

function portal(extra: Partial<PortalInfo> = {}): PortalInfo {
  return {
    user: { id: 7, email: 'luis@cotecnia.es', name: 'Luis', role: 'member', team: 'Ofertas', organization: 'Cotecnia', mfa_enabled: false, password_changed_at: null, last_login_at: null },
    personal_keys: { enabled: true, max_keys: 5, max_days: 365, max_grace_hours: 72, purposes: ['codigo'] },
    models: [{ model: 'claude-x', name: 'Claude X', family: 'anthropic', input_per_mtok: '3', output_per_mtok: '15' }],
    limits: [{ metric: 'cost_usd', period: 'month', value: '20', alert_pct: 80, used: '18', only_you: true }],
    ...extra,
  }
}

const KEY = { id: 3, prefix: 'plgw-abc123', label: 'portátil', created_at: '2026-10-01T08:00:00Z', expires_at: '2026-12-30T08:00:00Z', last_used_at: null }

describe('portal de usuario', () => {
  it('un miembro entra al portal y no ve el enlace al panel', async () => {
    mockApi({
      ...sessionRoutes(MEMBER),
      'GET /me/usage': { body: { days: 30, since: '2026-10-03T00:00:00Z', totals: { requests: 0, errors: 0, blocked: 0, input_tokens: 0, output_tokens: 0, cost_usd: '0' }, by_model: [], series: [] } },
      'GET /me/portal': { body: portal() },
      'GET /me/requests': { body: { data: [], next: null } },
    })
    renderWithProviders(<PortalApp />)
    expect(await screen.findByRole('heading', { name: 'Mi consumo' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Mis claves' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Panel de administración' })).not.toBeInTheDocument()
    expect(await screen.findByText('Todavía no has hecho ninguna petición')).toBeInTheDocument()
  })

  it('un administrador sin MFA tiene que activarlo antes', async () => {
    mockApi({ 'POST /auth/refresh': { body: { access_token: 't', scope: 'mfa_setup' } }, 'GET /auth/me': { body: { ...MEMBER, role: 'admin', scope: 'mfa_setup' } } })
    renderWithProviders(<PortalApp />)
    expect(await screen.findByRole('heading', { name: 'Activa el segundo factor' })).toBeInTheDocument()
  })

  it('el consumo muestra las cuotas propias y pagina las peticiones', async () => {
    const row = (id: number) => ({ id, request_id: `r${id}`, created_at: '2026-11-01T08:00:00Z', application: 'personal', purpose: null, model: 'claude-x', status: 'success', http_status: 200, error_type: null, input_tokens: 10, output_tokens: 5, cost_usd: '0.01', latency_ms: 300, api_key_prefix: 'plgw-abc123' })
    const calls = mockApi({
      ...sessionRoutes(MEMBER),
      'GET /me/usage': { body: { days: 30, since: '2026-10-03T00:00:00Z', totals: { requests: 2, errors: 0, blocked: 0, input_tokens: 20, output_tokens: 10, cost_usd: '0.02' }, by_model: [{ model: 'claude-x', requests: 2, tokens: 30, cost_usd: '0.02' }], series: [{ day: '2026-11-01', requests: 2, errors: 0, cost_usd: '0.02' }] } },
      'GET /me/portal': { body: portal() },
      'GET /me/requests': (c) => ({ body: c.url.includes('before_id') ? { data: [row(1)], next: null } : { data: [row(2)], next: 2 } }),
    })
    renderWithProviders(<MyUsage />)
    expect(await screen.findByText('Solo para ti')).toBeInTheDocument()
    expect(screen.getByRole('meter')).toHaveAttribute('aria-valuenow', '90')
    await userEvent.click(await screen.findByRole('button', { name: 'Cargar más' }))
    expect(await screen.findByTitle('r1')).toBeInTheDocument()
    expect(calls.some((c) => c.url.includes('before_id=2'))).toBe(true)
    expect(screen.queryByRole('button', { name: 'Cargar más' })).not.toBeInTheDocument()
  })

  it('crear una clave la enseña una vez con cómo usarla', async () => {
    const calls = mockApi({
      ...sessionRoutes(MEMBER),
      'GET /me/portal': { body: portal() },
      'GET /me/keys': { body: { data: [] } },
      'POST /me/keys': { status: 201, body: { ...KEY, key: 'plgw-abc123-secreto' } },
    })
    renderWithProviders(<MyKeys />)
    await userEvent.type(await screen.findByLabelText('Nombre'), 'portátil')
    await userEvent.click(screen.getByRole('button', { name: 'Crear clave' }))
    const box = await screen.findByRole('region', { name: 'Clave nueva' })
    expect(within(box).getByText('plgw-abc123-secreto')).toBeInTheDocument()
    expect(within(box).getByText(/ANTHROPIC_BASE_URL=/)).toBeInTheDocument()
    expect(calls.find((c) => c.method === 'POST' && c.url === '/me/keys')?.body).toEqual({ label: 'portátil', expires_days: 90 })
    expect(screen.getByText('claude-x')).toBeInTheDocument()
  })

  it('rotar pide el margen y avisa de hasta cuándo vale la anterior', async () => {
    const calls = mockApi({
      ...sessionRoutes(MEMBER),
      'GET /me/portal': { body: portal() },
      'GET /me/keys': { body: { data: [KEY] } },
      'POST /me/keys/3/rotate': { status: 201, body: { ...KEY, id: 4, prefix: 'plgw-def456', key: 'plgw-def456-nuevo', previous: { id: 3, expires_at: '2026-11-02T08:00:00Z', revoked_at: null } } },
    })
    renderWithProviders(<MyKeys />)
    await userEvent.click(await screen.findByRole('button', { name: 'Rotar' }))
    const dialog = screen.getByRole('dialog', { name: 'Rotar plgw-abc123…' })
    const grace = within(dialog).getByLabelText('Horas de margen para la anterior')
    await userEvent.clear(grace)
    await userEvent.type(grace, '4')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Rotar' }))
    expect(await screen.findByText('plgw-def456-nuevo')).toBeInTheDocument()
    expect(screen.getByText(/La clave anterior funciona hasta el/)).toBeInTheDocument()
    expect(calls.find((c) => c.url.endsWith('/rotate'))?.body).toEqual({ expires_days: 90, grace_hours: 4 })
  })

  it('sin la aplicación de claves personales no deja crear claves', async () => {
    mockApi({ ...sessionRoutes(MEMBER), 'GET /me/portal': { body: portal({ personal_keys: { enabled: false, max_keys: 5, max_days: 365, max_grace_hours: 72, purposes: [] } }) }, 'GET /me/keys': { body: { data: [] } } })
    renderWithProviders(<MyKeys />)
    expect(await screen.findByText(/no tiene activadas las claves personales/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Crear clave' })).not.toBeInTheDocument()
  })

  it('cambiar la contraseña exige repetirla igual', async () => {
    const calls = mockApi({
      ...sessionRoutes(MEMBER),
      'GET /me/portal': { body: portal() },
      'GET /auth/sessions': { body: { data: [] } },
      'POST /auth/password': { body: { other_sessions_closed: 2 } },
    })
    renderWithProviders(<MyAccount />)
    await userEvent.type(await screen.findByLabelText('Contraseña actual'), 'vieja')
    await userEvent.type(screen.getByLabelText('Contraseña nueva'), 'nueva-larga-1')
    await userEvent.type(screen.getByLabelText('Repite la nueva'), 'otra')
    expect(screen.getByRole('button', { name: 'Cambiar contraseña' })).toBeDisabled()
    await userEvent.clear(screen.getByLabelText('Repite la nueva'))
    await userEvent.type(screen.getByLabelText('Repite la nueva'), 'nueva-larga-1')
    await userEvent.click(screen.getByRole('button', { name: 'Cambiar contraseña' }))
    expect(await screen.findByText('Contraseña cambiada; se han cerrado 2 sesiones más')).toBeInTheDocument()
    expect(calls.find((c) => c.url === '/auth/password')?.body).toEqual({ current_password: 'vieja', new_password: 'nueva-larga-1' })
  })

  it('las sesiones permiten cerrar las otras, no la actual', async () => {
    mockApi({
      ...sessionRoutes(MEMBER),
      'GET /me/portal': { body: portal({ user: { ...portal().user, role: 'admin', mfa_enabled: true } }) },
      'GET /auth/sessions': {
        body: {
          data: [
            { id: 1, current: true, created_at: '2026-11-01T08:00:00Z', last_used_at: null, expires_at: '2026-12-01T08:00:00Z', ip_address: '10.0.0.1', user_agent: 'Firefox' },
            { id: 2, current: false, created_at: '2026-10-01T08:00:00Z', last_used_at: null, expires_at: '2026-12-01T08:00:00Z', ip_address: '10.0.0.2', user_agent: 'Chrome' },
          ],
        },
      },
    })
    renderWithProviders(<MyAccount />)
    const rows = await screen.findAllByRole('row')
    expect(within(rows[1]).getByText('Esta sesión')).toBeInTheDocument()
    expect(within(rows[2]).getByRole('button', { name: /Cerrar la sesión/ })).toBeInTheDocument()
    expect(screen.getByText(/Los administradores no pueden desactivarlo/)).toBeInTheDocument()
  })
})
