// Consumer API documentation — how a consumer app integrates the governed /v1/* API.
// Tenant-aware: pick a tenant and every curl/SDK snippet is filled with that tenant's real
// API key + base URL, ready to copy. Read-only; touches no backend it doesn't already have.
import React, { useState, useEffect, useCallback } from 'react'
import { api } from '../api.js'
import { Icon } from '../tos.jsx'

// The consumer surface. Bodies double as request-shape docs (mirror models.py QueryIn/ChatIn/RetrieveIn).
const ENDPOINTS = [
  { method: 'POST', path: '/v1/query', title: 'Governed SQL', sse: false,
    desc: 'Run a read-only SELECT that passes governance (table allowlist + RLAC row filter + CLAC PII block).',
    body: { sql: 'SELECT region, SUM(amount) AS total FROM orders GROUP BY region', datasource_id: null } },
  { method: 'POST', path: '/v1/chat', title: 'Agent chat', sse: false,
    desc: 'Agent pipeline over Database + Documents + Memory → governed answer + SQL + citations.',
    body: { message: 'total sales per region?', user_id: 'u1', datasource_id: null } },
  { method: 'POST', path: '/v1/chat_stream', title: 'Agent chat (SSE)', sse: true,
    desc: 'Same as /chat but streams Server-Sent Events: repeated `status` events, then `done` with the full answer. To stop, abort the active stream request (there is no separate cancel endpoint).',
    body: { message: 'total sales per region?', user_id: 'u1' } },
  { method: 'POST', path: '/v1/feedback', title: 'Response feedback', sse: false,
    desc: 'Save or update an end user\'s rating for one answer, keyed by trace_id.',
    body: { trace_id: 'trace_id_from_chat_response', rating: 'unhelpful', reason: 'incorrect', comment: 'The total figure needs checking.' } },
  { method: 'POST', path: '/v1/retrieve', title: 'Doc-RAG retrieval', sse: false,
    desc: 'Retrieve relevant document chunks (scoped by role ACL) with citations.',
    body: { query: 'customer refund policy', top_k: 5 } },
]

const ERRORS = [
  ['401', 'JWT_REQUIRED', 'The Authorization: Bearer <JWT> header was not sent alongside X-API-Key.'],
  ['401', 'JWT_INVALID', 'Invalid JWT signature or the token has expired.'],
  ['401', 'JWT_VERIFY_UNAVAILABLE', 'The tenant\'s JWT verifier is not configured yet.'],
  ['401', 'JWT_ASYMMETRIC_REQUIRED', 'Production requires a JWKS/public key with an RS*/ES* algorithm.'],
  ['401', 'JWT_ISSUER_AUDIENCE_REQUIRED', 'Tenant issuer and audience are required in production.'],
  ['403', 'JWT_ROLE_MISSING', 'The token carries no "role" claim.'],
  ['403', 'ROLE_NOT_FOUND', 'The role in the JWT is not registered for this tenant.'],
  ['403', 'ROLE_TABLE_DENIED · POLICY_* · CLAC_*', 'Blocked by governance: table/column/PII outside the role\'s permissions.'],
  ['429', 'RATE_LIMITED', 'Per-minute rate limit exceeded; check the retry_after field.'],
  ['429', 'BUDGET_EXCEEDED', 'The tenant\'s monthly hard budget is spent; wait for the reset or raise the budget.'],
  ['400', 'NO_DATASOURCE · DATASOURCE_NOT_FOUND', 'The datasource does not exist or is not a SQL type.'],
  ['400/404', 'FEEDBACK_*', 'Invalid feedback payload, or the trace_id does not belong to this tenant.'],
]

function copyText(text, toast) {
  if (!navigator.clipboard) { toast.bad('Clipboard unavailable'); return }
  navigator.clipboard.writeText(text).then(() => toast.ok('Copied'), () => toast.bad('Copy failed'))
}

function Code({ code, toast }) {
  return (
    <div style={{ position: 'relative', marginTop: 8 }}>
      <button onClick={() => copyText(code, toast)} title="Copy"
        style={{ position: 'absolute', top: 7, right: 7, display: 'flex', alignItems: 'center', gap: 4, border: '1px solid var(--border)', background: 'var(--surface)', color: 'var(--fg-muted)', borderRadius: 7, padding: '3px 8px', fontSize: 11, fontWeight: 600, cursor: 'pointer' }}>
        <Icon name="content_copy" size={13} />Copy
      </button>
      <pre className="mono" style={{ margin: 0, padding: '13px 15px', paddingRight: 74, background: 'var(--surface-2)', border: '1px solid var(--border)', borderRadius: 9, fontSize: 12, lineHeight: 1.6, whiteSpace: 'pre', overflowX: 'auto', color: 'var(--fg)' }}>{code}</pre>
    </div>
  )
}

