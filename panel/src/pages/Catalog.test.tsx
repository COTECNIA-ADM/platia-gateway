import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { Catalog } from './Catalog'
import { mockApi, renderWithProviders, sessionRoutes } from '../test/utils'

function catalog(canEdit: boolean) {
  return {
    can_edit: canEdit,
    providers: [{ id: 1, slug: 'anthropic', name: 'Anthropic', kind: 'anthropic', base_url: null, has_api_key: true, is_active: true }],
    models: [{ id: 2, model_key: 'claude-test', display_name: 'Claude', family: 'anthropic', context_window: null, max_output_tokens: null, capabilities: [], fallback_models: [], is_active: true, price: null }],
    deployments: [{ id: 3, model_id: 2, provider_id: 1, upstream_model: 'claude-up', priority: 0, weight: 100, is_active: true, cooled: true }],
  }
}

describe('catálogo', () => {
  it('un administrador lo ve sin poder cambiarlo y sin claves', async () => {
    mockApi({ ...sessionRoutes(), 'GET /admin/catalog': { body: catalog(false) } })
    renderWithProviders(<Catalog />)
    expect(await screen.findByText(/Solo lectura/)).toBeInTheDocument()
    expect(screen.getByText('Guardada')).toBeInTheDocument()
    expect(screen.getByText('Enfriado')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Nuevo modelo' })).not.toBeInTheDocument()
  })

  it('quien opera el gateway cambia el peso y quita el enfriamiento', async () => {
    const calls = mockApi({
      ...sessionRoutes({ platform_operator: true }),
      'GET /admin/catalog': { body: catalog(true) },
      'PATCH /admin/deployments/3': { body: {} },
      'DELETE /admin/deployments/3/cooldown': { status: 204 },
    })
    renderWithProviders(<Catalog />)
    const weight = await screen.findByLabelText('Peso del despliegue 3')
    await userEvent.clear(weight)
    await userEvent.type(weight, '50')
    await userEvent.click(screen.getByRole('button', { name: 'Guardar' }))
    await userEvent.click(screen.getByRole('button', { name: 'Quitar enfriamiento' }))
    expect(calls.find((c) => c.method === 'PATCH')?.body).toEqual({ priority: 0, weight: 50 })
    expect(calls.some((c) => c.method === 'DELETE')).toBe(true)
  })

  it('la clave de un proveedor se escribe y no se lee', async () => {
    const calls = mockApi({ ...sessionRoutes({ platform_operator: true }), 'GET /admin/catalog': { body: catalog(true) }, 'PATCH /admin/providers/1': { body: {} } })
    renderWithProviders(<Catalog />)
    await userEvent.click((await screen.findAllByRole('button', { name: 'Editar' }))[0])
    const dialog = await screen.findByRole('dialog')
    const key = within(dialog).getByLabelText('Clave nueva')
    expect(key).toHaveValue('')
    await userEvent.type(key, 'sk-nueva')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Guardar' }))
    expect(calls.find((c) => c.method === 'PATCH')?.body).toMatchObject({ api_key: 'sk-nueva', name: 'Anthropic' })
  })
})
