import React, { useState, useEffect, useMemo, useRef, useCallback } from 'react'
import PropTypes from 'prop-types'
import { api } from '../api.js'
import { Icon } from '../tos.jsx'
import { tenantType } from '../propTypes.js'

// SQL editor in the spirit of a desktop SQL client. It is NOT a side door: every statement
// runs server-side as the tenant role picked here, through the same governed path the chat
// agent uses (read-only, allowlists, role tables, PII, row cap, BigQuery cost guard), and
// each one is written to the audit log under the signed-in console user.

const LAST_TENANT = 'sqlConsole.tenant'
const draftKey = (tid) => `sqlConsole.draft.${tid}`
const store = {
  get: (k) => { try { return localStorage.getItem(k) || '' } catch { return '' } },
  set: (k, v) => { try { localStorage.setItem(k, v) } catch { /* private window */ } },
}

const inputStyle = {
  height: 34, padding: '0 10px', border: '1px solid var(--border)', borderRadius: 8,
  background: 'var(--surface)', color: 'var(--fg)', fontSize: 13, minWidth: 0,
}
const btn = (primary, disabled) => ({
  height: 34, padding: '0 14px', borderRadius: 8, fontSize: 13, fontWeight: 600,
  display: 'flex', alignItems: 'center', gap: 6, cursor: disabled ? 'not-allowed' : 'pointer',
  opacity: disabled ? 0.6 : 1,
  border: primary ? '1px solid var(--accent)' : '1px solid var(--border)',
  background: primary ? 'var(--accent)' : 'var(--surface)',
  color: primary ? '#fff' : 'var(--fg-muted)',
})

const fmtBytes = (n) => {
  if (n == null) return ''
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = Number(n); let i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1 }
  return `${v >= 10 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`
}

const csvCell = (v) => {
  if (v == null) return ''
  const s = typeof v === 'object' ? JSON.stringify(v) : String(v)
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

function downloadCsv(result) {
  const lines = [result.columns.map(csvCell).join(',')]
  for (const row of result.rows) lines.push(row.map(csvCell).join(','))
  const blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `query-${result.index + 1}.csv`
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

const cellText = (v) => (v != null && typeof v === 'object' ? JSON.stringify(v) : String(v))

// state updater: keep the picked data source while it is still listed, else the first one
const keepDatasource = (sources) => (current) => {
  if (sources.some(d => d.id === current)) return current
  return sources[0]?.id || ''
}

const tabLabel = (res) => {
  if (!res.ok) return res.code
  return `${res.row_count.toLocaleString()} row${res.row_count === 1 ? '' : 's'}`
}

function Field({ label, children }) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 }}>
      <span style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', color: 'var(--fg-subtle)' }}>{label}</span>
      {children}
    </label>
  )
}

