/* Knowledge Base — where the AI's understanding of a tenant's data is curated.
 *
 * The tree is Tenant > Role > Business > Database, then the curation for that pair. What
 * the tree does NOT say, and the panel therefore does, is that the curation belongs to the
 * (business, datasource) pair and not to the role it was reached through: two roles granted
 * the same business open the very same knowledge. Without that sentence on screen an admin
 * reasonably reads the path as ownership and edits "the analyst's copy". */
import React, { useCallback, useEffect, useMemo, useState } from 'react'
import PropTypes from 'prop-types'
import { api } from '../api.js'
import { Icon } from '../tos.jsx'
import { tenantType, toastType } from '../propTypes.js'
import CurationEditor, { ConnectionTablesEditor, PiiEditor } from './kb/CurationEditor.jsx'
import {
  ConfirmDialog, Loading, Toggle, btnGhost, btnPrimary, dashedSm, descInput, iconLink, lbl,
  miniBtn,
} from './kb/ui.jsx'

const SQL_ONLY = ds => ds.type !== 'docs'
const byText = (a, b) => String(a).localeCompare(String(b))
const railBtn = (active, depth) => ({
  display: 'flex', alignItems: 'center', gap: 8, width: '100%', textAlign: 'left',
  padding: `7px 10px 7px ${10 + depth * 16}px`, border: 'none', borderRadius: 8,
  background: active ? 'var(--accent-soft)' : 'transparent',
  color: active ? 'var(--accent)' : 'var(--fg-muted)',
  fontSize: 12.5, fontWeight: active ? 600 : 500, cursor: 'pointer',
})

