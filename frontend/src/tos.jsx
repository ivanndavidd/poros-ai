import React, { createContext, useContext, useState, useCallback } from 'react'
import PropTypes from 'prop-types'

/* Material Symbols icon */
export const Icon = ({ name, size = 20, color, style }) => (
  <span className="msym" style={{ fontSize: size, color, ...style }}>{name}</span>
)

/* formatters */
export const nfmt = (n) => {
  n = Number(n) || 0
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B'
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M'
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K'
  return String(n)
}
export const money = (n) => '$' + (Number(n) || 0).toLocaleString(undefined, { maximumFractionDigits: 2 })
export const keyboardActivate = (handler) => (event) => {
  if (event.target !== event.currentTarget) return
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    handler(event)
  }
}

/* toast system */
const ToastCtx = createContext(() => {})
export const useToast = () => useContext(ToastCtx)
const withoutToast = id => items => items.filter(item => item.id !== id)
export function ToastHost({ children }) {
  const [items, setItems] = useState([])
  const remove = useCallback(id => setItems(withoutToast(id)), [])
  const push = useCallback((msg, tone = 'ok') => {
    const id = Math.random().toString(36).slice(2)
    setItems(x => [...x, { id, msg, tone }])
    setTimeout(remove, 4200, id)
  }, [remove])
  const toast = useCallback(Object.assign((m, t) => push(m, t), {
    ok: m => push(m, 'ok'), bad: m => push(m, 'bad'), warn: m => push(m, 'warn'),
  }), [push])
  return <ToastCtx.Provider value={toast}>{children}
    <div className="toasts">{items.map(t => (
      <div key={t.id} className={'toast ' + t.tone}>{t.msg}<button type="button" className="x" aria-label="Dismiss notification" onClick={remove.bind(null, t.id)}>×</button></div>))}</div>
  </ToastCtx.Provider>
}

Icon.propTypes = {
  name: PropTypes.string.isRequired,
  size: PropTypes.number,
  color: PropTypes.string,
  style: PropTypes.object,
}
ToastHost.propTypes = { children: PropTypes.node }
