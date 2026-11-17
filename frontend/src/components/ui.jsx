import React from 'react'
import PropTypes from 'prop-types'

/* ---------- primitives ---------- */
export const Card = ({ children, className = '', ...p }) => <div className={'card ' + className} {...p}>{children}</div>
export const CardH = ({ title, sub, right }) => (
  <div className="card-h"><div><h3>{title}</h3>{sub && <div className="card-sub">{sub}</div>}</div>{right}</div>
)
export const Spinner = ({ className = '' }) => <span className={'spin ' + className} />
export const Badge = ({ tone = 'gray', dot, children }) => (
  <span className={'badge ' + tone}>{dot && <span className="dot" />}{children}</span>
)
export const Chip = ({ children, onRemove, onClick, active }) => {
  const className = 'chip' + (onClick ? ' pick' : '') + (active ? ' on' : '')
  if (onClick) {
    return <button type="button" className={className} aria-pressed={active} onClick={onClick}>{children}</button>
  }
  return <span className={className}>
    {children}{onRemove && <button type="button" onClick={onRemove}>×</button>}
  </span>
}

export function Button({ children, loading, variant = '', className = '', ...p }) {
  return <button className={'btn ' + variant + ' ' + className} disabled={loading || p.disabled} {...p}>
    {loading && <Spinner />}{children}
  </button>
}

export function Field({ label, hint, children }) {
  return <div className="field">{label && <label>{label}</label>}{children}{hint && <span className="hint">{hint}</span>}</div>
}
export const Input = (p) => <input className="input" {...p} />
export function Select({ options = [], children, ...p }) {
  return <select className="select" {...p}>{children || options.map(o =>
    typeof o === 'string' ? <option key={o} value={o}>{o}</option> : <option key={o.value} value={o.value}>{o.label}</option>)}</select>
}
export const Check = ({ label, ...p }) => <label className="check"><input type="checkbox" {...p} />{label}</label>

export function Empty({ icon = '∅', title = 'Nothing here yet', children }) {
  return <div className="empty"><div className="ic">{icon}</div><h4>{title}</h4><div>{children}</div></div>
}
export const Alert = ({ tone = 'info', children }) => <div className={'alert ' + tone}>{children}</div>

export function Stat({ k, v, d, icon }) {
  return <div className="stat"><div className="k">{icon && <span>{icon}</span>}{k}</div><div className="v">{v}</div>{d && <div className="d">{d}</div>}</div>
}

export function Loading({ label = 'Loading…' }) {
  return <div className="center-load"><Spinner /><span>{label}</span></div>
}

export function Tabs({ tabs, value, onChange }) {
  return <div className="tabs">{tabs.map(t => (
    <button key={t.value} className={'tab' + (value === t.value ? ' on' : '')} onClick={() => onChange(t.value)}>{t.label}</button>))}</div>
}

export function Modal({ title, onClose, children, wide }) {
  // Escape on document, not on the scrim div: a div only sees keydown while focused.
  React.useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])
  // native <dialog> instead of role="dialog"; the .modal class already supplies the
  // box styling, so the UA defaults it would otherwise add are reset inline.
  return <div className="scrim">
    <dialog open aria-modal="true" aria-label={title} className={'modal' + (wide ? ' lg' : '')}
      style={{ position: 'static', margin: 0, padding: 0, color: 'inherit' }}>
    <div className="card-h"><h3>{title}</h3><button className="btn ghost sm" onClick={onClose}>×</button></div>{children}</dialog></div>
}

/* ---------- tiny SVG charts (no deps) ---------- */
export function BarChart({ data, fmt = v => v, height = 150, color = 'var(--brand)' }) {
  if (!data || !data.length) return <Empty icon="📊" title="No data to chart" />
  const max = Math.max(...data.map(d => d.value), 1)
  return <div className="chart">{data.map((d, i) => (
    <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 10, margin: '7px 0' }}>
      <div style={{ width: 120, fontSize: 12, color: 'var(--ink-2)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{d.label}</div>
      <div className="bar-track" style={{ flex: 1 }}><div className="bar-fill" style={{ width: (d.value / max * 100) + '%', background: color }} /></div>
      <div style={{ width: 90, textAlign: 'right', fontSize: 12, fontWeight: 600 }}>{fmt(d.value)}</div>
    </div>))}</div>
}