export default function KnowledgeBaseView({ adminKey, toast, tenants }) {
  const [tid, setTid] = useState('')
  const [roles, setRoles] = useState(null)
  const [businesses, setBusinesses] = useState(null)
  const [sources, setSources] = useState(null)
  const [sel, setSel] = useState(null)        // {kind:'role'|'business'|'datasource', ...}
  const [expanded, setExpanded] = useState({})
  // open state per rail node; absent = open, so the toggle stores the inverse
  const toggleOpen = (key) => setExpanded(e => ({ ...e, [key]: e[key] === false }))
  // Collapsed by default. The explanation is read once and the switches are set once, but
  // both sat above the tree on every visit -- so the work started below the fold.
  const [helpOpen, setHelpOpen] = useState(false)
  const [cfgOpen, setCfgOpen] = useState(false)

  useEffect(() => {
    if (!tid && tenants.length) setTid(tenants[0].id)
  }, [tenants, tid])

  // both switches live in the SAME config section, so they are saved together —
  // posting one alone would wipe the other (set_config replaces the section)
  const [required, setRequired] = useState(false)
  const [confirmEntities, setConfirmEntities] = useState(false)
  // its own section (config/chat): saving it never touches the business pair above
  const [engine, setEngine] = useState('classic')
  const load = useCallback(async () => {
    if (!tid) return
    const [r, b, d, c, e] = await Promise.all([
      api.get(`/admin/tenants/${tid}/roles`, adminKey),
      api.get(`/admin/tenants/${tid}/businesses`, adminKey),
      api.get(`/admin/tenants/${tid}/datasources`, adminKey),
      api.get(`/admin/tenants/${tid}/config/business`, adminKey),
      api.get(`/admin/tenants/${tid}/config/chat`, adminKey),
    ])
    if (!r.ok) return toast.bad(r.error.message)
    if (!b.ok) return toast.bad(b.error.message)
    if (!d.ok) return toast.bad(d.error.message)
    setRoles(r.data)
    setBusinesses(b.data)
    setSources(d.data.filter(SQL_ONLY))
    if (c.ok) { setRequired(!!c.data.required); setConfirmEntities(!!c.data.confirm_entities) }
    if (e.ok) setEngine(e.data.engine === 'agentic' ? 'agentic' : 'classic')
  }, [tid, adminKey])            // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setRoles(null); setBusinesses(null); setSources(null); setSel(null); load() },
    [tid])                       // eslint-disable-line react-hooks/exhaustive-deps

  const saveConfig = async (patch, revert) => {
    const r = await api.post(`/admin/tenants/${tid}/config/business`, adminKey,
      { data: { required, confirm_entities: confirmEntities, ...patch } })
    if (!r.ok) { revert(); toast.bad(r.error.message); return false }
    return true
  }

  const setRequiredFlag = async next => {
    setRequired(next)                                   // optimistic: a toggle that lags reads as broken
    if (!await saveConfig({ required: next }, () => setRequired(!next))) return
    toast.ok(next ? 'Every question will now ask for a business first'
                  : 'Questions without a business are answered from the full catalog again')
  }

  const setConfirmEntitiesFlag = async next => {
    setConfirmEntities(next)
    if (!await saveConfig({ confirm_entities: next }, () => setConfirmEntities(!next))) return
    toast.ok(next ? 'Close-but-not-exact names will be confirmed first'
                  : 'Similar names are no longer confirmed')
  }

  const setEngineFlag = async next => {
    const prev = engine
    setEngine(next)
    const r = await api.post(`/admin/tenants/${tid}/config/chat`, adminKey, { data: { engine: next } })
    if (!r.ok) { setEngine(prev); return toast.bad(r.error.message) }
    toast.ok(next === 'agentic'
      ? 'Chat now reads the Knowledge Base itself (agentic engine)'
      : 'Chat is back on the classic engine')
  }

  const byId = useMemo(() => Object.fromEntries((sources || []).map(d => [d.id, d])),
    [sources])
  const bizById = useMemo(() => Object.fromEntries((businesses || []).map(b => [b.id, b])),
    [businesses])
  // Granted to nobody: invisible in the role tree, so the rail has to offer them or they
  // cannot be reached at all.
  const orphans = useMemo(() => (businesses || []).filter(b => !(b.roles || []).length),
    [businesses])

  const tenant = tenants.find(t => t.id === tid)
  const loading = !roles || !businesses || !sources

  const createBusiness = async (roleName) => {
    const name = window.prompt(roleName
      ? `New business name for role ${roleName} (e.g. 1P, 3P, GDV)`
      : 'New business name (e.g. 1P, 3P, GDV)')
    if (!name?.trim()) return
    const r = await api.post(`/admin/tenants/${tid}/businesses`, adminKey, { name: name.trim() })
    if (!r.ok) return toast.bad(r.error.message)
    // Granting it in the same step is the whole point of the + sitting on the role: a
    // business nobody granted appears under no role at all, so creating one from up there
    // and finding the tree unchanged is exactly the confusion being removed here.
    if (roleName) {
      const ids = businesses.filter(b => (b.roles || []).includes(roleName)).map(b => b.id)
      const g = await api.put(
        `/admin/tenants/${tid}/roles/${encodeURIComponent(roleName)}/businesses`,
        adminKey, { business_ids: [...ids, r.data.id] })
      if (!g.ok) {
        // Say which half succeeded: the business exists, so a retry must not read as
        // "nothing happened" and lead to a duplicate name.
        toast.bad(`Business ${name.trim()} was created, but granting it to role ${roleName} failed: ${g.error.message}`)
        return load()
      }
    }
    toast.ok(roleName ? `Business ${name.trim()} created for role ${roleName}`
      : `Business ${name.trim()} created`)
    load()
  }

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }} className="fade">
      <header style={{ padding: '16px 34px 10px', flexShrink: 0, display: 'flex',
        alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <h1 style={{ fontSize: 20, fontWeight: 700, letterSpacing: '-.02em' }}>Knowledge Base</h1>
        <label style={{ ...lbl, marginBottom: 0 }} htmlFor="kb-tenant">Tenant</label>
        <select id="kb-tenant" value={tid} onChange={e => setTid(e.target.value)}
          style={{ ...descInput, height: 34, width: 240 }}>
          {tenants.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
        </select>
        {tenant && <span style={{ fontSize: 12, color: 'var(--fg-subtle)' }}>
          {(businesses || []).length} businesses · {(sources || []).length} databases · {(roles || []).length} roles
        </span>}
        <span style={{ flex: 1 }} />
        {tenant && (
          // The summary is on the button itself: hiding the switches is fine, hiding
          // WHETHER THEY ARE ON is not -- that is the one thing worth a glance per visit.
          <button onClick={() => setCfgOpen(o => !o)} style={{ ...miniBtn, padding: '5px 10px' }}>
            <Icon name="tune" size={14} />
            Chat behavior:
            <span style={{ color: required || confirmEntities || engine === 'agentic' ? 'var(--accent)' : 'var(--fg-subtle)' }}>
              {[engine === 'agentic' && 'agentic engine', required && 'business required',
                confirmEntities && 'similar names flagged']
                .filter(Boolean).join(' · ') || 'classic, both off'}
            </span>
            <Icon name={cfgOpen ? 'expand_less' : 'expand_more'} size={15} />
          </button>
        )}
        <button onClick={() => setHelpOpen(o => !o)} style={{ ...miniBtn, padding: '5px 10px' }}>
          <Icon name="help" size={14} />About this page
        </button>
      </header>

      {helpOpen && (
        <p style={{ color: 'var(--fg-muted)', margin: '0 34px 12px', fontSize: 13, maxWidth: 860, lineHeight: 1.6 }}>
          What the AI knows about this tenant's data, split per <b>business</b>. One table can
          mean different things to 1P and 3P, so authorized tables, schema context, and the
          semantic catalog are stored per business × database pair — not once for everything.
          The database connections themselves are still managed in Tenant Management → Database.
        </p>
      )}

      {tenant && cfgOpen && (
        <div style={{ margin: '0 34px 12px', display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div style={{ padding: '11px 14px', borderRadius: 11, border: '1px solid var(--border)', background: 'var(--surface)', display: 'flex', alignItems: 'flex-start', gap: 12 }}>
            <Toggle on={engine === 'agentic'}
              onClick={() => setEngineFlag(engine === 'agentic' ? 'classic' : 'agentic')} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 12.5, fontWeight: 600 }}>Agentic engine</div>
              <div style={{ fontSize: 11.5, color: 'var(--fg-muted)', lineHeight: 1.55, marginTop: 3 }}>
                {engine === 'agentic'
                  ? <>The model reads the question first, then this Knowledge Base through
                    tools: it <b>picks the business</b> from the business descriptions (and asks
                    when two fit), reads the table index and curated definitions, checks how a
                    name is stored, then queries. Grants, governance, PII masking and the
                    semantic contracts still apply to every query. The answer's plan lists
                    the tool steps.</>
                  : <>Classic. Fixed rules pick the business, similar names, the KPI meaning
                    and the database from the question's words before the model reads it.
                    Turn this on to let the model work like an analyst with the warehouse
                    open; switching back is immediate.</>}
              </div>
            </div>
          </div>
          <div style={{ padding: '11px 14px', borderRadius: 11, border: '1px solid var(--border)', background: 'var(--surface)', display: 'flex', alignItems: 'flex-start', gap: 12 }}>
            <Toggle on={required} onClick={() => setRequiredFlag(!required)} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 12.5, fontWeight: 600 }}>Require a business</div>
              <div style={{ fontSize: 11.5, color: 'var(--fg-muted)', lineHeight: 1.55, marginTop: 3 }}>
                {required
                  ? <>Every question is answered inside a single business. In chat, Poros{' '}
                    <b>asks which business first</b> (the options show as buttons) and
                    remembers the pick for the rest of the conversation; roles with only
                    one business are never asked. Consumer apps can skip the question by
                    sending the {' '}<span className="mono">business</span> field directly.</>
                  : <>Off. Questions without a business are answered from the tenant's full
                    catalog, exactly as before the business layer existed. Requests that
                    {' '}<b>do send</b> a business are still scoped to that slice.</>}
              </div>
            </div>
          </div>
          <div style={{ padding: '11px 14px', borderRadius: 11, border: '1px solid var(--border)', background: 'var(--surface)', display: 'flex', alignItems: 'flex-start', gap: 12 }}>
            <Toggle on={confirmEntities} onClick={() => setConfirmEntitiesFlag(!confirmEntities)} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 12.5, fontWeight: 600 }}>Flag similar names</div>
              <div style={{ fontSize: 11.5, color: 'var(--fg-muted)', lineHeight: 1.55, marginTop: 3 }}>
                {confirmEntities
                  ? <>When a question names something that has <b>no exact match</b> in the
                    data but looks like a curated value, the AI does not ask first: it picks
                    the most likely value, <b>says so in the first line of the answer</b>,
                    and offers the other values as one-click corrections — "Philips" is
                    never silently answered as "Philipsome". Exact matches are never
                    flagged.</>
                  : <>Off. Similar names are left to the agent as usual. Turn this on when the
                    tenant has easily confused values — it runs on the
                    {' '}<b>reference values</b> in the semantic catalog, so fill those in
                    first if they are still empty.</>}
              </div>
            </div>
          </div>
        </div>
      )}

      {!tenants.length && <Empty icon="groups" text="No tenants yet. Create one in Tenant Management first." />}
      {tenants.length > 0 && loading && <Loading />}
      {tenants.length > 0 && !loading && (
        <div style={{ flex: 1, minHeight: 0, display: 'flex', gap: 0, borderTop: '1px solid var(--border)' }}>
          <nav style={{ width: 320, flexShrink: 0, borderRight: '1px solid var(--border)', overflowY: 'auto', padding: '14px 12px' }}>
            {roles.length === 0 && <Empty icon="shield_person" text="This tenant has no roles yet. Create one in the Role tab first." small />}
            {roles.map(role => {
              const granted = businesses.filter(b => (b.roles || []).includes(role.name))
              const open = expanded[role.name] !== false
              return (
                <div key={role.id} style={{ marginBottom: 4 }}>
                  <div style={{ display: 'flex', alignItems: 'center' }}>
                    <button onClick={() => toggleOpen(role.name)}
                      style={{ ...iconLink, padding: '4px 2px', cursor: 'pointer' }}>
                      <Icon name={open ? 'expand_more' : 'chevron_right'} size={18} />
                    </button>
                    <button onClick={() => setSel({ kind: 'role', roleName: role.name })}
                      style={{ ...railBtn(sel?.kind === 'role' && sel.roleName === role.name, 0), flex: 1 }}>
                      <Icon name="shield_person" size={16} />
                      <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>{role.name}</span>
                      <span style={{ fontSize: 10.5, color: 'var(--fg-subtle)' }}>{granted.length}</span>
                    </button>
                    <button onClick={() => createBusiness(role.name)}
                      title={`Add a business for role ${role.name}`}
                      aria-label={`Add a business for role ${role.name}`}
                      style={{ ...iconLink, padding: '4px 5px', cursor: 'pointer' }}>
                      <Icon name="add" size={16} />
                    </button>
                  </div>
                  {open && granted.length === 0 && (
                    <div style={{ fontSize: 11, color: 'var(--fg-subtle)', padding: '4px 0 6px 34px' }}>
                      No businesses for this role yet.
                    </div>
                  )}
                  {open && granted.map(b => (
                    <BusinessNode key={b.id} b={b} roleName={role.name} byId={byId} sel={sel}
                      open={expanded[`${role.name}/${b.id}`] !== false}
                      onToggle={() => toggleOpen(`${role.name}/${b.id}`)} onSelect={setSel} />
                  ))}
                </div>
              )
            })}
            {/* No "all businesses" list: it restated the tree above it. What is kept is the
              * one case that was NOT a restatement -- a business granted to no role appears
              * nowhere in the tree, and this is the only way to select it at all. Rendered
              * only when one exists, so the rail normally ends with the roles. */}
              {orphans.length > 0 && (
              <div style={{ marginTop: 14, paddingTop: 12, borderTop: '1px solid var(--border)' }}>
                <div style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: '.04em', color: 'var(--warn)', marginBottom: 6, paddingLeft: 4 }}>
                  NOT GRANTED TO ANY ROLE
                </div>
                <div style={{ fontSize: 10.5, color: 'var(--fg-subtle)', lineHeight: 1.45, margin: '0 4px 8px' }}>
                  Not used by anyone until granted to a role.
                </div>
                {orphans.map(b => (
                  <button key={b.id} onClick={() => setSel({ kind: 'business', businessId: b.id })}
                    style={railBtn(sel?.kind === 'business' && sel.businessId === b.id && !sel.roleName, 0)}>
                    <Icon name="donut_small" size={15} />
                    <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>{b.name}</span>
                  </button>
                ))}
                {/* Only when there is no role to hang the + on. */}
                {roles.length === 0 && (
                  <button onClick={() => createBusiness()} style={{ ...dashedSm, marginTop: 8, marginLeft: 4, cursor: 'pointer' }}>
                    <Icon name="add" size={15} />Add business
                  </button>
                )}
              </div>
              )}
              {roles.length === 0 && orphans.length === 0 && (
                <button onClick={() => createBusiness()} style={{ ...dashedSm, marginTop: 14, marginLeft: 4, cursor: 'pointer' }}>
                  <Icon name="add" size={15} />Add business
                </button>
              )}
          </nav>

          <section style={{ flex: 1, minWidth: 0, overflowY: 'auto', padding: '18px 26px 40px' }}>
            {!sel && <Empty icon="account_tree" text="Pick a role, business, or database on the left." />}
            {sel?.kind === 'role' && (
              <RolePanel tid={tid} roleName={sel.roleName} businesses={businesses}
                adminKey={adminKey} toast={toast} reload={load} />
            )}
            {sel?.kind === 'business' && bizById[sel.businessId] && (
              <BusinessPanel tid={tid} business={bizById[sel.businessId]} sources={sources}
                adminKey={adminKey} toast={toast} reload={load}
                onCleared={() => setSel(null)} />
            )}
            {sel?.kind === 'datasource' && bizById[sel.businessId] && (
              <KnowledgePanel tid={tid} business={bizById[sel.businessId]} businesses={businesses}
                datasourceId={sel.datasourceId} roleName={sel.roleName}
                adminKey={adminKey} toast={toast} reload={load} />
            )}
          </section>
        </div>
      )}
    </div>
  )
}