function Section({ icon, title, children }) {
  return (
    <section style={{ border: '1px solid var(--border)', borderRadius: 13, background: 'var(--surface)', padding: '18px 20px', marginBottom: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 12 }}>
        <Icon name={icon} size={18} color="var(--accent)" />
        <h2 style={{ fontSize: 15, fontWeight: 700, letterSpacing: '-.01em' }}>{title}</h2>
      </div>
      {children}
    </section>
  )
}

export default function ApiDocsView({ adminKey, tenants, toast }) {
  const [tid, setTid] = useState(tenants[0]?.id || '')
  const [base, setBase] = useState(api.base)
  const [keys, setKeys] = useState([])
  const [key, setKey] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [roles, setRoles] = useState([])
  const [role, setRole] = useState('')
  const [jwt, setJwt] = useState('')

  const load = useCallback(async () => {
    if (!tid) return
    const [k, r] = await Promise.all([
      api.get(`/admin/tenants/${tid}/apikeys`, adminKey),
      api.get(`/admin/tenants/${tid}/roles`, adminKey),
    ])
    if (k.ok) { setKeys(k.data.filter(x => x.active)); setKey('') }
    if (r.ok) { setRoles(r.data); setRole(p => r.data.some(x => x.name === p) ? p : (r.data[0]?.name || '')) }
    setJwt('')
  }, [tid, adminKey])
  useEffect(() => { load() }, [load])

  const mint = async () => {
    if (!role) { toast.bad('This tenant has no roles yet'); return }
    const res = await api.post('/admin/mint-jwt', adminKey, { tenant_id: tid, user_id: 'docs-demo-user', role, ttl_min: 60 })
    if (res.ok && res.data.token) { setJwt(res.data.token); toast.ok(`Demo JWT for role "${role}" minted (valid for 60 minutes)`) }
    else toast.bad(res.error?.message || 'Failed to mint demo JWT')
  }

  const keyPh = key || '<API_KEY>'
  const jwtPh = jwt || '<END_USER_JWT>'
  const b = (base || '').replace(/\/$/, '')

  const curlFor = (ep) => [
    `curl -X POST ${b}${ep.path} \\`,
    `  -H "X-API-Key: ${keyPh}" \\`,
    `  -H "Authorization: Bearer ${jwtPh}" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '${JSON.stringify(ep.body)}'`,
  ].join('\n')

  const sdkJs = [
    `import { AdminClient } from "./sdk/js/client.js"`,
    ``,
    `const c = new AdminClient("${b}", "${keyPh}")`,
    `const jwt = "${jwtPh}"  // end-user JWT (carries the "role" claim), signed by your identity provider`,
    ``,
    `const res = await c.chat("total sales per region?", { userId: "u1", jwt })`,
    `console.log(res.answer, res.citations)`,
  ].join('\n')

  const sdkPy = [
    `from sdk.python.client import Client`,
    ``,
    `c = Client("${b}", "${keyPh}")`,
    `jwt = "${jwtPh}"  # end-user JWT (carries the "role" claim)`,
    ``,
    `print(c.chat("total sales per region?", user_id="u1", jwt=jwt)["answer"])`,
  ].join('\n')

  return (
    <div style={{ flex: 1, overflowY: 'auto' }} className="fade">
      <header style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 20, padding: '26px 34px 18px', flexWrap: 'wrap' }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, letterSpacing: '-.02em' }}>API Docs</h1>
          <p style={{ color: 'var(--fg-muted)', marginTop: 4, fontSize: 13.5 }}>Consumer integration guide for the governed <span className="mono">/v1/*</span> API. Paste the app key you received on create/recreate to fill in the examples.</p>
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          <select value={tid} onChange={e => setTid(e.target.value)} style={sel}>{tenants.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}</select>
          <a href={`${b}/docs`} target="_blank" rel="noreferrer" style={{ height: 38, padding: '0 14px', border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface)', color: 'var(--fg-muted)', fontSize: 13, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 6, textDecoration: 'none' }}><Icon name="description" size={16} />OpenAPI</a>
        </div>
      </header>

      <div style={{ maxWidth: 960, margin: '0 auto', padding: '6px 34px 48px' }}>
        {/* Base URL */}
        <Section icon="link" title="Base URL">
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
            <input value={base} onChange={e => setBase(e.target.value)} spellCheck={false}
              style={{ ...box, flex: 1, minWidth: 260 }} />
            <button onClick={() => copyText(b, toast)} style={btn}><Icon name="content_copy" size={14} />Copy</button>
          </div>
          <p style={hint}>Dev environment default: <span className="mono">http://localhost:8000</span>. Switch to your production domain when you deploy.</p>
        </Section>

        {/* Authentication */}
        <Section icon="lock" title="Authentication — strict Pattern B (two headers)">
          <div style={{ display: 'grid', gap: 8, marginBottom: 14 }}>
            <div style={authRow}><span className="mono" style={tag}>X-API-Key</span><span style={{ fontSize: 12.5, color: 'var(--fg-muted)' }}>Identifies the <b>consumer app</b> (carries tenant, metering, rate limit). Does <b>not</b> carry a role.</span></div>
            <div style={authRow}><span className="mono" style={tag}>Authorization: Bearer</span><span style={{ fontSize: 12.5, color: 'var(--fg-muted)' }}>Verified <b>end-user</b> JWT; its <span className="mono">role</span> claim determines data access (RLAC/CLAC). Required on <b>every</b> request.</span></div>
          </div>

          {/* API key */}
          <label style={lbl}>This tenant's API key</label>
          {keys.length === 0
            ? <div style={warn}><Icon name="warning" size={15} color="var(--danger)" />This tenant has no API key yet. Create one first in the <b>App Key</b> tab (Tenant Management → tenant configuration).</div>
            : (
              <>
                <div style={{ fontSize: 11.5, color: 'var(--fg-subtle)', marginBottom: 7 }}>
                  Active keys: {keys.map(k => `${k.label || 'App key'} (${k.key_prefix || 'hashed'})`).join(', ')}. The backend stores only a hash, so the secret can't be recovered.
                </div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                <input type={showKey ? 'text' : 'password'} value={key} onChange={e => setKey(e.target.value)} placeholder="Paste app key (sk_…)" autoComplete="off" spellCheck={false} style={{ ...box, flex: 1, minWidth: 220, fontFamily: "'IBM Plex Mono',monospace" }} />
                <button onClick={() => setShowKey(s => !s)} style={btn} title={showKey ? 'Hide' : 'Show'}><Icon name={showKey ? 'visibility_off' : 'visibility'} size={15} /></button>
                <button onClick={() => copyText(key, toast)} style={btn}><Icon name="content_copy" size={14} />Copy</button>
                </div>
              </>
            )}

          {/* JWT mint */}
          <label style={{ ...lbl, marginTop: 16 }}>End-user JWT (demo)</label>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <select value={role} onChange={e => setRole(e.target.value)} style={{ ...sel, minWidth: 160 }}>
              {roles.length === 0 ? <option value="">(no roles yet)</option> : roles.map(r => <option key={r.name} value={r.name}>{r.name}</option>)}
            </select>
            <button onClick={mint} style={{ ...btn, borderColor: 'var(--accent)', color: 'var(--accent)' }}><Icon name="key" size={15} />Mint demo JWT</button>
            {jwt && <button onClick={() => copyText(jwt, toast)} style={btn}><Icon name="content_copy" size={14} />Copy token</button>}
          </div>
          {jwt
            ? <input readOnly value={jwt} spellCheck={false} style={{ ...box, marginTop: 8, width: '100%', fontFamily: "'IBM Plex Mono',monospace", color: 'var(--success)' }} />
            : <p style={hint}>In production the JWT is signed by your identity provider with a private key; Poros verifies the JWKS/public key + issuer + audience. The mint button is a local/dev helper only.</p>}
        </Section>

        {/* Endpoints */}
        <Section icon="api" title="Endpoints">
          <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
            {ENDPOINTS.map(ep => (
              <div key={ep.path} style={{ borderTop: '1px solid var(--border)', paddingTop: 14 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                  <span style={methodBadge}>{ep.method}</span>
                  <span className="mono" style={{ fontSize: 13.5, fontWeight: 600 }}>{ep.path}</span>
                  <span style={{ fontSize: 12, color: 'var(--fg-subtle)' }}>· {ep.title}</span>
                  {ep.sse && <span style={{ fontSize: 10, fontWeight: 700, color: 'var(--accent)', background: 'var(--accent-soft)', padding: '2px 7px', borderRadius: 6 }}>SSE</span>}
                </div>
                <p style={{ fontSize: 12.5, color: 'var(--fg-muted)', margin: '7px 0 0', lineHeight: 1.55 }}>{ep.desc}</p>
                <label style={{ ...lbl, marginTop: 12 }}>Request body</label>
                <Code code={JSON.stringify(ep.body, null, 2)} toast={toast} />
                <label style={{ ...lbl, marginTop: 12 }}>curl</label>
                <Code code={curlFor(ep)} toast={toast} />
              </div>
            ))}
          </div>
        </Section>

        {/* SDK */}
        <Section icon="code" title="SDK quickstart">
          <p style={{ fontSize: 12.5, color: 'var(--fg-muted)', margin: '0 0 4px', lineHeight: 1.55 }}>Zero-dependency SDKs for JavaScript (browser + Node 18+) and Python (stdlib). Available in the <span className="mono">sdk/</span> folder.</p>
          <label style={{ ...lbl, marginTop: 12 }}>JavaScript / TypeScript</label>
          <Code code={sdkJs} toast={toast} />
          <label style={{ ...lbl, marginTop: 14 }}>Python</label>
          <Code code={sdkPy} toast={toast} />
        </Section>

        {/* Errors */}
        <Section icon="error" title="Error codes">
          <div style={{ overflowX: 'auto', border: '1px solid var(--border)', borderRadius: 9 }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
              <thead>
                <tr>
                  <th style={{ ...th, width: 60 }}>HTTP</th>
                  <th style={th}>code</th>
                  <th style={th}>Meaning</th>
                </tr>
              </thead>
              <tbody>
                {ERRORS.map(([http, code, meaning], i) => (
                  <tr key={code}>
                    <td className="mono" style={{ ...td, color: 'var(--danger)', fontWeight: 600, borderBottom: i === ERRORS.length - 1 ? 'none' : td.borderBottom }}>{http}</td>
                    <td className="mono" style={{ ...td, borderBottom: i === ERRORS.length - 1 ? 'none' : td.borderBottom }}>{code}</td>
                    <td style={{ ...td, color: 'var(--fg-muted)', borderBottom: i === ERRORS.length - 1 ? 'none' : td.borderBottom }}>{meaning}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p style={hint}>All errors share the shape <span className="mono">{`{ "detail": { "code", "message" } }`}</span>. Governance blocks anything outside the role's permissions — figures only ever come from validated SQL.</p>
        </Section>
      </div>
    </div>
  )
}

const sel = { height: 38, padding: '0 12px', border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface)', color: 'var(--fg)', fontSize: 13 }
const box = { height: 38, padding: '0 12px', border: '1px solid var(--border)', borderRadius: 9, background: 'var(--bg)', color: 'var(--fg)', fontSize: 12.5, outline: 'none' }
const btn = { height: 38, padding: '0 12px', border: '1px solid var(--border)', borderRadius: 9, background: 'var(--surface)', color: 'var(--fg-muted)', fontSize: 12.5, fontWeight: 600, cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 5 }
const lbl = { display: 'block', fontSize: 11, fontWeight: 600, letterSpacing: '.04em', textTransform: 'uppercase', color: 'var(--fg-subtle)', marginBottom: 2 }
const hint = { fontSize: 11.5, color: 'var(--fg-subtle)', marginTop: 9, lineHeight: 1.55 }
const authRow = { display: 'flex', gap: 10, alignItems: 'baseline', flexWrap: 'wrap' }
const tag = { fontSize: 11.5, fontWeight: 700, color: 'var(--accent)', background: 'var(--accent-soft)', padding: '3px 8px', borderRadius: 6, flexShrink: 0 }
const warn = { display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5, color: 'var(--fg-muted)', background: 'var(--surface-2)', border: '1px solid var(--border)', borderRadius: 9, padding: '10px 12px' }
const methodBadge = { fontSize: 10.5, fontWeight: 700, color: '#fff', background: 'var(--accent)', padding: '3px 8px', borderRadius: 6, letterSpacing: '.03em' }
const th = { textAlign: 'left', padding: '9px 14px', borderBottom: '1px solid var(--border)', background: 'var(--surface-2)', fontWeight: 600, whiteSpace: 'nowrap' }
const td = { padding: '9px 14px', borderBottom: '1px solid var(--border)', verticalAlign: 'top' }
