import React, { useState, useEffect, useCallback, useRef } from 'react'
import PropTypes from 'prop-types'
import { api } from '../api.js'
import { Card, CardH, Button, Field, Select, Chip, Badge, Alert, Empty, Loading } from '../components/ui.jsx'
import { toastType } from '../propTypes.js'

export default function Documents({ adminKey, tenantId, toast, go }) {
  const [dsList, setDsList] = useState(null)
  const [roles, setRoles] = useState([])
  const [dsid, setDsid] = useState('')
  const [docs, setDocs] = useState([])
  const [acl, setAcl] = useState([])
  const [busy, setBusy] = useState(false)
  const fileRef = useRef()

  const load = useCallback(async () => {
    if (!tenantId) return
    const [d, r] = await Promise.all([
      api.get(`/admin/tenants/${tenantId}/datasources`, adminKey),
      api.get(`/admin/tenants/${tenantId}/roles`, adminKey)])
    if (d.ok) { const docsSrc = d.data.filter(x => x.type === 'docs'); setDsList(docsSrc); setDsid(p => p || docsSrc[0]?.id || '') }
    if (r.ok) setRoles(r.data)
  }, [adminKey, tenantId])
  useEffect(() => { setDsList(null); load() }, [tenantId])

  const loadDocs = useCallback(async () => {
    if (!dsid) return setDocs([])
    const r = await api.get(`/admin/tenants/${tenantId}/documents?datasource_id=${dsid}`, adminKey)
    if (r.ok) setDocs(r.data)
  }, [adminKey, tenantId, dsid])
  useEffect(() => { loadDocs() }, [dsid])

  const upload = async () => {
    const file = fileRef.current?.files?.[0]
    if (!file) return toast.warn('Choose a file (.md .txt .pdf .docx)')
    const fd = new FormData()
    fd.append('tenant_id', tenantId); fd.append('datasource_id', dsid)
    fd.append('acl_roles', acl.join(',')); fd.append('file', file)
    setBusy(true)
    const r = await api.upload('/admin/documents', adminKey, fd)
    setBusy(false)
    if (r.ok) { toast.ok(`Ingested ${file.name}: ${r.data.n_chunks} chunks (${r.data.embedder})`); fileRef.current.value = ''; loadDocs() }
    else toast.bad(r.error.message)
  }
  const remove = async (id) => {
    if (!confirm('Delete this document and its chunks?')) return
    const r = await api.del(`/admin/tenants/${tenantId}/documents/${id}`, adminKey)
    if (r.ok) {
      toast.ok('Deleted')
      loadDocs()
    } else {
      toast.bad(r.error.message)
    }
  }
  const toggleAcl = (n) => setAcl(a => a.includes(n) ? a.filter(x => x !== n) : [...a, n])

  if (dsList === null) return <Loading />
  return (
    <>
      <p className="page-intro">Upload SOPs/playbooks to a documentation source. Files are chunked, embedded and stored
        per-tenant with role ACL. The agent retrieves them with citations.</p>

      {dsList.length === 0
        ? <Empty icon="📄" title="No documentation source yet">Create a data source of type <b>Documentation</b> first.
          <div style={{ marginTop: 12 }}><Button variant="primary" onClick={() => go('datasources')}>Go to Data Sources</Button></div></Empty>
        : <>
          <Card>
            <CardH title="Upload document" />
            <div className="grid cols-2">
              <Field label="Documentation source"><Select value={dsid} onChange={e => setDsid(e.target.value)}
                options={dsList.map(d => ({ value: d.id, label: d.name }))} /></Field>
              <Field label="File" hint=".md .txt always supported · .pdf/.docx need optional libs">
                <input className="input" type="file" ref={fileRef} accept=".md,.txt,.pdf,.docx" /></Field>
            </div>
            <div className="field" style={{ marginTop: 12 }}>
              <label>Role ACL (empty = visible to all roles in the tenant)</label>
              <div className="pill-row">{roles.map(r => <Chip key={r.id} onClick={() => toggleAcl(r.name)} active={acl.includes(r.name)}>{r.name}</Chip>)}</div>
            </div>
            <div className="row end" style={{ marginTop: 12 }}><Button variant="primary" loading={busy} onClick={upload}>Upload & embed</Button></div>
          </Card>

          <Card>
            <CardH title="Documents" sub={`${docs.length} in this source`} />
            {docs.length === 0 ? <Empty icon="📭" title="No documents yet">Upload one above.</Empty> : (
              <div className="table-wrap"><table>
                <thead><tr><th>File</th><th>Chunks</th><th>ACL</th><th>Uploaded</th><th /></tr></thead>
                <tbody>{docs.map(d => (
                  <tr key={d.id}>
                    <td style={{ fontWeight: 600 }}>{d.filename}</td>
                    <td>{d.n_chunks}</td>
                    <td>{(d.acl_roles || []).length ? d.acl_roles.map(r => <Badge key={r} tone="blue">{r}</Badge>) : <Badge tone="gray">all roles</Badge>}</td>
                    <td className="muted">{d.created}</td>
                    <td style={{ textAlign: 'right' }}><Button variant="danger sm" onClick={() => remove(d.id)}>Delete</Button></td>
                  </tr>))}</tbody>
              </table></div>)}
          </Card>
        </>}
    </>
  )
}

Documents.propTypes = {
  adminKey: PropTypes.string.isRequired,
  tenantId: PropTypes.string,
  toast: toastType.isRequired,
  go: PropTypes.func,
}
