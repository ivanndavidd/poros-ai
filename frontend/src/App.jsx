import React, { useState, useEffect, useCallback } from 'react'
import PropTypes from 'prop-types'
import { toastType } from './propTypes.js'
import { api, devAuth } from './api.js'
import { Icon, ToastHost, useToast } from './tos.jsx'
import TenantsView from './views/Tenants.jsx'
import KnowledgeBaseView from './views/KnowledgeBase.jsx'
import MonitoringView from './views/Monitoring.jsx'
import ChatbotView from './views/Chatbot.jsx'
import AuditView from './views/Audit.jsx'
import SqlConsoleView from './views/SqlConsole.jsx'
import FeedbackView from './views/Feedback.jsx'
import ApiDocsView from './views/ApiDocs.jsx'
import UsersView from './views/Users.jsx'
import TenantConfigModal from './views/ConfigModal.jsx'
import PorosMark from './components/Logo.jsx'

const NAV = [
  { id: 'tenants', label: 'Tenant Management', icon: 'groups' },
  { id: 'knowledge', label: 'Knowledge Base', icon: 'account_tree' },
  { id: 'monitoring', label: 'Monitoring Dashboard', icon: 'monitoring' },
  { id: 'chatbot', label: 'Consumer ChatBot', icon: 'forum' },
  { id: 'sql', label: 'SQL Query', icon: 'terminal' },
  { id: 'audit', label: 'Audit Log', icon: 'history' },
  { id: 'feedback', label: 'Response Feedback', icon: 'reviews' },
  { id: 'apidocs', label: 'API Docs', icon: 'code' },
  { id: 'users', label: 'Console Users', icon: 'manage_accounts', adminOnly: true },
]