export function LineChart({ data, fmt = v => v, height = 160 }) {
  if (!data || !data.length) return <Empty icon="📈" title="No activity yet" />
  const W = 560, H = height, pad = 28
  const max = Math.max(...data.map(d => d.value), 1)
  const n = data.length
  const x = i => pad + (n <= 1 ? (W - 2 * pad) / 2 : i * (W - 2 * pad) / (n - 1))
  const y = v => H - pad - (v / max) * (H - 2 * pad)
  const pts = data.map((d, i) => `${x(i)},${y(d.value)}`).join(' ')
  const area = `${pad},${H - pad} ${pts} ${x(n - 1)},${H - pad}`
  return <svg viewBox={`0 0 ${W} ${H}`} className="chart" style={{ maxHeight: H }}>
    <polygon points={area} fill="var(--brand-bg)" />
    <polyline points={pts} fill="none" stroke="var(--brand)" strokeWidth="2.5" />
    {data.map((d, i) => <g key={i}><circle cx={x(i)} cy={y(d.value)} r="3" fill="var(--brand)" />
      <text x={x(i)} y={H - 9} fontSize="9" textAnchor="middle" fill="var(--muted)">{d.label}</text></g>)}
  </svg>
}

/* Legacy pages import these from ui.jsx; keep one shared toast implementation. */
export { ToastHost, useToast } from '../tos.jsx'

const dataPointType = PropTypes.shape({
  label: PropTypes.node,
  value: PropTypes.number.isRequired,
})

Card.propTypes = { children: PropTypes.node, className: PropTypes.string }
CardH.propTypes = { title: PropTypes.node, sub: PropTypes.node, right: PropTypes.node }
Spinner.propTypes = { className: PropTypes.string }
Badge.propTypes = { tone: PropTypes.string, dot: PropTypes.bool, children: PropTypes.node }
Chip.propTypes = {
  children: PropTypes.node,
  onRemove: PropTypes.func,
  onClick: PropTypes.func,
  active: PropTypes.bool,
}
Button.propTypes = {
  children: PropTypes.node,
  loading: PropTypes.bool,
  variant: PropTypes.string,
  className: PropTypes.string,
}
Field.propTypes = { label: PropTypes.node, hint: PropTypes.node, children: PropTypes.node }
Select.propTypes = {
  options: PropTypes.arrayOf(PropTypes.oneOfType([
    PropTypes.string,
    PropTypes.shape({ value: PropTypes.string, label: PropTypes.node }),
  ])),
  children: PropTypes.node,
}
Check.propTypes = { label: PropTypes.node }
Empty.propTypes = { icon: PropTypes.node, title: PropTypes.node, children: PropTypes.node }
Alert.propTypes = { tone: PropTypes.string, children: PropTypes.node }
Stat.propTypes = { k: PropTypes.node, v: PropTypes.node, d: PropTypes.node, icon: PropTypes.node }
Loading.propTypes = { label: PropTypes.node }
Tabs.propTypes = {
  tabs: PropTypes.arrayOf(PropTypes.shape({
    value: PropTypes.string.isRequired,
    label: PropTypes.node.isRequired,
  })).isRequired,
  value: PropTypes.string,
  onChange: PropTypes.func.isRequired,
}
Modal.propTypes = {
  title: PropTypes.string.isRequired,
  onClose: PropTypes.func.isRequired,
  children: PropTypes.node,
  wide: PropTypes.bool,
}
BarChart.propTypes = {
  data: PropTypes.arrayOf(dataPointType),
  fmt: PropTypes.func,
  height: PropTypes.number,
  color: PropTypes.string,
}
LineChart.propTypes = {
  data: PropTypes.arrayOf(dataPointType),
  fmt: PropTypes.func,
  height: PropTypes.number,
}
