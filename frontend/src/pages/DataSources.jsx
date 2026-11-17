import React, { useState, useEffect, useCallback } from 'react'
import { api } from '../api.js'
import { Card, CardH, Button, Field, Input, Select, Badge, Chip, Check, Alert, Empty, Loading } from '../components/ui.jsx'

const TYPE_OPTS = [
  { value: 'duckdb', label: 'DuckDB (file)' }, { value: 'postgres', label: 'PostgreSQL' },
  { value: 'mysql', label: 'MySQL' }, { value: 'sqlserver', label: 'SQL Server' },
  { value: 'bigquery', label: 'Google BigQuery' }, { value: 'docs', label: 'Documentation (doc-RAG)' },
]

export default function DataSources({ adminKey, tenantId, toast }) {
  const [specs, setSpecs] = useState(null)
  const [list, setList] = useState(null)
  const [type, setType] = useState('duckdb')
  const [name, setName] = useState('')
  const [conn, setConn] = useState({})
  const [test, setTest] = useState(null)
  const [busy, setBusy] = useState(false)

  const loadList = useCallback(async () => {
    if (!tenantId) return
    const r = await api.get(`/admin/tenants/${tenantId}/datasources`, adminKey)
    if (r.ok) setList(r.data)
  }, [adminKey, tenantId])
  useEffect(() => { (async () => { const s = await api.get('/admin/connector-specs', adminKey); if (s.ok) setSpecs(s.data) })() }, [])
  useEffect(() => { setList(null); loadList() }, [tenantId])
  useEffect(() => { setConn({}); setTest(null) }, [type])

  const fields = specs?.[type]?.fields || []
  const setField = (k, v) => setConn(c => ({ ...c, [k]: v }))
  const buildConn = () => {
    const out = {}
    for (const f of fields) {
      let v = conn[f.key]
      if (v == null || v === '') continue
      if (f.json) { try { v = JSON.parse(v) } catch { throw new Error(`${f.label} must be valid JSON`) } }
      out[f.key] = v
    }
    return out
  }

  const runTest = async () => {
    setBusy(true); setTest(null)
    try {
      const r = await api.post('/admin/test-connection', adminKey, { tenant_id: tenantId, type, conn: buildConn() })
      setTest(r.ok ? { ok: true, ...r.data } : { ok: false, msg: r.error.message })
    } catch (e) { setTest({ ok: false, msg: e.message }) }
    setBusy(false)
  }
  const create = async () => {
    if (!name.trim()) return toast.warn('Name required')
    setBusy(true)
    try {
      const r = await api.post('/admin/datasources', adminKey, { tenant_id: tenantId, type, name: name.trim(), conn: buildConn() })
      if (r.ok) { toast.ok(`Connected & scanned: ${r.data.mdl.allowed_tables?.join(', ') || 'docs source'}`); setName(''); setConn({}); setTest(null); loadList() }
      else toast.bad(r.error.message)
    } catch (e) { toast.bad(e.message) }
    setBusy(false)
  }

  if (list === null || specs === null) return <Loading />
  return (
    <>
      <p className="page-intro">Connect a database (auto-scanned into an MDL with PII detection) or a documentation
        source for doc-RAG. Review and enrich the model before consumers query it.</p>

      <Card>
        <CardH title="Add data source" />
        <div className="grid cols-2">
          <Field label="Type"><Select value={type} onChange={e => setType(e.target.value)} options={TYPE_OPTS} /></Field>
          <Field label="Name"><Input placeholder="e.g. Warehouse (prod)" value={name} onChange={e => setName(e.target.value)} /></Field>
        </div>
        {type === 'docs'
          ? <Alert tone="info">A documentation source holds uploaded SOPs/playbooks. Create it here, then upload files on the <b>Documents</b> page.</Alert>
          : <div className="grid cols-2" style={{ marginTop: 12 }}>
            {fields.map(f => (
              <Field key={f.key} label={f.label} hint={f.json ? 'paste JSON' : ''}>
                {f.json
                  ? <textarea rows={3} placeholder={f.placeholder} value={conn[f.key] || ''} onChange={e => setField(f.key, e.target.value)} />
                  : <Input type={f.secret ? 'password' : 'text'} placeholder={f.placeholder || ''} value={conn[f.key] || ''} onChange={e => setField(f.key, e.target.value)} />}
              </Field>))}
          </div>}
        {test && (test.ok
          ? <Alert tone="ok">Connection OK — {test.table_count ?? (test.tables?.length || 0)} table(s): {(test.tables || []).slice(0, 8).join(', ')}{(test.tables || []).length > 8 ? '…' : ''}</Alert>
          : <Alert tone="bad">{test.msg}</Alert>)}
        <div className="row end" style={{ marginTop: 14 }}>
          {type !== 'docs' && <Button loading={busy} onClick={runTest}>Test connection</Button>}
          <Button variant="primary" loading={busy} onClick={create}>Connect & scan</Button>
        </div>
      </Card>

      {list.length === 0 ? <Empty icon="🗄️" title="No data sources yet">Add a database or documentation source above.</Empty>
        : list.map(ds => <DSCard key={ds.id} ds={ds} adminKey={adminKey} toast={toast} reload={loadList} />)}
    </>
  )
}

