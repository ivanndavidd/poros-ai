import React, { useState, useEffect, useCallback } from 'react'
import PropTypes from 'prop-types'
import { api } from '../api.js'
import { Card, CardH, Button, Field, Input, Select, Badge, Alert, Empty, Loading } from '../components/ui.jsx'
import { toastType } from '../propTypes.js'

export default function Memory({ adminKey, tenantId, toast }) {
  const [mems, setMems] = useState(null)
  const [filter, setFilter] = useState('')
  const [uid, setUid] = useState('')
  const [kind, setKind] = useState('fact')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    if (!tenantId) return
    const q = filter ? `?user_id=${encodeURIComponent(filter)}` : ''
    const r = await api.get(`/admin/tenants/${tenantId}/memories${q}`, adminKey)
    if (r.ok) setMems(r.data)
  }, [adminKey, tenantId, filter])
  useEffect(() => { setMems(null); load() }, [tenantId])

  const add = async () => {
    if (!uid.trim() || !text.trim()) return toast.warn('User id and text required')
    setBusy(true)
    const r = await api.post('/admin/memories', adminKey, { tenant_id: tenantId, user_id: uid.trim(), kind, text: text.trim() })
    setBusy(false)
    if (r.ok) {
      toast.ok('Memory stored')
      setText('')
      load()
    } else {
      toast.bad(r.error.message)
    }
  }
  const remove = async (id) => {
    const r = await api.del(`/admin/tenants/${tenantId}/memories/${id}`, adminKey)
    if (r.ok) {
      toast.ok('Deleted')
      load()
    } else {
      toast.bad(r.error.message)
    }
  }

  if (mems === null) return <Loading />
  const tone = { fact: 'blue', preference: 'brand', context: 'gray' }
  return (
    <>
      <p className="page-intro">Per-(tenant, user) memory used by the chat agent. It is namespaced by user and
        <b> never crosses tenants</b> — this page lets you audit and clear it.</p>
      <Alert tone="info">Isolation: every entry below belongs to <b>{tenantId}</b> only. Other tenants cannot read it.</Alert>

      <Card>
        <CardH title="Add memory" />
        <div className="inline-form">
          <Field label="User id"><Input placeholder="e.g. u1" value={uid} onChange={e => setUid(e.target.value)} /></Field>
          <Field label="Kind"><Select value={kind} onChange={e => setKind(e.target.value)} options={['fact', 'preference', 'context']} /></Field>
          <Field label="Text"><Input placeholder="e.g. Manages the Jakarta region" value={text} onChange={e => setText(e.target.value)} /></Field>
          <Button variant="primary" loading={busy} onClick={add}>+ Store</Button>
        </div>
      </Card>

      <Card>
        <CardH title="Stored memory" sub={`${mems.length} entries`}
          right={<div className="row"><Input placeholder="filter by user id" value={filter} onChange={e => setFilter(e.target.value)} onKeyDown={e => e.key === 'Enter' && load()} style={{ width: 160 }} /><Button variant="ghost sm" onClick={load}>Filter</Button></div>} />
        {mems.length === 0 ? <Empty icon="🧠" title="No memory yet">Memory is written via /v1/chat (with a user_id) or added here.</Empty> : (
          <div className="table-wrap"><table>
            <thead><tr><th>User</th><th>Kind</th><th>Text</th><th>Created</th><th /></tr></thead>
            <tbody>{mems.map(m => (
              <tr key={m.id}>
                <td><code>{m.user_id}</code></td>
                <td><Badge tone={tone[m.kind] || 'gray'}>{m.kind}</Badge></td>
                <td>{m.text}</td>
                <td className="muted">{m.created}</td>
                <td style={{ textAlign: 'right' }}><Button variant="danger sm" onClick={() => remove(m.id)}>Delete</Button></td>
              </tr>))}</tbody>
          </table></div>)}
      </Card>
    </>
  )
}

Memory.propTypes = {
  adminKey: PropTypes.string.isRequired,
  tenantId: PropTypes.string,
  toast: toastType.isRequired,
}
