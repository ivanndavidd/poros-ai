import React, { useState, useEffect, useCallback } from 'react'
import { api } from '../api.js'
import { Card, CardH, Button, Field, Input, Tabs, Alert, Loading } from '../components/ui.jsx'

export default function Developers({ adminKey, tenantId, toast }) {
  const [keys, setKeys] = useState(null)
  const [key, setKey] = useState('')
  const [tab, setTab] = useState('curl')

  const load = useCallback(async () => {
    if (!tenantId) return
    const r = await api.get(`/admin/tenants/${tenantId}/apikeys`, adminKey)
    if (r.ok) { setKeys(r.data.filter(k => k.active)); setKey('') }
  }, [adminKey, tenantId])
  useEffect(() => { setKeys(null); load() }, [tenantId])

  const K = key || '<API_KEY>'
  const base = api.base
  const snippets = {
    curl: `curl -X POST ${base}/v1/chat \\
  -H "X-API-Key: ${K}" \\
  -H "Content-Type: application/json" \\
  -d '{"message": "total sales per region?", "user_id": "u1"}'`,
    js: `import { AdminClient } from "./sdk/js/client.js"

const client = new AdminClient("${base}", "${K}")
const res = await client.chat("total sales per region?", { userId: "u1" })
console.log(res.answer, res.citations)`,
    py: `from sdk.python.client import Client

client = Client("${base}", "${K}")
res = client.chat("total sales per region?", user_id="u1")
print(res["answer"], res["citations"])`,
  }
  const copy = () => { navigator.clipboard?.writeText(snippets[tab]); toast.ok('Snippet copied') }

  if (keys === null) return <Loading />
  return (
    <>
      <p className="page-intro">Everything a consumer app needs to integrate. The API is uniform and versioned under
        <code>/v1</code>; every call is governed, metered and logged.</p>

      <div className="grid cols-3">
        <Card><div className="stat"><div className="k">Base URL</div><div className="v" style={{ fontSize: 16 }}><code>{base}</code></div></div></Card>
        <Card><div className="stat"><div className="k">OpenAPI / Swagger</div><div className="v" style={{ fontSize: 16 }}><a href={base + '/docs'} target="_blank" rel="noreferrer">/docs ↗</a></div></div></Card>
        <Card><div className="stat"><div className="k">SDKs</div><div className="v" style={{ fontSize: 16 }}>JS · Python</div><div className="d">in <code>/sdk</code></div></div></Card>
      </div>

      <Card style={{ marginTop: 14 }}>
        <CardH title="Quickstart" right={<Button variant="ghost sm" onClick={copy}>Copy</Button>} />
        {keys.length === 0
          ? <Alert tone="warn">No active API keys. Create one on the API Keys page.</Alert>
          : <Field label="API key secret (paste value from create/recreate)"><Input type="password" value={key}
              onChange={e => setKey(e.target.value)} placeholder={keys.map(k => k.key_prefix).filter(Boolean).join(' · ')} /></Field>}
        <div style={{ height: 12 }} />
        <Tabs tabs={[{ value: 'curl', label: 'cURL' }, { value: 'js', label: 'JavaScript' }, { value: 'py', label: 'Python' }]} value={tab} onChange={setTab} />
        <pre style={{ background: '#0f1730', color: '#e7ecf7', padding: 14, borderRadius: 10, overflow: 'auto', fontSize: 12.5 }}>{snippets[tab]}</pre>
      </Card>

      <Card>
        <CardH title="Endpoints" />
        <div className="table-wrap"><table>
          <thead><tr><th>Method</th><th>Path</th><th>Purpose</th></tr></thead>
          <tbody>
            <tr><td><code>POST</code></td><td><code>/v1/chat</code></td><td>Agent pipeline: gatekeeper → router → DB+Docs+Memory → verified, cited answer</td></tr>
            <tr><td><code>POST</code></td><td><code>/v1/query</code></td><td>Run a raw SQL SELECT (governed: allowlist + RLAC/CLAC)</td></tr>
            <tr><td><code>POST</code></td><td><code>/v1/retrieve</code></td><td>Doc-RAG retrieval scoped to the key's role; returns citations</td></tr>
          </tbody>
        </table></div>
      </Card>
    </>
  )
}