KnowledgeBaseView.propTypes = {
  adminKey: PropTypes.string.isRequired,
  toast: toastType.isRequired,
  tenants: PropTypes.arrayOf(tenantType).isRequired,
}

function Empty({ icon, text, small }) {
  return (
    <div style={{ display: 'grid', placeItems: 'center', gap: 9, padding: small ? 18 : 60, color: 'var(--fg-subtle)', textAlign: 'center' }}>
      <Icon name={icon} size={small ? 22 : 34} />
      <span style={{ fontSize: small ? 11.5 : 13, maxWidth: 300, lineHeight: 1.5 }}>{text}</span>
    </div>
  )
}
Empty.propTypes = { icon: PropTypes.string.isRequired, text: PropTypes.string.isRequired, small: PropTypes.bool }

function PanelHead({ icon, eyebrow, title, children }) {
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ fontSize: 11, color: 'var(--fg-subtle)', letterSpacing: '.03em', marginBottom: 5 }}>{eyebrow}</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
        <Icon name={icon} size={20} color="var(--accent)" />
        <h2 style={{ fontSize: 17, fontWeight: 700 }}>{title}</h2>
      </div>
      {children && <div style={{ fontSize: 12.5, color: 'var(--fg-muted)', marginTop: 7, lineHeight: 1.6 }}>{children}</div>}
    </div>
  )
}
PanelHead.propTypes = { icon: PropTypes.string.isRequired, eyebrow: PropTypes.node, title: PropTypes.node.isRequired, children: PropTypes.node }