function Navigator({ tables, onInsert }) {
  const [filter, setFilter] = useState('')
  const [open, setOpen] = useState({})
  const q = filter.trim().toLowerCase()
  const shown = q
    ? tables.filter(t => t.name.toLowerCase().includes(q) || t.columns.some(c => c.name.toLowerCase().includes(q)))
    : tables
  return (
    <aside style={{ width: 280, flexShrink: 0, borderRight: '1px solid var(--border)', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <div style={{ padding: 10, borderBottom: '1px solid var(--border)' }}>
        <input value={filter} onChange={e => setFilter(e.target.value)} placeholder={`Filter ${tables.length} tables…`}
          style={{ ...inputStyle, width: '100%' }} />
      </div>
      <div style={{ overflowY: 'auto', flex: 1, padding: '6px 4px' }}>
        {shown.length === 0 && <div style={{ padding: 14, fontSize: 12.5, color: 'var(--fg-subtle)' }}>No tables.</div>}
        {shown.map(t => {
          const isOpen = !!open[t.name] || (q && !t.name.toLowerCase().includes(q))
          return (
            <div key={t.name}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 2, padding: '2px 4px', borderRadius: 6, opacity: t.role_denied ? 0.55 : 1 }}>
                <button type="button" aria-label={isOpen ? 'Hide columns' : 'Show columns'}
                  onClick={() => setOpen(o => ({ ...o, [t.name]: !o[t.name] }))}
                  style={{ border: 0, background: 'transparent', color: 'var(--fg-subtle)', cursor: 'pointer', padding: 0, display: 'grid', placeItems: 'center' }}>
                  <Icon name={isOpen ? 'expand_more' : 'chevron_right'} size={16} />
                </button>
                <Icon name="table" size={15} color="var(--fg-subtle)" />
                <button type="button" onClick={() => onInsert(t.name)} title="Insert the table name"
                  className="mono" style={{ border: 0, background: 'transparent', color: 'var(--fg)', cursor: 'pointer', fontSize: 12, textAlign: 'left', flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', padding: '3px 2px' }}>
                  {t.name}
                </button>
                {t.role_denied && <span title="This role is not authorized to read this table" style={{ fontSize: 9.5, fontWeight: 700, color: 'var(--danger)' }}>DENIED</span>}
              </div>
              {isOpen && (
                <div style={{ padding: '0 0 4px 34px' }}>
                  {t.columns.length === 0 && <div style={{ fontSize: 11, color: 'var(--fg-subtle)' }}>no columns scanned</div>}
                  {t.columns.map(c => (
                    <button key={c.name} type="button" onClick={() => onInsert(c.name)}
                      title={c.blocked ? 'This role cannot read this column' : 'Insert the column name'}
                      style={{ display: 'flex', width: '100%', alignItems: 'center', gap: 6, border: 0, background: 'transparent', cursor: 'pointer', padding: '2px 0', textAlign: 'left' }}>
                      <span className="mono" style={{ fontSize: 11.5, color: c.blocked ? 'var(--fg-subtle)' : 'var(--fg-muted)', textDecoration: c.blocked ? 'line-through' : 'none', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.name}</span>
                      {c.type && <span className="mono" style={{ fontSize: 10, color: 'var(--fg-subtle)' }}>{c.type.toLowerCase()}</span>}
                      {c.pii && <span style={{ fontSize: 9.5, fontWeight: 700, color: 'var(--warn)' }}>PII</span>}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </aside>
  )
}

// A result set never reorders, so its position is its identity: columns may repeat a name
// ("gmv" twice from a join) and rows carry no key of their own.
const keyedColumns = (columns) => columns.map((name, pos) => ({ id: `${pos}:${name}`, name, pos }))
const numberedRows = (rows) => rows.map((cells, pos) => ({ n: pos + 1, cells }))

function Cell({ value }) {
  if (value == null) {
    return (
      <td className="mono" title="NULL" style={{ ...tdStyle, textAlign: 'left' }}>
        <span style={{ color: 'var(--fg-subtle)', fontStyle: 'italic' }}>NULL</span>
      </td>
    )
  }
  const text = cellText(value)
  return (
    <td className="mono" title={text} style={{ ...tdStyle, textAlign: typeof value === 'number' ? 'right' : 'left' }}>
      {text}
    </td>
  )
}

function ResultView({ result }) {
  const [showSql, setShowSql] = useState(false)
  const cols = useMemo(() => keyedColumns(result.columns || []), [result.columns])
  const rows = useMemo(() => numberedRows(result.rows || []), [result.rows])
  if (!result.ok) {
    return (
      <div style={{ padding: 16 }}>
        <div style={{ border: '1px solid color-mix(in srgb,var(--danger) 35%,transparent)', background: 'color-mix(in srgb,var(--danger) 8%,transparent)', borderRadius: 10, padding: '12px 14px' }}>
          <div className="mono" style={{ fontSize: 12, fontWeight: 700, color: 'var(--danger)' }}>{result.code}</div>
          <div style={{ fontSize: 13, color: 'var(--fg)', marginTop: 4, whiteSpace: 'pre-wrap' }}>{result.error || 'The statement was refused.'}</div>
        </div>
        <pre className="mono" style={{ marginTop: 10, fontSize: 11.5, color: 'var(--fg-muted)', whiteSpace: 'pre-wrap' }}>{result.sql}</pre>
      </div>
    )
  }
  const meta = [
    `${result.row_count.toLocaleString()} row${result.row_count === 1 ? '' : 's'}`,
    result.truncated ? `first ${result.rows.length.toLocaleString()} shown` : null,
    `${result.elapsed_ms.toLocaleString()} ms`,
    result.estimated_bytes != null ? `${fmtBytes(result.estimated_bytes)} scanned (estimate)` : null,
  ].filter(Boolean)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0, flex: 1 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px', borderBottom: '1px solid var(--border)', flexWrap: 'wrap' }}>
        <span style={{ fontSize: 12, color: 'var(--fg-muted)' }}>{meta.join(' · ')}</span>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={() => setShowSql(s => !s)} style={{ ...btn(false, false), height: 28, fontSize: 12 }}>
          <Icon name="policy" size={15} />{showSql ? 'Hide governed SQL' : 'Governed SQL'}
        </button>
        <button type="button" onClick={() => downloadCsv(result)} disabled={!result.columns.length}
          style={{ ...btn(false, !result.columns.length), height: 28, fontSize: 12 }}>
          <Icon name="download" size={15} />CSV
        </button>
      </div>
      {showSql && (
        <pre className="mono" style={{ margin: 0, padding: '10px 12px', fontSize: 11.5, color: 'var(--fg-muted)', background: 'var(--surface-2)', borderBottom: '1px solid var(--border)', whiteSpace: 'pre-wrap', maxHeight: 160, overflowY: 'auto' }}>
          {result.safe_sql || result.sql}
        </pre>
      )}
      <div style={{ overflow: 'auto', flex: 1, minHeight: 0 }}>
        {result.columns.length === 0
          ? <div style={{ padding: 16, fontSize: 13, color: 'var(--fg-subtle)' }}>The statement returned no columns.</div>
          : (
            <table style={{ borderCollapse: 'separate', borderSpacing: 0, fontSize: 12, minWidth: '100%' }}>
              <thead>
                <tr>
                  <th style={{ ...thStyle, width: 44, textAlign: 'right', color: 'var(--fg-subtle)' }}>#</th>
                  {cols.map(c => <th key={c.id} className="mono" style={thStyle}>{c.name}</th>)}
                </tr>
              </thead>
              <tbody>
                {rows.map(({ n, cells }) => (
                  <tr key={n}>
                    <td style={{ ...tdStyle, textAlign: 'right', color: 'var(--fg-subtle)' }}>{n}</td>
                    {cols.map(c => <Cell key={c.id} value={cells[c.pos]} />)}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        {result.row_count === 0 && result.columns.length > 0 && (
          <div style={{ padding: 16, fontSize: 13, color: 'var(--fg-subtle)' }}>No rows.</div>
        )}
      </div>
    </div>
  )
}

const thStyle = {
  position: 'sticky', top: 0, zIndex: 1, background: 'var(--surface-2)', borderBottom: '1px solid var(--border)',
  borderRight: '1px solid var(--border)', padding: '6px 10px', textAlign: 'left', fontWeight: 600, whiteSpace: 'nowrap', color: 'var(--fg)',
}
const tdStyle = {
  borderBottom: '1px solid var(--border)', borderRight: '1px solid var(--border)', padding: '4px 10px',
  whiteSpace: 'nowrap', maxWidth: 360, overflow: 'hidden', textOverflow: 'ellipsis', color: 'var(--fg)',
}

export default function SqlConsoleView({ adminKey, tenants, toast }) {
  const [tid, setTid] = useState(() => store.get(LAST_TENANT))
  const [role, setRole] = useState('')
  const [business, setBusiness] = useState('')
  const [dsId, setDsId] = useState('')
  const [catalog, setCatalog] = useState(null)
  const [loading, setLoading] = useState(false)
  const [sql, setSql] = useState('')
  const [running, setRunning] = useState(false)
  const [run, setRun] = useState(null)
  const [active, setActive] = useState(0)
  const editor = useRef(null)

  const tenantId = tenants.some(t => t.id === tid) ? tid : (tenants[0]?.id || '')

  useEffect(() => {
    if (!tenantId) return
    store.set(LAST_TENANT, tenantId)
    setSql(store.get(draftKey(tenantId)))
    setRole(''); setBusiness(''); setDsId(''); setRun(null)
  }, [tenantId])

  useEffect(() => {
    if (!tenantId) return undefined
    let cancelled = false
    setLoading(true)
    const qs = new URLSearchParams()
    if (role) qs.set('role', role)
    if (business) qs.set('business', business)
    api.get(`/admin/tenants/${tenantId}/sql/catalog?${qs}`, adminKey).then(r => {
      if (cancelled) return
      setLoading(false)
      if (!r.ok) {
        toast.bad(r.error?.message || 'Could not load the catalog')
        if (business) setBusiness('')
        return
      }
      setCatalog(r.data)
      if (!role && r.data.role) setRole(r.data.role)
      setDsId(keepDatasource(r.data.datasources))
    })
    return () => { cancelled = true }
  }, [tenantId, role, business, adminKey, toast])

  const ds = useMemo(() => catalog?.datasources.find(d => d.id === dsId) || null, [catalog, dsId])
  const roleInfo = catalog?.roles.find(r => r.name === role)

  const changeSql = (value) => {
    setSql(value)
    if (tenantId) store.set(draftKey(tenantId), value)
  }

  const insert = useCallback((text) => {
    const el = editor.current
    if (!el) return
    const start = el.selectionStart ?? sql.length
    const end = el.selectionEnd ?? sql.length
    const next = sql.slice(0, start) + text + sql.slice(end)
    changeSql(next)
    requestAnimationFrame(() => {
      el.focus()
      el.selectionStart = el.selectionEnd = start + text.length
    })
  }, [sql, tenantId]) // eslint-disable-line react-hooks/exhaustive-deps

  const execute = async () => {
    const el = editor.current
    const selected = el && el.selectionEnd > el.selectionStart ? sql.slice(el.selectionStart, el.selectionEnd) : ''
    const script = selected.trim() ? selected : sql
    if (!script.trim() || !ds || running) return
    setRunning(true)
    const r = await api.post(`/admin/tenants/${tenantId}/sql`, adminKey, {
      sql: script, datasource_id: ds.id, role: role || null, business: business || null,
    })
    setRunning(false)
    if (!r.ok) { toast.bad(r.error?.message || 'The script could not run'); return }
    setRun(r.data)
    const firstError = r.data.results.findIndex(x => !x.ok)
    setActive(firstError >= 0 ? firstError : r.data.results.length - 1)
  }

  const onKeyDown = (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault()
      execute()
    } else if (e.key === 'Tab' && !e.shiftKey) {
      e.preventDefault()
      insert('  ')
    }
  }

  const results = run?.results || []
  const current = results[active]

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }} className="fade">
      <header style={{ padding: '22px 28px 14px', display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, letterSpacing: '-.02em' }}>SQL Query</h1>
          <p style={{ color: 'var(--fg-muted)', marginTop: 4, fontSize: 13.5 }}>
            Run read-only SQL as a tenant role. Every statement goes through governance (allowed tables, role, PII, row cap, cost guard) and is written to the audit log.
          </p>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(150px, 1fr))', gap: 12, maxWidth: 1100 }}>
          <Field label="Tenant">
            <select value={tenantId} onChange={e => setTid(e.target.value)} style={inputStyle}>
              {tenants.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
          </Field>
          <Field label="Run as role">
            <select value={role} onChange={e => { setRole(e.target.value); setBusiness('') }} style={inputStyle} disabled={!catalog?.roles.length}>
              {(catalog?.roles || []).map(r => (
                <option key={r.name} value={r.name}>{r.name}{r.full_access ? ' (full access)' : ''}{r.can_see_pii ? ' · sees PII' : ''}</option>
              ))}
            </select>
          </Field>
          <Field label="Business">
            <select value={business} onChange={e => setBusiness(e.target.value)} style={inputStyle}>
              <option value="">None (connection allowlist)</option>
              {(catalog?.businesses || []).map(b => <option key={b.id} value={b.id}>{b.name}</option>)}
            </select>
          </Field>
          <Field label="Data source">
            <select value={dsId} onChange={e => setDsId(e.target.value)} style={inputStyle} disabled={!catalog?.datasources.length}>
              {(catalog?.datasources || []).map(d => <option key={d.id} value={d.id}>{d.name} · {d.type}</option>)}
            </select>
          </Field>
        </div>
      </header>

      {tenants.length === 0 && <Empty text="No tenants yet." />}
      {tenantId && catalog && catalog.roles.length === 0 && <Empty text="This tenant has no roles yet. Add one in the tenant's Role tab first." />}
      {tenantId && catalog && catalog.roles.length > 0 && catalog.datasources.length === 0 && !loading && (
        <Empty text="No SQL data source is reachable as this role." />
      )}

      {ds && (
        <div style={{ flex: 1, minHeight: 0, margin: '0 28px 24px', border: '1px solid var(--border)', borderRadius: 12, background: 'var(--surface)', display: 'flex', overflow: 'hidden' }}>
          <Navigator key={`${ds.id}:${role}:${business}`} tables={ds.tables} onInsert={insert} />
          <section style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 10px', borderBottom: '1px solid var(--border)' }}>
              <button type="button" onClick={execute} disabled={running || !sql.trim()} style={btn(true, running || !sql.trim())}>
                <Icon name={running ? 'hourglass_top' : 'play_arrow'} size={17} />{running ? 'Running…' : 'Run'}
              </button>
              <button type="button" onClick={() => changeSql('')} disabled={!sql} style={btn(false, !sql)}>
                <Icon name="delete_sweep" size={16} />Clear
              </button>
              <span style={{ fontSize: 11.5, color: 'var(--fg-subtle)' }}>
                Ctrl+Enter runs the script, or only the selected text. Statements are split at <span className="mono">;</span>
              </span>
              <span style={{ flex: 1 }} />
              <span className="mono" style={{ fontSize: 11, color: 'var(--fg-subtle)' }}>
                {ds.dialect}{roleInfo && !roleInfo.can_see_pii ? ' · PII hidden' : ''}{loading ? ' · loading…' : ''}
              </span>
            </div>
            <textarea ref={editor} value={sql} onChange={e => changeSql(e.target.value)} onKeyDown={onKeyDown}
              spellCheck={false} className="mono" aria-label="SQL editor"
              placeholder={`SELECT *\nFROM ${ds.tables.find(t => !t.role_denied)?.name || 'table_name'}\nLIMIT 100`}
              style={{ height: 210, minHeight: 90, resize: 'vertical', border: 0, borderBottom: '1px solid var(--border)', outline: 'none', padding: '12px 14px', fontSize: 13, lineHeight: 1.55, background: 'var(--bg)', color: 'var(--fg)', tabSize: 2 }} />
            <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
              {results.length > 0 && (
                <div role="tablist" style={{ display: 'flex', gap: 2, padding: '6px 8px 0', borderBottom: '1px solid var(--border)', overflowX: 'auto' }}>
                  {results.map((res, i) => (
                    <button key={res.index} type="button" role="tab" aria-selected={i === active} onClick={() => setActive(i)}
                      title={res.sql}
                      style={{ border: '1px solid var(--border)', borderBottom: i === active ? '1px solid var(--surface)' : '1px solid var(--border)', marginBottom: -1, borderRadius: '8px 8px 0 0', padding: '5px 12px', fontSize: 12, fontWeight: 600, cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 6, whiteSpace: 'nowrap', background: i === active ? 'var(--surface)' : 'var(--surface-2)', color: 'var(--fg)' }}>
                      <Icon name={res.ok ? 'check_circle' : 'error'} size={15} color={res.ok ? 'var(--success)' : 'var(--danger)'} />
                      Result {i + 1}
                      <span style={{ fontWeight: 500, color: 'var(--fg-subtle)' }}>{tabLabel(res)}</span>
                    </button>
                  ))}
                </div>
              )}
              {current
                ? <ResultView key={`${run.trace_id}:${active}`} result={current} />
                : <div style={{ padding: 18, fontSize: 13, color: 'var(--fg-subtle)' }}>Results appear here. Click a table or column on the left to insert its name.</div>}
            </div>
          </section>
        </div>
      )}
    </div>
  )
}

function Empty({ text }) {
  return (
    <div style={{ margin: '0 28px', color: 'var(--fg-subtle)', padding: '40px 0', textAlign: 'center', border: '1px dashed var(--border)', borderRadius: 14 }}>{text}</div>
  )
}

const resultType = PropTypes.shape({
  index: PropTypes.number.isRequired,
  sql: PropTypes.string,
  ok: PropTypes.bool.isRequired,
  code: PropTypes.string,
  error: PropTypes.string,
  safe_sql: PropTypes.string,
  estimated_bytes: PropTypes.number,
  elapsed_ms: PropTypes.number,
  columns: PropTypes.arrayOf(PropTypes.string),
  rows: PropTypes.array,
  row_count: PropTypes.number,
  truncated: PropTypes.bool,
})

SqlConsoleView.propTypes = {
  adminKey: PropTypes.string,
  tenants: PropTypes.arrayOf(tenantType).isRequired,
  toast: PropTypes.func.isRequired,      // callable with .ok/.bad/.warn (tos.jsx)
}
Field.propTypes = { label: PropTypes.string.isRequired, children: PropTypes.node.isRequired }
Navigator.propTypes = {
  tables: PropTypes.arrayOf(PropTypes.shape({
    name: PropTypes.string.isRequired,
    role_denied: PropTypes.bool,
    columns: PropTypes.arrayOf(PropTypes.shape({
      name: PropTypes.string.isRequired, type: PropTypes.string, pii: PropTypes.bool, blocked: PropTypes.bool,
    })).isRequired,
  })).isRequired,
  onInsert: PropTypes.func.isRequired,
}
ResultView.propTypes = { result: resultType.isRequired }
Cell.propTypes = {
  value: PropTypes.oneOfType([PropTypes.string, PropTypes.number, PropTypes.bool, PropTypes.object, PropTypes.array]),
}
Empty.propTypes = { text: PropTypes.string.isRequired }
