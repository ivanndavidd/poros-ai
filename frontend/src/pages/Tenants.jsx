import React, { useState } from 'react'
import { api } from '../api.js'
import { Card, CardH, Button, Field, Input, Badge, Empty } from '../components/ui.jsx'

export default function Tenants({ adminKey, tenants, tenantId, setTenantId, reload, toast }) {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)

  const create = async () => {
    if (!name.trim()) return
    setBusy(true)
    const r = await api.post('/admin/tenants', adminKey, { name: name.trim() })
    setBusy(false)
    if (r.ok) { toast.ok('Tenant created'); setName(''); reload() }
    else toast.bad(r.error.message)
  }
  const remove = async (t) => {
    if (!confirm(`Delete tenant "${t.name}" and all its datasources, roles, docs and memory?`)) return
    const r = await api.del(`/admin/tenants/${t.id}`, adminKey)
    if (r.ok) { toast.ok('Tenant deleted'); reload() } else toast.bad(r.error.message)
  }

  return (
    <>
      <p className="page-intro">A tenant is an isolated customer of the platform. Each gets its own data sources,
        models, roles, API keys, documents and memory — fully separated server-side.</p>
      <Card>
        <CardH title="Create tenant" />
        <div className="inline-form">
          <Field label="Tenant name"><Input placeholder="e.g. Globex Inc." value={name}
            onChange={e => setName(e.target.value)} onKeyDown={e => e.key === 'Enter' && create()} /></Field>
          <Button variant="primary" loading={busy} onClick={create}>+ Create tenant</Button>
        </div>
      </Card>

      <Card>
        <CardH title="Tenants" sub={`${tenants.length} total`} />
        {tenants.length === 0 ? <Empty icon="🏢" title="No tenants yet">Create your first tenant above.</Empty> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Name</th><th>ID</th><th>Created</th><th style={{ width: 1 }} /></tr></thead>
              <tbody>{tenants.map(t => (
                <tr key={t.id} className={t.id === tenantId ? 'active-row' : ''}>
                  <td style={{ fontWeight: 600 }}>{t.name}</td>
                  <td><code>{t.id}</code></td>
                  <td className="muted">{t.created}</td>
                  <td><div className="row" style={{ flexWrap: 'nowrap', justifyContent: 'flex-end' }}>
                    {t.id === tenantId ? <Badge tone="brand" dot>active</Badge>
                      : <Button variant="ghost sm" onClick={() => setTenantId(t.id)}>Select</Button>}
                    <Button variant="danger sm" onClick={() => remove(t)}>Delete</Button>
                  </div></td>
                </tr>))}</tbody>
            </table>
          </div>)}
      </Card>
    </>
  )
}