/* ---------- role: which businesses it may use ---------- */
function RolePanel({ tid, roleName, businesses, adminKey, toast, reload }) {
  const granted = businesses.filter(b => (b.roles || []).includes(roleName)).map(b => b.id)
  const [picked, setPicked] = useState(granted)
  const [saving, setSaving] = useState(false)
  useEffect(() => { setPicked(granted) }, [roleName, businesses])  // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = JSON.stringify([...picked].sort(byText)) !== JSON.stringify([...granted].sort(byText))
  const save = async () => {
    setSaving(true)
    const r = await api.put(`/admin/tenants/${tid}/roles/${encodeURIComponent(roleName)}/businesses`,
      adminKey, { business_ids: picked })
    setSaving(false)
    if (!r.ok) return toast.bad(r.error.message)
    toast.ok(`Role ${roleName} now uses ${picked.length} businesses`)
    reload()
  }
  return (
    <div>
      <PanelHead icon="shield_person" eyebrow="Role" title={roleName}>
        Which businesses this role may use. This is <b>not</b> where knowledge is stored —
        knowledge belongs to the business, so two roles using the same business read the
        same content.
      </PanelHead>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxWidth: 620 }}>
        {businesses.length === 0 && <Empty icon="donut_small" text="No businesses yet. Add one from the left panel first." small />}
        {businesses.map(b => {
          const on = picked.includes(b.id)
          return (
            <button key={b.id}
              onClick={() => setPicked(on ? picked.filter(x => x !== b.id) : [...picked, b.id])}
              style={{ display: 'flex', alignItems: 'center', gap: 11, textAlign: 'left', padding: '11px 13px', borderRadius: 10, border: `1px solid ${on ? 'var(--accent)' : 'var(--border)'}`, background: on ? 'var(--accent-soft)' : 'var(--surface)', cursor: 'pointer' }}>
              <Icon name={on ? 'check_circle' : 'radio_button_unchecked'} size={19} color={on ? 'var(--accent)' : 'var(--fg-subtle)'} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: on ? 'var(--accent)' : 'var(--fg)' }}>{b.name}</div>
                <div style={{ fontSize: 11.5, color: 'var(--fg-muted)', marginTop: 2 }}>
                  {b.descr || 'no description'} · {(b.datasource_ids || []).length} databases
                </div>
              </div>
            </button>
          )
        })}
      </div>
      {dirty && (
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 16, maxWidth: 620 }}>
          <button onClick={() => setPicked(granted)} style={{ ...btnGhost, cursor: 'pointer' }}>Cancel</button>
          <button onClick={save} disabled={saving} style={{ ...btnPrimary, cursor: saving ? 'wait' : 'pointer' }}>
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      )}
    </div>
  )
}
RolePanel.propTypes = {
  tid: PropTypes.string.isRequired,
  roleName: PropTypes.string.isRequired,
  businesses: PropTypes.array.isRequired,
  adminKey: PropTypes.string.isRequired,
  toast: toastType.isRequired,
  reload: PropTypes.func.isRequired,
}

