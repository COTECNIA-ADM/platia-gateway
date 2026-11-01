import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { App } from './App'
import { ADMIN, mockApi, renderWithProviders, sessionRoutes, stubWebSocket } from './test/utils'

const OVERVIEW = {
  hours: 24,
  since: '2026-10-31T10:00:00Z',
  totals: { requests: 12, success: 10, blocked: 1, errors: 1, input_tokens: 100, output_tokens: 50, cache_read_tokens: 0, cache_write_tokens: 0, cost_usd: '0.25', cache_hits: 2 },
  latency_ms: { p50: 300, p95: 900 },
  overhead_ms: { p50: 8, p95: 20, p99: 40, sample: 10, target_p95: 50 },
  by_model: [{ model: 'claude-test', requests: 12, cost_usd: '0.25', tokens: 150 }],
  by_application: [{ application_id: 1, application: 'platia', requests: 12, cost_usd: '0.25', failed: 2 }],
  series: [{ at: '2026-11-01T09:00:00Z', requests: 12, errors: 1, blocked: 1, cost_usd: '0.25' }],
}

describe('entrada al panel', () => {
  beforeEach(stubWebSocket)

  it('con contraseña y código de la app autenticadora llega al resumen', async () => {
    const calls = mockApi({
      'POST /auth/refresh': { status: 401 },
      'POST /auth/login': { body: { mfa_required: true, mfa_ticket: 'ticket-1' } },
      'POST /auth/login/mfa': { body: { access_token: 'tok', scope: 'full' } },
      'GET /auth/me': { body: ADMIN },
      'GET /admin/overview': { body: OVERVIEW },
    })
    renderWithProviders(<App />)
    await userEvent.type(await screen.findByLabelText('Correo'), 'eva@cotecnia.es')
    await userEvent.type(screen.getByLabelText('Contraseña'), 'una contraseña larga')
    await userEvent.click(screen.getByRole('button', { name: 'Continuar' }))
    await userEvent.type(await screen.findByLabelText('Código de la app autenticadora'), '123456')
    await userEvent.click(screen.getByRole('button', { name: 'Entrar' }))
    expect(await screen.findByRole('heading', { name: 'Resumen' })).toBeInTheDocument()
    expect(await screen.findByText('0,25 US$')).toBeInTheDocument()
    expect(calls.find((c) => c.url === '/auth/login/mfa')?.body).toEqual({ mfa_ticket: 'ticket-1', code: '123456' })
    expect(calls.find((c) => c.url.startsWith('/admin/overview'))?.headers.Authorization).toBe('Bearer tok')
  })

  it('un error de credenciales se muestra sin salir del formulario', async () => {
    mockApi({
      'POST /auth/refresh': { status: 401 },
      'POST /auth/login': { status: 401, body: { type: 'error', error: { type: 'authentication_error', message: 'Correo o contraseña incorrectos' } } },
    })
    renderWithProviders(<App />)
    await userEvent.type(await screen.findByLabelText('Correo'), 'eva@cotecnia.es')
    await userEvent.type(screen.getByLabelText('Contraseña'), 'mala')
    await userEvent.click(screen.getByRole('button', { name: 'Continuar' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Correo o contraseña incorrectos')
  })

  it('un administrador sin MFA tiene que activarlo antes de seguir', async () => {
    mockApi({
      ...sessionRoutes({ scope: 'mfa_setup', mfa_enabled: false }),
      'POST /auth/mfa/setup': { body: { secret: 'JBSWY3DPEHPK3PXP', otpauth_uri: 'otpauth://totp/x' } },
      'POST /auth/mfa/enable': { body: { access_token: 'tok2', scope: 'full' } },
    })
    renderWithProviders(<App />)
    await userEvent.click(await screen.findByRole('button', { name: 'Empezar' }))
    expect(await screen.findByText('JBSWY3DPEHPK3PXP')).toBeInTheDocument()
  })

  it('una persona sin rol de administrador no ve el panel', async () => {
    mockApi(sessionRoutes({ role: 'member' }))
    renderWithProviders(<App />)
    expect(await screen.findByRole('heading', { name: 'Sin acceso al panel' })).toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: 'Secciones' })).not.toBeInTheDocument()
  })

  it('el resumen tiene vista de tabla', async () => {
    mockApi({ ...sessionRoutes(), 'GET /admin/overview': { body: OVERVIEW } })
    renderWithProviders(<App />, { route: '/overview' })
    const buttons = await screen.findAllByRole('button', { name: 'Ver tabla' })
    await userEvent.click(buttons[1])
    await waitFor(() => expect(screen.getByRole('columnheader', { name: 'Modelo' })).toBeInTheDocument())
  })
})
