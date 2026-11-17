import React, { useState, useEffect, useCallback } from 'react'
import PropTypes from 'prop-types'
import { api } from '../api.js'
import { Card, CardH, Button, Field, Input, Select, Badge, Alert, Loading } from '../components/ui.jsx'
import { toastType } from '../propTypes.js'

export default function Models({ adminKey, tenantId, toast }) {
  const [cat, setCat] = useState(null)
  const [providers, setProviders] = useState([])
  const [models, setModels] = useState([])
  const [budget, setBudget] = useState('')

  const load = useCallback(async () => {
    if (!tenantId) return
    const [p, m, b] = await Promise.all([
      api.get(`/admin/tenants/${tenantId}/providers`, adminKey),
      api.get(`/admin/tenants/${tenantId}/models`, adminKey),
      api.get(`/admin/tenants/${tenantId}/budget`, adminKey)])
    if (p.ok) setProviders(p.data)
    if (m.ok) setModels(m.data)
    if (b.ok) setBudget(b.data.monthly_usd ?? '')
  }, [adminKey, tenantId])
  useEffect(() => { (async () => { const c = await api.get('/admin/provider-catalog', adminKey); if (c.ok) setCat(c.data) })() }, [])
  useEffect(() => { setCat(c => c); load() }, [tenantId])

  if (!cat) return <Loading />
  const configured = Object.fromEntries(providers.map(p => [p.provider, p]))
  const modelByPurpose = Object.fromEntries(models.map(m => [m.purpose, m]))
  const saveBudget = async () => {
    const r = await api.post('/admin/budget', adminKey, { tenant_id: tenantId, monthly_usd: parseFloat(budget) || 0 })
    if (r.ok) toast.ok('Budget saved')
    else toast.bad(r.error.message)
  }

  return (
    <>
      <p className="page-intro">Configure which LLM providers and models this tenant uses. A gatekeeper screens every
        prompt; a router picks the answer model by complexity ∩ budget. Each tenant is configured independently.</p>

      {!cat.litellm_installed && <Alert tone="warn">LiteLLM is not installed on the server — models are saved but calls
        fall back to the rule-based/heuristic path until you <code>pip install litellm</code>.</Alert>}

      <Card>
        <CardH title="Providers" sub="Enter credentials for any providers this tenant should use. Keys are encrypted at rest." />
        <div className="grid cols-3">
          {cat.providers.map(p => <ProviderPanel key={p.id} p={p} current={configured[p.id]} adminKey={adminKey} tenantId={tenantId} toast={toast} reload={load} />)}
        </div>
      </Card>

      <Card>
        <CardH title="Model routing" sub="Assign a model to each purpose. Provider must be configured above." />
        {providers.length === 0
          ? <Alert tone="info">Add at least one provider above to assign models.</Alert>
          : <div className="stack">{cat.purposes.map(pp => (
            <PurposeRow key={pp.id} purpose={pp} catalog={cat} providers={providers} current={modelByPurpose[pp.id]}
              adminKey={adminKey} tenantId={tenantId} toast={toast} reload={load} />))}</div>}
      </Card>

      <Card>
        <CardH title="Budget" sub="Monthly cost ceiling. When spend reaches it, the router downgrades to the cheaper model." />
        <div className="inline-form">
          <Field label="Monthly budget (USD)"><Input type="number" min="0" step="1" value={budget} placeholder="e.g. 50" onChange={e => setBudget(e.target.value)} /></Field>
          <Button variant="primary" onClick={saveBudget}>Save budget</Button>
        </div>
      </Card>
    </>
  )
}

function ProviderPanel({ p, current, adminKey, tenantId, toast, reload }) {
  const [apiKey, setApiKey] = useState('')
  const [apiBase, setApiBase] = useState(current?.api_base || '')
  const [busy, setBusy] = useState(false)
  const needsBase = p.needs.includes('api_base')

  const save = async () => {
    setBusy(true)
    // blank api_key on an already-configured provider keeps the existing key (backend preserves it)
    const r = await api.post('/admin/providers', adminKey,
      { tenant_id: tenantId, provider: p.id, label: p.label, api_key: apiKey, api_base: apiBase })
    setBusy(false)
    if (r.ok) { toast.ok(`${p.label} saved`); setApiKey(''); reload() } else toast.bad(r.error.message)
  }
  const remove = async () => {
    const r = await api.del(`/admin/tenants/${tenantId}/providers/${p.id}`, adminKey)
    if (r.ok) {
      toast.ok('Removed')
      reload()
    } else {
      toast.bad(r.error.message)
    }
  }

  return (
    <div className="card" style={{ boxShadow: 'none', background: 'var(--surface)' }}>
      <div className="spread" style={{ marginBottom: 8 }}>
        <b>{p.label}</b>
        {current ? <Badge tone="ok" dot>configured</Badge> : <Badge tone="gray">not set</Badge>}
      </div>
      {p.needs.includes('api_key') && <Field label="API key" hint={current ? `current: ${current.api_key}` : ''}>
        <Input type="password" placeholder={current ? 'leave blank to keep' : 'sk-…'} value={apiKey} onChange={e => setApiKey(e.target.value)} /></Field>}
      {needsBase && <Field label="API base"><Input placeholder="https://…" value={apiBase} onChange={e => setApiBase(e.target.value)} /></Field>}
      <div className="hint" style={{ margin: '6px 0' }}>Models: {p.models.slice(0, 3).join(', ')}…</div>
      <div className="row">
        <Button variant="primary sm" loading={busy} onClick={save}>Save</Button>
        {current && <Button variant="danger sm" onClick={remove}>Remove</Button>}
      </div>
    </div>
  )
}