/* ---------- business: identity + which databases it spans ---------- */
function BusinessPanel({ tid, business, sources, adminKey, toast, reload, onCleared }) {
  const [name, setName] = useState(business.name)
  const [descr, setDescr] = useState(business.descr || '')
  const [saving, setSaving] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  useEffect(() => { setName(business.name); setDescr(business.descr || '') }, [business.id])  // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = name !== business.name || descr !== (business.descr || '')
  const paused = business.status !== 'active'
  const inChat = business.in_chat !== false
  const base = `/admin/tenants/${tid}/businesses/${business.id}`

  const patch = async (body, msg) => {
    setSaving(true)
    const r = await api.patch(base, adminKey, body)
    setSaving(false)
    if (!r.ok) return toast.bad(r.error.message)
    toast.ok(msg)
    reload()
  }
  const attach = async did => {
    const r = await api.patch(`${base}/datasources/${did}/knowledge`, adminKey, {})
    if (!r.ok) return toast.bad(r.error.message)
    toast.ok('Database added — now pick its authorized tables')
    reload()
  }
  const detach = async did => {
    const r = await api.del(`${base}/datasources/${did}`, adminKey)
    if (!r.ok) return toast.bad(r.error.message)
    toast.ok('Database detached from this business')
    reload()
  }
  const remove = async () => {
    setConfirmDelete(false)
    const r = await api.del(base, adminKey)
    if (!r.ok) return toast.bad(r.error.message)
    toast.ok(`Business ${business.name} deleted`)
    onCleared()
    reload()
  }

  const inBusiness = new Set(business.datasource_ids || [])
  return (
    <div>
      <PanelHead icon="donut_small" eyebrow="Business" title={business.name}>
        One business can span <b>multiple databases</b> — for example 3P reads L2 and L3
        while 1P reads only L2. The description below is used when Poros asks a user
        which business they mean.
      </PanelHead>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 11, maxWidth: 620 }}>
        <div>
          <label style={lbl} htmlFor={`bn-${business.id}`}>Name</label>
          <input id={`bn-${business.id}`} value={name} onChange={e => setName(e.target.value)}
            style={{ ...descInput, height: 38 }} />
        </div>
        <div>
          <label style={lbl} htmlFor={`bd-${business.id}`}>Description — what this business covers</label>
          <input id={`bd-${business.id}`} value={descr} onChange={e => setDescr(e.target.value)}
            style={{ ...descInput, height: 38 }}
            placeholder="e.g. Marketplace sales managed by sellers (3P)" />
        </div>
        {dirty && (
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
            <button onClick={() => { setName(business.name); setDescr(business.descr || '') }} style={{ ...btnGhost, cursor: 'pointer' }}>Cancel</button>
            <button onClick={() => patch({ name, descr }, 'Business updated')} disabled={saving}
              style={{ ...btnPrimary, cursor: saving ? 'wait' : 'pointer' }}>Save</button>
          </div>
        )}
        <div style={{ padding: '11px 14px', borderRadius: 11, border: '1px solid var(--border)', display: 'flex', alignItems: 'flex-start', gap: 12 }}>
          <Toggle on={inChat} onClick={() => patch({ in_chat: !inChat },
            inChat ? `${business.name} is no longer offered in chat` : `${business.name} is offered in chat again`)} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 12.5, fontWeight: 600 }}>Offer in chat</div>
            <div style={{ fontSize: 11.5, color: 'var(--fg-muted)', lineHeight: 1.55, marginTop: 3 }}>
              {inChat
                ? 'Listed when the chat asks which business a question is about.'
                : 'Left out of the chat’s business question. Still answers when a user or an app names it, for example the MBR report.'}
            </div>
          </div>
        </div>
      </div>

      <div style={{ marginTop: 22 }}>
        <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>Databases in this business</div>
        <div style={{ fontSize: 11.5, color: 'var(--fg-subtle)', marginBottom: 10, lineHeight: 1.5 }}>
          Adding a database here grants no table access yet — pick its authorized tables
          per database, on the database node in the left panel.
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxWidth: 620 }}>
          {sources.map(ds => {
            const on = inBusiness.has(ds.id)
            const tables = (business.scopes?.[ds.id]?.allowed_tables || []).length
            return (
              <div key={ds.id} style={{ display: 'flex', alignItems: 'center', gap: 11, padding: '10px 13px', borderRadius: 10, border: '1px solid var(--border)', background: on ? 'var(--surface)' : 'transparent' }}>
                <Icon name="database" size={17} color={on ? 'var(--accent)' : 'var(--fg-subtle)'} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontWeight: 600 }}>{ds.name}
                    <span className="mono" style={{ fontSize: 10.5, color: 'var(--fg-muted)', marginLeft: 8 }}>{ds.type}</span>
                  </div>
                  <div style={{ fontSize: 11.5, color: 'var(--fg-muted)', marginTop: 2 }}>
                    {on ? `${tables} tables selected for ${business.name}` : 'not in this business yet'}
                  </div>
                </div>
                {on
                  ? <button onClick={() => detach(ds.id)} style={{ ...miniBtn, cursor: 'pointer' }}><Icon name="link_off" size={15} />Detach</button>
                  : <button onClick={() => attach(ds.id)} style={{ ...miniBtn, cursor: 'pointer' }}><Icon name="add_link" size={15} />Add</button>}
              </div>
            )
          })}
          {sources.length === 0 && <Empty icon="database" text="This tenant has no databases yet. Add one in Tenant Management → Database." small />}
        </div>
      </div>

      <div style={{ marginTop: 26, paddingTop: 16, borderTop: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 12, maxWidth: 620 }}>
        <button onClick={() => patch({ status: paused ? 'active' : 'paused' },
          paused ? 'Business resumed' : 'Business paused')}
          style={{ ...miniBtn, cursor: 'pointer' }}>
          <Icon name={paused ? 'play_arrow' : 'pause'} size={15} />{paused ? 'Resume' : 'Pause'}
        </button>
        <span style={{ fontSize: 11.5, color: 'var(--fg-subtle)', flex: 1 }}>
          Pause keeps everything and can be undone. Delete cannot.
        </span>
        <button onClick={() => setConfirmDelete(true)}
          style={{ ...miniBtn, cursor: 'pointer', color: 'var(--danger,#e5484d)' }}>
          <Icon name="delete" size={15} />Delete business
        </button>
      </div>

      <ConfirmDialog open={confirmDelete} icon="delete" tone="danger"
        title={`Delete business ${business.name}?`} confirmLabel="Delete" cancelLabel="Cancel"
        onCancel={() => setConfirmDelete(false)} onConfirm={remove}>
        All of this business's curation across {(business.datasource_ids || []).length} databases
        {' '}will be lost and <b style={{ color: 'var(--fg)' }}>cannot be recovered</b>.
        Databases, schemas, and other businesses are untouched. If you only want to
        disable it temporarily, use <b style={{ color: 'var(--fg)' }}>Pause</b>.
      </ConfirmDialog>
    </div>
  )
}
BusinessPanel.propTypes = {
  tid: PropTypes.string.isRequired,
  business: PropTypes.object.isRequired,
  sources: PropTypes.array.isRequired,
  adminKey: PropTypes.string.isRequired,
  toast: toastType.isRequired,
  reload: PropTypes.func.isRequired,
  onCleared: PropTypes.func.isRequired,
}

