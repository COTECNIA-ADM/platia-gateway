// Utilidades de las pruebas: una API falsa (fetch simulado por rutas), un WebSocket falso y
// el árbol de proveedores que monta main.tsx. Adaptadas del frontal de PLATIA.
import type { ReactNode } from 'react'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router'
import { vi } from 'vitest'
import { AuthProvider } from '../lib/auth'
import { ToastProvider } from '../components/ui'
import type { Me } from '../lib/types'

export type Call = { method: string; url: string; headers: Record<string, string>; body: unknown }
type Reply = { status?: number; body?: unknown; headers?: Record<string, string> }
type Handler = (call: Call) => Reply | Promise<Reply>

/** Sustituye fetch por una tabla «MÉTODO /ruta» → respuesta (sin la query). Lo demás, 404. */
export function mockApi(routes: Record<string, Handler | Reply>) {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = String(input)
      const method = (init.method ?? 'GET').toUpperCase()
      const headers = { ...(init.headers as Record<string, string> | undefined) }
      const body = typeof init.body === 'string' ? JSON.parse(init.body) : init.body
      const call = { method, url, headers, body }
      calls.push(call)
      const route = routes[`${method} ${url.split('?')[0]}`]
      const reply = typeof route === 'function' ? await route(call) : (route ?? { status: 404, body: { type: 'error', error: { type: 'not_found_error', message: 'No existe' } } })
      return new Response(reply.body === undefined ? null : JSON.stringify(reply.body), {
        status: reply.status ?? 200,
        headers: { 'Content-Type': 'application/json', ...reply.headers },
      })
    }),
  )
  return calls
}

/** WebSocket que no conecta a nada: guarda las instancias para simular mensajes y cierres. */
export class FakeSocket {
  static instances: FakeSocket[] = []
  onopen: (() => void) | null = null
  onmessage: ((m: { data: string }) => void) | null = null
  onclose: ((c: { code: number }) => void) | null = null
  closed = false
  constructor(
    public url: string,
    public protocols: string[],
  ) {
    FakeSocket.instances.push(this)
  }
  close() {
    this.closed = true
  }
}

export function stubWebSocket() {
  FakeSocket.instances = []
  vi.stubGlobal('WebSocket', FakeSocket)
}

export const ADMIN: Me = {
  id: 1,
  email: 'eva@cotecnia.es',
  name: 'Eva',
  role: 'admin',
  organization_id: 1,
  mfa_enabled: true,
  scope: 'full',
  platform_operator: false,
}

/** Rutas de una sesión ya abierta: renovación correcta y /auth/me. */
export function sessionRoutes(me: Partial<Me> = {}) {
  return {
    'POST /auth/refresh': { body: { access_token: 'token-de-prueba', scope: 'full' } },
    'GET /auth/me': { body: { ...ADMIN, ...me } },
  }
}

export function renderWithProviders(ui: ReactNode, { route = '/' }: { route?: string } = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <ToastProvider>
          <AuthProvider>{ui}</AuthProvider>
        </ToastProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
