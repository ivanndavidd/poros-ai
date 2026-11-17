import React, { useState, useEffect, useCallback, useRef } from 'react'
import PropTypes from 'prop-types'
import { api } from '../api.js'
import { fmtShort, fmtDay } from '../time.js'
import { Icon } from '../tos.jsx'
import { datasourceType, tenantType, toastType } from '../propTypes.js'
import {
  Chip, ConfirmDialog, Loading, Sub, Toggle, alert, bareSel, btnGhost, btnPrimary, cardBox,
  chipStyle, dashed, dashedSm, fieldBox, hintStyle, iconBtn, iconLink, lbl, miniBtn, scrim,
  selBox, xBtn,
} from './kb/ui.jsx'

const bizNoun = n => (n === 1 ? 'business' : 'businesses')
const docCfgLabel = cfg => {
  if (!cfg?.chunk_size) return ''
  const ocr = cfg.ocr ? ' · OCR' : ''
  return `${cfg.chunk_size} words${ocr} · `
}
const TABS = [
  { id: 'database', icon: 'database', label: 'Database', title: 'Database Connections', desc: 'Data source connections for this tenant. Authorized tables, PII, schema context, and the semantic catalog are managed per business in the Knowledge Base menu.' },
  { id: 'document', icon: 'description', label: 'Document', title: 'Document Configuration', desc: 'Document ingestion, embedding, and vector store settings.' },
  { id: 'memory', icon: 'cognition', label: 'Memory', title: 'Memory Configuration', desc: 'How the agent stores and recalls conversation context.' },
  { id: 'role', icon: 'shield_person', label: 'Role', title: 'Role Configuration', desc: 'Roles, table access, and per-role policies (RLAC/CLAC).' },
  { id: 'model', icon: 'smart_toy', label: 'Model', title: 'Model Configuration', desc: 'Providers, per-purpose models (gatekeeper/answer), and parameters.' },
  { id: 'apikey', icon: 'key', label: 'App Key', title: 'Consumer App Keys', desc: 'Keys issued to this tenant for use in their consumer apps.' },
]

export default function TenantConfigModal({ tenant, adminKey, toast, onClose, onSaved }) {
  const [tab, setTab] = useState('database')
  const meta = TABS.find(t => t.id === tab)
  const st = tenant.status === 'active'
    ? { label: 'Active', color: 'var(--success)', bg: 'var(--success-soft)' }
    : { label: 'Inactive', color: 'var(--fg-subtle)', bg: 'var(--surface-2)' }
  const ctx = { tid: tenant.id, adminKey, toast }
  // Escape on document, not on the scrim div: a div only sees keydown while focused.
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div style={scrim}>
      {/* native <dialog> instead of role="dialog"; position/margin/padding reset the UA
          defaults so it lays out exactly as the plain div did inside the scrim */}
      <dialog open aria-modal="true" aria-label={`${tenant.name} configuration`} className="pop" style={{ position: 'static', margin: 0, padding: 0, width: 'min(900px,100%)', height: 'min(660px,92vh)', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 18, display: 'flex', flexDirection: 'column', overflow: 'hidden', boxShadow: '0 30px 80px -20px rgba(0,0,0,.5)', color: 'var(--fg)' }}>
        {/* header */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '20px 24px', borderBottom: '1px solid var(--border)' }}>
          <div style={{ width: 42, height: 42, borderRadius: 11, background: 'var(--accent-soft)', color: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><Icon name={tenant.icon || 'hub'} size={23} /></div>
          <div style={{ flex: 1 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{ fontWeight: 700, fontSize: 17, letterSpacing: '-.01em' }}>{tenant.name}</span>
              <span style={{ display: 'flex', alignItems: 'center', gap: 5, padding: '3px 9px', borderRadius: 20, background: st.bg, color: st.color, fontSize: 11, fontWeight: 600 }}><span style={{ width: 6, height: 6, borderRadius: '50%', background: st.color }} />{st.label}</span>
            </div>
            <div className="mono" style={{ fontSize: 12, color: 'var(--fg-muted)', marginTop: 2 }}>Tenant configuration · {tenant.id}</div>
          </div>
          <button onClick={onClose} style={{ width: 34, height: 34, border: '1px solid var(--border)', borderRadius: 9, background: 'var(--surface)', color: 'var(--fg-muted)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><Icon name="close" size={19} /></button>
        </div>

        <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
          {/* tab rail */}
          <div style={{ width: 218, flexShrink: 0, borderRight: '1px solid var(--border)', padding: '14px 12px', overflowY: 'auto', background: 'var(--surface-2)' }}>
            {TABS.map(c => {
              const active = tab === c.id
              return (
                <button key={c.id} onClick={() => setTab(c.id)} style={{ display: 'flex', alignItems: 'center', gap: 11, width: '100%', padding: '10px 12px', border: 'none', borderRadius: 9, fontSize: 13, fontWeight: active ? 600 : 500, textAlign: 'left', background: active ? 'var(--surface)' : 'transparent', color: active ? 'var(--accent)' : 'var(--fg-muted)', boxShadow: active ? '0 1px 3px rgba(0,0,0,.08)' : 'none', marginBottom: 2 }}>
                  <Icon name={c.icon} size={19} /><span style={{ flex: 1 }}>{c.label}</span>
                </button>
              )
            })}
          </div>

          {/* content */}
          <div style={{ flex: 1, minWidth: 0, overflowY: 'auto', padding: '24px 28px' }}>
            <div style={{ marginBottom: 20 }}>
              <div style={{ fontWeight: 700, fontSize: 16 }}>{meta.title}</div>
              <div style={{ fontSize: 13, color: 'var(--fg-muted)', marginTop: 3 }}>{meta.desc}</div>
            </div>
            {tab === 'database' && <DatabaseTab {...ctx} />}
            {tab === 'document' && <FieldsTab {...ctx} section="document" fields={DOC_FIELDS} extra={<DocumentsSection {...ctx} />} />}
            {tab === 'memory' && <FieldsTab {...ctx} section="memory" fields={MEM_FIELDS} extra={<MemoriesSection {...ctx} />} />}
            {tab === 'role' && <RoleTab {...ctx} />}
            {tab === 'model' && <ModelTab {...ctx} />}
            {tab === 'apikey' && <AppKeyTab {...ctx} />}
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, padding: '14px 24px', borderTop: '1px solid var(--border)', background: 'var(--surface-2)' }}>
          <span style={{ fontSize: 12.5, color: 'var(--fg-subtle)', display: 'flex', alignItems: 'center', gap: 6 }}><Icon name="shield" size={16} />Every consumer query is forced through govern() (Wren + RLAC/CLAC).</span>
          <button onClick={onClose} style={btnGhost}>Close</button>
        </div>
      </dialog>
    </div>
  )
}

/* ---------- shared field schemas ---------- */
const MEM_FIELDS = [
  { key: 'memory_type', label: 'Conversation memory (chat history)', type: 'select', options: ['Buffer + summary', 'Buffer window', 'Summary only', 'None'] },
  { key: 'context_window', label: 'Context window (recent messages)', type: 'number' },
  { key: 'summarization_model', label: 'Summarization model (optional)', type: 'text' },
  { key: 'max_memory_tokens', label: 'Max memory tokens', type: 'number' },
  { key: 'auto_memory', label: 'Auto-memory — AI takes its own notes', type: 'toggle' },
  { key: 'persistent', label: 'Long-term memory store', type: 'toggle' },
  { key: 'cross_session_recall', label: 'Recall memory during chat', type: 'toggle' },
  { key: 'ttl_hours', label: 'Auto-memory TTL (hours, 0 = forever)', type: 'number' },
]
const DOC_FIELDS = [
  {
    key: 'embedding_model', label: 'Embedding model (changing it requires a document reindex)', type: 'select',
    options: [
      { value: 'paraphrase-multilingual-MiniLM-L12-v2', label: 'Multilingual MiniLM — light & fast (default)' },
      { value: 'intfloat/multilingual-e5-base', label: 'multilingual-e5-base — more accurate, mid-size (~1.1 GB)' },
      { value: 'BAAI/bge-m3', label: 'bge-m3 — highest quality, heavy (~2.2 GB)' },
    ],
  },
  { key: 'max_documents', label: 'Max documents (active document cap; 0 = unlimited)', type: 'number' },
  { key: 'auto_reindex', label: 'Auto re-index — re-embed automatically on upload when the embedding model changed', type: 'toggle' },
  // chunk size / chunk overlap / OCR dipindah ke PER-DOKUMEN (diatur saat upload & bisa diedit tiap dokumen)
]

/* ---------- generic config-fields tab (Document, Memory) ---------- */
function FieldsTab({ tid, adminKey, toast, section, fields, extra }) {
  const [data, setData] = useState(null)
  const load = useCallback(async () => { const r = await api.get(`/admin/tenants/${tid}/config/${section}`, adminKey); if (r.ok) setData(r.data) }, [tid, section])
  useEffect(() => { load() }, [])
  if (!data) return <Loading />
  const set = (k, v) => setData(d => ({ ...d, [k]: v }))
  const save = async () => {
    const r = await api.post(`/admin/tenants/${tid}/config/${section}`, adminKey, { data })
    if (r.ok) toast.ok('Configuration saved')
    else toast.bad(r.error.message)
  }
  return (
    <>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px 20px' }}>
        {fields.map(f => (
          <div key={f.key} style={{ gridColumn: f.span2 ? 'span 2' : 'auto', display: 'flex', flexDirection: 'column' }}>
            {/* label grows to fill the (grid-stretched) cell so inputs sit flush at the bottom -> aligned
                across columns even when one label wraps to 2 lines */}
            <label style={{ ...lbl, flex: 1 }}>{f.label}</label>
            {f.type === 'toggle'
              ? <Toggle on={!!data[f.key]} onClick={() => set(f.key, !data[f.key])} />
              : f.type === 'select'
                ? <select value={data[f.key] ?? ''} onChange={e => set(f.key, e.target.value)} style={fieldBox}>{(f.options || []).map(o => { const v = typeof o === 'object' ? o.value : o; const l = typeof o === 'object' ? o.label : o; return <option key={v} value={v}>{l}</option> })}</select>
                : <input type={f.type === 'number' ? 'number' : 'text'} value={data[f.key] ?? ''} onChange={e => set(f.key, f.type === 'number' ? Number(e.target.value) : e.target.value)} style={fieldBox} />}
          </div>
        ))}
      </div>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 16 }}><button onClick={save} style={btnPrimary}>Save configuration</button></div>
      {extra}
    </>
  )
}

/* ---------- Memory: stored per-user memories (manual + auto-captured) ---------- */
function MemoriesSection({ tid, adminKey, toast }) {
  const [mems, setMems] = useState([]); const [uid, setUid] = useState(''); const [text, setText] = useState('')
  const [kind, setKind] = useState('fact'); const [filter, setFilter] = useState('')
  const load = useCallback(async (f = filter) => {
    const qs = f.trim() ? `?user_id=${encodeURIComponent(f.trim())}` : ''
    const r = await api.get(`/admin/tenants/${tid}/memories${qs}`, adminKey); if (r.ok) setMems(r.data)
  }, [tid, filter])
  useEffect(() => { load() }, [])
  const add = async () => {
    if (!uid.trim() || !text.trim()) return toast.warn('user id & text')
    const r = await api.post('/admin/memories', adminKey, { tenant_id: tid, user_id: uid.trim(), kind, text: text.trim() })
    if (r.ok) {
      toast.ok('stored')
      setText('')
      load()
    } else {
      toast.bad(r.error.message)
    }
  }
  const del = async (id) => {
    const r = await api.del(`/admin/tenants/${tid}/memories/${id}`, adminKey)
    if (r.ok) load()
  }
  const forgetUser = async () => {
    const u = filter.trim(); if (!u) return toast.warn('set the user id filter first')
    if (!window.confirm(`Delete ALL memories for user "${u}"?`)) return
    const r = await api.del(`/admin/tenants/${tid}/memories?user_id=${encodeURIComponent(u)}`, adminKey)
    if (r.ok) {
      toast.ok(`${r.data.deleted} memories deleted`)
      load()
    } else {
      toast.bad(r.error.message)
    }
  }
  const applyFilter = e => { const v = e.target.value; setFilter(v); load(v) }
  const badge = (bg, fg, label) => <span style={{ fontSize: 10, fontWeight: 600, padding: '1px 7px', borderRadius: 6, background: bg, color: fg, whiteSpace: 'nowrap' }}>{label}</span>
  return (
    <div style={{ marginTop: 22, borderTop: '1px solid var(--border)', paddingTop: 18 }}>
      <div style={{ fontWeight: 600, fontSize: 13.5, marginBottom: 4 }}>Stored memories (per tenant, user)</div>
      <div style={{ fontSize: 12, color: 'var(--fg-muted)', marginBottom: 12 }}>
        Long-term notes the agent recalls — isolated per user. <b>auto</b> = captured by the AI from conversations; <b>admin</b> = written manually.
      </div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
        <input value={uid} onChange={e => setUid(e.target.value)} placeholder="user id" style={{ ...fieldBox, width: 110 }} />
        <select value={kind} onChange={e => setKind(e.target.value)} style={{ ...fieldBox, width: 110 }}>
          <option value="fact">fact</option><option value="preference">preference</option><option value="context">context</option>
        </select>
        <input value={text} onChange={e => setText(e.target.value)} placeholder="memory text…" style={{ ...fieldBox, flex: 1, minWidth: 150 }} />
        <button onClick={add} style={btnGhost}>Add</button>
      </div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 12, alignItems: 'center' }}>
        <input value={filter} onChange={applyFilter} placeholder="filter user id…" style={{ ...fieldBox, width: 150 }} />
        {filter.trim() && <button onClick={forgetUser} style={{ ...btnGhost, color: 'var(--danger)', borderColor: 'var(--danger)' }}>Forget this user</button>}
        <span style={{ fontSize: 11.5, color: 'var(--fg-subtle)', marginLeft: 'auto' }}>{mems.length} memories</span>
      </div>
      {mems.length === 0 ? <div style={{ fontSize: 12.5, color: 'var(--fg-subtle)' }}>No memories stored yet.</div>
        : mems.slice(0, 30).map(m => (
          <div key={m.id} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 0', borderBottom: '1px solid var(--border)', fontSize: 13 }}>
            <span className="mono" style={{ fontSize: 11, color: 'var(--accent)', minWidth: 70 }}>{m.user_id}</span>
            {badge(m.source === 'auto' ? 'var(--accent-soft)' : 'var(--surface-2)', m.source === 'auto' ? 'var(--accent)' : 'var(--fg-muted)', m.source === 'auto' ? 'auto' : 'admin')}
            {badge('var(--surface-2)', 'var(--fg-subtle)', m.kind)}
            <span style={{ flex: 1 }}>{m.text}</span>
            <button onClick={() => del(m.id)} style={{ border: 'none', background: 'transparent', color: 'var(--fg-subtle)', display: 'flex' }}><Icon name="delete" size={16} /></button>
          </div>
        ))}
      {mems.length > 30 && <div style={{ fontSize: 11.5, color: 'var(--fg-subtle)', marginTop: 8 }}>Showing 30 of {mems.length} — use the user id filter to narrow down.</div>}
    </div>
  )
}

