import React, { useState, useEffect, useCallback } from 'react'
import PropTypes from 'prop-types'
import { api } from '../api.js'
import { Icon, nfmt, keyboardActivate } from '../tos.jsx'
import { tenantType, toastType } from '../propTypes.js'

const ICON_OPTS = ['monitoring', 'support_agent', 'build', 'badge', 'inventory_2', 'gavel', 'hub', 'smart_toy', 'analytics', 'shopping_cart']

function statusMeta(s) {
  if (s === 'active') return { label: 'Active', color: 'var(--success)', bg: 'var(--success-soft)' }
  return { label: 'Inactive', color: 'var(--fg-subtle)', bg: 'var(--surface-2)' }
}

export default function TenantsView({ adminKey, tenants, reload, openTenant, toast }) {
  const [stats, setStats] = useState({})
  const [q, setQ] = useState('')
  const [creating, setCreating] = useState(false)
  const [deleting, setDeleting] = useState(null)   // tenant pending type-to-confirm delete

  const loadStats = useCallback(async () => {
    const r = await api.get('/admin/monitoring', adminKey)
    if (r.ok) setStats(Object.fromEntries(r.data.per_tenant.map(p => [p.id, p])))
  }, [adminKey])
  useEffect(() => { loadStats() }, [tenants.length])

  const setStatus = async (t, status) => {
    const r = await api.patch(`/admin/tenants/${t.id}`, adminKey, { status })
    if (r.ok) {
      toast.ok(status === 'active' ? 'Tenant activated' : 'Tenant deactivated')
      reload()
    } else {
      toast.bad(r.error.message)
    }
  }
  const confirmDelete = async (t) => {
    const r = await api.del(`/admin/tenants/${t.id}`, adminKey)
    if (r.ok) { toast.ok('Tenant deleted'); setDeleting(null); reload() } else toast.bad(r.error.message)
  }

  const list = tenants.filter(t => t.name.toLowerCase().includes(q.toLowerCase()))

  return (
    <div style={{ flex: 1, overflowY: 'auto' }} className="fade">
      <header style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 20, padding: '26px 34px 20px' }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, letterSpacing: '-.02em' }}>Tenant Management</h1>
          <p style={{ color: 'var(--fg-muted)', marginTop: 4, fontSize: 13.5 }}>Manage all tenant agents and their configuration.</p>
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '0 12px', height: 38, border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface)', color: 'var(--fg-subtle)', minWidth: 220 }}>
            <Icon name="search" size={18} />
            <input value={q} onChange={e => setQ(e.target.value)} placeholder="Search tenants…" type="search" name="tenant-search" autoComplete="off" data-1p-ignore data-lpignore="true" style={{ border: 'none', outline: 'none', background: 'transparent', color: 'var(--fg)', fontSize: 13, width: '100%' }} />
          </div>
          <button onClick={() => setCreating(true)} style={{ display: 'flex', alignItems: 'center', gap: 7, height: 38, padding: '0 16px', border: 'none', borderRadius: 10, background: 'var(--accent)', color: '#fff', fontSize: 13.5, fontWeight: 600 }}>
            <Icon name="add" size={18} /> New Tenant
          </button>
        </div>
      </header>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(310px,1fr))', gap: 16, padding: '6px 34px 40px' }}>
        {list.map(t => <TenantCard key={t.id} t={t} s={stats[t.id] || {}} openTenant={openTenant} onStatus={setStatus} onDelete={setDeleting} />)}
        {list.length === 0 && <div style={{ gridColumn: '1/-1', textAlign: 'center', color: 'var(--fg-subtle)', padding: '50px 0', border: '1px dashed var(--border)', borderRadius: 14 }}>
          <Icon name="groups" size={32} /><div style={{ marginTop: 8 }}>{q ? 'No tenant matches your search.' : 'No tenants yet — create your first one.'}</div></div>}
      </div>

      {creating && <NewTenant adminKey={adminKey} toast={toast} onClose={() => setCreating(false)} onCreated={() => { setCreating(false); reload() }} />}
      {deleting && <DeleteTenant t={deleting} onClose={() => setDeleting(null)} onConfirm={() => confirmDelete(deleting)} />}
    </div>
  )
}

