import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { Audit } from './Audit'
import { mockApi, renderWithProviders, sessionRoutes } from '../test/utils'

function row(id: number) {
  return {
    id, request_id: `req-${id}`, created_at: '2026-11-01T08:00:00Z', completed_at: null, application_id: 1, application: 'platia', team_id: 1,
    user_id: null, end_user_ref: null, api_key_prefix: 'plgw-app-pla', purpose: 'resumen', contract: 'anthropic', stream: false,
    requested_model: 'claude-test', model: 'claude-test', provider: 'anthropic', deployment_id: 1, status: 'success', http_status: 200,
    error_type: null, input_tokens: 10, output_tokens: 5, cache_read_tokens: 0, cache_write_tokens: 0, cost_usd: '0.001', latency_ms: 400,
    overhead_ms: 9, attempts: 1, security_flags: null, prompt_hash: 'abc', sealed: true,
  }
}

describe('auditoría', () => {
  it('los filtros viajan a la API y la siguiente página usa el cursor', async () => {
    const calls = mockApi({
      ...sessionRoutes(),
      'GET /admin/applications': { body: { data: [] } },
      'GET /admin/audit': (call) =>
        call.url.includes('before_id=5') ? { body: { data: [row(4)], next: null } } : { body: { data: [row(6), row(5)], next: 5 } },
    })
    renderWithProviders(<Audit />, { route: '/audit?status=error' })
    expect(await screen.findAllByText('platia')).toHaveLength(2)
    expect(calls.find((c) => c.url.startsWith('/admin/audit'))?.url).toContain('status=error')
    await userEvent.click(screen.getByRole('button', { name: 'Cargar más' }))
    expect(await screen.findAllByText('platia')).toHaveLength(3)
    expect(screen.queryByRole('button', { name: 'Cargar más' })).not.toBeInTheDocument()
  })

  it('el detalle muestra la huella, no el texto', async () => {
    mockApi({ ...sessionRoutes(), 'GET /admin/applications': { body: { data: [] } }, 'GET /admin/audit': { body: { data: [row(1)], next: null } } })
    renderWithProviders(<Audit />, { route: '/audit' })
    await userEvent.click(await screen.findByRole('button', { name: 'Detalle' }))
    expect(await screen.findByText('req-1')).toBeInTheDocument()
    expect(screen.getByText('abc')).toBeInTheDocument()
  })
})