/* ---------- (business, database): the curation itself ---------- */
function BusinessNode({ b, roleName, open, onToggle, onSelect, sel, byId }) {
  const dsIds = b.datasource_ids || []
  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center' }}>
        <button onClick={onToggle}
          style={{ ...iconLink, padding: '4px 2px', marginLeft: 16, cursor: 'pointer' }}>
          <Icon name={open ? 'expand_more' : 'chevron_right'} size={16} />
        </button>
        <button onClick={() => onSelect({ kind: 'business', roleName, businessId: b.id })}
          style={{ ...railBtn(sel?.kind === 'business' && sel.businessId === b.id && sel.roleName === roleName, 0), flex: 1 }}>
          <Icon name="donut_small" size={15} />
          <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>{b.name}</span>
          {b.status !== 'active' && <span style={{ fontSize: 9.5, fontWeight: 700, color: 'var(--warn)', border: '1px solid var(--warn)', borderRadius: 5, padding: '1px 4px' }}>PAUSED</span>}
        </button>
      </div>
      {open && dsIds.map(did => (
        <button key={did}
          onClick={() => onSelect({ kind: 'datasource', roleName, businessId: b.id, datasourceId: did })}
          style={railBtn(sel?.kind === 'datasource' && sel.businessId === b.id && sel.datasourceId === did && sel.roleName === roleName, 3)}>
          <Icon name="database" size={15} />
          <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>{byId[did]?.name || did}</span>
          <span style={{ fontSize: 10.5, color: 'var(--fg-subtle)' }}>
            {(b.scopes?.[did]?.allowed_tables || []).length}t
          </span>
        </button>
      ))}
      {open && dsIds.length === 0 && (
        <div style={{ fontSize: 11, color: 'var(--fg-subtle)', padding: '3px 0 5px 50px' }}>
          No databases yet.
        </div>
      )}
    </div>
  )
}
BusinessNode.propTypes = {
  b: PropTypes.shape({
    id: PropTypes.string.isRequired,
    name: PropTypes.string,
    status: PropTypes.string,
    datasource_ids: PropTypes.arrayOf(PropTypes.string),
    scopes: PropTypes.object,
  }).isRequired,
  roleName: PropTypes.string.isRequired,
  open: PropTypes.bool.isRequired,
  onToggle: PropTypes.func.isRequired,
  onSelect: PropTypes.func.isRequired,
  sel: PropTypes.object,
  byId: PropTypes.object.isRequired,
}