function DeleteTenant({ t, onClose, onConfirm }) {
  const [text, setText] = useState(''); const [busy, setBusy] = useState(false)
  const match = text.trim() === t.name
  const go = async () => { if (!match || busy) return; setBusy(true); await onConfirm(); setBusy(false) }
  // Escape on document, not on the scrim div: a div only sees keydown while focused.
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div style={scrim}>
      <div className="pop" style={{ width: 'min(460px,94vw)', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 16, padding: 22, color: 'var(--fg)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 11, marginBottom: 13 }}>
          <div style={{ width: 38, height: 38, borderRadius: 10, background: 'color-mix(in srgb,var(--danger) 14%,transparent)', color: 'var(--danger)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}><Icon name="warning" size={21} /></div>
          <div style={{ fontWeight: 700, fontSize: 17 }}>Delete tenant</div>
        </div>
        <p style={{ fontSize: 13, color: 'var(--fg-muted)', lineHeight: 1.6, marginBottom: 16 }}>
          This action is <b style={{ color: 'var(--fg)' }}>permanent and cannot be undone</b>. All datasources, roles, documents, memory, and API keys belonging to <b style={{ color: 'var(--fg)' }}>{t.name}</b> will be deleted with it.
        </p>
        <label style={{ display: 'block', fontSize: 12.5, color: 'var(--fg-muted)', marginBottom: 7 }}>Type <span className="mono" style={{ color: 'var(--danger)', fontWeight: 600 }}>{t.name}</span> to confirm</label>
        <input autoFocus value={text} onChange={e => setText(e.target.value)} onKeyDown={e => e.key === 'Enter' && go()} placeholder={t.name}
          style={{ ...inp, fontFamily: "'IBM Plex Mono',monospace", borderColor: text && !match ? 'var(--danger)' : 'var(--border)' }} />
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 18 }}>
          <button onClick={onClose} style={btnGhost}>Cancel</button>
          <button onClick={go} disabled={!match || busy} style={{ ...btnDanger, opacity: match && !busy ? 1 : .5, cursor: match && !busy ? 'pointer' : 'not-allowed' }}>{busy && <span className="spin" />}<Icon name="delete" size={17} />Delete permanently</button>
        </div>
      </div>
    </div>
  )
}

function TenantCard({ t, s, openTenant, onStatus, onDelete }) {
  const [menu, setMenu] = useState(false)
  const st = statusMeta(t.status)
  const active = t.status === 'active'
  const stop = (e) => e.stopPropagation()
  return (
    <div className="hov-card" role="button" tabIndex={0} onClick={() => openTenant(t)}
      onKeyDown={keyboardActivate(() => openTenant(t))}
      style={{ position: 'relative', cursor: 'pointer', textAlign: 'left', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 14, padding: 18, color: 'var(--fg)', display: 'flex', flexDirection: 'column', gap: 14, opacity: active ? 1 : .72 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 13 }}>
        <div style={{ width: 44, height: 44, borderRadius: 11, background: 'var(--accent-soft)', color: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}><Icon name={t.icon || 'hub'} size={24} /></div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 600, fontSize: 15, letterSpacing: '-.01em' }}>{t.name}</div>
          <div style={{ fontSize: 12.5, color: 'var(--fg-muted)', marginTop: 2 }}>{t.descr || '—'}</div>
          {t.owner_email && <div title={'Created and owned by ' + t.owner_email} style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11, color: 'var(--fg-subtle)', marginTop: 5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            <Icon name="badge" size={13} />{t.owner_email}
          </div>}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '4px 9px', borderRadius: 20, background: st.bg, color: st.color, fontSize: 11, fontWeight: 600 }}>
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: st.color }} />{st.label}
          </div>
          <button onClick={(e) => { stop(e); setMenu(m => !m) }} title="Actions" style={kebab}><Icon name="more_vert" size={18} /></button>
        </div>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', borderRadius: 9, background: 'var(--surface-2)', fontFamily: "'IBM Plex Mono',monospace", fontSize: 12, color: 'var(--fg-muted)' }}>
        <Icon name="smart_toy" size={15} color="var(--accent)" />{s.model || '—'}
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', borderTop: '1px solid var(--border)', paddingTop: 13 }}>
        <div><div style={{ fontSize: 11, color: 'var(--fg-subtle)' }}>Tokens (30d)</div><div className="mono" style={{ fontWeight: 600, marginTop: 2 }}>{nfmt(s.tokens)}</div></div>
        <div><div style={{ fontSize: 11, color: 'var(--fg-subtle)' }}>Requests</div><div className="mono" style={{ fontWeight: 600, marginTop: 2 }}>{nfmt(s.requests)}</div></div>
        <div style={{ display: 'flex', alignItems: 'flex-end' }}><span style={{ display: 'flex', alignItems: 'center', gap: 3, fontSize: 12, fontWeight: 600, color: 'var(--accent)' }}>Configure <Icon name="arrow_forward" size={15} /></span></div>
      </div>
      {menu && <>
        <button type="button" aria-label="Close tenant menu" onClick={(e) => { stop(e); setMenu(false) }}
          style={{ position: 'fixed', inset: 0, zIndex: 40, border: 0, background: 'transparent' }} />
        <div style={menuBox}>
          <button onClick={(e) => { stop(e); setMenu(false); onStatus(t, active ? 'inactive' : 'active') }} style={menuItem}>
            <Icon name={active ? 'toggle_off' : 'toggle_on'} size={18} color={active ? 'var(--fg-subtle)' : 'var(--success)'} />{active ? 'Set inactive' : 'Set active'}
          </button>
          <div style={{ height: 1, background: 'var(--border)', margin: '4px 0' }} />
          <button onClick={(e) => { stop(e); setMenu(false); onDelete(t) }} style={{ ...menuItem, color: 'var(--danger)' }}>
            <Icon name="delete" size={18} />Delete tenant
          </button>
        </div>
      </>}
    </div>
  )
}

