import React, { useState, useEffect, useCallback } from 'react'
import PropTypes from 'prop-types'
import { api } from '../api.js'
import { Card, CardH, Button, Field, Input, Select, Badge, Empty, Loading } from '../components/ui.jsx'
import { toastType } from '../propTypes.js'

export default function ApiKeys({ adminKey, tenantId, toast }) {
  const [keys, setKeys] = useState(null)
  const [roles, setRoles] = useState([])
  const [role, setRole] = useState('')
  const [label, setLabel] = useState('')
  const [limit, setLimit] = useState('')
  const [busy, setBusy] = useState(false)
  const [issued, setIssued] = useState('')

  const load = useCallback(async () => {
    if (!tenantId) return
    const [k, r, rl] = await Promise.all([
      api.get(`/admin/tenants/${tenantId}/apikeys`, adminKey),
      api.get(`/admin/tenants/${tenantId}/roles`, adminKey),
      api.get(`/admin/tenants/${tenantId}/rate-limit`, adminKey)])
    if (k.ok) setKeys(k.data)
    if (r.ok) { setRoles(r.data); if (r.data[0]) setRole(p => p || r.data[0].name) }
    if (rl.ok) setLimit(rl.data.per_min)
  }, [adminKey, tenantId])
  useEffect(() => { setKeys(null); load() }, [tenantId])

  const issue = async () => {
    if (!role) return toast.warn('Create a role first')
    setBusy(true)
    const r = await api.post('/admin/apikeys', adminKey, { tenant_id: tenantId, role, label })
    setBusy(false)
    if (r.ok) { setIssued(r.data.key); navigator.clipboard?.writeText(r.data.key); toast.ok('API key issued — save it now'); setLabel(''); load() } else toast.bad(r.error.message)
  }
  const revoke = async (key) => {
    if (!confirm('Revoke this API key? Consumers using it will lose access.')) return
    const r = await api.del(`/admin/apikeys/${key}`, adminKey)
    if (r.ok) { toast.ok('Key revoked'); load() } else toast.bad(r.error.message)
  }
  const rotate = async (key) => {
    if (!confirm('Rotate this key? A new key is issued (same role/label) and this one is revoked immediately.')) return
    const r = await api.post(`/admin/apikeys/${key}/rotate`, adminKey, {})
    if (r.ok) { setIssued(r.data.key); navigator.clipboard?.writeText(r.data.key); toast.ok('Key rotated — save it now'); load() } else toast.bad(r.error.message)
  }
  const saveLimit = async () => {
    const r = await api.post('/admin/rate-limit', adminKey, { tenant_id: tenantId, per_min: parseInt(limit) || 0 })
    if (r.ok) toast.ok('Rate limit saved')
    else toast.bad(r.error.message)
  }
  if (keys === null) return <Loading />
  return (
    <>
      <p className="page-intro">One API key = the identity of a consumer app for this tenant + a role.
        Permissions live server-side as policy; data access is enforced at query time (RLAC/CLAC).</p>
      <Card>
        <CardH title="Issue API key" />
        {issued && <Alert tone="warn">Save once: <code>{issued}</code> <Button variant="ghost sm" onClick={() => navigator.clipboard?.writeText(issued)}>copy</Button></Alert>}
        <div className="inline-form">
          <Field label="Role"><Select value={role} onChange={e => setRole(e.target.value)}
            options={roles.map(r => r.name)} /></Field>
          <Field label="Label"><Input placeholder="e.g. CS Chatbot" value={label} onChange={e => setLabel(e.target.value)} /></Field>
          <Button variant="primary" loading={busy} onClick={issue}>+ Issue key</Button>
        </div>
      </Card>

      <Card>
        <CardH title="Rate limit" sub="Max consumer requests per minute, per API key (429 when exceeded)." />
        <div className="inline-form">
          <Field label="Requests / minute"><Input type="number" min="1" value={limit} onChange={e => setLimit(e.target.value)} /></Field>
          <Button onClick={saveLimit}>Save limit</Button>
        </div>
      </Card>

      <Card>
        <CardH title="API keys" sub={`${keys.length} total`} />
        {keys.length === 0 ? <Empty icon="🔑" title="No keys yet">Issue one above to let a consumer app connect.</Empty> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Key</th><th>Role</th><th>Label</th><th>Status</th><th /></tr></thead>
              <tbody>{keys.map(k => (
                <tr key={k.key}>
                  <td><code>{k.key_prefix || 'stored as hash'}</code></td>
                  <td><Badge tone="blue">{k.role}</Badge></td>
                  <td>{k.label || <span className="muted">—</span>}</td>
                  <td>{k.active ? <Badge tone="ok" dot>active</Badge> : <Badge tone="gray">revoked</Badge>}</td>
                  <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>{k.active && <>
                    <Button variant="ghost sm" onClick={() => rotate(k.key)}>Rotate</Button>
                    <Button variant="danger sm" onClick={() => revoke(k.key)}>Revoke</Button></>}</td>
                </tr>))}</tbody>
            </table>
          </div>)}
      </Card>
    </>
  )
}

ApiKeys.propTypes = {
  adminKey: PropTypes.string.isRequired,
  tenantId: PropTypes.string,
  toast: toastType.isRequired,
}
