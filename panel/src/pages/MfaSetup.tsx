// Primer acceso de un administrador: el MFA es obligatorio antes de usar el panel (ADR-014).
import { useState, type FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../lib/api'
import { useAuth } from '../lib/auth'
import { ErrorBox, Field } from '../components/ui'

type Setup = { secret: string; otpauth_uri: string }

export function MfaSetup() {
  const { enableMfa, logout } = useAuth()
  const [code, setCode] = useState('')
  const setup = useMutation({ mutationFn: () => api<Setup>('/auth/mfa/setup', { method: 'POST' }) })
  const enable = useMutation({ mutationFn: () => enableMfa(code.trim()) })

  function submit(e: FormEvent) {
    e.preventDefault()
    enable.mutate()
  }

  return (
    <main className="login">
      <section className="card login-card">
        <h1>Activa el segundo factor</h1>
        <p>Para administrar el gateway hace falta una app autenticadora (Google Authenticator, Microsoft Authenticator, 1Password…).</p>
        {!setup.data ? (
          <>
            {setup.isError && <ErrorBox error={setup.error} />}
            <button className="btn btn-primary" onClick={() => setup.mutate()} disabled={setup.isPending}>
              Empezar
            </button>
          </>
        ) : (
          <form onSubmit={submit}>
            <p>Añade esta clave en la app (o abre el enlace desde el móvil):</p>
            <code className="secret-value">{setup.data.secret}</code>
            <p>
              <a href={setup.data.otpauth_uri}>Abrir en la app autenticadora</a>
            </p>
            {enable.isError && <ErrorBox error={enable.error} />}
            <Field label="Código que muestra la app">
              <input
                value={code}
                onChange={(e) => setCode(e.target.value)}
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]{6}"
                required
                autoFocus
              />
            </Field>
            <button className="btn btn-primary" disabled={enable.isPending}>
              Activar y entrar
            </button>
          </form>
        )}
        <button className="btn btn-ghost" onClick={() => void logout()}>
          Salir
        </button>
      </section>
    </main>
  )
}
