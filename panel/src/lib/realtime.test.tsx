import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setAccessToken } from './api'
import { MAX_DELAY, nextDelay, streamUrl, useLiveEvents } from './realtime'
import { FakeSocket, mockApi, stubWebSocket } from '../test/utils'

describe('tiempo real', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    stubWebSocket()
    setAccessToken('tok')
  })
  afterEach(() => vi.useRealTimers())

  it('espera de 1 s a 30 s entre reconexiones', () => {
    expect(nextDelay(500)).toBe(1000)
    expect(nextDelay(1000)).toBe(2000)
    expect(nextDelay(20_000)).toBe(MAX_DELAY)
  })

  it('usa wss con https y nunca pone el token en la URL', () => {
    expect(streamUrl({ protocol: 'https:', host: 'gw.cotecnia.es' })).toBe('wss://gw.cotecnia.es/events/stream')
    renderHook(() => useLiveEvents())
    const socket = FakeSocket.instances[0]
    expect(socket.url).not.toContain('tok')
    expect(socket.protocols).toEqual(['platia.v1', 'bearer.tok'])
  })

  it('recibe eventos y se vuelve a conectar tras un corte', async () => {
    const { result } = renderHook(() => useLiveEvents())
    const socket = FakeSocket.instances[0]
    act(() => {
      socket.onopen?.()
      socket.onmessage?.({ data: JSON.stringify({ id: 'e1', type: 'request.finished', created_at: '2026-11-01T10:00:00Z', organization_id: 1, data: {} }) })
    })
    expect(result.current.state).toBe('open')
    expect(result.current.events.map((e) => e.id)).toEqual(['e1'])
    await act(async () => {
      await socket.onclose?.({ code: 1006 })
    })
    expect(result.current.state).toBe('closed')
    await act(async () => {
      vi.advanceTimersByTime(1000)
    })
    expect(FakeSocket.instances).toHaveLength(2)
  })

  it('con la sesión caducada renueva el token antes de reconectar; sin permiso, para', async () => {
    const calls = mockApi({ 'POST /auth/refresh': { body: { access_token: 'nuevo' } } })
    const { result } = renderHook(() => useLiveEvents())
    await act(async () => {
      await FakeSocket.instances[0].onclose?.({ code: 4401 })
      vi.advanceTimersByTime(1000)
    })
    expect(calls.map((c) => c.url)).toEqual(['/auth/refresh'])
    expect(FakeSocket.instances[1].protocols).toEqual(['platia.v1', 'bearer.nuevo'])
    await act(async () => {
      await FakeSocket.instances[1].onclose?.({ code: 4403 })
      vi.advanceTimersByTime(60_000)
    })
    expect(result.current.state).toBe('denied')
    expect(FakeSocket.instances).toHaveLength(2)
  })
})
