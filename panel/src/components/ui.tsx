// Piezas comunes de la interfaz: carga y error, insignias, diálogos, avisos y el secreto que
// se enseña una vez. Adaptadas del frontal de PLATIA (cotecnia-adm/platia-dev).
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { ApiError } from '../lib/api'

export function Spinner({ label = 'Cargando…' }: { label?: string }) {
  return (
    <div className="spinner" role="status">
      <span className="spinner-dot" aria-hidden />
      {label}
    </div>
  )
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return 'Ha fallado algo inesperado.'
}

export function ErrorBox({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return (
    <div className="notice notice-error" role="alert">
      <span>{errorMessage(error)}</span>
      {onRetry && (
        <button className="btn btn-small" onClick={onRetry}>
          Reintentar
        </button>
      )}
    </div>
  )
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <strong>{title}</strong>
      {children && <div>{children}</div>}
    </div>
  )
}

export type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'danger' | 'accent'

export function Badge({ tone = 'neutral', children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  )
}

export function Modal({
  title,
  onClose,
  children,
  wide = false,
}: {
  title: string
  onClose: () => void
  children: ReactNode
  wide?: boolean
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  useEffect(() => {
    const dialog = ref.current
    dialog?.showModal()
    return () => dialog?.close()
  }, [])
  return (
    <dialog
      ref={ref}
      className={`modal${wide ? ' modal-wide' : ''}`}
      aria-labelledby={titleId}
      onCancel={(e) => {
        e.preventDefault()
        onClose()
      }}
    >
      <header className="modal-head">
        <h2 id={titleId}>{title}</h2>
        <button className="btn-icon" onClick={onClose} aria-label="Cerrar">
          ×
        </button>
      </header>
      <div className="modal-body">{children}</div>
    </dialog>
  )
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  // La ayuda va fuera de la etiqueta: el nombre accesible del campo es solo `label`
  return (
    <div className="field">
      <label className="field-inner">
        <span className="field-label">{label}</span>
        {children}
      </label>
      {hint && <span className="field-hint">{hint}</span>}
    </div>
  )
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      {hint && <span className="stat-hint">{hint}</span>}
    </div>
  )
}

// ---------------------------------------------------------------- Avisos (toasts)
type Toast = { id: number; text: string; tone: Tone }
const ToastContext = createContext<(text: string, tone?: Tone) => void>(() => undefined)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const push = useCallback((text: string, tone: Tone = 'success') => {
    const id = Date.now() + Math.random()
    setToasts((t) => [...t, { id, text, tone }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 5000)
  }, [])
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.tone}`}>
            {t.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast() {
  return useContext(ToastContext)
}

/** Valor que solo cambia cuando se deja de escribir durante ``ms`` milisegundos. */
export function useDebounced<T>(value: T, ms = 300): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return debounced
}

/** Un secreto que el gateway enseña una sola vez (clave, secreto de firma). */
export function OneTimeSecret({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="secret" role="group" aria-label={label}>
      <p className="notice notice-warning">Cópialo ahora: no se volverá a mostrar.</p>
      <code className="secret-value">{value}</code>
      <button
        className="btn btn-small"
        onClick={() =>
          navigator.clipboard?.writeText(value).then(
            () => setCopied(true),
            () => setCopied(false),
          )
        }
      >
        {copied ? 'Copiado' : 'Copiar'}
      </button>
    </div>
  )
}

/** Botón que pide confirmación antes de una acción que no se deshace. */
export function ConfirmButton({
  children,
  question,
  onConfirm,
  className = 'btn btn-small btn-danger',
  disabled,
}: {
  children: ReactNode
  question: string
  onConfirm: () => void
  className?: string
  disabled?: boolean
}) {
  const [asking, setAsking] = useState(false)
  if (!asking)
    return (
      <button className={className} onClick={() => setAsking(true)} disabled={disabled}>
        {children}
      </button>
    )
  return (
    <span className="confirm" role="group" aria-label={question}>
      <span>{question}</span>
      <button
        className={className}
        onClick={() => {
          setAsking(false)
          onConfirm()
        }}
      >
        Sí
      </button>
      <button className="btn btn-small btn-ghost" onClick={() => setAsking(false)}>
        No
      </button>
    </span>
  )
}
