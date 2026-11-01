// Equipos y aplicaciones de la organización (RF-16).
import { useState, type FormEvent } from 'react'
import { Link } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import { fmtDate } from '../lib/format'
import { splitList } from '../lib/lists'
import type { Application, List, Team } from '../lib/types'
import { Badge, Empty, ErrorBox, Field, Modal, Spinner, useToast } from '../components/ui'

export function useTeams() {
  return useQuery({ queryKey: ['teams'], queryFn: () => api<List<Team>>('/admin/teams') })
}

export function Applications() {
  const apps = useQuery({ queryKey: ['applications'], queryFn: () => api<List<Application>>('/admin/applications') })
  const teams = useTeams()
  const [creating, setCreating] = useState(false)
  const [creatingTeam, setCreatingTeam] = useState(false)
  const teamName = (id: number) => teams.data?.data.find((t) => t.id === id)?.name ?? `#${id}`

  return (
    <>
      <header className="page-head">
        <h1>Aplicaciones</h1>
        <div className="btn-row">
          <button className="btn" onClick={() => setCreatingTeam(true)}>
            Nuevo equipo
          </button>
          <button className="btn btn-primary" onClick={() => setCreating(true)} disabled={!teams.data?.data.length}>
            Nueva aplicación
          </button>
        </div>
      </header>
      {apps.isError && <ErrorBox error={apps.error} onRetry={() => void apps.refetch()} />}
      {apps.isPending && <Spinner />}
      {apps.data && apps.data.data.length === 0 && <Empty title="Todavía no hay aplicaciones" />}
      {apps.data && apps.data.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Aplicación</th>
                <th>Equipo</th>
                <th>Tipo</th>
                <th>Claves activas</th>
                <th>Último uso</th>
              </tr>
            </thead>
            <tbody>
              {apps.data.data.map((a) => (
                <tr key={a.id}>
                  <td>
                    <Link to={`/applications/${a.id}`}>{a.name}</Link> <span className="muted small">{a.slug}</span>
                  </td>
                  <td>{teamName(a.team_id)}</td>
                  <td>
                    {a.trusted && <Badge tone="info">De confianza</Badge>}{' '}
                    {a.personal_keys && <Badge tone="accent">Claves personales</Badge>}{' '}
                    {a.status !== 'active' && <Badge tone="warning">{a.status}</Badge>}
                  </td>
                  <td>{a.active_keys}</td>
                  <td>{fmtDate(a.last_used_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {teams.data && teams.data.data.length > 0 && (
        <section className="card">
          <h2>Equipos</h2>
          <ul className="chips">
            {teams.data.data.map((t) => (
              <li key={t.id}>
                {t.name} <span className="muted small">{t.slug}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {creating && teams.data && <NewApplication teams={teams.data.data} onClose={() => setCreating(false)} />}
      {creatingTeam && <NewTeam onClose={() => setCreatingTeam(false)} />}
    </>
  )
}

function NewTeam({ onClose }: { onClose: () => void }) {
  const client = useQueryClient()
  const toast = useToast()
  const [slug, setSlug] = useState('')
  const [name, setName] = useState('')
  const create = useMutation({
    mutationFn: () => api<Team>('/admin/teams', { body: { slug, name } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['teams'] })
      toast('Equipo creado')
      onClose()
    },
  })
  return (
    <Modal title="Nuevo equipo" onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault()
          create.mutate()
        }}
      >
        {create.isError && <ErrorBox error={create.error} />}
        <Field label="Nombre">
          <input value={name} onChange={(e) => setName(e.target.value)} required />
        </Field>
        <Field label="Identificador" hint="Minúsculas, números y guiones">
          <input value={slug} onChange={(e) => setSlug(e.target.value)} pattern="[a-z0-9][a-z0-9\-]{1,62}" required />
        </Field>
        <button className="btn btn-primary" disabled={create.isPending}>
          Crear
        </button>
      </form>
    </Modal>
  )
}

function NewApplication({ teams, onClose }: { teams: Team[]; onClose: () => void }) {
  const client = useQueryClient()
  const toast = useToast()
  const [form, setForm] = useState({ name: '', slug: '', team_id: String(teams[0]?.id ?? ''), purposes: '', models: '', trusted: false, personal_keys: false })
  const create = useMutation({
    mutationFn: () =>
      api<Application>('/admin/applications', {
        body: {
          name: form.name,
          slug: form.slug,
          team_id: Number(form.team_id),
          allowed_purposes: splitList(form.purposes),
          allowed_models: form.models.trim() ? splitList(form.models) : null,
          trusted: form.trusted,
          personal_keys: form.personal_keys,
        },
      }),
    onSuccess: (app) => {
      void client.invalidateQueries({ queryKey: ['applications'] })
      toast(`Aplicación ${app.name} creada: ahora crea su clave`)
      onClose()
    },
  })
  const set = (changes: Partial<typeof form>) => setForm((f) => ({ ...f, ...changes }))
  return (
    <Modal title="Nueva aplicación" onClose={onClose}>
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault()
          create.mutate()
        }}
      >
        {create.isError && <ErrorBox error={create.error} />}
        <Field label="Nombre">
          <input value={form.name} onChange={(e) => set({ name: e.target.value })} required />
        </Field>
        <Field label="Identificador" hint="Minúsculas, números y guiones">
          <input value={form.slug} onChange={(e) => set({ slug: e.target.value })} pattern="[a-z0-9][a-z0-9\-]{1,62}" required />
        </Field>
        <Field label="Equipo">
          <select value={form.team_id} onChange={(e) => set({ team_id: e.target.value })}>
            {teams.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Propósitos permitidos" hint="Separados por comas; vacío: cualquiera">
          <input value={form.purposes} onChange={(e) => set({ purposes: e.target.value })} />
        </Field>
        <Field label="Modelos permitidos" hint="Separados por comas; vacío: todos los del catálogo">
          <input value={form.models} onChange={(e) => set({ models: e.target.value })} />
        </Field>
        <label className="check">
          <input type="checkbox" checked={form.trusted} onChange={(e) => set({ trusted: e.target.checked })} /> De confianza
          (puede declarar la persona final)
        </label>
        <label className="check">
          <input type="checkbox" checked={form.personal_keys} onChange={(e) => set({ personal_keys: e.target.checked })} /> Para
          las claves personales
        </label>
        <button className="btn btn-primary" disabled={create.isPending}>
          Crear
        </button>
      </form>
    </Modal>
  )
}
