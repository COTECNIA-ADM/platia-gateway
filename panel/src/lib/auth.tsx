// Sesión del panel (G13b): contraseña, segundo factor y renovación con la cookie de /auth.
// Los administradores necesitan MFA (ADR-014): sin él, el gateway da un token que solo
// sirve para configurarlo, y el panel lleva a la persona a hacerlo antes de seguir.
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { api, CSRF, refreshSession, setAccessToken } from './api'
import type { Me } from './types'

type Status = 'loading' | 'anonymous' | 'mfa' | 'ready'

type Session = { access_token: string; scope: string }
type LoginReply = Session | { mfa_required: true; mfa_ticket: string }

type Auth = {
  status: Status
  me: Me | null
  login: (email: string, password: string) => Promise<void>
  completeMfa: (code: string) => Promise<void>
  enableMfa: (code: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<Auth | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status>('loading')
  const [me, setMe] = useState<Me | null>(null)
  const [ticket, setTicket] = useState<string | null>(null)

  const loadMe = useCallback(async () => {
    setMe(await api<Me>('/auth/me'))
    setStatus('ready')
  }, [])

  const start = useCallback(
    async (session: Session) => {
      setAccessToken(session.access_token)
      setTicket(null)
      await loadMe()
    },
    [loadMe],
  )

  useEffect(() => {
    let alive = true
    refreshSession()
      .then(async (ok) => {
        if (!alive) return
        if (ok) await loadMe()
        else setStatus('anonymous')
      })
      .catch(() => alive && setStatus('anonymous'))
    const expired = () => {
      setMe(null)
      setStatus('anonymous')
    }
    window.addEventListener('auth:expired', expired)
    return () => {
      alive = false
      window.removeEventListener('auth:expired', expired)
    }
  }, [loadMe])

  const value = useMemo<Auth>(
    () => ({
      status,
      me,
      async login(email, password) {
        const reply = await api<LoginReply>('/auth/login', { body: { email, password } })
        if ('mfa_required' in reply) {
          setTicket(reply.mfa_ticket)
          setStatus('mfa')
        } else {
          await start(reply)
        }
      },
      async completeMfa(code) {
        await start(await api<Session>('/auth/login/mfa', { body: { mfa_ticket: ticket, code } }))
      },
      async enableMfa(code) {
        await start(await api<Session>('/auth/mfa/enable', { body: { code } }))
      },
      async logout() {
        try {
          await api('/auth/logout', { method: 'POST', headers: CSRF })
        } finally {
          setAccessToken(null)
          setMe(null)
          setTicket(null)
          setStatus('anonymous')
        }
      },
    }),
    [status, me, ticket, start],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): Auth {
  const auth = useContext(AuthContext)
  if (!auth) throw new Error('useAuth fuera de <AuthProvider>')
  return auth
}
