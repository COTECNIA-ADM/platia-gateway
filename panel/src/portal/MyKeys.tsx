// Mis claves (cap. 30): claves personales para Claude Code, los SDK o scripts propios.
// Cada una sigue las reglas de la aplicación de claves personales y queda a nombre de quien la crea.
import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import { fmtDate, fmtDay, fmtUsd } from '../lib/format'
import type { List } from '../lib/types'
import { ConfirmButton, Empty, ErrorBox, Field, Modal, OneTimeSecret, Spinner, useToast } from '../components/ui'
import type { MyKey, NewMyKey, PortalInfo } from './types'

type Rotated = NewMyKey & { previous: { id: number; expires_at: string | null; revoked_at: string | null } }

export function MyKeys() {
  const info = useQuery({ queryKey: ['me-portal'], queryFn: () => api<PortalInfo>('/me/portal') })
  const keys = useQuery({ queryKey: ['me-keys'], queryFn: () => api<List<MyKey>>('/me/keys') })
  const [created, setCreated] = useState<{ key: NewMyKey; note?: string } | null>(null)
  const [rotating, setRotating] = useState<MyKey | null>(null)
  const client = useQueryClient()
  const toast = useToast()
  const revoke = useMutation({
    mutationFn: (id: number) => api(`/me/keys/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      toast('Clave revocada')
      void client.invalidateQueries({ queryKey: ['me-keys'] })
    },
    onError: (e) => toast(e instanceof Error ? e.message : 'No se pudo revocar', 'danger'),
  })

  const personal = info.data?.personal_keys
  const list = keys.data?.data ?? []
  return (
    <>
      <header className="page-head">
        <h1>Mis claves</h1>
      </header>
      {(info.isError || keys.isError) && <ErrorBox error={info.error ?? keys.error} />}
      {(info.isPending || keys.isPending) && <Spinner />}
      {personal && !personal.enabled && (
        <p className="notice notice-warning">Tu organización no tiene activadas las claves personales. Pide a un administrador que cree la aplicación de claves personales.</p>
      )}
      {created && <NewKeyBox created={created.key} note={created.note} onClose={() => setCreated(null)} />}
      {personal?.enabled && keys.data && (
        <section className="card">
          <h2>Claves activas</h2>
          {list.length === 0 ? (
            <Empty title="Todavía no tienes claves" />
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Clave</th>
                    <th>Nombre</th>
                    <th>Creada</th>
                    <th>Caduca</th>
                    <th>Último uso</th>
                    <th>
                      <span className="sr-only">Acciones</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((k) => (
                    <tr key={k.id}>
                      <td>
                        <code>{k.prefix}…</code>
                      </td>
                      <td>{k.label ?? '—'}</td>
                      <td>{fmtDay(k.created_at)}</td>
                      <td>{fmtDate(k.expires_at)}</td>
                      <td>{fmtDate(k.last_used_at)}</td>
                      <td className="btn-row">
                        <button className="btn btn-small" onClick={() => setRotating(k)}>
                          Rotar
                        </button>
                        <ConfirmButton question="¿Revocar esta clave? Deja de funcionar al momento." onConfirm={() => revoke.mutate(k.id)}>
                          Revocar
                        </ConfirmButton>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <NewKeyForm
            maxDays={personal.max_days}
            full={list.length >= personal.max_keys}
            maxKeys={personal.max_keys}
            onCreated={(key) => {
              setCreated({ key })
              void client.invalidateQueries({ queryKey: ['me-keys'] })
            }}
          />
        </section>
      )}
      {rotating && personal && (
        <RotateModal
          keyRow={rotating}
          maxDays={personal.max_days}
          maxGrace={personal.max_grace_hours}
          onClose={() => setRotating(null)}
          onRotated={(key) => {
            setRotating(null)
            const prev = key.previous
            setCreated({
              key,
              note: prev.revoked_at ? 'La clave anterior ya no funciona.' : `La clave anterior funciona hasta el ${fmtDate(prev.expires_at)}.`,
            })
            void client.invalidateQueries({ queryKey: ['me-keys'] })
          }}
        />
      )}
      {info.data && <Models info={info.data} />}
    </>
  )
}

function NewKeyForm({ maxDays, maxKeys, full, onCreated }: { maxDays: number; maxKeys: number; full: boolean; onCreated: (k: NewMyKey) => void }) {
  const [label, setLabel] = useState('')
  const [days, setDays] = useState('90')
  const create = useMutation({
    mutationFn: () => api<NewMyKey>('/me/keys', { body: { label: label.trim() || null, expires_days: Number(days) } }),
    onSuccess: (key) => {
      setLabel('')
      onCreated(key)
    },
  })
  function submit(e: FormEvent) {
    e.preventDefault()
    create.mutate()
  }
  if (full) return <p className="muted">Tienes {maxKeys} claves, el máximo. Revoca una que no uses para crear otra.</p>
  return (
    <form className="filter-bar" onSubmit={submit}>
      <Field label="Nombre" hint="Para reconocerla: «portátil», «Claude Code»…">
        <input value={label} onChange={(e) => setLabel(e.target.value)} maxLength={100} />
      </Field>
      <Field label="Días de validez" hint={`Máximo ${maxDays}`}>
        <input type="number" min={1} max={maxDays} value={days} onChange={(e) => setDays(e.target.value)} required />
      </Field>
      <button className="btn btn-primary" disabled={create.isPending}>
        Crear clave
      </button>
      {create.isError && <ErrorBox error={create.error} />}
    </form>
  )
}

function RotateModal({
  keyRow,
  maxDays,
  maxGrace,
  onClose,
  onRotated,
}: {
  keyRow: MyKey
  maxDays: number
  maxGrace: number
  onClose: () => void
  onRotated: (k: Rotated) => void
}) {
  const [days, setDays] = useState('90')
  const [grace, setGrace] = useState('24')
  const rotate = useMutation({
    mutationFn: () => api<Rotated>(`/me/keys/${keyRow.id}/rotate`, { body: { expires_days: Number(days), grace_hours: Number(grace) } }),
    onSuccess: onRotated,
  })
  function submit(e: FormEvent) {
    e.preventDefault()
    rotate.mutate()
  }
  return (
    <Modal title={`Rotar ${keyRow.prefix}…`} onClose={onClose}>
      <form onSubmit={submit}>
        <p>Se crea una clave nueva con el mismo nombre. La anterior sigue funcionando el margen que elijas, para que cambies la configuración sin cortes.</p>
        <Field label="Días de validez de la nueva" hint={`Máximo ${maxDays}`}>
          <input type="number" min={1} max={maxDays} value={days} onChange={(e) => setDays(e.target.value)} required />
        </Field>
        <Field label="Horas de margen para la anterior" hint={`0 la revoca al momento; máximo ${maxGrace}`}>
          <input type="number" min={0} max={maxGrace} value={grace} onChange={(e) => setGrace(e.target.value)} required />
        </Field>
        {rotate.isError && <ErrorBox error={rotate.error} />}
        <div className="btn-row">
          <button className="btn btn-primary" disabled={rotate.isPending}>
            Rotar
          </button>
          <button type="button" className="btn btn-ghost" onClick={onClose}>
            Cancelar
          </button>
        </div>
      </form>
    </Modal>
  )
}

function NewKeyBox({ created, note, onClose }: { created: NewMyKey; note?: string; onClose: () => void }) {
  const origin = window.location.origin
  return (
    <section className="card" aria-label="Clave nueva">
      <header className="card-head">
        <h2>Clave nueva{created.label ? `: ${created.label}` : ''}</h2>
        <button className="btn btn-small btn-ghost" onClick={onClose}>
          Hecho
        </button>
      </header>
      {note && <p className="notice notice-info">{note}</p>}
      <OneTimeSecret label="Clave" value={created.key} />
      <h3>Cómo usarla</h3>
      <p>Claude Code y el SDK de Anthropic:</p>
      <pre className="code">{`ANTHROPIC_BASE_URL=${origin}\nANTHROPIC_API_KEY=<tu clave>`}</pre>
      <p>SDK de OpenAI y herramientas compatibles:</p>
      <pre className="code">{`OPENAI_BASE_URL=${origin}/v1\nOPENAI_API_KEY=<tu clave>`}</pre>
    </section>
  )
}

function Models({ info }: { info: PortalInfo }) {
  if (!info.personal_keys.enabled) return null
  return (
    <section className="card">
      <h2>Modelos que puedes usar</h2>
      {info.personal_keys.purposes.length > 0 && (
        <p className="muted">Usos permitidos (cabecera X-Platia-Purpose): {info.personal_keys.purposes.join(', ')}</p>
      )}
      {info.models.length === 0 ? (
        <Empty title="No hay modelos disponibles" />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Modelo</th>
                <th>Nombre</th>
                <th>Familia</th>
                <th>Entrada (por millón de tokens)</th>
                <th>Salida (por millón de tokens)</th>
              </tr>
            </thead>
            <tbody>
              {info.models.map((m) => (
                <tr key={m.model}>
                  <td>
                    <code>{m.model}</code>
                  </td>
                  <td>{m.name}</td>
                  <td>{m.family}</td>
                  <td>{fmtUsd(m.input_per_mtok)}</td>
                  <td>{fmtUsd(m.output_per_mtok)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
