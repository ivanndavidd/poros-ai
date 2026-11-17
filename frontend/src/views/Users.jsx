import React, { useState, useEffect, useCallback } from 'react'
import PropTypes from 'prop-types'
import { api } from '../api.js'
import { fmtShort } from '../time.js'
import { Icon } from '../tos.jsx'

const ROLE_LABEL = { admin: 'Platform Admin', tenant_manager: 'Tenant Manager' }
const input = { height: 38, padding: '0 12px', border: '1px solid var(--border)', borderRadius: 10, background: 'var(--bg)', color: 'var(--fg)', fontSize: 13, outline: 'none' }
const PROVIDERS = {
  local: { label: 'Password', icon: 'key' },
  microsoft: { label: 'Microsoft SSO', icon: 'cloud' },
  google: { label: 'Google SSO', icon: 'account_circle' },
  gateway: { label: 'Apps Gateway', icon: 'shield_person' },
}

export default function UsersView({ adminKey, toast, user }) {
  const [rows, setRows] = useState([])
  const [tenants, setTenants] = useState([])
  const [groups, setGroups] = useState([])
  const [groupName, setGroupName] = useState('')
  const [expandedGroup, setExpandedGroup] = useState(null)
  const [form, setForm] = useState({ email: '', name: '', role: 'tenant_manager', provider: 'local', password: '' })
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    // /admin/users, NOT /auth/users: the Apps Gateway owns /auth/** and never
    // forwards it, so behind the gateway only the /admin mirror is reachable.
    const [usersResult, tenantsResult, groupsResult] = await Promise.all([
      api.get('/admin/users', adminKey),
      api.get('/admin/tenants', adminKey),
      api.get('/admin/tenant-groups', adminKey),
    ])
    if (!usersResult.ok) return toast.bad(usersResult.error.message)
    if (!tenantsResult.ok) return toast.bad(tenantsResult.error.message)
    if (!groupsResult.ok) return toast.bad(groupsResult.error.message)
    setRows(usersResult.data)
    setTenants(tenantsResult.data)
    setGroups(groupsResult.data)
  }, [adminKey])
  useEffect(() => { load() }, [load])

  const create = async (e) => {
    e.preventDefault(); setBusy(true)
    const r = await api.post('/admin/users', adminKey, form)
    setBusy(false)
    if (!r.ok) return toast.bad(r.error.message)
    toast.ok('User created')
    setForm({ email: '', name: '', role: 'tenant_manager', provider: 'local', password: '' })
    load()
  }
  const patch = async (uid, body, okMsg) => {
    const r = await api.patch(`/admin/users/${uid}`, adminKey, body)
    if (!r.ok) return toast.bad(r.error.message)
    toast.ok(okMsg); load()
  }
  const del = async (u) => {
    if (!window.confirm(`Delete user ${u.email}? Their tenants are kept (owner becomes unassigned).`)) return
    const r = await api.del(`/admin/users/${u.id}`, adminKey)
    if (!r.ok) return toast.bad(r.error.message)
    toast.ok('User deleted'); load()
  }
  const resetPw = (u) => {
    const pw = window.prompt(`New password for ${u.email} (min 8 characters):`)
    if (pw === null) return
    patch(u.id, { password: pw }, 'Password reset')
  }
  const createGroup = async (e) => {
    e.preventDefault()
    const name = groupName.trim()
    if (!name) return
    const r = await api.post('/admin/tenant-groups', adminKey, { name, tenant_ids: [], user_ids: [] })
    if (!r.ok) return toast.bad(r.error.message)
    setGroupName('')
    setExpandedGroup(r.data.id)
    toast.ok('Tenant group created')
    load()
  }
  const updateGroup = async (gid, body, okMessage) => {
    const r = await api.patch(`/admin/tenant-groups/${gid}`, adminKey, body)
    if (!r.ok) {
      toast.bad(r.error.message)
      load()
      return false
    }
    setGroups(gs => gs.map(g => g.id === gid ? r.data : g))
    if (okMessage) toast.ok(okMessage)
    return true
  }
  const toggleGroupMember = (group, field, id) => {
    const current = group[field] || []
    const next = current.includes(id) ? current.filter(x => x !== id) : [...current, id]
    updateGroup(group.id, { [field]: next })
  }
  const renameGroup = (group) => {
    const name = window.prompt('Group name:', group.name)
    const trimmed = name?.trim()
    if (!trimmed || trimmed === group.name) return
    updateGroup(group.id, { name: trimmed }, 'Group name updated')
  }
  const deleteGroup = async (group) => {
    if (!window.confirm(`Delete group "${group.name}"? Tenants are not deleted, but manager access granted through this group is revoked immediately.`)) return
    const r = await api.del(`/admin/tenant-groups/${group.id}`, adminKey)
    if (!r.ok) return toast.bad(r.error.message)
    if (expandedGroup === group.id) setExpandedGroup(null)
    toast.ok('Tenant group deleted')
    load()
  }
  const managers = rows.filter(u => u.role === 'tenant_manager')

  return (
    <div style={{ flex: 1, overflowY: 'auto' }} className="fade">
      <header style={{ padding: '26px 34px 20px' }}>
        <h1 style={{ fontSize: 22, fontWeight: 700, letterSpacing: '-.02em' }}>Console Users</h1>
        <p style={{ color: 'var(--fg-muted)', marginTop: 4, fontSize: 13.5 }}>
          Login accounts for this console. <b>Platform Admin</b> sees every tenant; <b>Tenant Manager</b> sees their own tenants plus tenants from assigned groups.
          Microsoft and Google accounts sign in via SSO (no local password).
        </p>
      </header>

      <div style={{ padding: '0 34px 18px' }}>
        <form onSubmit={create} style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'flex-end', border: '1px solid var(--border)', borderRadius: 14, background: 'var(--surface)', padding: 16 }}>
          <Field label="Email"><input required type="email" value={form.email} onChange={e => setForm({ ...form, email: e.target.value })} placeholder="manager@company.com" style={{ ...input, width: 220 }} /></Field>
          <Field label="Name"><input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="(optional)" style={{ ...input, width: 160 }} /></Field>
          <Field label="Role">
            <select value={form.role} onChange={e => setForm({ ...form, role: e.target.value })} style={{ ...input, width: 170 }}>
              <option value="tenant_manager">Tenant Manager</option>
              <option value="admin">Platform Admin</option>
            </select>
          </Field>
          <Field label="Login via">
            <select value={form.provider} onChange={e => setForm({ ...form, provider: e.target.value })} style={{ ...input, width: 150 }}>
              <option value="local">Email + password</option>
              <option value="microsoft">Microsoft SSO</option>
              <option value="google">Google SSO</option>
            </select>
          </Field>
          {form.provider === 'local' && (
            <Field label="Password (min 8)"><input required type="password" value={form.password} onChange={e => setForm({ ...form, password: e.target.value })} style={{ ...input, width: 160 }} /></Field>
          )}
          <button type="submit" disabled={busy} style={{ height: 38, padding: '0 16px', border: 'none', borderRadius: 10, background: 'var(--accent)', color: '#fff', fontSize: 13, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 7 }}>
            {busy ? <span className="spin" /> : <Icon name="person_add" size={16} />}Add user
          </button>
        </form>
      </div>

      <section style={{ margin: '0 34px 22px', border: '1px solid var(--border)', borderRadius: 14, background: 'var(--surface)', overflow: 'hidden' }}>
        <div style={{ padding: '15px 16px', display: 'flex', gap: 14, alignItems: 'center', justifyContent: 'space-between', borderBottom: groups.length ? '1px solid var(--border)' : 'none' }}>
          <div>
            <div style={{ fontSize: 14, fontWeight: 700 }}>Tenant Groups</div>
            <div style={{ fontSize: 11.5, color: 'var(--fg-subtle)', marginTop: 3 }}>Group tenants, then assign one or more Tenant Managers.</div>
          </div>
          <form onSubmit={createGroup} style={{ display: 'flex', gap: 8 }}>
            <input value={groupName} onChange={e => setGroupName(e.target.value)} placeholder="e.g. Group A" maxLength={120}
              style={{ ...input, width: 210 }} />
            <button type="submit" disabled={!groupName.trim()}
              style={{ height: 38, padding: '0 14px', border: 'none', borderRadius: 10, background: 'var(--accent)', color: '#fff', fontWeight: 600, display: 'flex', alignItems: 'center', gap: 6, opacity: groupName.trim() ? 1 : .5 }}>
              <Icon name="create_new_folder" size={16} />Add group
            </button>
          </form>
        </div>
        {groups.length === 0 && (
          <div style={{ color: 'var(--fg-subtle)', fontSize: 12.5, padding: '18px 16px' }}>
            No groups yet. New Tenant Manager users won't see any tenants until they're assigned to a group or create their own tenant.
          </div>
        )}
        {groups.map(group => {
          const open = expandedGroup === group.id
          return (
            <div key={group.id} style={{ borderTop: groups[0].id === group.id ? 'none' : '1px solid var(--border)' }}>
              <div style={{ padding: '11px 14px', display: 'flex', alignItems: 'center', gap: 10 }}>
                <button onClick={() => setExpandedGroup(open ? null : group.id)}
                  style={{ flex: 1, minWidth: 0, border: 0, background: 'transparent', color: 'var(--fg)', padding: 0, display: 'flex', alignItems: 'center', gap: 10, textAlign: 'left' }}>
                  <span style={{ width: 30, height: 30, borderRadius: 9, background: 'var(--accent-soft)', color: 'var(--accent)', display: 'grid', placeItems: 'center' }}>
                    <Icon name="folder_shared" size={17} />
                  </span>
                  <span style={{ flex: 1 }}>
                    <span style={{ display: 'block', fontSize: 13.5, fontWeight: 650 }}>{group.name}</span>
                    <span style={{ display: 'block', color: 'var(--fg-subtle)', fontSize: 11.5, marginTop: 2 }}>
                      {group.tenant_ids.length} tenants · {group.user_ids.length} managers
                    </span>
                  </span>
                  <Icon name={open ? 'expand_less' : 'expand_more'} size={18} />
                </button>
                <IconBtn title="Rename group" icon="edit" onClick={() => renameGroup(group)} />
                <IconBtn title="Delete group" icon="delete" danger onClick={() => deleteGroup(group)} />
              </div>
              {open && (
                <div style={{ background: 'var(--surface-2)', borderTop: '1px solid var(--border)', padding: 14, display: 'grid', gridTemplateColumns: 'minmax(260px,1fr) minmax(260px,1fr)', gap: 14 }}>
                  <MemberBox title="Tenants in this group" empty="No tenants available yet.">
                    {tenants.map(tenant => (
                      <MemberCheck key={tenant.id} checked={group.tenant_ids.includes(tenant.id)}
                        label={tenant.name} detail={tenant.status}
                        onChange={() => toggleGroupMember(group, 'tenant_ids', tenant.id)} />
                    ))}
                  </MemberBox>
                  <MemberBox title="Tenant Managers in this group" empty="No Tenant Manager accounts yet.">
                    {managers.map(manager => (
                      <MemberCheck key={manager.id} checked={group.user_ids.includes(manager.id)}
                        label={manager.name || manager.email}
                        detail={manager.email + (manager.active ? '' : ' · inactive')}
                        onChange={() => toggleGroupMember(group, 'user_ids', manager.id)} />
                    ))}
                  </MemberBox>
                </div>
              )}
            </div>
          )
        })}
      </section>

      <div style={{ padding: '0 34px 40px', display: 'flex', flexDirection: 'column', gap: 6 }}>
        {rows.length === 0 && <div style={{ color: 'var(--fg-subtle)', padding: '40px 0', textAlign: 'center', border: '1px dashed var(--border)', borderRadius: 14 }}>
          No users yet. Create the first account above (you're signed in with {user?.via === 'master_key' ? 'the master key' : 'a session'}).
        </div>}
        {rows.map(u => (
          <div key={u.id} style={{ display: 'flex', alignItems: 'center', gap: 12, border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface)', padding: '10px 14px', opacity: u.active ? 1 : .55 }}>
            <div style={{ width: 34, height: 34, borderRadius: '50%', background: 'var(--accent-soft)', color: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 700, fontSize: 13, flexShrink: 0 }}>
              {(u.name || u.email)[0].toUpperCase()}
            </div>
            <div style={{ flex: 1, minWidth: 0, lineHeight: 1.3 }}>
              <div style={{ fontWeight: 600, fontSize: 13.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{u.name || u.email}</div>
              <div className="mono" style={{ fontSize: 11.5, color: 'var(--fg-subtle)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{u.email}</div>
              {u.role === 'tenant_manager' && (
                <div style={{ fontSize: 10.5, color: 'var(--accent)', marginTop: 3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {groups.filter(g => g.user_ids.includes(u.id)).map(g => g.name).join(', ') || 'Not assigned to any group'}
                </div>
              )}
            </div>
            <span style={{ fontSize: 10.5, fontWeight: 600, padding: '3px 9px', borderRadius: 999, background: 'var(--surface-2)', color: 'var(--fg-muted)', flexShrink: 0, display: 'flex', alignItems: 'center', gap: 5 }}>
              <Icon name={(PROVIDERS[u.provider] || PROVIDERS.local).icon} size={13} />
              {(PROVIDERS[u.provider] || PROVIDERS.local).label}
            </span>
            <select value={u.role} onChange={e => patch(u.id, { role: e.target.value }, 'Role updated')} disabled={u.id === user?.user_id}
              style={{ ...input, height: 32, fontSize: 12, width: 150 }}>
              <option value="tenant_manager">Tenant Manager</option>
              <option value="admin">Platform Admin</option>
            </select>
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--fg-subtle)', flex: '0 0 120px' }}>
              {u.last_login ? 'last login ' + fmtShort(u.last_login) : 'never logged in'}
            </span>
            {u.provider === 'local' && <IconBtn title="Reset password" icon="lock_reset" onClick={() => resetPw(u)} />}
            <IconBtn title={u.active ? 'Deactivate (revokes sessions)' : 'Reactivate'} icon={u.active ? 'block' : 'check_circle'}
              onClick={() => patch(u.id, { active: !u.active }, u.active ? 'User deactivated' : 'User activated')} disabled={u.id === user?.user_id} />
            <IconBtn title="Delete user" icon="delete" danger onClick={() => del(u)} disabled={u.id === user?.user_id} />
          </div>
        ))}
      </div>
    </div>
  )
}

function Field({ label, children }) {
  return <label style={{ display: 'flex', flexDirection: 'column', gap: 6, fontSize: 11.5, fontWeight: 600, color: 'var(--fg-subtle)' }}>{label}{children}</label>
}

function MemberBox({ title, empty, children }) {
  const items = React.Children.toArray(children)
  return (
    <div style={{ border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface)', padding: 10 }}>
      <div style={{ fontSize: 11.5, fontWeight: 700, color: 'var(--fg-muted)', marginBottom: 8 }}>{title}</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 5, maxHeight: 230, overflowY: 'auto' }}>
        {items.length ? items : <div style={{ color: 'var(--fg-subtle)', fontSize: 11.5, padding: 6 }}>{empty}</div>}
      </div>
    </div>
  )
}

function MemberCheck({ checked, label, detail, onChange, disabled }) {
  return (
    <label aria-label={label} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 8px', borderRadius: 8, background: checked ? 'var(--accent-soft)' : 'transparent', opacity: disabled ? .5 : 1, cursor: disabled ? 'not-allowed' : 'pointer' }}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={onChange} />
      <span style={{ minWidth: 0 }}>
        <span style={{ display: 'block', fontSize: 12.5, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{label}</span>
        <span style={{ display: 'block', fontSize: 10.5, color: 'var(--fg-subtle)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{detail}</span>
      </span>
    </label>
  )
}

function IconBtn({ title, icon, onClick, danger, disabled }) {
  return (
    <button title={title} onClick={onClick} disabled={disabled}
      style={{ border: '1px solid var(--border)', background: 'var(--surface-2)', color: danger ? 'var(--danger)' : 'var(--fg-muted)', borderRadius: 8, width: 32, height: 32, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, opacity: disabled ? .4 : 1, cursor: disabled ? 'not-allowed' : 'pointer' }}>
      <Icon name={icon} size={16} />
    </button>
  )
}

UsersView.propTypes = {
  adminKey: PropTypes.string,
  toast: PropTypes.shape({
    bad: PropTypes.func.isRequired,
    ok: PropTypes.func.isRequired,
  }).isRequired,
  user: PropTypes.shape({
    user_id: PropTypes.string,
    via: PropTypes.string,
  }),
}
Field.propTypes = {
  label: PropTypes.string.isRequired,
  children: PropTypes.node.isRequired,
}
MemberBox.propTypes = {
  title: PropTypes.string.isRequired,
  empty: PropTypes.string.isRequired,
  children: PropTypes.node,
}
MemberCheck.propTypes = {
  checked: PropTypes.bool.isRequired,
  label: PropTypes.string.isRequired,
  detail: PropTypes.string,
  onChange: PropTypes.func.isRequired,
  disabled: PropTypes.bool,
}
IconBtn.propTypes = {
  title: PropTypes.string.isRequired,
  icon: PropTypes.string.isRequired,
  onClick: PropTypes.func.isRequired,
  danger: PropTypes.bool,
  disabled: PropTypes.bool,
}
