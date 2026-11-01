// Portal de usuario (G14, RF-17): cada persona ve y gestiona lo suyo, y nada de los demás.
import { Navigate, Route, Routes } from 'react-router'
import { useAuth } from '../lib/auth'
import { Shell, type Section } from '../components/Shell'
import { Spinner } from '../components/ui'
import { Login } from '../pages/Login'
import { MfaSetup } from '../pages/MfaSetup'
import { MyUsage } from './MyUsage'
import { MyKeys } from './MyKeys'
import { MyAccount } from './MyAccount'

const SECTIONS: Section[] = [
  { to: '/usage', label: 'Mi consumo' },
  { to: '/keys', label: 'Mis claves' },
  { to: '/account', label: 'Mi cuenta' },
]

export function PortalApp() {
  const { status, me } = useAuth()
  if (status === 'loading') return <Spinner label="Comprobando la sesión…" />
  if (status !== 'ready' || !me) return <Login title="Mi portal" />
  if (me.scope === 'mfa_setup') return <MfaSetup />
  return (
    <Shell sections={SECTIONS} subtitle="Mi portal" link={me.role === 'admin' ? <a className="small" href="/panel/">Panel de administración</a> : undefined}>
      <Routes>
        <Route path="/" element={<Navigate to="/usage" replace />} />
        <Route path="/usage" element={<MyUsage />} />
        <Route path="/keys" element={<MyKeys />} />
        <Route path="/account" element={<MyAccount />} />
        <Route path="*" element={<Navigate to="/usage" replace />} />
      </Routes>
    </Shell>
  )
}