/* ---------- Database tab: datasources + MDL ---------- */
function DatabaseTab({ tid, adminKey, toast }) {
  const [specs, setSpecs] = useState(null); const [list, setList] = useState(null)
  const [type, setType] = useState('duckdb'); const [name, setName] = useState(''); const [conn, setConn] = useState({}); const [test, setTest] = useState(null); const [busy, setBusy] = useState(false); const [adding, setAdding] = useState(false); const [editing, setEditing] = useState(null)
  const loadList = useCallback(async () => { const r = await api.get(`/admin/tenants/${tid}/datasources`, adminKey); if (r.ok) setList(r.data) }, [tid])
  useEffect(() => { (async () => { const s = await api.get('/admin/connector-specs', adminKey); if (s.ok) setSpecs(s.data) })(); loadList() }, [])
  if (!specs || !list) return <Loading />
  const fields = specs[type]?.fields || []
  const buildConn = () => { const o = {}; for (const f of fields) { let v = conn[f.key]; if (!v) continue; if (f.json) { try { v = JSON.parse(v) } catch { throw new Error(f.label + ' must be JSON') } } o[f.key] = v } return o }
  const doTest = async () => {
    setBusy(true); setTest({ pending: true, msg: 'Preparing connection test...' })
    try {
      const started = await api.post('/admin/jobs/datasource-test', adminKey, { tenant_id: tid, type, conn: buildConn() })
      const r = started.ok
        ? await api.waitJob(started.data.status_url, adminKey, ev => {
          if (ev.text || ev.phase) setTest({ pending: true, msg: ev.text || ev.phase })
        })
        : started
      setTest(r.ok ? { ok: 1, ...r.data } : { msg: r.error.message })
    } catch (e) { setTest({ msg: e.message }) }
    setBusy(false)
  }
  const create = async () => {
    if (!name.trim()) return toast.warn('name')
    setBusy(true)
    try {
      const started = await api.post('/admin/jobs/datasources', adminKey, { tenant_id: tid, type, name: name.trim(), conn: buildConn() })
      const r = started.ok
        ? await api.waitJob(started.data.status_url, adminKey, ev => {
          if (ev.text) setTest({ pending: true, msg: ev.text })
        })
        : started
      if (r.ok) {
        toast.ok('Connected & scanned')
        setAdding(false)
        setName('')
        setConn({})
        setTest(null)
        loadList()
      } else {
        toast.bad(r.error.message)
      }
    } catch (e) {
      toast.bad(e.message)
    }
    setBusy(false)
  }
  // Edit connection: prefill non-secret fields; masked '***' (password/credentials) are DROPPED —
  // re-enter to keep them, or leave blank to go keyless (BigQuery WIF/impersonation).
  const startEdit = ds => {
    setAdding(false); setEditing(ds); setType(ds.type); setName(ds.name); setTest(null)
    setConn(Object.fromEntries(Object.entries(ds.conn || {}).filter(([, v]) => v !== '***')
      .map(([k, v]) => [k, typeof v === 'object' ? JSON.stringify(v) : String(v)])))
  }
  const cancelForm = () => { setAdding(false); setEditing(null); setConn({}); setTest(null); setName('') }
  const update = async () => {
    setBusy(true)
    try {
      const started = await api.post(`/admin/jobs/datasources/${editing.id}/connection`, adminKey, { conn: buildConn() })
      const r = started.ok
        ? await api.waitJob(started.data.status_url, adminKey, ev => {
          if (ev.text) setTest({ pending: true, msg: ev.text })
        })
        : started
      if (r.ok) {
        toast.ok('Connection updated & validated')
        cancelForm()
        loadList()
      } else {
        toast.bad(r.error.message)
      }
    } catch (e) {
      toast.bad(e.message)
    }
    setBusy(false)
  }
  const dbList = list.filter(ds => ds.type !== 'docs')   // docs sources are managed in the Document tab now
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 11 }}>
      {dbList.map(ds => <DSCard key={ds.id} ds={ds} adminKey={adminKey} toast={toast} reload={loadList} onEdit={startEdit} />)}
      {dbList.length === 0 && <div style={{ fontSize: 12.5, color: 'var(--fg-subtle)' }}>No databases yet. Click "Add data source". (Document sources are managed in the Document tab.)</div>}
      {(adding || editing) ? (
        <div style={{ border: '1px solid var(--border)', borderRadius: 12, padding: 16, background: 'var(--bg)' }}>
          {editing && <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}><Icon name="edit" size={15} color="var(--accent)" />Edit connection — {editing.name}</div>}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
            <div><label style={lbl}>Type</label><select value={type} disabled={!!editing} onChange={e => { setType(e.target.value); setConn({}); setTest(null) }} style={fieldBox}>{Object.entries(specs).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}</select></div>
            <div><label style={lbl}>Name</label><input value={name} disabled={!!editing} onChange={e => setName(e.target.value)} style={fieldBox} placeholder="e.g. Warehouse" /></div>
            {type !== 'docs' && fields.map(f => <div key={f.key} style={{ gridColumn: f.json ? 'span 2' : 'auto' }}><label style={lbl}>{f.label}</label>{f.json ? <textarea rows={2} value={conn[f.key] || ''} onChange={e => setConn(c => ({ ...c, [f.key]: e.target.value }))} style={{ ...fieldBox, fontFamily: "'IBM Plex Mono',monospace", height: 'auto' }} placeholder={f.placeholder} /> : <input type={f.secret ? 'password' : 'text'} value={conn[f.key] || ''} onChange={e => setConn(c => ({ ...c, [f.key]: e.target.value }))} style={fieldBox} placeholder={f.placeholder || ''} />}</div>)}
          </div>
          {test && (test.ok ? <div style={{ ...alert, color: 'var(--success)' }}>OK — {(test.tables || []).slice(0, 6).join(', ')}</div> : <div style={{ ...alert, color: test.pending ? 'var(--fg-muted)' : 'var(--danger)' }}>{test.msg}</div>)}
          {editing && <div style={{ fontSize: 11.5, color: 'var(--fg-muted)', marginTop: 10 }}>Secret fields (password/credentials) are not reloaded — re-enter them if still in use, or leave blank to go keyless (WIF/impersonation). The new connection is validated first; if it fails, the old config stays in place. MDL &amp; documents are untouched.</div>}
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 12 }}>
            <button onClick={cancelForm} style={btnGhost}>Cancel</button>
            {type !== 'docs' && <button onClick={doTest} disabled={busy} style={btnGhost}>Test</button>}
            <button onClick={editing ? update : create} disabled={busy} style={btnPrimary}>{busy && <span className="spin" />}{editing ? 'Update connection' : 'Connect & scan'}</button>
          </div>
        </div>
      ) : <button onClick={() => setAdding(true)} style={dashed}><Icon name="add" size={18} />Add data source</button>}
    </div>
  )
}

// Curation whose table/kolom hilang dari scan TIDAK dibuang diam-diam: backend memarkirnya
// (curation_quarantine) dan melaporkannya di `pruned`, jadi admin tahu apa yang berubah.
function reportPrunedCuration({ pruned, toast }) {
  const p = pruned || {}
  if (p.dropped_total) {
    const gone = (p.vanished_tables || []).slice(0, 3).join(', ')
    const missing = gone ? ` · missing tables: ${gone}` : ''
    toast.warn(`${p.dropped_total} curation entries quarantined${missing}`
      + ' — restored automatically if the tables come back')
  }
  if (p.restored_total) toast.ok(`${p.restored_total} curation entries restored from quarantine`)
}

async function runDatasourceRescan({ ds, adminKey, toast, setRescanning, reload }) {
  setRescanning(true)
  const started = await api.post(`/admin/jobs/datasources/${ds.id}/rescan`, adminKey, {})
  const result = started.ok ? await api.waitJob(started.data.status_url, adminKey) : started
  setRescanning(false)
  if (!result.ok) return toast.bad(result.error?.message || 'Rescan failed')
  const mdl = result.data || {}
  toast.ok(`Rescan complete · ${Object.keys(mdl.tables || {}).length} tables`)
  reportPrunedCuration({ pruned: mdl.pruned, toast })
  reportBusinessPruning({ removed: mdl.business_pruned, toast })
  reload()
}

// A table that vanished from the scan is dropped from every business allowlist that held
// it. That is the right thing to do and the wrong thing to do quietly: the business stops
// being able to answer about it, so the admin hears about it here.
function reportBusinessPruning({ removed, toast }) {
  const entries = Object.entries(removed || {})
  if (!entries.length) return
  const total = entries.reduce((n, [, list]) => n + list.length, 0)
  const sample = [...new Set(entries.flatMap(([, list]) => list))].slice(0, 3).join(', ')
  toast.warn(`${total} tables removed from ${entries.length} businesses (${sample})`
    + ' — check the Knowledge Base')
}

function renderRescanButton({ rescanning, onClick }) {
  return (
    <button type="button" onClick={onClick} disabled={rescanning} title="Rescan schema and metadata"
      style={{ ...miniBtn, padding: '5px 9px', opacity: rescanning ? 0.6 : 1, cursor: rescanning ? 'wait' : 'pointer' }}>
      {rescanning ? <span className="spin" style={{ width: 13, height: 13 }} /> : <Icon name="refresh" size={16} />}
      {rescanning ? 'Rescanning…' : 'Rescan'}
    </button>
  )
}

// A paused datasource must be impossible to mistake for an active one, which takes several
// conditional styles. Resolved once here, outside DSCard: inline in the card they were six
// ternaries buried in JSX attributes, which reads worse and says less.
function pausedLook(enabled) {
  if (enabled) {
    return { card: 1, icon: 'var(--accent)', strike: 'none', toggleIcon: 'toggle_on',
             toggleColor: 'var(--fg-subtle)',
             toggleTitle: 'Disable — stops answering with it, configuration is kept' }
  }
  return { card: 0.62, icon: 'var(--fg-subtle)', strike: 'line-through', toggleIcon: 'toggle_off',
           toggleColor: 'var(--warn)', toggleTitle: 'Re-enable' }
}

function DSToggle({ look, busy, onClick }) {
  return (
    <button type="button" onClick={onClick} disabled={busy} title={look.toggleTitle}
      style={{ border: 'none', background: 'transparent', color: look.toggleColor, display: 'flex',
               cursor: busy ? 'default' : 'pointer', opacity: busy ? 0.5 : 1 }}>
      <Icon name={look.toggleIcon} size={20} />
    </button>
  )
}

DSToggle.propTypes = {
  look: PropTypes.shape({
    toggleTitle: PropTypes.string,
    toggleColor: PropTypes.string,
    toggleIcon: PropTypes.string,
  }).isRequired,
  busy: PropTypes.bool,
  onClick: PropTypes.func.isRequired,
}

