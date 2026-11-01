// Cliente de la API del gateway para el panel (G13b).
// - El token de acceso vive en memoria, nunca en localStorage ni sessionStorage: un script
//   inyectado no puede leerlo de ahí. La renovación va en una cookie HttpOnly (ruta /auth)
//   que JavaScript no ve, y la ruta exige X-Requested-With (ADR-014).
// - Si varias peticiones reciben 401 a la vez, se renueva una sola vez y todas esperan.
// Adaptado del cliente del frontal de PLATIA (cotecnia-adm/platia-dev, frontend/src/lib/api.ts).

let accessToken: string | null = null
let refreshing: Promise<boolean> | null = null

export const CSRF = { 'X-Requested-With': 'platia-panel' }

export function setAccessToken(token: string | null) {
  accessToken = token
}

export function getAccessToken(): string | null {
  return accessToken
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public kind = '',
  ) {
    super(message)
  }
}

/** Avisa a la aplicación de que la sesión se ha perdido (la renovación ha fallado). */
function sessionExpired() {
  accessToken = null
  window.dispatchEvent(new CustomEvent('auth:expired'))
}

export function refreshSession(): Promise<boolean> {
  refreshing ??= (async () => {
    try {
      const resp = await fetch('/auth/refresh', { method: 'POST', credentials: 'same-origin', headers: CSRF })
      if (!resp.ok) return false
      const body = (await resp.json()) as { access_token: string }
      accessToken = body.access_token
      return true
    } catch {
      return false
    } finally {
      // Se libera en la siguiente vuelta para que las peticiones que esperan lo vean resuelto
      setTimeout(() => (refreshing = null), 0)
    }
  })()
  return refreshing
}

export type Query = Record<string, string | number | boolean | null | undefined>

type Options = { method?: string; body?: unknown; query?: Query; headers?: Record<string, string> }

export function buildUrl(path: string, query?: Query) {
  if (!query) return path
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
  }
  const qs = params.toString()
  return qs ? `${path}?${qs}` : path
}

async function send(path: string, options: Options): Promise<Response> {
  const headers: Record<string, string> = { ...options.headers }
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  return fetch(buildUrl(path, options.query), {
    method: options.method ?? (options.body !== undefined ? 'POST' : 'GET'),
    headers,
    credentials: 'same-origin',
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  })
}

/** Hace la petición y, si el token ha caducado, lo renueva una vez y la repite. */
async function request(path: string, options: Options): Promise<Response> {
  let resp = await send(path, options)
  if (resp.status === 401 && accessToken !== null) {
    if (await refreshSession()) resp = await send(path, options)
    else sessionExpired()
  }
  return resp
}

/** Los errores del gateway llegan como {"type": "error", "error": {"type", "message"}}. */
async function fail(resp: Response): Promise<never> {
  const body = (await resp.json().catch(() => ({}))) as { error?: { type?: string; message?: string } | string }
  const error = typeof body.error === 'object' ? body.error : undefined
  const message =
    error?.message ??
    (typeof body.error === 'string' ? body.error : undefined) ??
    (resp.status === 429 ? 'Demasiadas peticiones seguidas. Espera un momento.' : `Error ${resp.status}`)
  throw new ApiError(resp.status, message, error?.type ?? '')
}

export async function api<T>(path: string, options: Options = {}): Promise<T> {
  const resp = await request(path, options)
  if (!resp.ok) return fail(resp)
  if (resp.status === 204) return undefined as T
  return (await resp.json()) as T
}

/** Descarga un fichero protegido (con el token) y lo guarda con su nombre. */
export async function download(path: string, query: Query, fallbackName = 'descarga') {
  const resp = await request(buildUrl(path, query), {})
  if (!resp.ok) return fail(resp)
  const disposition = resp.headers.get('Content-Disposition') ?? ''
  const match = /filename="?([^";]+)"?/i.exec(disposition)
  const url = URL.createObjectURL(await resp.blob())
  const link = document.createElement('a')
  link.href = url
  link.download = match ? match[1] : fallbackName
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
}
