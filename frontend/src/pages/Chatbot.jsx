import React, { useState, useEffect, useCallback, useRef } from 'react'
import { api } from '../api.js'
import { Card, CardH, Button, Field, Input, Select, Badge, Alert, Empty, Loading } from '../components/ui.jsx'

export default function Chatbot({ adminKey, tenantId, toast }) {
  const [keys, setKeys] = useState(null)
  const [roles, setRoles] = useState([])
  const [apiKey, setApiKey] = useState('')
  const [mode, setMode] = useState('chat')
  const [userId, setUserId] = useState('u1')
  const [jwt, setJwt] = useState('')
  const [mintRole, setMintRole] = useState('')
  const [input, setInput] = useState('total sales per region?')
  const [log, setLog] = useState([])
  const [busy, setBusy] = useState(false)
  const endRef = useRef()

  const load = useCallback(async () => {
    if (!tenantId) return
    const [k, r] = await Promise.all([
      api.get(`/admin/tenants/${tenantId}/apikeys`, adminKey),
      api.get(`/admin/tenants/${tenantId}/roles`, adminKey)])
    if (k.ok) { setKeys(k.data.filter(x => x.active)); setApiKey('') }
    if (r.ok) { setRoles(r.data); setMintRole(p => p || r.data[0]?.name || '') }
  }, [adminKey, tenantId])
  useEffect(() => { setKeys(null); setLog([]); load() }, [tenantId])

  const mint = async () => {
    if (!mintRole) return toast.warn('Pick a role to mint')
    const r = await api.post('/admin/mint-jwt', adminKey, { tenant_id: tenantId, user_id: userId || 'demo-user', role: mintRole })
    if (r.ok) { setJwt(r.data.token); toast.ok(`Minted JWT for role "${mintRole}"`) }
    else toast.bad(r.error.message)
  }
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [log])

  const send = async () => {
    if (!apiKey) return toast.warn('Select an API key (issue one on API Keys)')
    if (!input.trim()) return
    const text = input
    setLog(l => [...l, { who: 'me', text, mode }])
    setInput(''); setBusy(true)
    const path = mode === 'chat' ? '/v1/chat' : mode === 'query' ? '/v1/query' : '/v1/retrieve'
    const body = mode === 'chat' ? { message: text, user_id: userId || undefined }
      : mode === 'query' ? { sql: text } : { query: text }
    const r = await api.consumer(path, apiKey, body, jwt || undefined)
    setBusy(false)
    setLog(l => [...l, { who: 'bot', mode, ok: r.ok, data: r.data, error: r.error }])
  }

  if (keys === null) return <Loading />
  return (
    <>
      <p className="page-intro">A consumer app talks to the platform with only an API key. Every answer is governed,
        grounded and cited — try a data question, a documentation question, or raw SQL.</p>

      <Card>
        <div className="grid cols-3">
          <Field label="API key (consumer identity + role)">
            {keys.length === 0 ? <Alert tone="warn">No active keys — issue one on API Keys.</Alert>
              : <Input type="password" value={apiKey} onChange={e => setApiKey(e.target.value)}
                  placeholder={`paste secret (${keys.map(k => k.key_prefix).filter(Boolean).join(' · ')})`} />}
          </Field>
          <Field label="Mode"><Select value={mode} onChange={e => setMode(e.target.value)}
            options={[{ value: 'chat', label: 'Agent chat (DB+Docs+Memory)' }, { value: 'query', label: 'Raw SQL' }, { value: 'retrieve', label: 'Doc retrieval' }]} /></Field>
          <Field label="End-user id (for memory)"><Input value={userId} onChange={e => setUserId(e.target.value)} placeholder="optional" /></Field>
        </div>
        <div className="hint">Try: <code>total sales per region?</code> · <code>what is the refund policy?</code> · raw SQL <code>SELECT * FROM internal_costs</code> (blocked by policy)</div>
        <div className="hr" />
        <div className="row" style={{ flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <Field label="End-user JWT — Pattern B (overrides the key's role, verified server-side)">
            <Input value={jwt} onChange={e => setJwt(e.target.value)} placeholder="paste a signed JWT, or mint one →" style={{ minWidth: 300 }} />
          </Field>
          <Field label="Mint demo JWT as role">
            <Select value={mintRole} onChange={e => setMintRole(e.target.value)} options={roles.map(r => r.name)} />
          </Field>
          <Button onClick={mint}>Mint</Button>
          {jwt && <Button variant="ghost" onClick={() => setJwt('')}>Clear</Button>}
        </div>
        <div className="hint">The JWT's verified role drives governance. Production uses the tenant's JWKS/public key, issuer, and audience; this mint button is a local/dev helper.</div>
      </Card>

      <Card>
        <CardH title="Conversation" right={log.length > 0 && <Button variant="ghost sm" onClick={() => setLog([])}>Clear</Button>} />
        {log.length === 0 ? <Empty icon="💬" title="Ask something">Pick a mode and send a message.</Empty> : (
          <div className="chat-log">
            {log.map((m, i) => m.who === 'me'
              ? <div className="msg me" key={i}><div className="av">U</div><div className="bubble"><div className="answer">{m.text}</div><Badge tone="gray">{m.mode}</Badge></div></div>
              : <div className="msg bot" key={i}><div className="av">A</div><div className="bubble" style={{ minWidth: 280 }}><BotMsg m={m} /></div></div>)}
            <div ref={endRef} />
          </div>)}
        <div className="row" style={{ marginTop: 14, flexWrap: 'nowrap' }}>
          <Input value={input} onChange={e => setInput(e.target.value)} onKeyDown={e => e.key === 'Enter' && !busy && send()}
            placeholder={mode === 'query' ? 'SELECT … ' : 'Ask a question…'} />
          <Button variant="primary" loading={busy} onClick={send}>Send</Button>
        </div>
      </Card>
    </>
  )
}

function ResultTable({ columns, rows }) {
  if (!columns?.length) return null
  return <div className="table-wrap" style={{ marginTop: 8 }}><table>
    <thead><tr>{columns.map(c => <th key={c}>{c}</th>)}</tr></thead>
    <tbody>{(rows || []).slice(0, 50).map((r, i) => <tr key={i}>{r.map((v, j) => <td key={j}>{String(v)}</td>)}</tr>)}</tbody>
  </table></div>
}

function BotMsg({ m }) {
  if (!m.ok) return <Alert tone="bad"><b>{m.error?.code || 'Error'}:</b> {m.error?.message}</Alert>
  const d = m.data

  if (m.mode === 'query') return (
    <div>
      <Badge tone="ok" dot>allowed · {d.role}</Badge>
      <h4 style={{ margin: '8px 0 4px' }}>Governed SQL</h4>
      <pre style={{ margin: 0 }}>{d.safe_sql}</pre>
      <ResultTable columns={d.columns} rows={d.rows} />
    </div>)

  if (m.mode === 'retrieve') return (
    <div>
      <Badge tone="brand">{(d.results || []).length} results · {d.role}</Badge>
      {(d.results || []).map((h, i) => (
        <div key={i} className="evidence"><span className="cite">📄 {h.citation} · {h.score}</span>
          <div style={{ marginTop: 4 }}>{h.text.slice(0, 300)}…</div></div>))}
      {(!d.results || !d.results.length) && <div className="muted" style={{ marginTop: 6 }}>No matching documents for this role.</div>}
    </div>)

  // chat (agent)
  if (d.blocked) return (
    <div><Alert tone="bad"><b>Blocked by gatekeeper.</b> {(d.gatekeeper?.reasons || []).join('; ')}</Alert>
      <div className="pill-row"><Badge tone="warn">intent: {d.gatekeeper?.intent}</Badge><Badge tone="gray">{d.gatekeeper?.engine}</Badge></div></div>)

  const g = d.gatekeeper || {}, r = d.router || {}, v = d.verifier || {}
  return (
    <div>
      <div className="answer">{d.answer}</div>

      <div className="pill-row">
        <Badge tone="blue">intent: {g.intent}</Badge>
        <Badge tone="gray">complexity: {r.complexity}</Badge>
        <Badge tone="brand">model: {r.purpose}</Badge>
        <Badge tone="gray">gatekeeper: {g.engine}</Badge>
        {v.ok ? <Badge tone="ok" dot>verified</Badge> : <Badge tone="warn">verifier: {(v.issues || []).length} note(s)</Badge>}
      </div>

      {(d.plan || []).length > 0 && <div className="hint" style={{ marginTop: 6 }}>tools used: {d.plan.join(' → ')}</div>}

      {d.sql && (d.sql.ok
        ? <div className="evidence"><b>📊 SQL (validated)</b>
          <pre style={{ margin: '6px 0' }}>{d.sql.safe_sql}</pre>
          <ResultTable columns={d.sql.columns} rows={d.sql.rows} /></div>
        : <div className="evidence"><Alert tone="warn"><b>{d.sql.code}:</b> {d.sql.error}</Alert></div>)}

      {(d.citations || []).length > 0 && <div className="evidence"><b>📄 Sources</b><div className="pill-row">
        {d.documents.map((doc, i) => <span key={i} className="cite" title={doc.text}>{doc.citation} · {doc.score}</span>)}</div></div>}

      {(d.recalled_memory || []).length > 0 && <div className="evidence"><b>🧠 Recalled memory</b>
        <div className="pill-row">{d.recalled_memory.map((mm, i) => <span key={i} className="chip">{mm.text}</span>)}</div></div>}

      {!v.ok && (v.issues || []).length > 0 && <div className="evidence"><b>⚠ Verifier notes</b>
        <ul style={{ margin: '4px 0 0 18px' }}>{v.issues.map((s, i) => <li key={i} style={{ fontSize: 12 }}>{s}</li>)}</ul></div>}

      {d.usage && (d.usage.prompt_tokens + d.usage.completion_tokens > 0) &&
        <div className="hint" style={{ marginTop: 8 }}>{d.usage.model} · {d.usage.prompt_tokens + d.usage.completion_tokens} tokens · ${(d.usage.cost || 0).toFixed(5)}</div>}
    </div>)
}