function KnowledgePanel({ tid, business, businesses, datasourceId, roleName, adminKey, toast, reload }) {
  const [snapshot, setSnapshot] = useState(null)
  const url = `/admin/tenants/${tid}/businesses/${business.id}/datasources/${datasourceId}/knowledge`
  const fetchSnapshot = useCallback(async () => {
    const r = await api.get(url, adminKey)
    if (!r.ok) { toast.bad(r.error.message); return }
    setSnapshot(r.data)
  }, [url, adminKey])            // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setSnapshot(null); fetchSnapshot() }, [fetchSnapshot])

  if (!snapshot) return <Loading />
  const ds = snapshot.datasource
  const shared = (business.roles || []).filter(r => r !== roleName)
  const crumb = roleName ? `${roleName} › ` : ''
  return (
    <div>
      <PanelHead icon="database"
        eyebrow={`${crumb}${business.name} › database`}
        title={ds.name}>
        This knowledge belongs to the <b>{business.name} × {ds.name}</b> pair.
        {shared.length > 0
          ? <> Other roles using this business — <b>{shared.join(', ')}</b> — read this same
            content; it is not a per-role copy.</>
          : <> If another role is granted business {business.name} later, it will read this
            same content.</>}
        {!ds.enabled && <> <b style={{ color: 'var(--warn)' }}>This datasource is disabled</b>, so
          it is not used for answers until re-enabled.</>}
      </PanelHead>
      <CurationEditor tenantId={tid} business={business} snapshot={snapshot}
        adminKey={adminKey} toast={toast}
        onSaved={() => { fetchSnapshot(); reload() }} />
      <ConnectionTablesEditor snapshot={snapshot} businesses={businesses}
        adminKey={adminKey} toast={toast}
        onSaved={() => { fetchSnapshot(); reload() }} />
      <PiiEditor snapshot={snapshot} adminKey={adminKey} toast={toast}
        onSaved={() => { fetchSnapshot(); reload() }} />
    </div>
  )
}
KnowledgePanel.propTypes = {
  tid: PropTypes.string.isRequired,
  business: PropTypes.object.isRequired,
  businesses: PropTypes.array,
  datasourceId: PropTypes.string.isRequired,
  roleName: PropTypes.string,
  adminKey: PropTypes.string.isRequired,
  toast: toastType.isRequired,
  reload: PropTypes.func.isRequired,
}
