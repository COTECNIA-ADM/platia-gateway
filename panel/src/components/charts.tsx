// Gráficos sencillos en SVG, sin librerías. Copiados del frontal de PLATIA
// (cotecnia-adm/platia-dev, frontend/src/components/charts.tsx): barras finas, un solo eje,
// tooltip con ratón o teclado y siempre una vista de tabla con los mismos datos.
import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'

export type Series = { key: string; label: string; color: string }

type Tip = { x: number; y: number; title: string; rows: { label?: string; value: string; color?: string }[] }

function useTooltip() {
  const ref = useRef<HTMLDivElement>(null)
  const [tip, setTip] = useState<Tip | null>(null)
  function show(e: { clientX: number; clientY: number } | null, target: Element, data: Omit<Tip, 'x' | 'y'>) {
    const box = ref.current?.getBoundingClientRect()
    if (!box) return
    const rect = target.getBoundingClientRect()
    const x = e ? e.clientX - box.left : rect.left + rect.width / 2 - box.left
    const y = e ? e.clientY - box.top : rect.top - box.top
    setTip({ ...data, x, y })
  }
  const node = tip && (
    <div
      className="chart-tip"
      role="status"
      style={{ left: Math.min(tip.x + 12, (ref.current?.clientWidth ?? 400) - 200), top: Math.max(tip.y - 12, 0) }}
    >
      <div className="chart-tip-title">{tip.title}</div>
      {tip.rows.map((r, i) => (
        <div key={i} className="chart-tip-row">
          {r.color && <span className="chart-key-line" style={{ background: r.color }} />}
          <strong>{r.value}</strong>
          {r.label && <span className="muted">{r.label}</span>}
        </div>
      ))}
    </div>
  )
  return { ref, show, hide: () => setTip(null), node }
}

/** Ancho real del contenedor, para dibujar el SVG a escala 1:1 (el texto no encoge). */
function useWidth(ref: RefObject<HTMLElement | null>, fallback = 600) {
  const [width, setWidth] = useState(fallback)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const update = () => setWidth(Math.max(240, Math.floor(el.clientWidth)))
    update()
    const observer = new ResizeObserver(update)
    observer.observe(el)
    return () => observer.disconnect()
  }, [ref])
  return width
}

/** Marco de un gráfico: título, leyenda, botón de tabla y el propio gráfico. */
export function ChartCard({
  title,
  subtitle,
  legend,
  table,
  children,
  actions,
}: {
  title: string
  subtitle?: ReactNode
  legend?: Series[]
  table: ReactNode
  children: ReactNode
  actions?: ReactNode
}) {
  const [asTable, setAsTable] = useState(false)
  return (
    <section className="card chart-card">
      <header className="card-head">
        <div>
          <h2>{title}</h2>
          {subtitle && <div className="muted chart-subtitle">{subtitle}</div>}
        </div>
        <div className="btn-row">
          {actions}
          <button className="btn btn-small btn-ghost" onClick={() => setAsTable((t) => !t)} aria-pressed={asTable}>
            {asTable ? 'Ver gráfico' : 'Ver tabla'}
          </button>
        </div>
      </header>
      {legend && legend.length > 1 && !asTable && (
        <ul className="chart-legend">
          {legend.map((s) => (
            <li key={s.key}>
              <span className="chart-swatch" style={{ background: s.color }} />
              {s.label}
            </li>
          ))}
        </ul>
      )}
      {asTable ? <div className="table-wrap chart-table">{table}</div> : children}
    </section>
  )
}

function niceMax(value: number) {
  if (value <= 0) return 1
  const exp = 10 ** Math.floor(Math.log10(value))
  const f = value / exp
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * exp
}

export function compact(n: number) {
  return new Intl.NumberFormat('es-ES', { notation: 'compact', maximumFractionDigits: 1 }).format(n)
}

