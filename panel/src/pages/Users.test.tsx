import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { Users } from './Users'
import { mockApi, renderWithProviders, sessionRoutes } from '../test/utils'

function user(id: number, email: string, extra = {}) {
  return { id, email, name: email.split('@')[0], role: 'admin', team_id: null, is_active: true, mfa_enabled: true, has_password: true, platform_operator: false, last_login_at: null, active_sessions: 1, active_keys: 0, created_at: '2026-11-01T08:00:00Z', ...extra }
}

describe('personas', () => {
  it('nadie se cambia a sí mismo el rol ni se da de baja desde aquí', async () => {
    mockApi({ ...sessionRoutes(), 'GET /admin/teams': { body: { data: [] } }, 'GET /admin/users': { body: { data: [user(1, 'eva@cotecnia.es'), user(2, 'luis@cotecnia.es')] } } })
    renderWithProviders(<Users />)
    expect(await screen.findByLabelText('Rol de eva')).toBeDisabled()
    expect(screen.getByLabelText('Rol de luis')).toBeEnabled()
    const rows = screen.getAllByRole('row')
    expect(within(rows[1]).queryByRole('button', { name: 'Baja' })).not.toBeInTheDocument()
    expect(within(rows[2]).getByRole('button', { name: 'Baja' })).toBeInTheDocument()
  })

  it('cerrar sesiones revoca también las claves personales', async () => {
    const calls = mockApi({
      ...sessionRoutes(),
      'GET /admin/teams': { body: { data: [] } },
      'GET /admin/users': { body: { data: [user(1, 'eva@cotecnia.es'), user(2, 'luis@cotecnia.es')] } },
      'POST /admin/users/2/revoke-sessions': { body: { sessions_closed: 1, keys_revoked: 0 } },
    })
    renderWithProviders(<Users />)
    await userEvent.click(await screen.findByRole('button', { name: 'Cerrar sesiones' }))
    await userEvent.click(screen.getByRole('button', { name: 'Sí' }))
    expect(await screen.findByText('Sesiones cerradas y claves revocadas')).toBeInTheDocument()
    expect(calls.find((c) => c.url === '/admin/users/2/revoke-sessions')?.body).toEqual({ keys: true })
  })
})