function DSCard({ ds, adminKey, toast, reload }) {
  const isDocs = ds.type === 'docs'
  const tables = Object.keys(ds.mdl?.tables || {})
  const allCols = [...new Set(Object.values(ds.mdl?.tables || {}).flat())]
    .sort((a, b) => String(a).localeCompare(String(b)))
  const [allowed, setAllowed] = useState(ds.mdl?.allowed_tables || [])
  const [pii, setPii] = useState(ds.mdl?.pii_columns || [])
  const [busy, setBusy] = useState(false)
  const dirty = JSON.stringify([allowed, pii]) !== JSON.stringify([ds.mdl?.allowed_tables || [], ds.mdl?.pii_columns || []])

  const save = async () => {
    setBusy(true)
    const r = await api.patch(`/admin/datasources/${ds.id}/mdl`, adminKey, { allowed_tables: allowed, pii_columns: pii })
    setBusy(false)
    if (r.ok) { toast.ok('MDL updated'); reload() } else toast.bad(r.error.message)
  }
  const rescan = async () => {
    setBusy(true); const r = await api.post(`/admin/datasources/${ds.id}/rescan`, adminKey, {}); setBusy(false)
    if (r.ok) { toast.ok('Re-scanned (kept your edits)'); reload() } else toast.bad(r.error.message)
  }
  const remove = async () => { if (!confirm(`Delete data source "${ds.name}"?`)) return; const r = await api.del(`/admin/datasources/${ds.id}`, adminKey); if (r.ok) { toast.ok('Deleted'); reload() } else toast.bad(r.error.message) }
  const toggle = (arr, set, v) => set(arr.includes(v) ? arr.filter(x => x !== v) : [...arr, v])

  return (
    <Card>
      <CardH title={<span>{ds.name} <Badge tone="blue">{ds.type}</Badge></span>}
        sub={ds.id}
        right={<div className="row">{!isDocs && <Button variant="ghost sm" loading={busy} onClick={rescan}>Rescan</Button>}
          <Button variant="danger sm" onClick={remove}>Delete</Button></div>} />
      {isDocs ? <Alert tone="info">Documentation source — manage files on the Documents page.</Alert> : (
        <>
          <div className="field">
            <label>Allowlisted tables (only these are queryable)</label>
            <div className="pill-row">{tables.map(t => <Chip key={t} onClick={() => toggle(allowed, setAllowed, t)} active={allowed.includes(t)}>{t}</Chip>)}</div>
            <span className="hint">Greyed-out tables are blocked by the Wren policy even if a query references them.</span>
          </div>
          <div className="field" style={{ marginTop: 12 }}>
            <label>PII columns (CLAC blocks these for roles without PII access)</label>
            <div className="pill-row">{allCols.map(c => <Chip key={c} onClick={() => toggle(pii, setPii, c)} active={pii.includes(c)}>{pii.includes(c) ? '🔒 ' : ''}{c}</Chip>)}</div>
          </div>
          {dirty && <div className="row end" style={{ marginTop: 12 }}><Button variant="primary" loading={busy} onClick={save}>Save MDL changes</Button></div>}
        </>)}
    </Card>
  )
}