function DSCard({ ds, adminKey, toast, reload, onEdit }) {
  const tables = Object.keys(ds.mdl?.tables || {})
  const [rescanning, setRescanning] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [toggling, setToggling] = useState(false)
  const [businesses, setBusinesses] = useState(null)

  // Which businesses curate this connection. Loaded because the two destructive actions
  // here — pause and delete — are the ones whose blast radius lives in the OTHER screen,
  // and an admin should not have to go looking for it.
  useEffect(() => {
    let live = true
    api.get(`/admin/tenants/${ds.tenant_id}/businesses`, adminKey).then(r => {
      if (live && r.ok) setBusinesses(r.data.filter(b => (b.datasource_ids || []).includes(ds.id)))
    })
    return () => { live = false }
  }, [ds.id, ds.tenant_id, adminKey])

  // Paused = kept but invisible to every answering path (routing, cross-datasource
  // planning, /v1/query, document retrieval). The reversible alternative to deleting.
  const enabled = ds.enabled !== false
  const look = pausedLook(enabled)
  const toggleEnabled = async () => {
    setToggling(true)
    const r = await api.patch(`/admin/datasources/${ds.id}`, adminKey, { enabled: !enabled })
    setToggling(false)
    if (!r.ok) return toast.bad(r.error?.message || 'Failed to update status')
    toast.ok(enabled ? `${ds.name} disabled — no longer used to answer`
                     : `${ds.name} re-enabled`)
    reload()
  }
  const del = async () => {
    setConfirmDelete(false)
    const r = await api.del(`/admin/datasources/${ds.id}`, adminKey)
    if (!r.ok) return toast.bad(r.error?.message || 'Failed to delete')
    toast.ok('Data source deleted')
    reload()
  }
  // A rescan can retire tables. `business_pruned` says which businesses lost which ones —
  // silence there would read as "nothing changed" while an allowlist quietly shrank.
  const rescan = () => runDatasourceRescan({ ds, adminKey, toast, setRescanning, reload })
  const curated = (businesses || []).reduce(
    (n, b) => n + (b.scopes?.[ds.id]?.allowed_tables || []).length, 0)

  return (
    <div style={{ border: '1px solid var(--border)', borderRadius: 12, padding: '14px 16px', background: 'var(--bg)', opacity: look.card }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <Icon name="database" size={18} color={look.icon} />
        <span style={{ fontWeight: 600, fontSize: 14, textDecoration: look.strike }}>{ds.name}</span>
        <span className="mono" style={{ fontSize: 11, color: 'var(--fg-muted)', padding: '3px 8px', borderRadius: 6, background: 'var(--surface-2)' }}>{ds.type}</span>
        {!enabled && <span title="Not used to answer; configuration & curation are kept"
          style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: .3, color: 'var(--warn)', padding: '3px 8px', borderRadius: 6, border: '1px solid var(--warn)' }}>DISABLED</span>}
        <span style={{ flex: 1 }} />
        {enabled && renderRescanButton({ rescanning, onClick: rescan })}
        <DSToggle look={look} busy={toggling} onClick={toggleEnabled} />
        <button type="button" onClick={() => onEdit(ds)} title="Edit connection" style={{ ...iconLink, cursor: 'pointer' }}><Icon name="edit" size={17} /></button>
        <button type="button" onClick={() => setConfirmDelete(true)} title="Delete data source" style={{ ...iconLink, cursor: 'pointer' }}><Icon name="delete" size={17} /></button>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginTop: 10, fontSize: 11.5, color: 'var(--fg-subtle)', flexWrap: 'wrap' }}>
        <span>{tables.length} tables scanned</span>
        <span>{(ds.mdl?.allowed_tables || []).length} allowed by connection</span>
        <span>{(ds.mdl?.pii_columns || []).length} PII columns</span>
        {businesses !== null && (
          <span>
            {businesses.length
              ? `used by ${businesses.length} ${bizNoun(businesses.length)} (${businesses.map(b => b.name).join(', ')}) · ${curated} tables curated`
              : 'not used by any business yet — cannot answer questions yet'}
          </span>
        )}
      </div>
      <div style={{ fontSize: 11, color: 'var(--fg-subtle)', marginTop: 9, lineHeight: 1.55, borderTop: '1px solid var(--border)', paddingTop: 9 }}>
        <Icon name="account_tree" size={13} color="var(--accent)" style={{ verticalAlign: '-2px', marginRight: 5 }} />
        Authorized tables, PII, schema context, and the semantic catalog are now managed per
        business in the <b style={{ color: 'var(--fg-muted)' }}>Knowledge Base</b> menu.
      </div>

      <ConfirmDialog open={confirmDelete} icon="delete" tone="danger"
        title={`Delete data source ${ds.name}?`} confirmLabel="Delete" cancelLabel="Cancel"
        onCancel={() => setConfirmDelete(false)} onConfirm={del}>
        The connection, schema scan, attached documents, and the curation
        {businesses?.length
          ? <> in <b style={{ color: 'var(--fg)' }}>{businesses.length} {bizNoun(businesses.length)} ({businesses.map(b => b.name).join(', ')})</b></>
          : null}
        {' '}will be lost and <b style={{ color: 'var(--fg)' }}>cannot be recovered</b>.
        To pause it instead, use the disable toggle.
      </ConfirmDialog>
    </div>
  )
}

/* ---------- Document: documents list + upload ---------- */
const DOC_CFG_DEFAULT = { chunk_size: 180, chunk_overlap: 40, ocr: false }

/* Per-document RAG config editor: change chunk size/overlap/OCR for ONE document, then
   re-chunk + re-embed it from its stored original (the embedding model stays per-tenant). */
function DocConfigEditor({ doc, onSave, onCancel, busy }) {
  const c = doc.config || {}
  const [chunkSize, setChunkSize] = useState(c.chunk_size ?? DOC_CFG_DEFAULT.chunk_size)
  const [overlap, setOverlap] = useState(c.chunk_overlap ?? DOC_CFG_DEFAULT.chunk_overlap)
  const [ocr, setOcr] = useState(!!c.ocr)
  const lbl = { fontSize: 11, color: 'var(--fg-subtle)', display: 'flex', flexDirection: 'column', gap: 4 }
  return (
    <div style={{ margin: '2px 0 10px 26px', padding: 12, border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface-2)' }}>
      <div style={{ fontSize: 11.5, color: 'var(--fg-muted)', marginBottom: 10, lineHeight: 1.5 }}>
        Change and save → this document is <b style={{ color: 'var(--fg)' }}>re-chunked & re-embedded</b> from its original file. (Needs the stored original — documents uploaded before this feature must be re-uploaded.)
      </div>
      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <label style={lbl}>Chunk size (words)<input type="number" min="40" max="800" value={chunkSize} onChange={e => setChunkSize(e.target.value)} style={{ ...fieldBox, width: 120 }} /></label>
        <label style={lbl}>Overlap (words)<input type="number" min="0" value={overlap} onChange={e => setOverlap(e.target.value)} style={{ ...fieldBox, width: 120 }} /></label>
        <label style={{ ...lbl, flexDirection: 'row', alignItems: 'center', gap: 7 }}><input type="checkbox" checked={ocr} onChange={e => setOcr(e.target.checked)} />OCR images (PPT/PDF)</label>
        <span style={{ flex: 1 }} />
        <button onClick={onCancel} style={btnGhost}>Cancel</button>
        <button onClick={() => onSave({ chunk_size: Number(chunkSize), chunk_overlap: Number(overlap), ocr })} disabled={busy} style={btnPrimary}>{busy && <span className="spin" />}Save & re-embed</button>
      </div>
    </div>
  )
}

function DocumentsSection({ tid, adminKey, toast }) {
  const [dsId, setDsId] = useState(''); const [dsList, setDsList] = useState([]); const [docs, setDocs] = useState([]); const fileRef = useRef()
  const [newName, setNewName] = useState(''); const [creating, setCreating] = useState(false)
  const [reindexOpen, setReindexOpen] = useState(false); const [reindexing, setReindexing] = useState(false)
  const [up, setUp] = useState({ ...DOC_CFG_DEFAULT }); const [uploading, setUploading] = useState(false)
  const [editId, setEditId] = useState(''); const [busyId, setBusyId] = useState('')
  const reindex = async () => {
    setReindexing(true)
    const started = await api.post(`/admin/jobs/tenants/${tid}/reindex-docs`, adminKey, {})
    const r = started.ok ? await api.waitJob(started.data.status_url, adminKey) : started
    setReindexing(false)
    if (r.ok) toast.ok(`Reindex complete — ${r.data.reindexed_chunks} chunks (${r.data.embedder})`)
    else toast.bad(r.error.message)
    loadDocs()
  }
  const load = useCallback(async () => { const r = await api.get(`/admin/tenants/${tid}/datasources`, adminKey); if (r.ok) { const d = r.data.filter(x => x.type === 'docs'); setDsList(d); setDsId(p => (d.some(x => x.id === p) ? p : (d[0]?.id || ''))) } }, [tid])
  useEffect(() => { load() }, [])
  const loadDocs = useCallback(async () => { if (!dsId) return setDocs([]); const r = await api.get(`/admin/tenants/${tid}/documents?datasource_id=${dsId}`, adminKey); if (r.ok) setDocs(r.data) }, [tid, dsId])
  useEffect(() => { loadDocs() }, [dsId])
  const createSource = async () => {
    if (!newName.trim()) return toast.warn('document source name')
    setCreating(true)
    const r = await api.post('/admin/datasources', adminKey, { tenant_id: tid, type: 'docs', name: newName.trim(), conn: {} })
    setCreating(false)
    if (!r.ok) return toast.bad(r.error.message)
    toast.ok('Document source created'); setNewName(''); await load(); setDsId(r.data.id)
  }
  const delSource = async () => {
    const ds = dsList.find(d => d.id === dsId); if (!ds) return
    if (!confirm(`Delete document source "${ds.name}" and every document inside it?`)) return
    const r = await api.del(`/admin/datasources/${dsId}`, adminKey)
    if (r.ok) {
      toast.ok('Document source deleted')
      setDsId('')
      load()
    } else {
      toast.bad(r.error.message)
    }
  }
  const upload = async () => {
    const f = fileRef.current?.files?.[0]; if (!f) return toast.warn('choose a file')
    const fd = new FormData(); fd.append('tenant_id', tid); fd.append('datasource_id', dsId); fd.append('acl_roles', ''); fd.append('file', f)
    fd.append('chunk_size', String(up.chunk_size)); fd.append('chunk_overlap', String(up.chunk_overlap)); fd.append('ocr', String(up.ocr))
    setUploading(true)
    const started = await api.upload('/admin/documents', adminKey, fd)
    const r = started.ok ? await api.waitJob(started.data.status_url, adminKey, () => loadDocs()) : started
    setUploading(false)
    if (!r.ok) return toast.bad(r.error.message)
    toast.ok(`Ingested ${r.data.n_chunks} chunks` + (r.data.replaced?.length ? ' — previous version archived automatically' : ''))
    if (r.data.possible_duplicates?.length) toast.warn(`Similar to existing documents: ${r.data.possible_duplicates.map(x => x.filename).join(', ')} — archive the outdated one if this is its newer version`)
    fileRef.current.value = ''; loadDocs()
  }
  const saveDocConfig = async (docId, cfg) => {
    setBusyId(docId)
    const started = await api.post(`/admin/jobs/documents/${docId}/reingest`, adminKey, cfg)
    const r = started.ok ? await api.waitJob(started.data.status_url, adminKey) : started
    setBusyId('')
    if (!r.ok) return toast.bad(r.error.message)
    toast.ok(`Re-embedded — ${r.data.n_chunks} chunks`); setEditId(''); loadDocs()
  }
  const toggleArchive = async (d) => {
    const r = await api.patch(`/admin/documents/${d.id}`, adminKey, { archived: !d.archived })
    if (r.ok) {
      toast.ok(d.archived ? 'Unarchived — used by the AI again' : 'Archived — no longer used by the AI')
      loadDocs()
    } else {
      toast.bad(r.error.message)
    }
  }
  return (
    <div style={{ marginTop: 22, borderTop: '1px solid var(--border)', paddingTop: 18 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 18, padding: '10px 12px', border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface)' }}>
        <Icon name="sync" size={17} color="var(--accent)" />
        <div style={{ flex: 1, fontSize: 11.5, color: 'var(--fg-muted)', lineHeight: 1.5 }}>Changed the <b style={{ color: 'var(--fg)' }}>Embedding model</b> above? Click reindex to re-embed every document with the new model — old vectors have a different dimension and won't be found without it.</div>
        <button onClick={() => setReindexOpen(true)} disabled={reindexing} style={btnGhost}>{reindexing && <span className="spin" />}Reindex documents</button>
      </div>
      <div style={{ fontWeight: 600, fontSize: 13.5, marginBottom: 4 }}>Document sources</div>
      <div style={{ fontSize: 11.5, color: 'var(--fg-subtle)', marginBottom: 12, lineHeight: 1.5 }}>Containers for RAG documents (e.g. "Warehouse SOP"). Create a source, then upload PDF/DOCX/PPTX/MD/TXT files into it.</div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 14, alignItems: 'center' }}>
        <input value={newName} onChange={e => setNewName(e.target.value)} onKeyDown={e => e.key === 'Enter' && createSource()} style={{ ...fieldBox, flex: 1 }} placeholder="New document source name (e.g. Warehouse SOP)" />
        <button onClick={createSource} disabled={creating} style={btnPrimary}>{creating && <span className="spin" />}<Icon name="add" size={16} />Create source</button>
      </div>
      {dsList.length === 0 ? <div style={{ fontSize: 12.5, color: 'var(--fg-subtle)' }}>No document sources yet — create one above.</div>
        : <>
          <div style={{ display: 'flex', gap: 8, marginBottom: 10, flexWrap: 'wrap', alignItems: 'center' }}>
            <select value={dsId} onChange={e => setDsId(e.target.value)} style={{ ...fieldBox, width: 'auto' }}>{dsList.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}</select>
            <button title="Delete this document source" onClick={delSource} style={iconBtn}><Icon name="delete" size={16} /></button>
          </div>
          <div style={{ display: 'flex', gap: 12, marginBottom: 14, flexWrap: 'wrap', alignItems: 'flex-end', padding: '10px 12px', border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface)' }}>
            <div style={{ fontSize: 11, color: 'var(--fg-subtle)', width: '100%', lineHeight: 1.5 }}>Settings for the uploaded file (each document can differ — change them any time in the list below):</div>
            <label style={{ fontSize: 11, color: 'var(--fg-subtle)', display: 'flex', flexDirection: 'column', gap: 4 }}>Chunk size (words)<input type="number" min="40" max="800" value={up.chunk_size} onChange={e => setUp(u => ({ ...u, chunk_size: e.target.value }))} style={{ ...fieldBox, width: 110 }} /></label>
            <label style={{ fontSize: 11, color: 'var(--fg-subtle)', display: 'flex', flexDirection: 'column', gap: 4 }}>Overlap (words)<input type="number" min="0" value={up.chunk_overlap} onChange={e => setUp(u => ({ ...u, chunk_overlap: e.target.value }))} style={{ ...fieldBox, width: 110 }} /></label>
            <label style={{ fontSize: 11.5, color: 'var(--fg-muted)', display: 'flex', alignItems: 'center', gap: 7 }}><input type="checkbox" checked={up.ocr} onChange={e => setUp(u => ({ ...u, ocr: e.target.checked }))} />OCR images (PPT/PDF)</label>
            <span style={{ flex: 1 }} />
            <input type="file" ref={fileRef} accept=".md,.txt,.pdf,.docx,.pptx" style={{ fontSize: 12.5 }} />
            <button onClick={upload} disabled={uploading} style={btnGhost}>{uploading && <span className="spin" />}Upload</button>
          </div>
          {docs.map(d => (
            <div key={d.id}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '7px 0', borderBottom: editId === d.id ? 'none' : '1px solid var(--border)', fontSize: 13, opacity: d.archived ? .55 : 1 }}>
                <Icon name="description" size={16} color="var(--accent)" />
                <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{d.filename}</span>
                {d.archived ? <span className="mono" style={{ fontSize: 10.5, padding: '2px 7px', borderRadius: 6, background: 'var(--surface-2)', color: 'var(--fg-muted)', flexShrink: 0 }}>archived — not used by the AI</span> : null}
                {d.job_status === 'processing' ? <span className="mono" style={{ fontSize: 10.5, padding: '2px 7px', borderRadius: 6, background: 'var(--accent-soft)', color: 'var(--accent)', flexShrink: 0 }}>processing</span> : null}
                {d.job_status === 'failed' ? <span className="mono" title={d.job_error || 'INGEST_FAILED'} style={{ fontSize: 10.5, padding: '2px 7px', borderRadius: 6, background: 'var(--danger-soft)', color: 'var(--danger)', flexShrink: 0 }}>processing failed</span> : null}
                <span className="mono" style={{ fontSize: 10.5, color: 'var(--fg-subtle)', flexShrink: 0 }} title="this document's settings">{docCfgLabel(d.config)}{d.n_chunks} chunks</span>
                <button title="Edit settings (chunk/OCR) & re-embed this document" onClick={() => setEditId(editId === d.id ? '' : d.id)} style={{ border: 'none', background: 'transparent', color: editId === d.id ? 'var(--accent)' : 'var(--fg-subtle)', display: 'flex', cursor: 'pointer', flexShrink: 0 }}>
                  <Icon name="tune" size={16} />
                </button>
                <button title={d.archived ? 'Unarchive (used by the AI again)' : 'Archive (removed from AI search, file is kept)'} onClick={() => toggleArchive(d)} style={{ border: 'none', background: 'transparent', color: 'var(--fg-subtle)', display: 'flex', cursor: 'pointer', flexShrink: 0 }}>
                  <Icon name={d.archived ? 'unarchive' : 'archive'} size={16} />
                </button>
              </div>
              {editId === d.id && <DocConfigEditor doc={d} busy={busyId === d.id} onCancel={() => setEditId('')} onSave={cfg => saveDocConfig(d.id, cfg)} />}
            </div>
          ))}
        </>}
      <ConfirmDialog
        open={reindexOpen}
        icon="sync"
        title="Reindex all documents?"
        confirmLabel="Reindex"
        cancelLabel="Cancel"
        onCancel={() => setReindexOpen(false)}
        onConfirm={() => { setReindexOpen(false); reindex() }}
      >
        Every document chunk is re-embedded with the current embedding model. Required after switching models (old vectors have a different dimension). Large models (e.g. <b style={{ color: 'var(--fg)' }}>bge-m3</b>) download ~1–2 GB on first use & can take a few minutes.
      </ConfirmDialog>
    </div>
  )
}

