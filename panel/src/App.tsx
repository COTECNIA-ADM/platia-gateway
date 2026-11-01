// Rutas del panel. Cada sección usa el mismo nombre que su ruta de la API (/admin/...).
import { Navigate, Route, Routes } from 'react-router'
import { useAuth } from './lib/auth'
import { Shell } from './components/Shell'
import { Spinner } from './components/ui'
import { Login } from './pages/Login'
import { MfaSetup } from './pages/MfaSetup'
import { Overview } from './pages/Overview'
import { Applications } from './pages/Applications'
import { ApplicationDetail } from './pages/ApplicationDetail'
import { Keys } from './pages/Keys'
import { Users } from './pages/Users'
import { Limits } from './pages/Limits'
import { ContentPolicies } from './pages/ContentPolicies'
import { Audit } from './pages/Audit'
import { Webhooks } from './pages/Webhooks'
import { Catalog } from './pages/Catalog'
import { Actions } from './pages/Actions'

export function App() {
  const { status, me, logout } = useAuth()
  if (status === 'loading') return <Spinner label="Comprobando la sesión…" />
  if (status !== 'ready' || !me) return <Login />
  if (me.scope === 'mfa_setup') return <MfaSetup />
  if (me.role !== 'admin')
    return (
      <main className="login">
        <section className="card login-card">
          <h1>Sin acceso al panel</h1>
          <p>El panel es para administradores. Tus claves y tu consumo estarán en el portal de usuario.</p>
          <button className="btn" onClick={() => void logout()}>
            Salir
          </button>
        </section>
      </main>
    )
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<Navigate to="/overview" replace />} />
        <Route path="/overview" element={<Overview />} />
        <Route path="/applications" element={<Applications />} />
        <Route path="/applications/:id" element={<ApplicationDetail />} />
        <Route path="/keys" element={<Keys />} />
        <Route path="/users" element={<Users />} />
        <Route path="/limits" element={<Limits />} />
        <Route path="/content-policies" element={<ContentPolicies />} />
        <Route path="/audit" element={<Audit />} />
        <Route path="/webhooks" element={<Webhooks />} />
        <Route path="/catalog" element={<Catalog />} />
        <Route path="/actions" element={<Actions />} />
        <Route path="*" element={<Navigate to="/overview" replace />} />
      </Routes>
    </Shell>
  )
}