/** Columnas agrupadas por categoría (p. ej. meses), una o varias series. */
export function ColumnChart({
  categories,
  series,
  values,
  format = compact,
  height = 220,
}: {
  categories: { key: string; label: string }[]
  series: Series[]
  values: Record<string, Record<string, number>>
  format?: (n: number) => string
  height?: number
}) {
  const tip = useTooltip()
  const width = useWidth(tip.ref)
  const max = niceMax(Math.max(0, ...categories.flatMap((c) => series.map((s) => values[c.key]?.[s.key] ?? 0))))
  const padL = 52
  const padB = 24
  const padT = 8
  const band = (width - padL) / Math.max(categories.length, 1)
  const barW = Math.max(2, Math.min(16, (band - 6) / series.length - 2))
  const plotH = height - padB - padT
  const ticks = [0, 0.5, 1].map((f) => f * max)
  const y = (v: number) => padT + plotH - (v / max) * plotH
  // Una etiqueta cada tantas columnas como haga falta para que no se pisen (~48 px cada una)
  const labelEvery = Math.max(1, Math.ceil(48 / band))

  return (
    <div className="chart" ref={tip.ref}>
      <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height} role="img" aria-label="Gráfico de columnas" onMouseLeave={tip.hide}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={padL} x2={width} y1={y(t)} y2={y(t)} className={t === 0 ? 'chart-axis' : 'chart-grid'} />
            <text x={padL - 6} y={y(t)} className="chart-tick" textAnchor="end" dominantBaseline="middle">
              {format(t)}
            </text>
          </g>
        ))}
        {categories.map((c, i) => {
          const x0 = padL + i * band + (band - series.length * (barW + 2)) / 2
          const rows = series.map((s) => ({ label: s.label, value: format(values[c.key]?.[s.key] ?? 0), color: s.color }))
          return (
            <g
              key={c.key}
              tabIndex={0}
              className="chart-hit"
              onMouseMove={(e) => tip.show(e, e.currentTarget, { title: c.label, rows })}
              onFocus={(e) => tip.show(null, e.currentTarget, { title: c.label, rows })}
              onBlur={tip.hide}
            >
              <rect x={padL + i * band} y={padT} width={band} height={plotH} fill="transparent" />
              {series.map((s, j) => {
                const v = values[c.key]?.[s.key] ?? 0
                const h = Math.max(0, (v / max) * plotH)
                return h > 0 ? (
                  <path key={s.key} d={roundedTop(x0 + j * (barW + 2), y(v), barW, h)} fill={s.color} />
                ) : null
              })}
              {i % labelEvery === 0 && (
                <text x={padL + i * band + band / 2} y={height - 6} className="chart-tick" textAnchor="middle">
                  {c.label}
                </text>
              )}
            </g>
          )
        })}
      </svg>
      {tip.node}
    </div>
  )
}