const DEFAULT_VIEW = 'tenants'
const VIEW_IDS = new Set(NAV.map(item => item.id))
const routeHash = (view) => `#/${view}`
const readViewFromHash = () => {
  const view = window.location.hash.replace(/^#\/?/, '')
  return VIEW_IDS.has(view) ? view : DEFAULT_VIEW
}

const ROLE_LABEL = { admin: 'Platform Admin', tenant_manager: 'Tenant Manager' }

// Console sessions are HttpOnly cookies. Master key is break-glass and intentionally
// lives in memory only, so refreshing requires entering it again.
const clearLegacyCred = () => {
  localStorage.removeItem('porosCred')
  localStorage.removeItem('adminKey')
}

export default function Root() { return <ToastHost><App /></ToastHost> }

function App() {
  const toast = useToast()
  const [cred, setCred] = useState('')
  const [user, setUser] = useState(null)          // {name,email,role,via} from /admin/whoami
  const [cfg, setCfg] = useState({
    gateway: false,
    sso_configured: false,
    microsoft_sso_configured: false,
    google_sso_configured: false,
  })  // deployment mode
  const [gatewayError, setGatewayError] = useState('')
  const [booting, setBooting] = useState(true)
  const [theme, setTheme] = useState(localStorage.getItem('tosTheme') || 'light')
  const [view, setView] = useState(readViewFromHash)
  const [chatWasOpened, setChatWasOpened] = useState(() => readViewFromHash() === 'chatbot')
  const [tenants, setTenants] = useState([])
  const [modalTenant, setModalTenant] = useState(null)

  const navigateTo = useCallback((next) => {
    const safe = VIEW_IDS.has(next) ? next : DEFAULT_VIEW
    setView(safe)
    if (window.location.hash !== routeHash(safe)) window.location.hash = routeHash(safe)
  }, [])

  useEffect(() => {
    if (view === 'chatbot') setChatWasOpened(true)
  }, [view])

  const loadTenants = useCallback(async (key) => {
    const r = await api.get('/admin/tenants', key)
    if (r.ok) { setTenants(r.data); return true }
    return r
  }, [])

  // sign-in with a credential (session token or master key): resolve who it is, then load.
  // Uses /admin/whoami (a NON-reserved path) not /auth/me, so it ALSO works behind the Apps
  // Gateway, where /auth/** is the gateway's. In gateway mode k is '' and identity comes
  // from the gateway-injected X-User-Id header, so nothing is stored locally.
  const signIn = useCallback(async (k) => {
    const me = await api.get('/admin/whoami', k)
    if (!me.ok) return me
    const t = await loadTenants(k)
    if (t !== true) return t
    setUser(me.data); setCred(k || '')
    return true
  }, [loadTenants])

  useEffect(() => { (async () => {
    // discover deployment mode from a public, non-reserved path (works before login and
    // whether or not the Apps Gateway is in front)
    // Cache-bust deployment mode: an operator may enable SSO while this dev server and
    // browser profile are already running, and the old `false` must not hide the button.
    const c = await api.get(`/dangerously-skip-login/console-config?_=${Date.now()}`, '')
    const mode = c.ok ? c.data : {
      gateway: false,
      sso_configured: false,
      microsoft_sso_configured: false,
      google_sso_configured: false,
    }
    setCfg(mode)
    // "Which build am I on?" — answered in F12 without hunting a dashboard. The same
    // fields are on /dangerously-skip-login/health for curl (no gateway login needed).
    if (c.ok) {
      const builtAt = mode.built ? ` (image built ${mode.built})` : ''
      console.log(`Poros build ${mode.version || 'unknown'}${builtAt}`)
    }

    // SSO callback sets an HttpOnly session cookie and lands on /#sso_ok=1.
    const h = window.location.hash || ''
    let ssoOk = false
    if (h.startsWith('#sso_ok=')) {
      ssoOk = true
      history.replaceState(null, '', window.location.pathname)
    } else if (h.startsWith('#sso_error=')) {
      toast.bad('SSO: ' + decodeURIComponent(h.slice('#sso_error='.length)))
      history.replaceState(null, '', window.location.pathname)
    }
    clearLegacyCred()
    if (mode.gateway) {
      // Behind the gateway there is no app login or stored credential. If identity
      // bootstrap fails, production shows a configuration error rather than a second
      // password screen; the dev build still offers its local gateway simulator.
      const gatewayOk = await signIn('')
      if (gatewayOk !== true) {
        setUser(null)
        setGatewayError(
          gatewayOk?.error?.message
          || 'The Apps Gateway did not pass a user identity to Poros.'
        )
      }
    } else {
      // Also runs on ordinary refresh: a live HttpOnly cookie restores the session.
      const ok = await signIn('')
      if (ok !== true) setUser(null)
      else if (ssoOk) toast.ok('Signed in')
    }
    setBooting(false)
  })() }, [])
  useEffect(() => { localStorage.setItem('tosTheme', theme) }, [theme])

  // Keep the active workspace in the URL so refresh and browser Back/Forward preserve it.
  // Wait until boot completes because the SSO callback temporarily uses the URL hash too.
  useEffect(() => {
    if (booting) return undefined
    const syncView = () => {
      const next = readViewFromHash()
      setView(next)
      if (window.location.hash !== routeHash(next)) {
        history.replaceState(null, '', `${window.location.pathname}${window.location.search}${routeHash(next)}`)
      }
    }
    syncView()
    window.addEventListener('hashchange', syncView)
    return () => window.removeEventListener('hashchange', syncView)
  }, [booting])

  // Console Users is admin-only; a copied/deep link must not leave other roles on a blank page.
  useEffect(() => {
    if (user && user.role !== 'admin' && view === 'users') navigateTo(DEFAULT_VIEW)
  }, [navigateTo, user, view])

  const reload = useCallback(() => loadTenants(cred), [cred, loadTenants])
  const openTenant = (t) => setModalTenant(t)
  const signOut = async () => {
    if (user?.via === 'gateway') {
      // dev gateway-sim: no real gateway to log out of — just drop the simulated identity
      if (devAuth.enabled) { devAuth.clear(); setUser(null); setCred(''); navigateTo(DEFAULT_VIEW); return }
      // behind the real gateway: logout is the gateway's — full-page navigation (never SPA
      // routing) to /auth/logout so it clears the gateway session / does CAS single-logout
      clearLegacyCred(); window.location.assign('/auth/logout'); return
    }
    if (user?.via === 'session') await api.post('/auth/logout', cred, {})
    clearLegacyCred(); setUser(null); setCred(''); navigateTo(DEFAULT_VIEW)
  }

  if (booting) return <div style={{ display: 'grid', placeItems: 'center', height: '100vh', background: 'var(--bg)' }} data-theme={theme}><span className="spin" style={{ color: 'var(--accent)' }} /></div>
  if (!user && cfg.gateway && !devAuth.enabled) return <GatewayIdentityError
    theme={theme}
    message={gatewayError}
  />
  if (!user) return <Login
    theme={theme}
    toast={toast}
    gateway={cfg.gateway}
    microsoftSsoConfigured={cfg.microsoft_sso_configured ?? cfg.sso_configured}
    googleSsoConfigured={cfg.google_sso_configured}
    onSignIn={async (k) => {
      const ok = await signIn(k)
      if (ok === true) toast.ok('Signed in')
      else toast.bad(ok?.error?.message || 'Sign-in failed')
      return ok === true
    }}
  />

  const isAdmin = user.role === 'admin'
  const nav = NAV.filter(n => !n.adminOnly || isAdmin)
  const initials = (user.name || user.email || 'U').split(/[\s@.]+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join('') || 'U'
  const ctx = { adminKey: cred, toast, tenants, reload, openTenant, theme, user }
  return (
    <div data-theme={theme} style={{ height: '100vh', display: 'flex', overflow: 'hidden', background: 'var(--bg)', color: 'var(--fg)', fontFamily: "'IBM Plex Sans',sans-serif", fontSize: 14 }}>
      <aside style={{ width: 248, flexShrink: 0, background: 'var(--sidebar)', borderRight: '1px solid var(--border)', display: 'flex', flexDirection: 'column', padding: '18px 14px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 11, padding: '6px 8px 18px' }}>
          <PorosMark size={36} />
          <div style={{ lineHeight: 1.2, overflow: 'hidden' }}>
            <div style={{ fontWeight: 700, fontSize: 15, letterSpacing: '-.01em', whiteSpace: 'nowrap' }}>Poros</div>
            <div style={{ fontSize: 11, color: 'var(--fg-subtle)', whiteSpace: 'nowrap' }}>Governance Platform</div>
          </div>
        </div>
        <div style={sideLabel}>Workspace</div>
        <nav style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
          {nav.map(n => {
            const active = view === n.id
            return (
              <button key={n.id} onClick={() => navigateTo(n.id)} style={{ display: 'flex', alignItems: 'center', gap: 12, width: '100%', padding: '10px 12px', border: 'none', borderRadius: 10, fontSize: 13.5, fontWeight: active ? 600 : 500, textAlign: 'left', background: active ? 'var(--accent-soft)' : 'transparent', color: active ? 'var(--accent)' : 'var(--fg-muted)' }}>
                <Icon name={n.icon} size={20} /><span style={{ flex: 1 }}>{n.label}</span>
                {active && <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--accent)' }} />}
              </button>
            )
          })}
        </nav>
        <div style={{ marginTop: 'auto', display: 'flex', flexDirection: 'column', gap: 10 }}>
          <button onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 12px', border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface-2)', color: 'var(--fg-muted)', fontSize: 13, fontWeight: 500 }}>
            <Icon name={theme === 'light' ? 'dark_mode' : 'light_mode'} size={18} /><span style={{ flex: 1, textAlign: 'left' }}>{theme === 'light' ? 'Dark mode' : 'Light mode'}</span>
          </button>
          <div style={{ display: 'flex', alignItems: 'center', gap: 11, padding: '9px 6px', borderTop: '1px solid var(--border)' }}>
            <div style={{ width: 32, height: 32, borderRadius: '50%', background: 'var(--accent-soft)', color: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 700, fontSize: 13, flexShrink: 0 }}>{initials}</div>
            <div style={{ lineHeight: 1.25, overflow: 'hidden', flex: 1 }}>
              <div style={{ fontWeight: 600, fontSize: 13, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={user.email || undefined}>{user.name || user.email}</div>
              <div style={{ fontSize: 11, color: 'var(--fg-subtle)', whiteSpace: 'nowrap' }}>{ROLE_LABEL[user.role] || user.role}</div>
            </div>
            <button title="Sign out" onClick={signOut} style={{ border: 'none', background: 'transparent', color: 'var(--fg-subtle)', display: 'flex' }}><Icon name="logout" size={18} /></button>
          </div>
        </div>
      </aside>

      <main style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        {view === 'tenants' && <TenantsView {...ctx} />}
        {view === 'knowledge' && <KnowledgeBaseView {...ctx} />}
        {view === 'monitoring' && <MonitoringView {...ctx} />}
        {/* Keep Chatbot mounted while another menu is visible. Its stream and
            encrypted-session persistence must continue until done or explicit Stop. */}
        {(chatWasOpened || view === 'chatbot') && (
          <div aria-hidden={view !== 'chatbot'}
            style={{ flex: 1, minWidth: 0, overflow: 'hidden', display: view === 'chatbot' ? 'flex' : 'none' }}>
            <ChatbotView {...ctx} />
          </div>
        )}
        {view === 'sql' && <SqlConsoleView {...ctx} />}
        {view === 'audit' && <AuditView {...ctx} />}
        {view === 'feedback' && <FeedbackView {...ctx} />}
        {view === 'apidocs' && <ApiDocsView {...ctx} />}
        {view === 'users' && isAdmin && <UsersView {...ctx} />}
      </main>

      {modalTenant && <TenantConfigModal tenant={modalTenant} adminKey={cred} toast={toast}
        onClose={() => setModalTenant(null)} onSaved={reload} />}
    </div>
  )
}

const sideLabel = { fontSize: 10.5, fontWeight: 600, letterSpacing: '.07em', color: 'var(--fg-subtle)', textTransform: 'uppercase', padding: '8px 10px 6px' }

const inputStyle = { width: '100%', height: 42, padding: '0 13px', border: '1px solid var(--border)', borderRadius: 10, background: 'var(--bg)', color: 'var(--fg)', fontSize: 14, outline: 'none' }
const labelStyle = { fontSize: 12.5, fontWeight: 500, color: 'var(--fg-muted)', display: 'block', margin: '12px 0 7px' }

function GatewayIdentityError({ theme, message }) {
  return (
    <div data-theme={theme} style={{ display: 'grid', placeItems: 'center', height: '100vh', background: 'radial-gradient(1100px 560px at 50% -10%,var(--accent-soft) 0%,var(--bg) 55%)', color: 'var(--fg)' }}>
      <section role="alert" style={{ width: 'min(440px,92vw)', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 18, padding: 26, boxShadow: '0 30px 80px -20px rgba(0,0,0,.4)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 11, marginBottom: 16 }}>
          <PorosMark size={40} />
          <div>
            <div style={{ fontWeight: 700, fontSize: 16 }}>Poros</div>
            <div style={{ fontSize: 12, color: 'var(--fg-subtle)' }}>Governance Platform</div>
          </div>
        </div>
        <div style={{ fontWeight: 650, marginBottom: 8 }}>Apps Gateway identity unavailable</div>
        <p style={{ margin: 0, color: 'var(--fg-muted)', fontSize: 13, lineHeight: 1.55 }}>
          {message || 'Gateway sign-in succeeded, but no user identity reached the app.'}
        </p>
        <p style={{ margin: '10px 0 0', color: 'var(--fg-subtle)', fontSize: 12, lineHeight: 1.5 }}>
          Ask your ops team to check the <code>X-User-Id</code> header. No need to sign in to Poros again.
        </p>
        <div style={{ display: 'flex', gap: 9, marginTop: 18 }}>
          <button type="button" onClick={() => window.location.reload()}
            style={{ flex: 1, height: 40, border: 'none', borderRadius: 10, background: 'var(--accent)', color: '#fff', fontSize: 13, fontWeight: 600 }}>
            Try again
          </button>
          <button type="button" onClick={() => window.location.assign('/auth/logout')}
            style={{ flex: 1, height: 40, border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface-2)', color: 'var(--fg)', fontSize: 13, fontWeight: 600 }}>
            Sign in again via gateway
          </button>
        </div>
      </section>
    </div>
  )
}

GatewayIdentityError.propTypes = {
  theme: PropTypes.string.isRequired,
  message: PropTypes.string,
}

function Login({ theme, toast, gateway, microsoftSsoConfigured, googleSsoConfigured, onSignIn }) {
  const [mode, setMode] = useState('password')       // 'password' | 'masterkey'
  const [email, setEmail] = useState('')
  const [pw, setPw] = useState('')
  const [mk, setMk] = useState('')
  const [busy, setBusy] = useState(false)
  const [devEmail, setDevEmail] = useState(devAuth.get())
  // Provider availability comes from /console-config (a public, non-reserved path).
  const microsoftSso = !!microsoftSsoConfigured
  const googleSso = !!googleSsoConfigured
  const anySso = microsoftSso || googleSso

  // DEV-only: act as an Apps Gateway user locally (sends a simulated X-User-Id). Shown only
  // in a dev build AND when the backend is trusting the header (APPS_GATEWAY_TRUST_HEADER=1,
  // reported via /console-config as `gateway`) — the only time it can actually resolve.
  const devSignIn = async () => {
    const e = devEmail.trim()
    if (!e) return
    devAuth.set(e)
    const ok = await onSignIn('')       // gateway-style: identity comes from the simulated header
    if (!ok) devAuth.clear()            // onSignIn already toasts the reason
  }

  const submit = async (e) => {
    e.preventDefault(); setBusy(true)
    try {
      if (mode === 'masterkey') { await onSignIn(mk.trim()); return }
      const r = await api.post('/auth/login', '', { email: email.trim(), password: pw })
      if (!r.ok) { toast.bad(r.error?.message || 'Sign-in failed'); return }
      await onSignIn('')
    } finally { setBusy(false) }
  }

  return (
    <div data-theme={theme} style={{ display: 'grid', placeItems: 'center', height: '100vh', background: 'radial-gradient(1100px 560px at 50% -10%,var(--accent-soft) 0%,var(--bg) 55%)', color: 'var(--fg)' }}>
      <form onSubmit={submit} style={{ width: 'min(400px,92vw)', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 18, padding: 26, boxShadow: '0 30px 80px -20px rgba(0,0,0,.4)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 11, marginBottom: 10 }}>
          <PorosMark size={40} />
          <div><div style={{ fontWeight: 700, fontSize: 16 }}>Poros</div><div style={{ fontSize: 12, color: 'var(--fg-subtle)' }}>Governance Platform · sign in</div></div>
        </div>

        {gateway ? (
          /* ---- GATEWAY MODE ----------------------------------------------------------
             The gateway itself handles login, so the standalone email/password form and
             the Microsoft SSO button are irrelevant here and hidden. We show the dev
             simulation (dev builds only) + a discreet break-glass master key. In a prod
             build the dev panel is stripped, leaving break-glass as the only UI path —
             which is why it's never removed entirely. */
          <>
            <div style={{ fontSize: 12, color: 'var(--fg-muted)', background: 'var(--surface-2)', border: '1px solid var(--border)', borderRadius: 10, padding: '10px 12px', marginTop: 12, lineHeight: 1.45 }}>
              <b>Apps Gateway</b> mode is on — in production, the gateway handles sign-in (identity via <code>X-User-Id</code>). This screen only appears locally, with no gateway in front.
            </div>

            {devAuth.enabled && (
              <div style={{ marginTop: 12, padding: 12, border: '1px dashed var(--border)', borderRadius: 10, background: 'var(--surface-2)' }}>
                <div style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: '.06em', color: 'var(--fg-subtle)', textTransform: 'uppercase', marginBottom: 6 }}>Dev · Apps Gateway simulation</div>
                <div style={{ fontSize: 11.5, color: 'var(--fg-subtle)', marginBottom: 8, lineHeight: 1.4 }}>Sign in as a gateway user (sends <code>X-User-Id</code>). Dev builds only — never shipped to production.</div>
                <div style={{ display: 'flex', gap: 8 }}>
                  <input type="email" autoFocus value={devEmail} onChange={e => setDevEmail(e.target.value)} placeholder="name@company.com"
                    onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); devSignIn() } }}
                    style={{ ...inputStyle, height: 38 }} />
                  <button type="button" onClick={devSignIn} style={{ height: 38, padding: '0 15px', border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface)', color: 'var(--fg)', fontSize: 13, fontWeight: 600, whiteSpace: 'nowrap' }}>Sign in</button>
                </div>
              </div>
            )}

            {mode === 'masterkey' && (
              <div style={{ marginTop: 14 }}>
                <label style={labelStyle}>Master admin key</label>
                <input type="password" autoFocus value={mk} onChange={e => setMk(e.target.value)} placeholder="admin-dev-key" style={inputStyle} />
                <button type="submit" disabled={busy} style={{ marginTop: 12, width: '100%', height: 42, border: 'none', borderRadius: 10, background: 'var(--accent)', color: '#fff', fontSize: 14, fontWeight: 600, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8 }}>
                  {busy && <span className="spin" />}Sign in
                </button>
              </div>
            )}

            <button type="button" onClick={() => setMode(mode === 'masterkey' ? 'password' : 'masterkey')}
              style={{ marginTop: 14, width: '100%', border: 'none', background: 'transparent', color: 'var(--fg-subtle)', fontSize: 11.5, textDecoration: 'underline', cursor: 'pointer' }}>
              {mode === 'masterkey' ? 'Hide break-glass' : 'Break-glass: master admin key'}
            </button>
          </>
        ) : (
          /* ---- STANDALONE MODE: email/password (+ master key + configured SSO providers) ---- */
          <>
            {mode === 'password' ? (
              <>
                <label style={labelStyle}>Email</label>
                <input type="email" autoFocus autoComplete="username" value={email} onChange={e => setEmail(e.target.value)} placeholder="name@company.com" style={inputStyle} />
                <label style={labelStyle}>Password</label>
                <input type="password" autoComplete="current-password" value={pw} onChange={e => setPw(e.target.value)} placeholder="••••••••" style={inputStyle} />
              </>
            ) : (
              <>
                <label style={labelStyle}>Master admin key</label>
                <input type="password" autoFocus value={mk} onChange={e => setMk(e.target.value)} placeholder="admin-dev-key" style={inputStyle} />
                <div style={{ fontSize: 11.5, color: 'var(--fg-subtle)', marginTop: 8 }}>Break-glass / bootstrap: sign in as Platform Admin, then create accounts in Console Users.</div>
              </>
            )}

            <button type="submit" disabled={busy} style={{ marginTop: 16, width: '100%', height: 42, border: 'none', borderRadius: 10, background: 'var(--accent)', color: '#fff', fontSize: 14, fontWeight: 600, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8 }}>
              {busy && <span className="spin" />}Sign in
            </button>

            {anySso && mode === 'password' && (
              <>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, margin: '14px 0' }}>
                  <span style={{ flex: 1, height: 1, background: 'var(--border)' }} />
                  <span style={{ fontSize: 11, color: 'var(--fg-subtle)' }}>or</span>
                  <span style={{ flex: 1, height: 1, background: 'var(--border)' }} />
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {googleSso && (
                    <button type="button" onClick={() => window.location.assign(api.base + '/auth/sso/google/login')}
                      style={{ width: '100%', height: 42, border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface-2)', color: 'var(--fg)', fontSize: 13.5, fontWeight: 600, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 9 }}>
                      <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
                        <path fill="#4285F4" d="M17.64 9.205c0-.638-.057-1.252-.164-1.841H9v3.482h4.844a4.14 4.14 0 0 1-1.797 2.715v2.258h2.909c1.702-1.567 2.684-3.875 2.684-6.614z"/>
                        <path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.181l-2.909-2.258c-.806.54-1.836.859-3.047.859-2.344 0-4.328-1.585-5.037-3.714H.956v2.332A9 9 0 0 0 9 18z"/>
                        <path fill="#FBBC05" d="M3.963 10.706A5.41 5.41 0 0 1 3.681 9c0-.592.102-1.168.282-1.706V4.962H.956A9 9 0 0 0 0 9c0 1.452.347 2.827.956 4.038l3.007-2.332z"/>
                        <path fill="#EA4335" d="M9 3.58c1.321 0 2.507.454 3.441 1.346l2.581-2.581C13.463.892 11.426 0 9 0A9 9 0 0 0 .956 4.962l3.007 2.332C4.672 5.165 6.656 3.58 9 3.58z"/>
                      </svg>
                      Sign in with Google
                    </button>
                  )}
                  {microsoftSso && (
                    <button type="button" onClick={() => window.location.assign(api.base + '/auth/sso/microsoft/login')}
                      style={{ width: '100%', height: 42, border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface-2)', color: 'var(--fg)', fontSize: 13.5, fontWeight: 600, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 9 }}>
                      <svg width="17" height="17" viewBox="0 0 21 21" aria-hidden="true"><rect x="1" y="1" width="9" height="9" fill="#f25022"/><rect x="11" y="1" width="9" height="9" fill="#7fba00"/><rect x="1" y="11" width="9" height="9" fill="#00a4ef"/><rect x="11" y="11" width="9" height="9" fill="#ffb900"/></svg>
                      Sign in with Microsoft
                    </button>
                  )}
                </div>
              </>
            )}

            <button type="button" onClick={() => setMode(mode === 'password' ? 'masterkey' : 'password')}
              style={{ marginTop: 14, width: '100%', border: 'none', background: 'transparent', color: 'var(--fg-subtle)', fontSize: 11.5, textDecoration: 'underline', cursor: 'pointer' }}>
              {mode === 'password' ? 'Sign in with master admin key' : 'Sign in with email & password'}
            </button>
          </>
        )}
      </form>
    </div>
  )
}
Login.propTypes = {
  theme: PropTypes.string.isRequired,
  toast: toastType.isRequired,
  gateway: PropTypes.bool,
  microsoftSsoConfigured: PropTypes.bool,
  googleSsoConfigured: PropTypes.bool,
  onSignIn: PropTypes.func.isRequired,
}
