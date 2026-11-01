// Marco del panel y del portal: menú lateral, persona y el contenido de cada sección.
import type { ReactNode } from 'react'
import { NavLink } from 'react-router'
import { useAuth } from '../lib/auth'

export type Section = { to: string; label: string }

export const PANEL_SECTIONS: Section[] = [
  { to: '/overview', label: 'Resumen' },
  { to: '/applications', label: 'Aplicaciones' },
  { to: '/keys', label: 'Claves' },
  { to: '/users', label: 'Personas' },
  { to: '/limits', label: 'Cuotas' },
  { to: '/content-policies', label: 'Seguridad de contenido' },
  { to: '/audit', label: 'Auditoría' },
  { to: '/webhooks', label: 'Webhooks' },
  { to: '/catalog', label: 'Catálogo de modelos' },
  { to: '/actions', label: 'Registro de cambios' },
]

export function Shell({
  children,
  sections = PANEL_SECTIONS,
  subtitle = 'Panel',
  link,
}: {
  children: ReactNode
  sections?: Section[]
  subtitle?: string
  link?: ReactNode
}) {
  const { me, logout } = useAuth()
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <strong>PLATIA Gateway</strong>
          <span className="muted">{subtitle}</span>
        </div>
        <nav aria-label="Secciones">
          {sections.map((s) => (
            <NavLink key={s.to} to={s.to} className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}>
              {s.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div>{me?.name}</div>
          <div className="muted small">{me?.email}</div>
          {me?.platform_operator && <div className="small">Opera el gateway</div>}
          {link}
          <button className="btn btn-small btn-ghost" onClick={() => void logout()}>
            Salir
          </button>
        </div>
      </aside>
      <main className="content">{children}</main>
    </div>
  )
}