/* ---------- Role tab ---------- */
function RoleTab({ tid, adminKey, toast }) {
  const [roles, setRoles] = useState(null); const [pool, setPool] = useState([]); const [cols, setCols] = useState([])
  const [editing, setEditing] = useState(null)   // role name | '__new__' | null
  const load = useCallback(async () => {
    const [r, d] = await Promise.all([api.get(`/admin/tenants/${tid}/roles`, adminKey), api.get(`/admin/tenants/${tid}/datasources`, adminKey)])
    if (r.ok) setRoles(r.data)
    if (d.ok) {
      const ts = new Set(), cs = new Set()
      d.data.forEach(ds => { const tb = ds.mdl?.tables || {}; Object.keys(tb).forEach(t => ts.add(t)); Object.values(tb).forEach(cl => (cl || []).forEach(c => cs.add(c))) })
      setPool([...ts].sort((a, b) => String(a).localeCompare(String(b))))
      setCols([...cs].sort((a, b) => String(a).localeCompare(String(b))))
    }
  }, [tid])
  useEffect(() => { load() }, [])
  if (!roles) return <Loading />
  const savePolicy = async (name, policy) => {
    const r = await api.post('/admin/roles', adminKey, { tenant_id: tid, name, policy })
    if (r.ok) {
      load()
      return true
    }
    toast.bad(r.error.message)
    return false
  }
  const delRole = async (name) => {
    if (!confirm('Delete role ' + name + '?')) return
    const r = await api.del(`/admin/tenants/${tid}/roles/${name}`, adminKey)
    if (r.ok) {
      toast.ok('Deleted')
      load()
    }
  }
  const submit = async (origName, name, policy) => {
    if (!name.trim()) return toast.warn('Role name is required')
    if (origName == null && roles.some(x => x.name === name.trim())) return toast.warn('A role with that name already exists')
    const ok = await savePolicy(name.trim(), policy)
    if (ok) { setEditing(null); toast.ok(origName == null ? 'Role created' : 'Role updated') }
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 11 }}>
      {roles.map(r => editing === r.name
        ? <RoleEditor key={r.id} tablePool={pool} colPool={cols} initialName={r.name} initialPolicy={r.policy || {}}
            onCancel={() => setEditing(null)} onSubmit={(name, policy) => submit(r.name, name, policy)} />
        : <RoleCard key={r.id} r={r} onEdit={() => setEditing(r.name)} onDelete={() => delRole(r.name)} />)}
      {editing === '__new__'
        ? <RoleEditor tablePool={pool} colPool={cols} onCancel={() => setEditing(null)} onSubmit={(name, policy) => submit(null, name, policy)} />
        : <button onClick={() => setEditing('__new__')} style={dashed}><Icon name="add" size={18} />Add role</button>}
      <RoleMappingSection tid={tid} adminKey={adminKey} toast={toast} roles={roles} />
    </div>
  )
}