function PurposeRow({ purpose, catalog, providers, current, adminKey, tenantId, toast, reload }) {
  const [provider, setProvider] = useState(current?.provider || providers[0]?.provider || '')
  const [model, setModel] = useState(current?.model || '')
  const [busy, setBusy] = useState(false)
  const [testRes, setTestRes] = useState(null)
  const modelOpts = catalog.providers.find(p => p.id === provider)?.models || []

  const save = async () => {
    if (!model.trim()) return toast.warn('Model required')
    setBusy(true)
    const r = await api.post('/admin/models', adminKey, { tenant_id: tenantId, purpose: purpose.id, provider, model: model.trim(), enabled: true })
    setBusy(false)
    if (r.ok) {
      toast.ok(`${purpose.label} → ${model}`)
      reload()
    } else {
      toast.bad(r.error.message)
    }
  }
  const test = async () => {
    setBusy(true); setTestRes(null)
    const r = await api.post(`/admin/tenants/${tenantId}/llm-test?purpose=${purpose.id}`, adminKey, {})
    setBusy(false)
    setTestRes(r.ok ? { ok: true, ...r.data } : { ok: false, msg: r.error.message })
    if (r.ok) toast.ok('Model responded')
    else toast.bad(r.error.message)
  }
  const clear = async () => {
    const r = await api.del(`/admin/tenants/${tenantId}/models/${purpose.id}`, adminKey)
    if (r.ok) {
      toast.ok('Cleared')
      reload()
    } else {
      toast.bad(r.error.message)
    }
  }

  return (
    <div style={{ borderBottom: '1px solid var(--line)', paddingBottom: 12 }}>
      <div className="spread" style={{ marginBottom: 8 }}>
        <div><b>{purpose.label}</b> {current && <Badge tone="brand">{current.provider}/{current.model}</Badge>}<div className="hint">{purpose.hint}</div></div>
      </div>
      <div className="row" style={{ flexWrap: 'wrap' }}>
        <Select value={provider} onChange={e => { setProvider(e.target.value); setModel('') }}
          options={providers.map(p => ({ value: p.provider, label: p.provider }))} />
        <Input list={`m-${purpose.id}`} placeholder="model id" value={model} onChange={e => setModel(e.target.value)} style={{ maxWidth: 240 }} />
        <datalist id={`m-${purpose.id}`}>{modelOpts.map(m => <option key={m} value={m} />)}</datalist>
        <Button variant="primary sm" loading={busy} onClick={save}>Save</Button>
        {current && <><Button variant="sm" loading={busy} onClick={test}>Test</Button>
          <Button variant="ghost sm" onClick={clear}>Clear</Button></>}
      </div>
      {testRes && (testRes.ok
        ? <div className="hint" style={{ color: 'var(--ok)', marginTop: 6 }}>✓ {testRes.model} replied “{testRes.text}” · {testRes.prompt_tokens + testRes.completion_tokens} tokens · ${testRes.cost?.toFixed?.(5)}</div>
        : <div className="hint" style={{ color: 'var(--bad)', marginTop: 6 }}>✗ {testRes.msg}</div>)}
    </div>
  )
}

const providerType = PropTypes.shape({
  id: PropTypes.string,
  provider: PropTypes.string.isRequired,
})
const purposeType = PropTypes.shape({
  id: PropTypes.string.isRequired,
  label: PropTypes.string.isRequired,
  hint: PropTypes.string,
})

Models.propTypes = {
  adminKey: PropTypes.string.isRequired,
  tenantId: PropTypes.string,
  toast: toastType.isRequired,
}
ProviderPanel.propTypes = {
  p: providerType.isRequired,
  current: PropTypes.object,
  adminKey: PropTypes.string.isRequired,
  tenantId: PropTypes.string.isRequired,
  toast: toastType.isRequired,
  reload: PropTypes.func.isRequired,
}
PurposeRow.propTypes = {
  purpose: purposeType.isRequired,
  catalog: PropTypes.object.isRequired,
  providers: PropTypes.arrayOf(providerType).isRequired,
  current: PropTypes.object,
  adminKey: PropTypes.string.isRequired,
  tenantId: PropTypes.string.isRequired,
  toast: toastType.isRequired,
  reload: PropTypes.func.isRequired,
}
