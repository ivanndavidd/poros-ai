import React, { useState, useEffect, useCallback } from 'react'
import { api } from '../api.js'
import { Card, CardH, Button, Field, Input, Select, Check, Badge, Chip, Empty, Loading } from '../components/ui.jsx'

const ALL_SKILLS = ['run_sql', 'search_docs', 'recall_memory', 'generate_report']
const blank = { name: '', filters: [{ col: '', val: '' }], canPii: false, denied: [], restrictSkills: false, skills: [] }

export default function Roles({ adminKey, tenantId, toast }) {
  const [roles, setRoles] = useState(null)
  const [cols, setCols] = useState([])
  const [f, setF] = useState(blank)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    if (!tenantId) return
    const [r, d] = await Promise.all([
      api.get(`/admin/tenants/${tenantId}/roles`, adminKey),
      api.get(`/admin/tenants/${tenantId}/datasources`, adminKey)])
    if (r.ok) setRoles(r.data)
    if (d.ok) {
      const set = new Set()
      d.data.forEach(ds => Object.values(ds.mdl?.tables || {}).forEach(cs => cs.forEach(c => set.add(c))))
      setCols([...set].sort((a, b) => String(a).localeCompare(String(b))))
    }
  }, [adminKey, tenantId])
  useEffect(() => { setRoles(null); load() }, [tenantId])

  const edit = (r) => setF({
    name: r.name,
    filters: Object.entries(r.policy.row_filters || {}).map(([col, val]) => ({ col, val })).concat([{ col: '', val: '' }]),
    canPii: !!r.policy.can_see_pii, denied: r.policy.denied_columns || [],
    restrictSkills: Array.isArray(r.policy.skills), skills: r.policy.skills || [],
  })
  const setFilter = (i, k, v) => setF(s => { const filters = s.filters.map((x, j) => j === i ? { ...x, [k]: v } : x); if (i === filters.length - 1 && (filters[i].col || filters[i].val)) filters.push({ col: '', val: '' }); return { ...s, filters } })
  const toggleDenied = (c) => setF(s => ({ ...s, denied: s.denied.includes(c) ? s.denied.filter(x => x !== c) : [...s.denied, c] }))
  const toggleSkill = (s2) => setF(s => ({ ...s, skills: s.skills.includes(s2) ? s.skills.filter(x => x !== s2) : [...s.skills, s2] }))

  const save = async () => {
    if (!f.name.trim()) return toast.warn('Role name required')
    const row_filters = {}; f.filters.forEach(x => { if (x.col.trim() && x.val.trim()) row_filters[x.col.trim()] = x.val.trim() })
    const policy = { row_filters, can_see_pii: f.canPii, denied_columns: f.denied }
    if (f.restrictSkills) policy.skills = f.skills
    setBusy(true)
    const r = await api.post('/admin/roles', adminKey, { tenant_id: tenantId, name: f.name.trim(), policy })
    setBusy(false)
    if (r.ok) { toast.ok('Role saved'); setF(blank); load() } else toast.bad(r.error.message)
  }
  const remove = async (name) => {
    if (!confirm(`Delete role "${name}"?`)) return
    const r = await api.del(`/admin/tenants/${tenantId}/roles/${name}`, adminKey)
    if (r.ok) { toast.ok('Role deleted'); load() } else toast.bad(r.error.message)
  }

  if (roles === null) return <Loading />
  return (
    <>
      <p className="page-intro">Roles define server-side policy: row filters (RLAC), PII/column access (CLAC),
        and which agent tools the role may use. The platform enforces these at query time — consumers never pick their own data.</p>

      <Card>
        <CardH title={roles.some(r => r.name === f.name) ? `Edit role · ${f.name}` : 'Create / edit role'}
          right={f !== blank && <Button variant="ghost sm" onClick={() => setF(blank)}>Clear</Button>} />
        <div className="grid cols-2">
          <Field label="Role name"><Input placeholder="e.g. analyst_jakarta" value={f.name} onChange={e => setF(s => ({ ...s, name: e.target.value }))} /></Field>
          <Field label="PII access (CLAC)"><Check label="May see PII columns" checked={f.canPii} onChange={e => setF(s => ({ ...s, canPii: e.target.checked }))} /></Field>
        </div>

        <div className="field" style={{ marginTop: 12 }}>
          <label>Row filters (RLAC) — applied as <code>WHERE col = value</code> when the column exists</label>
          {f.filters.map((x, i) => (
            <div className="row" key={i} style={{ flexWrap: 'nowrap' }}>
              <Input placeholder="column (e.g. region)" value={x.col} onChange={e => setFilter(i, 'col', e.target.value)} list="colopts" />
              <span className="muted">=</span>
              <Input placeholder="value (e.g. Jakarta)" value={x.val} onChange={e => setFilter(i, 'val', e.target.value)} />
            </div>))}
          <datalist id="colopts">{cols.map(c => <option key={c} value={c} />)}</datalist>
        </div>

        <div className="field" style={{ marginTop: 12 }}>
          <label>Denied columns (CLAC) — explicitly block, in addition to detected PII</label>
          {cols.length === 0 ? <span className="hint">Add a data source to populate columns.</span> :
            <div className="pill-row">{cols.map(c => <Chip key={c} onClick={() => toggleDenied(c)} active={f.denied.includes(c)}>{c}</Chip>)}</div>}
        </div>

        <div className="field" style={{ marginTop: 12 }}>
          <Check label="Restrict agent tools for this role" checked={f.restrictSkills} onChange={e => setF(s => ({ ...s, restrictSkills: e.target.checked }))} />
          {f.restrictSkills && <div className="pill-row">{ALL_SKILLS.map(s => <Chip key={s} onClick={() => toggleSkill(s)} active={f.skills.includes(s)}>{s}</Chip>)}</div>}
          {!f.restrictSkills && <span className="hint">All tools allowed (run_sql, search_docs, recall_memory, generate_report).</span>}
        </div>

        <div className="row end" style={{ marginTop: 14 }}><Button variant="primary" loading={busy} onClick={save}>Save role</Button></div>
      </Card>

      <Card>
        <CardH title="Roles" sub={`${roles.length} total`} />
        {roles.length === 0 ? <Empty icon="🛡️" title="No roles yet">Create one above.</Empty> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Role</th><th>Row filter</th><th>PII</th><th>Denied cols</th><th>Tools</th><th /></tr></thead>
              <tbody>{roles.map(r => (
                <tr key={r.id}>
                  <td style={{ fontWeight: 600 }}>{r.name}</td>
                  <td><code>{Object.keys(r.policy.row_filters || {}).length ? JSON.stringify(r.policy.row_filters) : '—'}</code></td>
                  <td>{r.policy.can_see_pii ? <Badge tone="warn">allowed</Badge> : <Badge tone="ok">blocked</Badge>}</td>
                  <td>{(r.policy.denied_columns || []).length ? r.policy.denied_columns.join(', ') : <span className="muted">—</span>}</td>
                  <td>{Array.isArray(r.policy.skills) ? r.policy.skills.map(s => <Badge key={s} tone="gray">{s}</Badge>) : <Badge tone="blue">all</Badge>}</td>
                  <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                    <Button variant="ghost sm" onClick={() => edit(r)}>Edit</Button>
                    <Button variant="danger sm" onClick={() => remove(r.name)}>Delete</Button></td>
                </tr>))}</tbody>
            </table>
          </div>)}
      </Card>
    </>
  )
}