// External RBAC alias layer: map a CLIENT system's role/group ids (e.g. "SPV-01",
// opaque group GUIDs) onto our tenant roles — integrators keep their own names.
// Also surfaces "unmapped roles": values seen on verified JWTs that resolved to
// nothing (rejected 403), each with a one-click "map to..." action.
function RoleMappingSection({ tid, adminKey, toast, roles }) {
  const [maps, setMaps] = useState(null)
  const [unmapped, setUnmapped] = useState([])
  const [ext, setExt] = useState('')
  const [target, setTarget] = useState(roles[0]?.name || '')
  const load = useCallback(async () => {
    const [m, u] = await Promise.all([
      api.get(`/admin/tenants/${tid}/role-mappings`, adminKey),
      api.get(`/admin/tenants/${tid}/unmapped-roles`, adminKey),
    ])
    if (m.ok) setMaps(m.data)
    if (u.ok) setUnmapped(u.data)
  }, [tid])
  useEffect(() => { load() }, [load])
  const add = async (externalId, roleName) => {
    if (!externalId.trim()) return toast.warn('External ID is required')
    if (!roleName) return toast.warn('Pick a target role')
    const r = await api.post(`/admin/tenants/${tid}/role-mappings`, adminKey, { external_id: externalId.trim(), role_name: roleName })
    if (r.ok) {
      toast.ok(`"${externalId.trim()}" → ${roleName}`)
      setExt('')
      load()
    } else {
      toast.bad(r.error.message)
    }
  }
  const del = async (externalId) => {
    const r = await api.del(`/admin/tenants/${tid}/role-mappings/${encodeURIComponent(externalId)}`, adminKey)
    if (r.ok) {
      toast.ok('Mapping deleted')
      load()
    } else {
      toast.bad(r.error.message)
    }
  }
  if (!maps) return null
  return (
    <div style={{ border: '1px solid var(--border)', borderRadius: 12, padding: '14px 16px', background: 'var(--bg)', marginTop: 4 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 4 }}>
        <Icon name="sync_alt" size={17} color="var(--accent)" />
        <span style={{ fontWeight: 600, fontSize: 13.5 }}>Role mapping (external systems)</span>
      </div>
      <div style={hintStyle}>Map a client system's role/group IDs (e.g. <span className="mono">SPV-01</span>, GUIDs) to this tenant's roles — clients keep their own role names. JWT claims are matched against these mappings first, then against exact role names.</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 12 }}>
        {maps.length === 0 && <div style={{ ...hintStyle, marginTop: 0 }}>No mappings yet.</div>}
        {maps.map(m => {
          const broken = !roles.some(x => x.name === m.role_name)   // target role deleted -> mapping DENIES (authoritative)
          return (
            <div key={m.external_id} style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '7px 10px', border: `1px solid ${broken ? 'color-mix(in srgb,var(--danger) 45%,var(--border))' : 'var(--border)'}`, borderRadius: 9, background: 'var(--surface)' }}>
              <span className="mono" style={{ fontSize: 12.5, fontWeight: 600 }}>{m.external_id}</span>
              <Icon name="arrow_right_alt" size={16} color="var(--fg-subtle)" />
              <span style={{ fontSize: 12.5, color: broken ? 'var(--danger)' : 'var(--accent)', fontWeight: 600 }}>{m.role_name}</span>
              {broken && <span title="The target role was deleted. This mapping now DENIES access — delete it or remap to another role." style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 10.5, fontWeight: 700, color: 'var(--danger)', background: 'color-mix(in srgb,var(--danger) 10%,transparent)', padding: '2px 8px', borderRadius: 6 }}><Icon name="warning" size={12} />role deleted · deny</span>}
              <span style={{ flex: 1 }} />
              <button onClick={() => del(m.external_id)} title="Delete mapping" style={{ border: 'none', background: 'transparent', color: 'var(--fg-subtle)', display: 'flex', cursor: 'pointer' }}><Icon name="delete" size={16} /></button>
            </div>
          )
        })}
      </div>
      <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
        <input value={ext} onChange={e => setExt(e.target.value)} placeholder="external id (e.g. SPV-01)" style={{ ...fieldBox, height: 36, flex: 1, fontSize: 12.5 }} />
        <select value={target} onChange={e => setTarget(e.target.value)} style={{ ...fieldBox, height: 36, width: 180, fontSize: 12.5, fontFamily: 'inherit' }}>
          {roles.map(r => <option key={r.name} value={r.name}>{r.name}</option>)}
        </select>
        <button onClick={() => add(ext, target)} style={{ ...miniBtn, height: 36, color: 'var(--accent)', borderColor: 'var(--accent)' }}><Icon name="add" size={15} />Map</button>
      </div>
      {unmapped.length > 0 && (
        <div style={{ marginTop: 14, paddingTop: 12, borderTop: '1px dashed var(--border)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginBottom: 8 }}>
            <Icon name="report" size={15} color="var(--danger)" />
            <span style={{ fontSize: 12.5, fontWeight: 600 }}>Seen but not mapped</span>
            <span style={{ ...hintStyle, marginTop: 0 }}>— role values on valid JWTs that were rejected (403)</span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {unmapped.map(u => (
              <div key={u.external_id} style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '7px 10px', border: '1px solid color-mix(in srgb,var(--danger) 30%,var(--border))', borderRadius: 9, background: 'var(--surface)' }}>
                <span className="mono" style={{ fontSize: 12.5, fontWeight: 600 }}>{u.external_id}</span>
                <span style={{ fontSize: 11, color: 'var(--fg-subtle)', flex: 1 }}>{u.count}× · last seen {fmtShort(u.last_seen)}</span>
                <select defaultValue="" onChange={e => e.target.value && add(u.external_id, e.target.value)} style={{ height: 30, padding: '0 8px', border: '1px solid var(--border)', borderRadius: 7, background: 'var(--bg)', color: 'var(--fg)', fontSize: 12 }}>
                  <option value="" disabled>map to…</option>
                  {roles.map(r => <option key={r.name} value={r.name}>{r.name}</option>)}
                </select>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// "All tables" = full_access true (govern bypasses the per-role table check); seed roles
// without an authorized_tables key behave like full access, so we surface them that way.
function RoleCard({ r, onEdit, onDelete }) {
  const p = r.policy || {}
  const full = p.full_access === true || !Array.isArray(p.authorized_tables)
  const rf = Object.entries(p.row_filters || {})
  const denied = p.denied_columns || []
  return (
    <div style={{ border: '1px solid var(--border)', borderRadius: 12, padding: '14px 16px', background: 'var(--bg)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 13 }}>
        <div style={{ width: 34, height: 34, borderRadius: 9, background: 'var(--accent-soft)', color: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}><Icon name="shield_person" size={19} /></div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 600, fontSize: 13.5 }}>{r.name}</div>
          <div style={{ fontSize: 12, color: 'var(--fg-muted)', marginTop: 1 }}>{full ? 'All tables' : `${(p.authorized_tables || []).length} tables`} · {p.can_see_pii ? 'May see PII' : 'PII blocked'}</div>
        </div>
        <button onClick={onEdit} style={miniBtn}><Icon name="tune" size={15} />Edit</button>
        <button onClick={onDelete} style={{ border: 'none', background: 'transparent', color: 'var(--fg-subtle)', display: 'flex' }}><Icon name="delete" size={17} /></button>
      </div>
      {(rf.length > 0 || denied.length > 0) && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 11, paddingTop: 11, borderTop: '1px solid var(--border)' }}>
          {rf.map(([c, v]) => <span key={c} className="mono" style={tagStyle('var(--accent-soft)', 'var(--accent)')}><Icon name="filter_alt" size={13} />{c} = {String(v)}</span>)}
          {denied.map(c => <span key={c} className="mono" style={tagStyle('color-mix(in srgb,var(--danger) 10%,transparent)', 'var(--danger)')}><Icon name="visibility_off" size={13} />{c}</span>)}
        </div>
      )}
    </div>
  )
}
const tagStyle = (bg, color) => ({ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 11.5, padding: '4px 9px', borderRadius: 7, background: bg, color, border: '1px solid var(--border)' })

function RoleEditor({ tablePool, colPool, initialName = '', initialPolicy = null, onCancel, onSubmit }) {
  const isNew = initialName === ''
  const p0 = initialPolicy || {}
  const startSpecific = Array.isArray(p0.authorized_tables) && p0.authorized_tables.length > 0 && p0.full_access !== true
  const [name, setName] = useState(initialName)
  const [mode, setMode] = useState(startSpecific ? 'specific' : 'all')        // table access
  const [tables, setTables] = useState(p0.authorized_tables || [])
  const [pii, setPii] = useState(!!p0.can_see_pii)
  const [denied, setDenied] = useState(p0.denied_columns || [])
  const [filters, setFilters] = useState(Object.entries(p0.row_filters || {}).map(([col, val]) => ({ col, val: String(val) })))

  const addTable = () => { const avail = tablePool.find(t => !tables.includes(t)); if (avail) setTables([...tables, avail]) }
  const setFilter = (i, k, v) => setFilters(filters.map((f, idx) => idx === i ? { ...f, [k]: v } : f))
  const addDenied = () => { const avail = colPool.find(c => !denied.includes(c)); if (avail) setDenied([...denied, avail]) }

  const save = () => {
    const row_filters = {}
    filters.forEach(f => { if (f.col && f.col.trim()) row_filters[f.col.trim()] = f.val })
    onSubmit(name, {
      full_access: mode === 'all',
      authorized_tables: mode === 'specific' ? tables : [],
      can_see_pii: pii,
      denied_columns: denied,
      row_filters,
    })
  }
  return (
    <div style={{ ...cardBox, display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
        <Icon name="shield_person" size={18} color="var(--accent)" />
        <span style={{ fontWeight: 700, fontSize: 14 }}>{isNew ? 'New role' : `Edit role · ${initialName}`}</span>
      </div>

      <div><label style={lbl}>Role name</label>
        <input value={name} onChange={e => setName(e.target.value)} disabled={!isNew} style={{ ...fieldBox, opacity: isNew ? 1 : .6 }} placeholder="e.g. analyst_jakarta" />
        {!isNew && <div style={hintStyle}>The role name is the identity used in JWTs — it can't be changed (create a new role if needed).</div>}
      </div>

      {/* Table access */}
      <div>
        <label style={lbl}><Icon name="table_chart" size={14} color="var(--accent)" /> Table access</label>
        <div style={{ display: 'flex', gap: 8 }}>
          {['all', 'specific'].map(m => <button key={m} onClick={() => setMode(m)} style={segBtn(mode === m)}>{m === 'all' ? 'All tables' : 'Specific tables'}</button>)}
        </div>
        <div style={hintStyle}>{mode === 'all'
          ? 'Every table the datasource allowlist permits (Database tab) — not every physical table. The role still cannot reach tables outside the allowlist.'
          : 'Limit this role to a subset of the datasource allowlist.'}</div>
        {mode === 'specific' && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 7, alignItems: 'center', marginTop: 8 }}>
            {tables.map(t => <span key={t} className="mono" style={chipStyle}>{t}<button onClick={() => setTables(tables.filter(x => x !== t))} style={xBtn}><Icon name="close" size={14} /></button></span>)}
            <button onClick={addTable} style={dashedSm}><Icon name="add" size={15} />Add table</button>
            {tables.length === 0 && <span style={{ ...hintStyle, color: 'var(--danger)' }}>Empty = every query from this role is refused.</span>}
          </div>
        )}
      </div>

      {/* RLAC */}
      <div>
        <label style={lbl}><Icon name="filter_alt" size={14} color="var(--accent)" /> Row filters · RLAC</label>
        <div style={hintStyle}>Each filter becomes <span className="mono">WHERE column = value</span> for this role. Handy for scoping data to a region/branch.</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 8 }}>
          {filters.map((f, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <input list="rolecols" value={f.col} onChange={e => setFilter(i, 'col', e.target.value)} style={{ ...fieldBox, flex: 1 }} placeholder="column (e.g. region)" />
              <span className="mono" style={{ color: 'var(--fg-subtle)' }}>=</span>
              <input value={f.val} onChange={e => setFilter(i, 'val', e.target.value)} style={{ ...fieldBox, flex: 1 }} placeholder="value (e.g. Jakarta)" />
              <button onClick={() => setFilters(filters.filter((_, idx) => idx !== i))} style={xBtn}><Icon name="delete" size={17} /></button>
            </div>
          ))}
          <datalist id="rolecols">{colPool.map(c => <option key={c} value={c} />)}</datalist>
          <button onClick={() => setFilters([...filters, { col: '', val: '' }])} style={dashedSm}><Icon name="add" size={15} />Add row filter</button>
        </div>
      </div>

      {/* CLAC */}
      <div>
        <label style={lbl}><Icon name="lock" size={14} color="var(--accent)" /> Column access · CLAC</label>
        <button onClick={() => setPii(!pii)} style={{ ...toggleRow, marginTop: 6 }}>
          <Icon name={pii ? 'toggle_on' : 'toggle_off'} size={26} color={pii ? 'var(--accent)' : 'var(--fg-subtle)'} />
          <div style={{ flex: 1 }}><div style={{ fontWeight: 600, fontSize: 13 }}>{pii ? 'May see PII columns' : 'PII columns blocked'}</div>
            <div style={hintStyle}>PII columns (email, name, salary, etc.) are never sent to an LLM while blocked.</div></div>
        </button>
        <div style={{ marginTop: 10 }}>
          <div style={{ ...hintStyle, marginBottom: 6 }}>Denied columns (beyond PII) — blocked for this role:</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 7, alignItems: 'center' }}>
            {denied.map(c => <span key={c} className="mono" style={chipStyle}>{c}<button onClick={() => setDenied(denied.filter(x => x !== c))} style={xBtn}><Icon name="close" size={14} /></button></span>)}
            <button onClick={addDenied} style={dashedSm}><Icon name="add" size={15} />Add column</button>
          </div>
        </div>
      </div>

      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, borderTop: '1px solid var(--border)', paddingTop: 14 }}>
        <button onClick={onCancel} style={btnGhost}>Cancel</button>
        <button onClick={save} style={btnPrimary}>{isNew ? 'Create role' : 'Save changes'}</button>
      </div>
    </div>
  )
}
const toggleRow = { display: 'flex', alignItems: 'center', gap: 11, width: '100%', textAlign: 'left', padding: '10px 12px', border: '1px solid var(--border)', borderRadius: 10, background: 'var(--bg)', cursor: 'pointer' }
const segBtn = (on) => ({ flex: 1, padding: '9px 12px', border: `1px solid ${on ? 'var(--accent)' : 'var(--border)'}`, borderRadius: 9, background: on ? 'var(--accent-soft)' : 'var(--surface)', color: on ? 'var(--accent)' : 'var(--fg-muted)', fontSize: 13, fontWeight: 600, cursor: 'pointer' })

