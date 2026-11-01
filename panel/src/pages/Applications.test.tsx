import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { Route, Routes } from 'react-router'
import { ApplicationDetail } from './ApplicationDetail'
import { mockApi, renderWithProviders, sessionRoutes } from '../test/utils'

const APP = {
  id: 3, slug: 'informes', name: 'Informes', team_id: 1, status: 'active', trusted: false, allowed_purposes: [], allowed_models: null,
  personal_keys: false, cache_ttl_seconds: 0, cache_purposes: null, active_keys: 1, last_used_at: null, created_at: '2026-11-01T08:00:00Z',
}
const KEY = { id: 9, kind: 'application', prefix: 'plgw-app-inf', label: 'producción', state: 'active', application_id: 3, user_id: null, owner: 'informes', created_at: '2026-11-01T08:00:00Z', expires_at: null, revoked_at: null, last_used_at: null }

function renderDetail() {
  return renderWithProviders(
    <Routes>
      <Route path="/applications/:id" element={<ApplicationDetail />} />
    </Routes>,
    { route: '/applications/3' },
  )
}

describe('detalle de una aplicación', () => {
  it('la clave nueva se enseña una vez y no queda en la lista', async () => {
    const calls = mockApi({
      ...sessionRoutes(),
      'GET /admin/applications/3': { body: APP },
      'GET /admin/applications/3/keys': { body: { data: [KEY] } },
      'POST /admin/applications/3/keys': { status: 201, body: { ...KEY, id: 10, key: 'plgw-app-informes-secreta' } },
    })
    renderDetail()
    await userEvent.type(await screen.findByLabelText('Etiqueta'), 'nueva')
    await userEvent.click(screen.getByRole('button', { name: 'Crear clave' }))
    const dialog = await screen.findByRole('dialog', { name: 'Clave creada' })
    expect(within(dialog).getByText('plgw-app-informes-secreta')).toBeInTheDocument()
    expect(within(dialog).getByText(/no se volverá a mostrar/)).toBeInTheDocument()
    expect(calls.find((c) => c.method === 'POST' && c.url.startsWith('/admin/'))?.body).toEqual({ label: 'nueva', expires_days: null })
    await userEvent.click(within(dialog).getByRole('button', { name: 'Cerrar' }))
    expect(screen.queryByText('plgw-app-informes-secreta')).not.toBeInTheDocument()
  })

  it('revocar pide confirmación', async () => {
    const calls = mockApi({
      ...sessionRoutes(),
      'GET /admin/applications/3': { body: APP },
      'GET /admin/applications/3/keys': { body: { data: [KEY] } },
      'DELETE /admin/keys/9': { status: 204 },
    })
    renderDetail()
    await userEvent.click(await screen.findByRole('button', { name: 'Revocar' }))
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false)
    await userEvent.click(screen.getByRole('button', { name: 'Sí' }))
    expect(await screen.findByText('Clave revocada')).toBeInTheDocument()
    expect(calls.some((c) => c.method === 'DELETE' && c.url === '/admin/keys/9')).toBe(true)
  })

  it('guarda las reglas con listas separadas por comas', async () => {
    const calls = mockApi({
      ...sessionRoutes(),
      'GET /admin/applications/3': { body: APP },
      'GET /admin/applications/3/keys': { body: { data: [] } },
      'PATCH /admin/applications/3': (call) => ({ body: { ...APP, ...(call.body as object) } }),
    })
    renderDetail()
    await userEvent.type(await screen.findByLabelText('Propósitos permitidos'), 'resumen, informe ,')
    await userEvent.click(screen.getByRole('button', { name: 'Guardar' }))
    expect(await screen.findByText('Cambios guardados')).toBeInTheDocument()
    expect(calls.find((c) => c.method === 'PATCH')?.body).toMatchObject({ allowed_purposes: ['resumen', 'informe'], allowed_models: null })
  })
})
