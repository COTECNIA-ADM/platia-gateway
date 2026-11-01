// Entrada al panel: contraseña y, después, el código de la app autenticadora.
import { useState, type FormEvent } from 'react'
import { useAuth } from '../lib/auth'
import { ErrorBox, Field } from '../components/ui'

export function Login({ title = 'Panel de administración' }: { title?: string }) {
  const { status, login, completeMfa } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent, action: () => Promise<void>) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await action()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="login">
      <section className="card login-card">
        <h1>PLATIA Gateway</h1>
        <p className="muted">{title}</p>
        {error !== null && <ErrorBox error={error} />}
        {status === 'mfa' ? (
          <form onSubmit={(e) => submit(e, () => completeMfa(code.trim()))}>
            <Field label="Código de la app autenticadora">
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
            <button className="btn btn-primary" disabled={busy}>
              Entrar
            </button>
          </form>
        ) : (
          <form onSubmit={(e) => submit(e, () => login(email.trim(), password))}>
            <Field label="Correo">
              <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" required autoFocus />
            </Field>
            <Field label="Contraseña">
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
              />
            </Field>
            <button className="btn btn-primary" disabled={busy}>
              Continuar
            </button>
          </form>
        )}
      </section>
    </main>
  )
}