/* ---------- Model tab (design-style cards, backed by config 'models'; Primary drives the gateway) ---------- */
const ROLE_TONE = {
  Primary: { c: 'var(--accent)', b: 'var(--accent-soft)' },
  Simple: { c: 'var(--success)', b: 'var(--success-soft)' },
  Gatekeeper: { c: '#7c3aed', b: 'color-mix(in srgb,#7c3aed 14%,transparent)' },
  Fallback: { c: 'var(--warn)', b: 'color-mix(in srgb,var(--warn) 15%,transparent)' },
  Standby: { c: 'var(--fg-subtle)', b: 'var(--surface-2)' },
}
// Roles that only make sense once per tenant: adding a second demotes the old holder.
const UNIQUE_ROLE_DEMOTE = { Primary: 'Fallback', Simple: 'Standby', Gatekeeper: 'Standby' }
function ModelTab({ tid, adminKey, toast }) {
  const [cat, setCat] = useState(null); const [models, setModels] = useState(null); const [provs, setProvs] = useState([]); const [adding, setAdding] = useState(false); const [editIdx, setEditIdx] = useState(null)
  const load = useCallback(async () => {
    const [c, m, p] = await Promise.all([api.get(`/admin/provider-catalog?tenant_id=${encodeURIComponent(tid)}`, adminKey), api.get(`/admin/tenants/${tid}/config/models`, adminKey), api.get(`/admin/tenants/${tid}/providers`, adminKey)])
    if (c.ok) setCat(c.data); if (m.ok) setModels(m.data.models || []); if (p.ok) setProvs(p.data)
  }, [tid])
  useEffect(() => { load() }, [])
  if (!cat || models === null) return <Loading />
  const persist = async (list) => { setModels(list); const r = await api.post(`/admin/tenants/${tid}/config/models`, adminKey, { data: { models: list } }); if (!r.ok) toast.bad(r.error.message) }
  const setPrimary = (i) => persist(models.map((m, idx) => idx === i ? { ...m, role: 'Primary', enabled: true } : (m.role === 'Primary' ? { ...m, role: 'Fallback' } : m)))
  const remove = (i) => {
    if (!confirm(`Remove model "${models[i].name}" from this tenant?`)) return
    persist(models.filter((_, idx) => idx !== i))
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 11 }}>
      {models.map((m, i) => editIdx === i
        ? <ModelForm key={i} cat={cat} provs={provs} existing={models} editIdx={i} tid={tid} adminKey={adminKey} toast={toast} onDone={(list) => { setEditIdx(null); if (list) persist(list) }} />
        : (() => {
          const t = ROLE_TONE[m.role] || ROLE_TONE.Standby; const active = m.enabled !== false
          return (
            <div key={i} style={cardBox}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 14 }}>
                <Icon name="smart_toy" size={19} color="var(--accent)" />
                <span className="mono" style={{ fontWeight: 600, fontSize: 14 }}>{m.name}</span>
                <span style={{ fontSize: 11.5, color: 'var(--fg-muted)', padding: '3px 9px', borderRadius: 6, background: 'var(--surface-2)' }}>{m.provider}</span>
                <span style={{ fontSize: 11, fontWeight: 600, padding: '3px 10px', borderRadius: 20, background: t.b, color: t.c }}>{m.role}</span>
                <span style={{ flex: 1 }} />
                <span style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11.5, fontWeight: 600, color: active ? 'var(--success)' : 'var(--fg-subtle)' }}><span style={{ width: 6, height: 6, borderRadius: '50%', background: active ? 'var(--success)' : 'var(--fg-subtle)' }} />{active ? 'Active' : 'Disabled'}</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'flex-end', gap: 28, flexWrap: 'wrap' }}>
                <Param label="Temperature" value={m.temperature ?? '—'} />
                <Param label="Max tokens" value={m.max_tokens != null ? Number(m.max_tokens).toLocaleString() : '—'} />
                <Param label="Top P" value={m.top_p ?? '—'} />
                <span style={{ flex: 1 }} />
                <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                  <button onClick={() => setEditIdx(i)} style={miniBtn}><Icon name="tune" size={15} />Edit</button>
                  {m.role === 'Primary'
                    ? <span style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 12, fontWeight: 600, color: 'var(--accent)' }}><Icon name="star" size={17} />Primary</span>
                    : <button onClick={() => setPrimary(i)} style={miniBtn}>Set as primary</button>}
                  <button onClick={() => remove(i)} style={iconBtn} title="Delete model"><Icon name="delete" size={17} /></button>
                </div>
              </div>
            </div>
          )
        })())}
      {models.length === 0 && <div style={{ fontSize: 12.5, color: 'var(--fg-subtle)' }}>No models yet — add one below.</div>}
      {adding
        ? <ModelForm cat={cat} provs={provs} existing={models} editIdx={null} tid={tid} adminKey={adminKey} toast={toast} onDone={(list) => { setAdding(false); if (list) persist(list) }} />
        : editIdx == null && <button onClick={() => setAdding(true)} style={dashed}><Icon name="add" size={18} />Add model</button>}
    </div>
  )
}
function Param({ label, value }) { return <div><div style={{ fontSize: 11, color: 'var(--fg-subtle)' }}>{label}</div><div className="mono" style={{ fontWeight: 600, marginTop: 2, fontSize: 13.5 }}>{value}</div></div> }
function SearchableModelSelect({ id, value, options, onChange }) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const root = useRef(null)
  const unavailable = !!value && !options.includes(value)
  const filtered = options.filter(model => model.toLowerCase().includes(query.trim().toLowerCase()))
  let valueColor = 'var(--fg-subtle)'          // nothing picked yet
  if (unavailable) valueColor = 'var(--danger)'   // picked a model the provider no longer lists
  else if (value) valueColor = 'var(--fg)'
  const popupId = `${id}-popup`

  useEffect(() => { setQuery('') }, [options])
  useEffect(() => {
    const close = (event) => { if (!root.current?.contains(event.target)) setOpen(false) }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [])

  return (
    <div ref={root} style={{ position: 'relative' }}>
      <button id={id} type="button" aria-haspopup="true" aria-expanded={open} aria-controls={popupId} onClick={() => setOpen(v => !v)} style={{ ...fieldBox, display: 'flex', alignItems: 'center', width: '100%', textAlign: 'left', cursor: 'pointer', borderColor: unavailable ? 'var(--danger)' : 'var(--border)' }}>
        <span className="mono" style={{ flex: 1, color: valueColor }}>{value || 'Select model'}</span>
        <Icon name={open ? 'arrow_drop_up' : 'arrow_drop_down'} size={21} color="var(--fg-muted)" />
      </button>
      {open && <div id={popupId} style={{ position: 'absolute', zIndex: 80, top: 'calc(100% + 5px)', left: 0, right: 0, padding: 7, border: '1px solid var(--border)', borderRadius: 9, background: 'var(--surface)', boxShadow: '0 12px 30px rgba(0,0,0,.16)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '0 9px', border: '1px solid var(--border)', borderRadius: 7, background: 'var(--bg)' }}>
          <Icon name="search" size={17} color="var(--fg-subtle)" />
          <input autoFocus value={query} onChange={e => setQuery(e.target.value)} onKeyDown={e => { if (e.key === 'Escape') setOpen(false) }} placeholder="Search model..." style={{ width: '100%', height: 36, border: 'none', outline: 'none', background: 'transparent', color: 'var(--fg)', fontFamily: 'var(--mono)', fontSize: 12.5 }} />
        </div>
        <div style={{ maxHeight: 230, overflowY: 'auto', marginTop: 6 }}>
          {filtered.map(model => <button type="button" key={model} onClick={() => { onChange(model); setQuery(''); setOpen(false) }} style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%', padding: '9px 10px', border: 'none', borderRadius: 6, background: model === value ? 'var(--accent-soft)' : 'transparent', color: model === value ? 'var(--accent)' : 'var(--fg)', cursor: 'pointer', textAlign: 'left' }}>
            <Icon name={model === value ? 'check' : 'smart_toy'} size={15} color={model === value ? 'var(--accent)' : 'var(--fg-subtle)'} />
            <span className="mono" style={{ fontSize: 12.5 }}>{model}</span>
          </button>)}
          {filtered.length === 0 && <div style={{ padding: '12px 10px', color: 'var(--fg-subtle)', fontSize: 12 }}>No models found.</div>}
        </div>
      </div>}
    </div>
  )
}
function CodexDeviceAuthPanel({ adminKey, toast }) {
  const [status, setStatus] = useState(null)
  const [busy, setBusy] = useState(false)
  const load = useCallback(async (quiet = false) => {
    if (!quiet) setBusy(true)
    const r = await api.get('/admin/codex-auth/status', adminKey)
    if (!quiet) setBusy(false)
    if (r.ok) setStatus(r.data)
    else if (!quiet) toast.bad(r.error.message)
  }, [adminKey, toast])
  useEffect(() => { load() }, [load])
  const login = status?.login || {}
  const phase = login.phase || 'idle'
  const active = phase === 'starting' || phase === 'waiting_for_user'
  const connected = !!status?.logged_in || phase === 'connected'
  useEffect(() => {
    if (!active) return undefined
    const timer = setInterval(() => load(true), 1500)
    return () => clearInterval(timer)
  }, [active, load])
  const run = async (path) => {
    setBusy(true)
    const r = await api.post(path, adminKey, {})
    setBusy(false)
    if (r.ok) setStatus(r.data)
    else toast.bad(r.error.message)
  }
  const copyCode = async () => {
    try { await navigator.clipboard?.writeText(login.user_code); toast.ok('Device code copied') }
    catch { toast.warn('Copy the device code manually') }
  }
  const deviceUrl = login.verification_url || status?.device_url || 'https://auth.openai.com/codex/device'
  let statusLabel = 'Not connected'
  if (connected) statusLabel = 'Connected'
  else if (active) statusLabel = 'Waiting for login'
  let phaseColor = 'var(--fg-subtle)'
  if (phase === 'failed' || phase === 'expired') phaseColor = 'var(--danger)'
  else if (connected) phaseColor = 'var(--success)'
  return (
    <div style={{ gridColumn: 'span 2', border: '1px solid var(--border)', borderRadius: 10, background: 'var(--bg)', padding: 13 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <Icon name={connected ? 'check_circle' : 'phonelink_lock'} size={18} color={connected ? 'var(--success)' : 'var(--accent)'} />
        <span style={{ fontSize: 12.5, fontWeight: 700 }}>Codex device login</span>
        <span style={{ flex: 1 }} />
        {(busy || active) && <span className="spin" />}
        <span style={{ fontSize: 11, fontWeight: 600, color: connected ? 'var(--success)' : 'var(--fg-subtle)' }}>
          {statusLabel}
        </span>
      </div>
      <div style={{ fontSize: 11.5, color: 'var(--fg-subtle)', lineHeight: 1.55, marginBottom: 10 }}>
        Poros runs the device login directly on the backend. Open the OpenAI page and enter the one-time code; no VM terminal or <span className="mono">kubectl exec</span> needed.
      </div>
      {login.user_code && <div style={{ display: 'flex', alignItems: 'center', gap: 10, background: 'var(--accent-soft)', border: '1px solid color-mix(in srgb, var(--accent) 30%, var(--border))', borderRadius: 9, padding: '10px 12px', marginBottom: 9 }}>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 10.5, color: 'var(--fg-subtle)', marginBottom: 3 }}>One-time device code</div>
          <div className="mono" style={{ fontSize: 18, fontWeight: 700, letterSpacing: '.08em', color: 'var(--accent)' }}>{login.user_code}</div>
        </div>
        <button type="button" onClick={copyCode} style={miniBtn}><Icon name="content_copy" size={15} />Copy code</button>
      </div>}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        {status?.can_manage && !active && <button type="button" disabled={busy || !status?.installed} onClick={() => run('/admin/codex-auth/device/start')} style={miniBtn}><Icon name={connected ? 'sync' : 'login'} size={15} />{connected ? 'Reconnect account' : 'Start device login'}</button>}
        {active && login.user_code && <a href={deviceUrl} target="_blank" rel="noreferrer" style={{ ...miniBtn, textDecoration: 'none' }}><Icon name="open_in_new" size={15} />Open device page</a>}
        {status?.can_manage && active && <button type="button" disabled={busy} onClick={() => run('/admin/codex-auth/device/cancel')} style={miniBtn}><Icon name="close" size={15} />Cancel login</button>}
        <button type="button" disabled={busy} onClick={() => load()} style={miniBtn}><Icon name="refresh" size={15} />Refresh status</button>
      </div>
      {!status?.can_manage && <div style={{ fontSize: 11, color: 'var(--warning)', marginTop: 9 }}>Only a platform admin can change the shared Codex account.</div>}
      {(login.message || status?.status) && <div style={{ fontSize: 11, color: phaseColor, marginTop: 9 }}>{login.message || status.status}</div>}
    </div>
  )
}
function ModelForm({ cat, provs, existing, editIdx, tid, adminKey, toast, onDone }) {
  const init = editIdx != null ? existing[editIdx] : null
  const [provider, setProvider] = useState(init?.provider || cat.providers[0]?.id || 'openai')
  const [name, setName] = useState(init?.name || cat.providers[0]?.models?.[0] || '')
  const [role, setRole] = useState(init?.role || 'Fallback')
  const [temp, setTemp] = useState(String(init?.temperature ?? '0.2'))
  const [maxTok, setMaxTok] = useState(String(init?.max_tokens ?? '4096'))
  const [topP, setTopP] = useState(String(init?.top_p ?? '0.95'))
  const [enabled, setEnabled] = useState(init ? init.enabled !== false : true)
  const [apiKey, setApiKey] = useState('')
  const entry = cat.providers.find(p => p.id === provider) || {}
  const opts = entry.models || []
  const modelUnavailable = !!name && !opts.includes(name)
  const needs = entry.needs || ['api_key']
  const needsKey = needs.includes('api_key'); const needsCodex = needs.includes('codex_device')
  const hasKey = !needsCodex && provs.find(p => p.provider === provider)
  const save = async () => {
    if (!name.trim()) return toast.warn('model name required')
    if (modelUnavailable) return toast.warn('This model is not available for the selected provider/account')
    if (!needsCodex && apiKey.trim()) await api.post('/admin/providers', adminKey, { tenant_id: tid, provider, label: provider, api_key: apiKey.trim(), api_base: '' })
    const card = { name: name.trim(), provider, role, temperature: parseFloat(temp), max_tokens: parseInt(maxTok), top_p: parseFloat(topP), enabled }
    let list = editIdx != null ? existing.map((m, idx) => idx === editIdx ? card : m) : [...existing, card]
    const selfIdx = editIdx != null ? editIdx : list.length - 1
    if (UNIQUE_ROLE_DEMOTE[role]) list = list.map((m, idx) => idx === selfIdx ? m : (m.role === role ? { ...m, role: UNIQUE_ROLE_DEMOTE[role] } : m))
    onDone(list)
  }
  const authBlock = needsCodex
    ? <CodexDeviceAuthPanel adminKey={adminKey} toast={toast} />
    : hasKey
      ? <div style={{ gridColumn: 'span 2', display: 'flex', alignItems: 'center', gap: 6, fontSize: 11.5, color: 'var(--success)' }}><Icon name="check_circle" size={15} />API key for {provider} is already saved.</div>
      : needsKey
        ? <div style={{ gridColumn: 'span 2' }}><label style={lbl}>{provider} API key</label><input type="password" value={apiKey} onChange={e => setApiKey(e.target.value)} style={fieldBox} placeholder="sk-..." />
          <div style={{ fontSize: 11, color: 'var(--fg-subtle)', marginTop: 5, lineHeight: 1.5 }}>Without a key, this model cannot call a live LLM. Fill it in to enable real model answers.</div></div>
        : null
  return (
    <div style={cardBox}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 14 }}>
        <Icon name="smart_toy" size={18} color="var(--accent)" />
        <span style={{ fontWeight: 700, fontSize: 14 }}>{editIdx != null ? `Edit model · ${init?.name || ''}` : 'New model'}</span>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        <div><label htmlFor="model-provider" style={lbl}>Provider</label><select id="model-provider" value={provider} onChange={e => { const next = cat.providers.find(p => p.id === e.target.value); setProvider(e.target.value); setName(next?.models?.[0] || '') }} style={fieldBox}>{cat.providers.map(p => <option key={p.id} value={p.id}>{p.label}</option>)}</select></div>
        <div><label htmlFor="model-name" style={lbl}>Model</label>
          <SearchableModelSelect id="model-name" value={name} options={opts} onChange={setName} />
          {modelUnavailable && <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginTop: 6, color: 'var(--danger)', fontSize: 11.5 }}><Icon name="error" size={15} />This saved model is no longer available. Pick a replacement from the list.</div>}
          <div style={{ fontSize: 10.5, color: 'var(--fg-subtle)', marginTop: 5, lineHeight: 1.45 }}>The list follows the provider. Final availability still depends on your account/provider access.</div>
        </div>
        <div><label style={lbl} htmlFor="model-role">Role (usage order)</label><select id="model-role" value={role} onChange={e => setRole(e.target.value)} style={fieldBox}><option>Primary</option><option>Simple</option><option>Gatekeeper</option><option>Fallback</option><option>Standby</option></select>
          <div style={{ fontSize: 10.5, color: 'var(--fg-subtle)', marginTop: 5, lineHeight: 1.45 }}>Primary = main model (every analysis answer) · Simple = budget model for greetings, follow-up rewrites + background work (memory/summaries) · Gatekeeper = screens every prompt (fastest model) · Fallback = when other models fail · Standby = inactive spare</div></div>
        <div><label style={lbl}>Temperature</label><input value={temp} onChange={e => setTemp(e.target.value)} style={fieldBox} /></div>
        <div><label style={lbl}>Max tokens</label><input value={maxTok} onChange={e => setMaxTok(e.target.value)} style={fieldBox} /></div>
        <div><label style={lbl}>Top P</label><input value={topP} onChange={e => setTopP(e.target.value)} style={fieldBox} /></div>
        <button type="button" onClick={() => setEnabled(v => !v)} style={{ gridColumn: 'span 2', display: 'flex', alignItems: 'center', gap: 9, padding: '8px 11px', border: '1px solid var(--border)', borderRadius: 9, background: 'var(--bg)', cursor: 'pointer', textAlign: 'left' }}>
          <Icon name={enabled ? 'toggle_on' : 'toggle_off'} size={24} color={enabled ? 'var(--success)' : 'var(--fg-subtle)'} />
          <span style={{ fontSize: 12.5, fontWeight: 600 }}>{enabled ? 'Active — used by the gateway' : 'Disabled — skipped by the gateway'}</span>
        </button>
        {authBlock}

      </div>
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 12 }}>
        <button onClick={() => onDone(null)} style={btnGhost}>Cancel</button>
        <button onClick={save} style={btnPrimary}>{editIdx != null ? 'Save changes' : 'Add model'}</button>
      </div>
    </div>
  )
}