const kebab = { display: 'flex', alignItems: 'center', justifyContent: 'center', width: 30, height: 30, border: '1px solid var(--border)', borderRadius: 8, background: 'var(--surface)', color: 'var(--fg-subtle)', cursor: 'pointer', flexShrink: 0 }
const menuBox = { position: 'absolute', top: 50, right: 14, zIndex: 50, minWidth: 176, background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 11, padding: 6, boxShadow: '0 10px 30px rgba(8,12,20,.18)' }
const menuItem = { display: 'flex', alignItems: 'center', gap: 9, width: '100%', textAlign: 'left', padding: '9px 11px', border: 'none', borderRadius: 8, background: 'transparent', color: 'var(--fg)', fontSize: 13, fontWeight: 500, cursor: 'pointer' }

function NewTenant({ adminKey, toast, onClose, onCreated }) {
  const [name, setName] = useState(''); const [descr, setDescr] = useState('')
  const [icon, setIcon] = useState('hub'); const [busy, setBusy] = useState(false)
  const create = async () => {
    if (!name.trim()) return toast.warn('Name required')
    setBusy(true)
    const r = await api.post('/admin/tenants', adminKey, { name: name.trim(), descr: descr.trim(), icon, status: 'active' })
    setBusy(false)
    if (r.ok) {
      toast.ok('Tenant created')
      onCreated()
    } else {
      toast.bad(r.error.message)
    }
  }
  // Escape on document, not on the scrim div: a div only sees keydown while focused.
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div style={scrim}>
      <div className="pop" style={{ width: 'min(440px,94vw)', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 16, padding: 22, color: 'var(--fg)' }}>
        <div style={{ fontWeight: 700, fontSize: 17, marginBottom: 16 }}>New tenant</div>
        <Field label="Name"><input value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Procurement" style={inp} /></Field>
        <Field label="Description"><input value={descr} onChange={e => setDescr(e.target.value)} placeholder="Short description" style={inp} /></Field>
        <Field label="Icon">
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 7 }}>
            {ICON_OPTS.map(ic => <button key={ic} onClick={() => setIcon(ic)} style={{ width: 38, height: 38, borderRadius: 9, border: '1px solid ' + (icon === ic ? 'var(--accent)' : 'var(--border)'), background: icon === ic ? 'var(--accent-soft)' : 'var(--surface)', color: icon === ic ? 'var(--accent)' : 'var(--fg-muted)' }}><Icon name={ic} size={19} /></button>)}
          </div>
        </Field>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 18 }}>
          <button onClick={onClose} style={btnGhost}>Cancel</button>
          <button onClick={create} disabled={busy} style={btnPrimary}>{busy && <span className="spin" />} Create</button>
        </div>
      </div>
    </div>
  )
}

const scrim = { position: 'fixed', inset: 0, background: 'rgba(8,12,20,.55)', backdropFilter: 'blur(3px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 60, padding: 32 }
const inp = { width: '100%', height: 40, padding: '0 13px', border: '1px solid var(--border)', borderRadius: 9, background: 'var(--bg)', color: 'var(--fg)', fontSize: 13.5, outline: 'none' }
const btnGhost = { padding: '9px 18px', border: '1px solid var(--border)', borderRadius: 9, background: 'var(--surface)', color: 'var(--fg)', fontSize: 13.5, fontWeight: 600 }
const btnPrimary = { padding: '9px 20px', border: 'none', borderRadius: 9, background: 'var(--accent)', color: '#fff', fontSize: 13.5, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 7 }
const btnDanger = { padding: '9px 18px', border: 'none', borderRadius: 9, background: 'var(--danger)', color: '#fff', fontSize: 13.5, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 6 }
function Field({ label, children }) { return <div style={{ marginBottom: 13 }}><label style={{ display: 'block', fontSize: 12.5, fontWeight: 500, color: 'var(--fg-muted)', marginBottom: 7 }}>{label}</label>{children}</div> }

TenantsView.propTypes = {
  adminKey: PropTypes.string.isRequired,
  tenants: PropTypes.arrayOf(tenantType).isRequired,
  reload: PropTypes.func.isRequired,
  openTenant: PropTypes.func.isRequired,
  toast: toastType.isRequired,
}
DeleteTenant.propTypes = {
  t: tenantType.isRequired,
  onClose: PropTypes.func.isRequired,
  onConfirm: PropTypes.func.isRequired,
}
TenantCard.propTypes = {
  t: tenantType.isRequired,
  s: PropTypes.shape({
    model: PropTypes.string,
    tokens: PropTypes.number,
    requests: PropTypes.number,
  }).isRequired,
  openTenant: PropTypes.func.isRequired,
  onStatus: PropTypes.func.isRequired,
  onDelete: PropTypes.func.isRequired,
}
NewTenant.propTypes = {
  adminKey: PropTypes.string.isRequired,
  toast: toastType.isRequired,
  onClose: PropTypes.func.isRequired,
  onCreated: PropTypes.func.isRequired,
}
Field.propTypes = { label: PropTypes.node, children: PropTypes.node }
