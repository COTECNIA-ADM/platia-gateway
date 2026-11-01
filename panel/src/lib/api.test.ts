import { describe, expect, it, vi } from 'vitest'
import { api, ApiError, buildUrl, getAccessToken, setAccessToken } from './api'
import { mockApi } from '../test/utils'

describe('api', () => {
  it('manda el token en memoria y no usa el almacenamiento del navegador', async () => {
    const calls = mockApi({ 'GET /admin/teams': { body: { data: [] } } })
    const storage = vi.spyOn(Storage.prototype, 'setItem')
    setAccessToken('t1')
    await api('/admin/teams')
    expect(calls[0].headers.Authorization).toBe('Bearer t1')
    expect(storage).not.toHaveBeenCalled()
  })

  it('con un 401 renueva una vez (con X-Requested-With) y repite', async () => {
    let first = true
    const calls = mockApi({
      'GET /admin/users': () => {
        if (first) {
          first = false
          return { status: 401 }
        }
        return { body: { data: [1] } }
      },
      'POST /auth/refresh': { body: { access_token: 't2' } },
    })
    setAccessToken('caducado')
    expect(await api('/admin/users')).toEqual({ data: [1] })
    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual(['GET /admin/users', 'POST /auth/refresh', 'GET /admin/users'])
    expect(calls[1].headers['X-Requested-With']).toBeTruthy()
    expect(calls[2].headers.Authorization).toBe('Bearer t2')
  })

  it('si la renovación falla, avisa de que la sesión ha caducado', async () => {
    mockApi({ 'GET /admin/users': { status: 401 }, 'POST /auth/refresh': { status: 401 } })
    const expired = vi.fn()
    window.addEventListener('auth:expired', expired)
    setAccessToken('caducado')
    await expect(api('/admin/users')).rejects.toBeInstanceOf(ApiError)
    expect(expired).toHaveBeenCalled()
    expect(getAccessToken()).toBeNull()
    window.removeEventListener('auth:expired', expired)
  })

  it('lee el mensaje de error del gateway', async () => {
    mockApi({ 'POST /admin/teams': { status: 409, body: { type: 'error', error: { type: 'invalid_request_error', message: 'Ya existe' } } } })
    await expect(api('/admin/teams', { body: {} })).rejects.toMatchObject({ status: 409, message: 'Ya existe' })
  })

  it('construye la query sin valores vacíos', () => {
    expect(buildUrl('/admin/audit', { status: 'error', model: '', before_id: null, limit: 50 })).toBe('/admin/audit?status=error&limit=50')
  })
})