/* ---------- App Key tab (design-style: env / per-key rate-limit + expiry / last-used) ---------- */
const ENV_TONE = { Production: { c: 'var(--accent)', b: 'var(--accent-soft)' }, Sandbox: { c: 'var(--warn)', b: 'color-mix(in srgb,var(--warn) 14%,transparent)' } }
const RATE_OPTS = [60, 120, 300, 1000, 2000]
const EXPIRY_OPTS = [['', 'No expiry'], ['30', '30 days'], ['90', '90 days'], ['365', '1 year']]
function fmtDate(iso) { return iso ? fmtDay(iso) : 'No expiry' }
function fmtWhen(iso) { return fmtShort(iso) }
function AppKeyTab({ tid, adminKey, toast }) {
  const [keys, setKeys] = useState(null); const [roles, setRoles] = useState([])
  const [issuedKey, setIssuedKey] = useState('')
  const [genOpen, setGenOpen] = useState(false); const [genName, setGenName] = useState(''); const [genEnv, setGenEnv] = useState('Production')
  const load = useCallback(async () => {
    const [k, r] = await Promise.all([api.get(`/admin/tenants/${tid}/apikeys`, adminKey), api.get(`/admin/tenants/${tid}/roles`, adminKey)])
    if (k.ok) setKeys(k.data); if (r.ok) setRoles(r.data)
  }, [tid])
  useEffect(() => { load() }, [])
  if (!keys) return <Loading />
  const patch = async (key, body) => {
    const r = await api.patch(`/admin/apikeys/${key}`, adminKey, body)
    if (r.ok) load()
    else toast.bad(r.error.message)
  }
  const setExp = (key, days) => { let iso = ''; if (days) { const d = new Date(); d.setDate(d.getDate() + parseInt(days)); iso = d.toISOString().slice(0, 10) } patch(key, { expiry: iso }) }
  const rotate = async (key) => {
    if (!confirm('Recreate key? The old one is revoked permanently.')) return
    const r = await api.post(`/admin/apikeys/${key}/rotate`, adminKey, {})
    if (r.ok) {
      setIssuedKey(r.data.key)
      navigator.clipboard?.writeText(r.data.key)
      toast.ok('Recreated — save the new key now')
      load()
    } else {
      toast.bad(r.error.message)
    }
  }
  const revoke = async (key) => {
    if (!confirm('Revoke this key?')) return
    const r = await api.del(`/admin/apikeys/${key}`, adminKey)
    if (r.ok) {
      toast.ok('Revoked')
      load()
    }
  }
  const gen = async () => {
    const label = genName.trim() || (genEnv + ' App Key')
    const r = await api.post('/admin/apikeys', adminKey, { tenant_id: tid, label, env: genEnv })
    if (r.ok) {
      setIssuedKey(r.data.key)
      navigator.clipboard?.writeText(r.data.key)
      toast.ok('App key created — save it now')
      setGenOpen(false)
      setGenName('')
      load()
    } else {
      toast.bad(r.error.message)
    }
  }
  const active = keys.filter(k => k.active)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 13 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 9, padding: '11px 13px', borderRadius: 10, background: 'var(--accent-soft)' }}>
        <Icon name="info" size={18} color="var(--accent)" style={{ marginTop: 1 }} />
        <span style={{ fontSize: 12.5, color: 'var(--fg-muted)', lineHeight: 1.55 }}><b style={{ color: 'var(--fg)' }}>1 app key = 1 consumer app per environment</b> (e.g. Production, Development) — not per role. Every request <b style={{ color: 'var(--fg)' }}>must</b> carry an <b style={{ color: 'var(--fg)' }}>end-user JWT</b> (roles are defined in the <b style={{ color: 'var(--fg)' }}>Role</b> tab) — <b style={{ color: 'var(--fg)' }}>requests without a JWT are rejected</b>. <b style={{ color: 'var(--fg)' }}>Recreate</b> permanently revokes the old key.</span>
      </div>
      {issuedKey && <div style={{ ...cardBox, padding: '14px 16px', borderColor: 'var(--warn)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 9 }}>
          <Icon name="warning" size={17} color="var(--warn)" />
          <b style={{ fontSize: 13 }}>Save this app key now — it is shown only once</b>
          <span style={{ flex: 1 }} />
          <button onClick={() => setIssuedKey('')} style={iconLink}><Icon name="close" size={17} /></button>
        </div>
        <div className="mono" style={{ display: 'flex', alignItems: 'center', gap: 8, minHeight: 40, padding: '0 12px', border: '1px solid var(--border)', borderRadius: 9, background: 'var(--surface)' }}>
          <span style={{ flex: 1, overflowWrap: 'anywhere', fontSize: 12.5 }}>{issuedKey}</span>
          <button onClick={() => { navigator.clipboard?.writeText(issuedKey); toast.ok('App key copied') }} style={iconLink}><Icon name="content_copy" size={17} /></button>
        </div>
      </div>}
      {active.map(k => {
        const et = ENV_TONE[k.env] || ENV_TONE.Production
        const shown = k.key_prefix || 'stored securely'
        return (
          <div key={k.key} style={{ ...cardBox, padding: '16px 17px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 11, marginBottom: 13 }}>
              <Icon name="vpn_key" size={19} color="var(--accent)" />
              <span style={{ fontWeight: 600, fontSize: 14 }}>{k.label || 'App key'}</span>
              <span style={{ fontSize: 11, fontWeight: 600, padding: '3px 10px', borderRadius: 20, background: et.b, color: et.c }}>{k.env || 'Production'}</span>
              <span style={{ flex: 1 }} />
              <span style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11.5, fontWeight: 600, color: 'var(--success)' }}><span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--success)' }} />Active</span>
            </div>
            <div className="mono" style={{ display: 'flex', alignItems: 'center', gap: 8, height: 40, padding: '0 13px', border: '1px solid var(--border)', borderRadius: 9, background: 'var(--surface)', fontSize: 13 }}>
              <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis' }}>{shown}</span>
              <span style={{ fontSize: 11, color: 'var(--fg-subtle)' }}>secret stored as a hash</span>
            </div>
            <div style={{ fontSize: 11.5, color: 'var(--fg-subtle)', marginTop: 8, display: 'flex', alignItems: 'flex-start', gap: 6, lineHeight: 1.5 }}><Icon name="shield_person" size={14} color="var(--accent)" style={{ marginTop: 1, flexShrink: 0 }} /><span>Usage: the <span className="mono" style={{ color: 'var(--fg-muted)' }}>X-API-Key</span> header takes the secret issued at create/recreate + <span className="mono" style={{ color: 'var(--fg-muted)' }}>Authorization: Bearer &lt;JWT&gt;</span>. The old secret cannot be shown again.</span></div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginTop: 14 }}>
              <div><label style={lbl}>Rate limit</label>
                <div style={selBox}><Icon name="speed" size={17} color="var(--accent)" />
                  <select value={k.rate_limit || ''} onChange={e => patch(k.key, { rate_limit: parseInt(e.target.value) || 0 })} style={bareSel}>
                    <option value="">tenant default</option>{RATE_OPTS.map(o => <option key={o} value={o}>{o.toLocaleString()} req/min</option>)}</select></div></div>
              <div><label style={lbl}>Expiry date</label>
                <div style={selBox}><Icon name="event" size={17} color="var(--accent)" />
                  <span style={{ flex: 1 }}>{fmtDate(k.expiry)}</span>
                  <select value="" onChange={e => setExp(k.key, e.target.value)} style={{ ...bareSel, flex: 'none', width: 'auto' }}>
                    <option value="">change…</option>{EXPIRY_OPTS.map(([v, l]) => <option key={l} value={v}>{l}</option>)}</select></div></div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginTop: 14, paddingTop: 13, borderTop: '1px solid var(--border)' }}>
              <span style={{ fontSize: 12, color: 'var(--fg-subtle)', display: 'flex', gap: 16 }}>
                <span>Created <b style={{ color: 'var(--fg-muted)' }}>{(k.created || '').slice(0, 10)}</b></span>
                <span>Last used <b style={{ color: 'var(--fg-muted)' }}>{fmtWhen(k.last_used)}</b></span></span>
              <div style={{ display: 'flex', gap: 8 }}>
                <button onClick={() => rotate(k.key)} style={miniBtn}><Icon name="autorenew" size={16} />Recreate key</button>
                <button onClick={() => revoke(k.key)} style={{ ...miniBtn, color: 'var(--danger)' }}>Revoke</button></div>
            </div>
          </div>
        )
      })}
      {genOpen
        ? <div style={cardBox}><div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          <div><label style={lbl}>App name</label><input value={genName} onChange={e => setGenName(e.target.value)} style={fieldBox} placeholder="e.g. Production App Key" /></div>
          <div><label style={lbl}>Environment</label><select value={genEnv} onChange={e => setGenEnv(e.target.value)} style={fieldBox}><option>Production</option><option>Development</option><option>Sandbox</option></select></div>
        </div><div style={{ fontSize: 11.5, color: 'var(--fg-subtle)', marginTop: 8 }}>The end-user role travels per request via JWT (Role tab). This key only identifies the app per environment.</div><div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 12 }}><button onClick={() => setGenOpen(false)} style={btnGhost}>Cancel</button><button onClick={gen} style={btnPrimary}>Generate app key</button></div></div>
        : <button onClick={() => setGenOpen(true)} style={dashed}><Icon name="add" size={18} />Generate new app key</button>}
      <JwtSecretSection tid={tid} adminKey={adminKey} toast={toast} />
    </div>
  )
}