/** Rectángulo con las esquinas de arriba redondeadas (4px) y la base recta. */
function roundedTop(x: number, y: number, w: number, h: number) {
  const r = Math.min(4, w / 2, h)
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`
}

/** Rectángulo horizontal con el extremo derecho redondeado. */
function roundedEnd(x: number, y: number, w: number, h: number) {
  const r = Math.min(4, h / 2, w)
  return `M${x},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h - r}Q${x + w},${y + h} ${x + w - r},${y + h}H${x}Z`
}

/** Barras horizontales de una sola serie, con el valor al final de cada barra. */
export function BarList({
  items,
  color = 'var(--series-1)',
  format = compact,
  detail,
}: {
  items: { key: string; label: string; value: number }[]
  color?: string
  format?: (n: number) => string
  detail?: (key: string) => { label: string; value: string }[]
}) {
  const tip = useTooltip()
  const max = Math.max(1, ...items.map((i) => i.value))
  return (
    <div className="chart" ref={tip.ref}>
      <ul className="barlist">
        {items.map((item) => {
          const rows = [{ value: format(item.value) }, ...(detail?.(item.key) ?? [])]
          return (
            <li
              key={item.key}
              tabIndex={0}
              className="chart-hit"
              onMouseMove={(e) => tip.show(e, e.currentTarget, { title: item.label, rows })}
              onMouseLeave={tip.hide}
              onFocus={(e) => tip.show(null, e.currentTarget, { title: item.label, rows })}
              onBlur={tip.hide}
            >
              <span className="barlist-label">{item.label}</span>
              <span className="barlist-track">
                <svg viewBox="0 0 100 12" preserveAspectRatio="none" width="100%" height="12" aria-hidden>
                  {item.value > 0 && <path d={roundedEnd(0, 0, Math.max(1, (item.value / max) * 100), 12)} fill={color} />}
                </svg>
              </span>
              <span className="barlist-value">{format(item.value)}</span>
            </li>
          )
        })}
      </ul>
      {tip.node}
    </div>
  )
}

/** Mapa de calor: filas x columnas, intensidad de un solo tono según el valor.
 *  Con ``max`` la escala es fija (p. ej. puntuaciones 0-100); con ``onSelect`` las
 *  celdas con dato se pueden elegir con el ratón o con Intro. */
export function Heatmap({
  rows,
  cols,
  cell,
  format = compact,
  max: fixedMax,
  onSelect,
  selected,
}: {
  rows: { key: string; label: string }[]
  cols: { key: string; label: string }[]
  cell: (row: string, col: string) => { value: number; detail?: string } | undefined
  format?: (n: number) => string
  max?: number
  onSelect?: (row: string, col: string) => void
  selected?: { row: string; col: string } | null
}) {
  const tip = useTooltip()
  const max = fixedMax ?? Math.max(1, ...rows.flatMap((r) => cols.map((c) => cell(r.key, c.key)?.value ?? 0)))
  const steps = ['var(--seq-1)', 'var(--seq-2)', 'var(--seq-3)', 'var(--seq-4)', 'var(--seq-5)']
  return (
    <div className="chart heatmap-wrap" ref={tip.ref}>
      <table className="heatmap">
        <thead>
          <tr>
            <th />
            {cols.map((c) => (
              <th key={c.key} scope="col">
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}>
              <th scope="row">{r.label}</th>
              {cols.map((c) => {
                const data = cell(r.key, c.key)
                const v = data?.value ?? 0
                const isSelected = selected?.row === r.key && selected?.col === c.key
                const selectable = !!onSelect && !!data
                const step = v > 0 ? Math.min(steps.length - 1, Math.floor((v / max) * steps.length - 1e-9)) : -1
                const tipData = {
                  title: `${r.label} · ${c.label}`,
                  rows: [{ value: format(v) }, ...(data?.detail ? [{ value: data.detail }] : [])],
                }
                return (
                  <td
                    key={c.key}
                    tabIndex={v > 0 || selectable ? 0 : -1}
                    className={`heat-cell${step >= 3 ? ' heat-dark' : ''}${selectable ? ' heat-selectable' : ''}${isSelected ? ' heat-selected' : ''}`}
                    aria-selected={onSelect ? isSelected : undefined}
                    onClick={selectable ? () => onSelect(r.key, c.key) : undefined}
                    onKeyDown={
                      selectable
                        ? (e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault()
                              onSelect(r.key, c.key)
                            }
                          }
                        : undefined
                    }
                    style={{ background: step >= 0 ? steps[step] : undefined }}
                    onMouseMove={(e) => tip.show(e, e.currentTarget, tipData)}
                    onMouseLeave={tip.hide}
                    onFocus={(e) => tip.show(null, e.currentTarget, tipData)}
                    onBlur={tip.hide}
                  >
                    {v > 0 || selectable ? format(v) : ''}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {tip.node}
    </div>
  )
}

/** Medidor de consumo: la barra cambia de tono al acercarse al límite. */
export function Meter({ value, max, warnAt = 0.8 }: { value: number; max: number | null; warnAt?: number }) {
  if (!max) return <span className="muted">Sin límite</span>
  const pct = Math.min(1, value / max)
  const tone = pct >= 1 ? 'danger' : pct >= warnAt ? 'warning' : 'ok'
  return (
    <span className={`meter meter-${tone}`} role="meter" aria-valuenow={Math.round(pct * 100)} aria-valuemin={0} aria-valuemax={100}>
      <span className="meter-fill" style={{ width: `${pct * 100}%` }} />
    </span>
  )
}