/* ---------- End-user JWT verifier (JWKS/public key in production; explicit HS256 staging seam) ---------- */
function JwtSecretSection({ tid, adminKey, toast }) {
  const [cfg, setCfg] = useState(null)
  const [secret, setSecret] = useState('')      // NEW secret to set; blank = keep existing
  const [reveal, setReveal] = useState(false)
  const [roleClaim, setRoleClaim] = useState('role')
  const [alg, setAlg] = useState('RS256')
  const [jwksUrl, setJwksUrl] = useState('')
  const [publicKey, setPublicKey] = useState('')
  const [issuer, setIssuer] = useState('')
  const [audience, setAudience] = useState('')
  const [saved, setSaved] = useState(false)
  const load = useCallback(async () => {
    const r = await api.get(`/admin/tenants/${tid}/config/jwt`, adminKey)   // secret NEVER returned (masked), only has_secret
    if (r.ok) {
      setCfg(r.data)
      setRoleClaim(r.data.role_claim || 'role')
      setAlg((r.data.algorithms || [])[0] || (r.data.jwks_url || r.data.public_key ? 'RS256' : 'HS256'))
      setJwksUrl(r.data.jwks_url || '')
      setPublicKey(r.data.public_key || '')
      setIssuer(r.data.issuer || '')
      setAudience(r.data.audience || '')
    }
  }, [tid])
  useEffect(() => { load() }, [])
  if (!cfg) return null
  const generate = () => {
    const a = new Uint8Array(32); (window.crypto || window.msCrypto).getRandomValues(a)   // 256-bit
    setSecret([...a].map(b => b.toString(16).padStart(2, '0')).join('')); setReveal(true); setSaved(false)
  }
  const _cfgData = () => ({
    role_claim: roleClaim.trim() || 'role',
    algorithms: [alg],
    jwks_url: jwksUrl.trim(),
    public_key: publicKey.trim(),
    issuer: issuer.trim(),
    audience: audience.trim(),
  })
  // Main save NEVER touches the secret (sent blank -> store keeps existing). So an accidental
  // click can't rotate the secret / break the consumer.
  const saveConfig = async () => {
    const r = await api.post(`/admin/tenants/${tid}/config/jwt`, adminKey, { data: _cfgData() })
    if (r.ok) { toast.ok('Settings saved (secret NOT changed)'); await load() } else toast.bad(r.error.message)
  }
  // The ONLY path that sets/changes the secret — deliberate button. Rotating an EXISTING
  // secret needs a confirm (it breaks live consumers); the FIRST-time set does not.
  const applySecret = async () => {
    if (!secret.trim()) return
    const rotate = cfg.has_secret
    if (rotate && !window.confirm("Rotate this tenant's JWT secret?\n\nConsumers still on the old secret will FAIL (401) until you install the new secret on the consumer and restart it.\n\nContinue?")) return
    const r = await api.post(`/admin/tenants/${tid}/config/jwt`, adminKey, { data: { ..._cfgData(), secret: secret.trim() } })
    if (r.ok) { toast.ok(rotate ? 'JWT secret rotated — set the same value on the consumer and restart it' : 'JWT secret saved — set this value on the consumer'); setSaved(true); await load() } else toast.bad(r.error.message)
  }
  const active = !!(cfg.jwks_url || cfg.public_key)
  const hasLegacySecret = cfg.has_secret
  return (
    <div style={{ ...cardBox, marginTop: 6 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
        <Icon name="key" size={19} color="var(--accent)" />
        <span style={{ fontWeight: 600, fontSize: 14 }}>JWT end-user verifier</span>
        <span style={{ flex: 1 }} />
        <span style={{ fontSize: 11, fontWeight: 600, padding: '3px 10px', borderRadius: 20,
          background: active ? 'var(--success-soft)' : 'var(--warn-soft)', color: active ? 'var(--success)' : 'var(--warn)' }}>
          {active ? 'Asymmetric active' : 'HS active (dev/staging)'}</span>
      </div>
      <div style={{ fontSize: 12, color: 'var(--fg-muted)', lineHeight: 1.55, marginBottom: 12 }}>
        Production uses a <b>JWKS URL</b> or a public key from your identity provider. Poros stores
        only the public key and follows JWKS key rotation automatically; the private key stays with the consumer/IdP.
        Staging may use HS256 only when the operator enables a dedicated flag; issuer and audience are still required.
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        <div><label style={lbl} htmlFor="jwt-jwks-url">JWKS URL <span style={{ color: 'var(--fg-subtle)', fontWeight: 400 }}>· recommended</span></label>
          <input id="jwt-jwks-url" value={jwksUrl} onChange={e => setJwksUrl(e.target.value)} style={fieldBox}
            placeholder="https://idp.example.com/.well-known/jwks.json" /></div>
        <div><label style={lbl} htmlFor="jwt-alg">Algorithm</label><select id="jwt-alg" value={alg} onChange={e => setAlg(e.target.value)} style={fieldBox}>
          <option>RS256</option><option>RS384</option><option>RS512</option>
          <option>ES256</option><option>ES384</option><option>ES512</option>
          <option>HS256</option><option>HS384</option><option>HS512</option>
        </select></div>
      </div>
      <label style={lbl} htmlFor="jwt-public-key">Public key PEM <span style={{ color: 'var(--fg-subtle)', fontWeight: 400 }}>· alternative when there is no JWKS</span></label>
      <textarea id="jwt-public-key" value={publicKey} onChange={e => setPublicKey(e.target.value)} style={{ ...fieldBox, height: 82, padding: 10, resize: 'vertical' }}
        placeholder={'-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----'} />

      <label style={lbl} htmlFor="jwt-hs-secret">HS256 secret <span style={{ color: 'var(--warn)', fontWeight: 400 }}>· local, or staging with the flag on</span></label>
      <div className="mono" style={{ display: 'flex', alignItems: 'center', gap: 8, height: 40, padding: '0 13px', border: '1px solid var(--border)', borderRadius: 9, background: 'var(--surface)', fontSize: 13 }}>
        <input id="jwt-hs-secret" type={reveal ? 'text' : 'password'} value={secret} onChange={e => { setSecret(e.target.value); setSaved(false) }}
          placeholder={hasLegacySecret ? 'stored — enter a value to rotate' : 'Generate or paste a dev secret'} spellCheck={false}
          name="tenantos-jwt-secret" autoComplete="new-password" data-1p-ignore data-lpignore="true"
          style={{ flex: 1, minWidth: 0, border: 'none', background: 'transparent', color: 'var(--fg)', fontSize: 13, outline: 'none', fontFamily: 'inherit' }} />
        <button onClick={generate} title="Generate a strong secret (256-bit)" style={iconLink}><Icon name="casino" size={17} /></button>
        <button onClick={() => setReveal(r => !r)} title={reveal ? 'Hide' : 'Show'} style={iconLink}><Icon name={reveal ? 'visibility_off' : 'visibility'} size={17} /></button>
        <button onClick={() => { if (secret) { navigator.clipboard?.writeText(secret); toast.ok('Secret copied') } }} title="Copy" style={iconLink}><Icon name="content_copy" size={17} /></button>
      </div>
      {secret && <div style={{ display: 'flex', alignItems: 'flex-start', gap: 6, fontSize: 11.5, color: 'var(--warn)', marginTop: 7, lineHeight: 1.5 }}>
        <Icon name="warning" size={14} color="var(--warn)" style={{ marginTop: 1, flexShrink: 0 }} />
        <span>Copy it now (not shown again after saving). Set the <b>same</b> value on the consumer &amp; <b>restart</b> it — on Docker, change <span className="mono">TENANTOS_JWT_SECRET</span> in <span className="mono">docker-compose.yml</span> (not <span className="mono">.env</span> if the value is hardcoded) then <span className="mono">up -d --force-recreate</span>. A mismatch → JWT_INVALID.</span></div>}
      {secret && <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 8 }}>
        <button onClick={applySecret} style={{ ...btnPrimary, background: hasLegacySecret ? 'var(--danger)' : 'var(--accent)' }}><Icon name={hasLegacySecret ? 'published_with_changes' : 'save'} size={16} />{hasLegacySecret ? 'Rotate secret now' : 'Save dev secret'}</button>
        <span style={{ fontSize: 11, color: 'var(--fg-subtle)' }}>{hasLegacySecret ? 'rotates the dev secret (asks to confirm)' : 'local/dev compatibility only'}</span>
      </div>}
      {saved && <div style={{ ...alert, color: 'var(--success)' }}>✓ Per-tenant secret saved. Make sure you copied it into the consumer's <span className="mono">.env</span> (<span className="mono">TENANTOS_JWT_SECRET</span>).</div>}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 14 }}>
        <div><label style={lbl} htmlFor="jwt-role-claim">Role claim <span style={{ color: 'var(--fg-subtle)', fontWeight: 400 }}>· JWT field name, usually "role"</span></label>
          <input id="jwt-role-claim" value={roleClaim} onChange={e => setRoleClaim(e.target.value)} style={fieldBox} placeholder="role" />
          <div style={{ fontSize: 10.5, color: 'var(--fg-subtle)', marginTop: 5, lineHeight: 1.45 }}>The claim that HOLDS the role (e.g. <span className="mono">role</span> or <span className="mono">groups</span>) — <b>not</b> the role value. Don't put <span className="mono">admin</span> here.</div></div>
        <div><label style={lbl} htmlFor="jwt-issuer">Issuer <span style={{ color: 'var(--danger)', fontWeight: 400 }}>(required in production)</span></label><input id="jwt-issuer" value={issuer} onChange={e => setIssuer(e.target.value)} style={fieldBox} placeholder="https://idp.example.com/" /></div>
        <div><label style={lbl} htmlFor="jwt-audience">Audience <span style={{ color: 'var(--danger)', fontWeight: 400 }}>(required in production)</span></label><input id="jwt-audience" value={audience} onChange={e => setAudience(e.target.value)} style={fieldBox} placeholder="poros-consumer" /></div>
      </div>
      <div style={{ fontSize: 11, color: 'var(--fg-subtle)', marginTop: 8, lineHeight: 1.5 }}>
        <span className="mono">role_claim</span> = the JWT claim holding the role (string or array).
        Real production accepts only the tenant's asymmetric JWTs. The staging-only HS256 exception
        must be enabled by the operator and still needs a matching tenant secret + issuer + audience.
      </div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 10, marginTop: 12 }}>
        <span style={{ fontSize: 11, color: 'var(--fg-subtle)', flex: 1 }}>Saves role claim / algorithm / issuer / audience. Does <b>not</b> change the secret.</span>
        <button onClick={saveConfig} style={btnPrimary}><Icon name="save" size={16} />Save settings</button>
      </div>
    </div>
  )
}

TenantConfigModal.propTypes = {
  tenant: tenantType.isRequired,
  adminKey: PropTypes.string.isRequired,
  toast: toastType.isRequired,
  onClose: PropTypes.func.isRequired,
  onSaved: PropTypes.func,
}
const tenantSectionPropTypes = {
  tid: PropTypes.string.isRequired,
  adminKey: PropTypes.string.isRequired,
  toast: toastType.isRequired,
}
FieldsTab.propTypes = {
  ...tenantSectionPropTypes,
  section: PropTypes.string.isRequired,
  fields: PropTypes.array.isRequired,
  extra: PropTypes.node,
}
MemoriesSection.propTypes = tenantSectionPropTypes
DatabaseTab.propTypes = tenantSectionPropTypes
DSCard.propTypes = {
  ds: datasourceType.isRequired,
  adminKey: PropTypes.string.isRequired,
  toast: toastType.isRequired,
  reload: PropTypes.func.isRequired,
  onEdit: PropTypes.func.isRequired,
}
DocConfigEditor.propTypes = {
  doc: PropTypes.object.isRequired,
  onSave: PropTypes.func.isRequired,
  onCancel: PropTypes.func.isRequired,
  busy: PropTypes.bool,
}
DocumentsSection.propTypes = tenantSectionPropTypes
RoleTab.propTypes = tenantSectionPropTypes
RoleMappingSection.propTypes = {
  ...tenantSectionPropTypes,
  roles: PropTypes.array.isRequired,
}
RoleCard.propTypes = {
  r: PropTypes.object.isRequired,
  onEdit: PropTypes.func.isRequired,
  onDelete: PropTypes.func.isRequired,
}
SearchableModelSelect.propTypes = {
  id: PropTypes.string.isRequired,
  value: PropTypes.string,
  options: PropTypes.arrayOf(PropTypes.string).isRequired,
  onChange: PropTypes.func.isRequired,
}
RoleEditor.propTypes = {
  tablePool: PropTypes.arrayOf(PropTypes.string).isRequired,
  colPool: PropTypes.arrayOf(PropTypes.string).isRequired,
  initialName: PropTypes.string,
  initialPolicy: PropTypes.object,
  onCancel: PropTypes.func.isRequired,
  onSubmit: PropTypes.func.isRequired,
}
ModelTab.propTypes = tenantSectionPropTypes
Param.propTypes = { label: PropTypes.node, value: PropTypes.node }
SearchableModelSelect.propTypes = {
  value: PropTypes.string,
  options: PropTypes.arrayOf(PropTypes.string).isRequired,
  onChange: PropTypes.func.isRequired,
}
CodexDeviceAuthPanel.propTypes = {
  adminKey: PropTypes.string.isRequired,
  toast: toastType.isRequired,
}
ModelForm.propTypes = {
  cat: PropTypes.object.isRequired,
  provs: PropTypes.array.isRequired,
  existing: PropTypes.object,
  editIdx: PropTypes.number,
  tid: PropTypes.string.isRequired,
  adminKey: PropTypes.string.isRequired,
  toast: toastType.isRequired,
  onDone: PropTypes.func.isRequired,
}
AppKeyTab.propTypes = tenantSectionPropTypes
JwtSecretSection.propTypes = tenantSectionPropTypes
Toggle.propTypes = { on: PropTypes.bool, onClick: PropTypes.func.isRequired }
Chip.propTypes = { on: PropTypes.bool, onClick: PropTypes.func.isRequired, children: PropTypes.node }
Sub.propTypes = { label: PropTypes.node, children: PropTypes.node }
