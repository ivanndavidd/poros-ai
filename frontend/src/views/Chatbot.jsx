import React, { useState, useEffect, useCallback, useRef } from 'react'
import PropTypes from 'prop-types'
import { api } from '../api.js'
import { Icon } from '../tos.jsx'
import { tenantType, toastType } from '../propTypes.js'
import { extractBlocks, AnswerVisuals } from './viz.jsx'

const SUGGESTIONS = [
  { icon: 'public', text: 'heatmap of sales per region from Shopee' },
  { icon: 'leaderboard', text: 'bar chart of total sales per marketplace' },
  { icon: 'show_chart', text: 'daily sales trend for the last 14 days' },
  { icon: 'dashboard', text: 'build an interactive sales dashboard per marketplace' },
]

/* Chat sessions are persisted encrypted by the backend. Governed questions, SQL and
   answers are deliberately never written to browser localStorage. */
const newSession = () => ({ id: 'cs_' + Math.random().toString(36).slice(2, 10), title: '', created: Date.now(), updated: Date.now(), messages: [], business: null })
// stable per-message render key: assigned at append/load time (an array index would
// re-key every row when history is trimmed or a session reloads)
let msgSeq = 0
const msgKey = () => `m${++msgSeq}`
// a loaded session's messages get fresh render keys (keys are never persisted)
const withMessageKeys = s => ({ ...s, messages: (s.messages || []).map(m => ({ ...m, _k: msgKey() })) })
const LONG_PASTE_CHARS = 1800
const LONG_PASTE_LINES = 14
const MAX_COMPOSER_HEIGHT = 220

function DsRow({ k, v, mono }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10 }}>
      <span style={{ color: 'var(--fg-subtle)' }}>{k}</span>
      <span className={mono ? 'mono' : undefined} style={{ color: 'var(--fg)', fontWeight: 500, textAlign: 'right', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 160 }}>{v}</span>
    </div>
  )
}

export default function ChatbotView({ adminKey, tenants, toast }) {
  const [tenantId, setTenantId] = useState(tenants[0]?.id || '')
  const [keys, setKeys] = useState([])
  const [apiKey, setApiKey] = useState('')
  const [roles, setRoles] = useState([])
  const [role, setRole] = useState('')
  const [jwt, setJwt] = useState('')
  const [dsName, setDsName] = useState('')
  const [model, setModel] = useState('agent')
  const [analysisMode, setAnalysisMode] = useState('auto')
  const [sessions, setSessions] = useState([])   // [{id,title,created,updated,messages}] — newest first
  const [sessionsReady, setSessionsReady] = useState(false)
  const [sid, setSid] = useState('')             // active session id
  const session = sessions.find(s => s.id === sid) || {}
  const messages = session.messages || []
  // the business this conversation runs in — asked once, then sent with every turn
  const business = session.business || null
  const [busySid, setBusySid] = useState('')     // sesi yang SEDANG menghasilkan jawaban ('' = idle)
  const thinking = !!busySid && busySid === sid  // indikator hanya tampil di sesi asalnya
  const [liveStatus, setLiveStatus] = useState('')
  // SATU feed kronologis utk proses live: status/finding (aktivitas kecil) + note
  // (narasi reasoning_delta sbg paragraf prosa) — dirender bergantian "teks → berpikir →
  // teks lagi" seperti Claude/Codex; aktivitas yang sudah lewat dilipat jadi satu baris.
  const [liveFeed, setLiveFeed] = useState([])
  const [streamingText, setStreamingText] = useState('')  // teks jawaban yang di-stream progresif (answer_delta)
  const [dsInfo, setDsInfo] = useState(null)
  const [dbOpen, setDbOpen] = useState(false)
  const [draft, setDraft] = useState('')
  const [pastedBlocks, setPastedBlocks] = useState([])
  const removePastedBlock = (id) => setPastedBlocks(current => current.filter(item => item.id !== id))
  const inputRef = useRef(); const scrollRef = useRef()
  const activeStreamRef = useRef(null)

  const load = useCallback(async () => {
    if (!tenantId) return
    const [k, d, mo, rl] = await Promise.all([
      api.get(`/admin/tenants/${tenantId}/apikeys`, adminKey),
      api.get(`/admin/tenants/${tenantId}/datasources`, adminKey),
      api.get(`/admin/tenants/${tenantId}/config/models`, adminKey),
      api.get(`/admin/tenants/${tenantId}/roles`, adminKey)])
    if (k.ok) { setKeys(k.data.filter(x => x.active)); setApiKey('') }
    if (d.ok) {
      const sqlSources = d.data.filter(x => x.type !== 'docs')
      setDsName(sqlSources.length ? `${sqlSources.length} SQL sources · auto-route` : 'no datasource')
      if (sqlSources.length) {
        const tabs = sqlSources.reduce((n, source) => n + Object.keys(source.mdl?.tables || {}).length, 0)
        const allow = sqlSources.reduce((n, source) => n + (source.mdl?.allowed_tables || []).length, 0)
        const metrics = sqlSources.reduce((n, source) => n + (source.mdl?.metrics || []).length, 0)
        setDsInfo({ name: 'Auto-route across datasources', type: 'governed SQL',
          dataset: 'picked from the question', tables: tabs, allow, metrics })
      } else setDsInfo(null)
    }
    if (mo.ok) { const dm = mo.data.models || []; const pm = dm.find(x => x.role === 'Primary' && x.name) || dm.find(x => x.name); setModel(pm ? pm.name : 'rule-based agent') }
    if (rl.ok) { setRoles(rl.data); setRole(p => rl.data.some(x => x.name === p) ? p : (rl.data[0]?.name || '')) }
  }, [adminKey, tenantId])
  // Tenant switch: restore encrypted backend sessions and resume the latest.
  useEffect(() => {
    let active = true
    setSessionsReady(false)
    setJwt('')
    load()
    ;(async () => {
      const legacyKey = `poros_chat_sessions_${tenantId}`
      let legacy = []
      try {
        const parsed = JSON.parse(localStorage.getItem(legacyKey) || '[]')
        legacy = Array.isArray(parsed)
          ? parsed.filter(session => session && session.id).slice(0, 12)
          : []
      } catch { legacy = [] }
      const response = tenantId
        ? await api.get(`/admin/tenants/${tenantId}/chat-sessions`, adminKey)
        : { ok: false }
      if (!active) return
      const restored = response.ok && Array.isArray(response.data)
        ? response.data.filter(session => session && session.id).slice(0, 12)
        : []
      const stamp = value => {
        const numeric = Number(value)
        if (Number.isFinite(numeric)) return numeric
        return Date.parse(value || '') || 0
      }
      const mergedById = new Map(restored.map(session => [session.id, session]))
      const toMigrate = []
      for (const session of legacy) {
        const encrypted = mergedById.get(session.id)
        if (!encrypted || stamp(session.updated) > stamp(encrypted.updated)) {
          mergedById.set(session.id, session)
          toMigrate.push({ ...session, messages: (session.messages || []).slice(-30) })
        }
      }
      if (tenantId && response.ok && legacy.length) {
        const migration = await Promise.all(toMigrate.map(session => (
          api.post(`/admin/tenants/${tenantId}/chat-sessions`, adminKey, session)
        )))
        if (migration.every(result => result.ok)) {
          // Remove the historical plaintext copy only after encrypted server
          // persistence succeeds, so the P2 migration never destroys chat history.
          try { localStorage.removeItem(legacyKey) } catch { /* storage unavailable */ }
        }
      }
      if (!active) return
      const merged = [...mergedById.values()]
        .sort((a, b) => stamp(b.updated) - stamp(a.updated))
        .slice(0, 12)
        .map(withMessageKeys)
      const initial = merged.length ? merged : [newSession()]
      setSessions(initial)
      setSid(initial[0].id)
      setSessionsReady(true)
    })()
    return () => { active = false }
  }, [tenantId, adminKey, load])
  // Debounced server persistence: max 12 sessions × 30 messages, encrypted at rest.
  useEffect(() => {
    if (!sessionsReady || !tenantId || !sessions.length) return undefined
    const timer = setTimeout(() => {
      const keep = sessions.filter(session => session.messages.length || session.id === sid)
        .slice(0, 12)
        .map(session => ({ ...session, messages: (session.messages || []).slice(-30) }))
      Promise.all(keep.map(session => (
        api.post(`/admin/tenants/${tenantId}/chat-sessions`, adminKey, session)
      ))).catch(() => {})
    }, 500)
    return () => clearTimeout(timer)
  }, [sessions, sid, tenantId, adminKey, sessionsReady])
  // ikuti konten live yang tumbuh (feed proses + jawaban streaming), tapi JANGAN
  // menyeret user yang sedang scroll ke atas membaca — hanya lengket saat dekat bawah
  const nearBottomRef = useRef(true)
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return undefined
    const onScroll = () => { nearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 140 }
    el.addEventListener('scroll', onScroll)
    return () => el.removeEventListener('scroll', onScroll)
  }, [])
  useEffect(() => { requestAnimationFrame(() => { const el = scrollRef.current; if (el && nearBottomRef.current) el.scrollTop = el.scrollHeight }) }, [messages, thinking, liveFeed, streamingText])

  // Strict Pattern B: the consumer must send a VERIFIED end-user JWT. This console mints one
  // (via the admin helper) for the chosen role; the backend verifies it and applies its RLAC/CLAC.
  // The token lives 60 minutes; one older than 50 is minted again before a question, or
  // a console left open for an hour answers every question with JWT_INVALID.
  const mintedAt = useRef(0)
  const mint = useCallback(async (r) => {
    if (!tenantId || !r) { setJwt(''); return '' }
    const res = await api.post('/admin/mint-jwt', adminKey, { tenant_id: tenantId, user_id: 'console-user', role: r, ttl_min: 60 })
    if (res.ok) { mintedAt.current = Date.now(); setJwt(res.data.token); return res.data.token }
    toast.warn(res.error?.message || 'Demo JWT is only available with a local/dev HS config')
    setJwt(''); return ''
  }, [adminKey, tenantId, toast])
  useEffect(() => { if (role) mint(role) }, [role, mint])
  const token = () => (jwt && Date.now() - mintedAt.current < 50 * 60 * 1000 ? jwt : mint(role))

  // citation chip click -> PREVIEW modal first (extracted text / inline PDF); download only on demand
  const [docView, setDocView] = useState(null)
  const openDoc = useCallback((d) => { if (d?.document_id) setDocView(d) }, [])

  const ask = async (text, display = null, opts = {}) => {
    if (!text || !text.trim()) return
    if (busySid) return toast.warn('Still answering in another session — one moment')
    if (!apiKey) return toast.warn('Paste an app key secret to try the consumer API')
    if (!role) return toast.warn('Pick an end-user role first (define at least one role in the Role tab)')
    const tok = await token()
    if (!tok) return  // mint failed (e.g. production uses an external asymmetric IdP)
    // conversation memory (v2): send prior turns so follow-up questions resolve server-side
    const history = messages.slice(-20).map(m => m.role === 'user'
      ? { role: 'user', content: m.text || '' }
      : { role: 'assistant', content: (m.answer || m.error?.message || '') }).filter(t => t.content)
    const requestSid = sid
    // Append to the session that was active when the question was sent. It may no
    // longer be the visible session when the background response finishes.
    const appendMsg = (m) => setSessions(ss => {
      const cur = ss.find(s => s.id === requestSid) || { ...newSession(), id: requestSid }
      const upd = { ...cur, messages: [...cur.messages, { _k: msgKey(), ...m }], updated: Date.now(),
                    title: cur.title || (m.role === 'user' ? String(m.text || '').slice(0, 60) : '') }
      return [upd, ...ss.filter(s => s.id !== requestSid)]
    })
    appendMsg({
      role: 'user',
      text,
      displayText: display?.displayText ?? text,
      pastedBlocks: display?.pastedBlocks || [],
    })
    const controller = new AbortController()
    activeStreamRef.current = { controller, sid: requestSid }
    setBusySid(requestSid); setLiveStatus(''); setLiveFeed([]); setStreamingText('')
    let terminal = false
    await api.consumerJob('/v1/chat/jobs', apiKey, {
      message: text,
      user_id: 'console-user',
      history,
      analysis_mode: analysisMode,
      business: opts.business || business?.id || undefined,
      confirmation: opts.confirmation || undefined,
      session_id: requestSid,
    }, tok, (ev) => {
      if (ev.type === 'status') { setLiveStatus(ev.text); setLiveFeed(t => [...t, { kind: 'status', text: ev.text }]) }
      else if (ev.type === 'finding') setLiveFeed(t => [...t, { kind: 'finding', text: ev.goal, ok: ev.ok, rows: ev.rows, error: ev.error }])
      else if (ev.type === 'reasoning_delta') setLiveFeed(t => [...t, { kind: 'note', text: ev.text || '' }])
      else if (ev.type === 'answer_delta') setStreamingText(t => t + (ev.text || ''))
      else if (ev.type === 'answer_reset') setStreamingText('')
      else if (ev.type === 'done' && !terminal) {
        terminal = true
        setStreamingText(''); setLiveFeed([])
        if (ev.result?.business) rememberBusiness(requestSid, ev.result.business)
        appendMsg({ role: 'assistant', question: text, ...ev.result })
      } else if (ev.type === 'error' && !terminal) {
        terminal = true
        setStreamingText(''); setLiveFeed([])
        appendMsg({ role: 'assistant', error: { code: ev.code || 'ERROR', message: ev.error } })
      } else if (ev.type === 'cancelled' && !terminal) {
        terminal = true
        setStreamingText(''); setLiveFeed([])
        appendMsg({ role: 'assistant', cancelled: true, text: 'Stopped by the user.' })
      }
    }, { signal: controller.signal })
    if (activeStreamRef.current?.controller === controller) activeStreamRef.current = null
    setBusySid(current => current === requestSid ? '' : current)
    setLiveStatus('')
  }

  const rememberBusiness = (sessionId, picked) => setSessions(ss => ss.map(s => (
    s.id === sessionId ? { ...s, business: picked, updated: Date.now() } : s)))

  // Picking is what ANSWERS a confirmation — the pending question is re-sent, resolved.
  // A business is remembered for the conversation. Every other pick is a one-off
  // correction of an answer's open reading (a name, a brand, a KPI meaning): its option
  // carries the rewritten question the server already built.
  const pickConfirmation = (option, confirmation) => {
    if (confirmation.kind === 'business') {
      // passed explicitly: setSessions has not landed yet when ask() reads it
      rememberBusiness(sid, { id: option.id, name: option.name })
      if (confirmation.pending_question) {
        ask(confirmation.pending_question, null, { business: option.id })
      }
      return
    }
    ask(option.question || confirmation.pending_question)
  }

  const stopGeneration = () => {
    const active = activeStreamRef.current
    if (!active) return
    setLiveStatus('Stopping…')
    active.controller.abort()
  }

  const sendFeedback = async (message, payload) => {
    if (!message?.trace_id || !apiKey) return { ok: false }
    const tok = await token()
    if (!tok) return { ok: false }
    const r = await api.consumer('/v1/feedback', apiKey, { trace_id: message.trace_id, ...payload }, tok)
    if (!r.ok) { toast.bad(r.error?.message || "Couldn't send feedback"); return r }
    setSessions(current => current.map(session => ({
      ...session,
      messages: (session.messages || []).map(item => item.trace_id === message.trace_id
        ? { ...item, feedback: r.data }
        : item),
    })))
    toast.ok('Feedback saved')
    return r
  }

  const newChat = () => {
    const cur = sessions.find(s => s.id === sid)
    if (cur && cur.messages.length === 0) return          // sesi kosong aktif — pakai saja
    const ns = newSession()
    setSessions(ss => [ns, ...ss]); setSid(ns.id)
  }
  const delSession = (id) => {
    if (id === busySid) return toast.warn('Stop the run before deleting this session')
    api.del(`/admin/tenants/${tenantId}/chat-sessions/${encodeURIComponent(id)}`, adminKey)
    const left = sessions.filter(s => s.id !== id)
    const next = left[0] || newSession()
    setSessions(left.length ? left : [next])
    if (id === sid) setSid(next.id)
  }
  const resizeComposer = useCallback((el = inputRef.current) => {
    if (!el) return
    el.style.height = 'auto'
    const height = Math.min(Math.max(el.scrollHeight, 24), MAX_COMPOSER_HEIGHT)
    el.style.height = `${height}px`
    el.style.overflowY = el.scrollHeight > MAX_COMPOSER_HEIGHT ? 'auto' : 'hidden'
  }, [])
  useEffect(() => { requestAnimationFrame(() => resizeComposer()) }, [draft, resizeComposer])

  const send = () => {
    const typed = draft.trim()
    const transcriptText = pastedBlocks.map(block => (
      `[Pasted transcript: ${block.name}]\n${block.text}`
    ))
    const text = [...transcriptText, typed].filter(Boolean).join('\n\n')
    if (!text) return
    const pasteSummary = pastedBlocks.map(({ id, name, chars, lines, preview }) => ({
      id, name, chars, lines, preview,
    }))
    setDraft('')
    setPastedBlocks([])
    requestAnimationFrame(() => resizeComposer())
    ask(text, { displayText: typed, pastedBlocks: pasteSummary })
  }
  const onKey = (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      send()
    }
  }
  const onPaste = (e) => {
    const text = e.clipboardData?.getData('text/plain') || ''
    const lines = text.split(/\r?\n/).length
    if (text.length < LONG_PASTE_CHARS && lines < LONG_PASTE_LINES) return
    e.preventDefault()
    const index = pastedBlocks.length + 1
    setPastedBlocks(current => [...current, {
      id: `paste_${Date.now()}_${index}`,
      name: `Pasted text ${index}`,
      text,
      chars: text.length,
      lines,
      preview: text.trim().split(/\r?\n/).find(Boolean)?.slice(0, 140) || 'Pasted text',
    }])
  }

  return (
    <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }} className="fade">
      {/* left rail */}
      <div style={{ width: 228, flexShrink: 0, borderRight: '1px solid var(--border)', background: 'var(--surface)', display: 'flex', flexDirection: 'column', padding: '14px 12px' }}>
        <label style={railLbl}>Tenant</label>
        <select value={tenantId} onChange={e => setTenantId(e.target.value)} disabled={!!busySid}
          title={busySid ? 'Stop the run before switching tenants' : undefined}
          style={{ ...railSel, opacity: busySid ? .65 : 1 }}>{tenants.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}</select>
        <label style={{ ...railLbl, marginTop: 10 }}>App key (per env)</label>
        <input type="password" value={apiKey} onChange={e => setApiKey(e.target.value)}
          placeholder={keys.length ? 'paste secret sk_…' : 'no active key'}
          autoComplete="off" spellCheck={false} style={railSel} />
        {keys.length > 0 && <div style={{ fontSize: 10, color: 'var(--fg-subtle)', padding: '4px 4px 0', lineHeight: 1.35 }}>
          {keys.map(k => `${k.label || 'App key'} ${k.key_prefix || ''}`).join(' · ')}
        </div>}
        <label style={{ ...railLbl, marginTop: 10 }}>End-user role (JWT)</label>
        <select value={role} onChange={e => setRole(e.target.value)} style={railSel}>
          {roles.length === 0 && <option value="">no role defined</option>}
          {roles.map(r => <option key={r.name} value={r.name}>{r.name}</option>)}
        </select>
        <div style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 10.5, color: jwt ? 'var(--success)' : 'var(--fg-subtle)', padding: '6px 4px 0' }}>
          <Icon name={jwt ? 'verified_user' : 'gpp_maybe'} size={13} />{jwt ? 'JWT minted — sent with every request' : 'JWT not ready'}
        </div>
        <label style={{ ...railLbl, marginTop: 10 }}>Analysis mode</label>
        <select value={analysisMode} onChange={e => setAnalysisMode(e.target.value)} style={railSel}>
          <option value="auto">Auto — Poros explores as far as the question needs</option>
          <option value="deep">Deep Research — maximum quality</option>
          <option value="brand">Brand analysis — the brand's fixed numbers + an analyst's answer</option>
        </select>
        <button onClick={newChat} style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '10px 12px', border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface)', color: 'var(--fg)', fontSize: 13.5, fontWeight: 600, margin: '16px 0 18px' }}>
          <Icon name="add" size={18} /> New chat
        </button>
        <div style={railLbl}>Recents</div>
        <div style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 3 }}>
          {sessions.filter(s => s.messages.length || s.id === sid).map(s => (
            <div key={s.id} title={s.title || 'New session'}
              style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 8px', borderRadius: 8, cursor: 'pointer',
                       background: s.id === sid ? 'var(--accent-soft)' : 'transparent',
                       color: s.id === sid ? 'var(--accent)' : 'var(--fg-muted)', fontSize: 12.5, fontWeight: 500 }}>
              <button type="button" onClick={() => setSid(s.id)} aria-current={s.id === sid ? 'true' : undefined}
                style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1, minWidth: 0, padding: 0, border: 0, background: 'transparent', color: 'inherit', font: 'inherit', textAlign: 'left' }}>
                {s.id === busySid ? <span className="spin" style={{ width: 13, height: 13, flexShrink: 0 }} /> : <Icon name="chat_bubble" size={15} />}
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.title || 'New session'}</span>
              </button>
              <button type="button" onClick={e => { e.stopPropagation(); delSession(s.id) }} title="Delete session"
                style={{ display: 'flex', opacity: .55, padding: 2, border: 0, background: 'transparent', color: 'inherit' }}><Icon name="close" size={13} /></button>
            </div>
          ))}
          {sessions.every(s => !s.messages.length) && sessions.length <= 1 &&
            <div style={{ fontSize: 12.5, color: 'var(--fg-subtle)', padding: '4px 6px' }}>No sessions yet.</div>}
        </div>
      </div>

      {/* main chat */}
      <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', background: 'var(--bg)' }}>
        <div style={{ position: 'relative', display: 'flex', alignItems: 'center', gap: 13, padding: '14px 22px', borderBottom: '1px solid var(--border)', background: 'var(--surface)' }}>
          <Icon name="auto_awesome" size={20} color="var(--accent)" />
          <span style={{ fontWeight: 700, fontSize: 15 }}>Governed Analytics Chat</span>
          <span className="mono" style={{ fontSize: 11.5, color: 'var(--fg-muted)', background: 'var(--surface-2)', padding: '4px 9px', borderRadius: 7 }}>{model} · {dsName}</span>
          {business && (
            <span title="The business this conversation runs in — only its tables are reachable"
              style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 11.5, fontWeight: 600, color: 'var(--accent)', background: 'var(--accent-soft)', padding: '4px 6px 4px 9px', borderRadius: 7 }}>
              <Icon name="account_tree" size={14} />{business.name}
              <button type="button" onClick={() => rememberBusiness(sid, null)} title="Switch business"
                style={{ display: 'flex', padding: 1, border: 0, background: 'transparent', color: 'inherit' }}>
                <Icon name="close" size={13} />
              </button>
            </span>
          )}
          <div style={{ flex: 1 }} />
          <button onClick={() => setDbOpen(o => !o)} title="Datasource info" style={{ border: 'none', background: 'transparent', cursor: 'pointer', display: 'flex', padding: 4, borderRadius: 7, color: dbOpen ? 'var(--accent)' : 'var(--fg-subtle)' }}><Icon name="database" size={19} /></button>
          <span title="Governed — read-only SELECT, RLAC/CLAC enforced, PII never sent to the LLM" style={{ display: 'flex' }}><Icon name="verified_user" size={19} color="var(--success)" /></span>
          {dbOpen && <>
            <button type="button" aria-label="Close database list" onClick={() => setDbOpen(false)}
              style={{ position: 'fixed', inset: 0, zIndex: 40, border: 0, background: 'transparent' }} />
            <div style={{ position: 'absolute', top: 50, right: 16, zIndex: 50, width: 256, background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, padding: 14, boxShadow: '0 14px 36px -14px rgba(8,12,20,.32)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 11 }}><Icon name="database" size={17} color="var(--accent)" /><span style={{ fontWeight: 700, fontSize: 13.5 }}>Datasource</span></div>
              {dsInfo ? <div style={{ display: 'flex', flexDirection: 'column', gap: 7, fontSize: 12.5 }}>
                <DsRow k="Source" v={`${dsInfo.type} · ${dsInfo.name}`} />
                <DsRow k="Dataset" v={dsInfo.dataset} mono />
                <DsRow k="Tables" v={`${dsInfo.allow} / ${dsInfo.tables} allowed`} />
                <DsRow k="Metrics" v={`${dsInfo.metrics} defined`} />
                <DsRow k="Model" v={model} mono />
                <div style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11, color: 'var(--success)', marginTop: 3, paddingTop: 8, borderTop: '1px solid var(--border)' }}><Icon name="verified_user" size={14} />Governed · read-only · RLAC/CLAC</div>
              </div> : <div style={{ fontSize: 12.5, color: 'var(--fg-subtle)' }}>This tenant has no SQL datasource yet.</div>}
            </div>
          </>}
        </div>

        <div ref={scrollRef} style={{ flex: 1, overflowY: 'auto', padding: '30px 22px' }}>
          <div style={{ maxWidth: 760, margin: '0 auto' }}>
            {messages.length === 0 && (
              <>
                <div style={{ display: 'flex', alignItems: 'center', gap: 11, marginBottom: 22 }}>
                  <Icon name="auto_awesome" size={28} color="var(--accent)" />
                  <h2 style={{ fontSize: 25, fontWeight: 700, letterSpacing: '-.02em' }}>What would you like to analyze?</h2>
                </div>
                <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 16, padding: 20, fontSize: 12.5, color: 'var(--fg-muted)', lineHeight: 1.6 }}>
                  Model <b style={{ color: 'var(--fg)' }}>{model}</b> · source <b style={{ color: 'var(--fg)' }}>{dsName}</b> — ask in plain language; the agent (gatekeeper → router → tools → verifier) writes the SQL, and every result is governed (allowlist + RLAC/CLAC).
                </div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 9, marginTop: 18 }}>
                  {SUGGESTIONS.map(s => (
                    <button key={s.text} className="hov-accent" onClick={() => ask(s.text)} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px', border: '1px solid var(--border)', borderRadius: 11, background: 'var(--surface)', color: 'var(--fg)', fontSize: 13, textAlign: 'left' }}>
                      <Icon name={s.icon} size={17} color="var(--accent)" />{s.text}
                    </button>
                  ))}
                </div>
              </>
            )}

            <div style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
              {messages.map((m, i) => (
                <Message key={m._k} m={m} openDoc={openDoc} onFeedback={sendFeedback}
                  onPick={i === messages.length - 1 && !busySid ? pickConfirmation : null} />
              ))}
              {thinking && (
                <div style={{ display: 'flex', gap: 13, alignItems: 'flex-start' }}>
                  <div style={botAvatar}><Icon name="auto_awesome" size={18} /></div>
                  <div style={{ flex: 1, minWidth: 0, paddingTop: 3 }}>
                    {streamingText ? (
                      // jawaban final mulai mengalir -> proses di atasnya langsung DILIPAT
                      // jadi satu chip ringkas (tetap bisa dibuka), jawaban mengambil tempatnya
                      <>
                        {liveFeed.length > 0 && (
                          <details style={{ marginBottom: 12 }}>
                            <summary style={{ cursor: 'pointer', listStyle: 'none', display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 11.5, color: 'var(--fg-subtle)', padding: '2px 8px', border: '1px solid var(--border)', borderRadius: 8, background: 'var(--surface)' }}>
                              <Icon name="check_circle" size={13} color="var(--success)" />
                              Analysis process · {liveFeed.filter(s => s.kind !== 'note').length} steps
                              <Icon name="expand_more" size={13} />
                            </summary>
                            <div style={{ margin: '9px 0 4px' }}><LiveFlow feed={liveFeed} working={false} /></div>
                          </details>
                        )}
                        <Answer text={streamingText} />
                      </>
                    ) : (
                      <LiveFlow feed={liveFeed} fallback={liveStatus} working />
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>

        <div style={{ padding: '14px 22px 18px', background: 'var(--bg)' }}>
          <div style={{ maxWidth: 760, margin: '0 auto' }}>
            <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 14, padding: pastedBlocks.length ? '8px' : '6px 6px 6px 18px' }}>
              {pastedBlocks.length > 0 && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 7 }}>
                  {pastedBlocks.map(block => (
                    <div key={block.id} style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '8px 9px', border: '1px solid var(--border)', borderRadius: 9, background: 'var(--surface-2)' }}>
                      <Icon name="description" size={17} color="var(--accent)" />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: 11.5, fontWeight: 650, color: 'var(--fg)' }}>{block.name}</div>
                        <div style={{ fontSize: 10.5, color: 'var(--fg-subtle)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {block.chars.toLocaleString('en-US')} characters · {block.lines.toLocaleString('en-US')} lines · {block.preview}
                        </div>
                      </div>
                      <button type="button" title="Remove pasted text" onClick={() => removePastedBlock(block.id)}
                        style={{ width: 27, height: 27, border: 0, borderRadius: 7, background: 'transparent', color: 'var(--fg-subtle)', display: 'grid', placeItems: 'center', cursor: 'pointer' }}>
                        <Icon name="close" size={15} />
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <div style={{ display: 'flex', alignItems: 'flex-end', gap: 10, paddingLeft: pastedBlocks.length ? 10 : 0 }}>
                <textarea ref={inputRef} rows={1} value={draft}
                  onChange={e => setDraft(e.target.value)} onPaste={onPaste} onKeyDown={onKey}
                  placeholder={pastedBlocks.length ? 'Add instructions for this transcript…' : 'Ask about your ops data…'}
                  style={{
                    flex: 1, width: '100%', minWidth: 0, minHeight: 24, maxHeight: MAX_COMPOSER_HEIGHT,
                    border: 'none', outline: 'none', resize: 'none', overflowY: 'hidden',
                    background: 'transparent', fontFamily: 'inherit', fontSize: 14, lineHeight: 1.5,
                    color: 'var(--fg)', padding: '8px 0', boxSizing: 'border-box',
                    whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', wordBreak: 'break-word',
                  }} />
                {busySid ? (
                  <button type="button" onClick={stopGeneration} title="Stop"
                    aria-label="Stop"
                    style={{ width: 38, height: 38, border: 'none', borderRadius: '50%', background: 'var(--fg)', color: 'var(--surface)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, cursor: 'pointer' }}>
                    <Icon name="stop" size={19} />
                  </button>
                ) : (
                  <button onClick={send} disabled={!draft.trim() && pastedBlocks.length === 0}
                    style={{ width: 38, height: 38, border: 'none', borderRadius: 10, background: 'var(--accent)', color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, opacity: (!draft.trim() && pastedBlocks.length === 0) ? .45 : 1, cursor: (!draft.trim() && pastedBlocks.length === 0) ? 'default' : 'pointer' }}>
                    <Icon name="arrow_upward" size={20} />
                  </button>
                )}
              </div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 9, padding: '0 4px' }}>
              <span className="mono" style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--fg-muted)' }}><span style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--success)' }} />{model}</span>
              <span style={{ fontSize: 12, color: 'var(--fg-subtle)' }}>Enter to send · Shift+Enter for a new line</span>
            </div>
            <div style={{ textAlign: 'center', fontSize: 11.5, color: 'var(--fg-subtle)', marginTop: 11 }}>AI answers may be inaccurate. Numbers come only from validated SQL.</div>
          </div>
        </div>
      </div>
      {docView && <DocViewer doc={docView} adminKey={adminKey} toast={toast} onClose={() => setDocView(null)} />}
    </div>
  )
}

// Preview-first document viewer: shows the cited excerpt (or the PDF inline) immediately;
// the original file is only DOWNLOADED when the user explicitly clicks the button.
function DocViewer({ doc, adminKey, toast, onClose }) {
  const [pdfUrl, setPdfUrl] = useState(null)
  const isPdf = (doc.filename || '').toLowerCase().endsWith('.pdf')
  useEffect(() => {
    let url
    if (isPdf) {
      (async () => {
        try {
          const r = await fetch(`${api.base}/admin/documents/${doc.document_id}/file`, { headers: { 'X-Admin-Key': adminKey } })
          if (r.ok) { url = URL.createObjectURL(await r.blob()); setPdfUrl(url) }
        } catch { /* preview falls back to text */ }
      })()
    }
    return () => { if (url) URL.revokeObjectURL(url) }
  }, [doc.document_id])
  // Escape closes the viewer. Bound on document, not on the overlay div: a plain div
  // never receives keydown unless it happens to hold focus, so the old inline handler
  // only fired by accident.
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])
  const download = async () => {
    try {
      const r = await fetch(`${api.base}/admin/documents/${doc.document_id}/file`, { headers: { 'X-Admin-Key': adminKey } })
      if (!r.ok) return toast.warn('Original file not stored — re-upload this document in the Document tab')
      const url = URL.createObjectURL(await r.blob())
      const a = document.createElement('a'); a.href = url; a.download = doc.filename || 'document'; a.click()
      setTimeout(() => URL.revokeObjectURL(url), 60000)
    } catch { toast.bad("Couldn't download the document") }
  }
  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.45)', zIndex: 90, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24 }}>
      {/* native <dialog> instead of role="dialog"; `position/margin/padding/color` undo the
          UA defaults (absolute + auto margin + 1em padding) so it lays out exactly as the
          plain div did inside this centering flex overlay */}
      <dialog open aria-modal="true" aria-label={doc.filename || 'Document preview'}
        style={{ position: 'static', margin: 0, padding: 0, color: 'var(--fg)', width: 'min(780px, 94vw)', maxHeight: '84vh', display: 'flex', flexDirection: 'column', background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 14, overflow: 'hidden' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '13px 16px', borderBottom: '1px solid var(--border)' }}>
          <Icon name="description" size={17} color="var(--accent)" />
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 13.5, fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{doc.filename}</div>
            <div className="mono" style={{ fontSize: 11, color: 'var(--fg-muted)' }}>{doc.page_label || doc.citation}</div>
          </div>
          <span style={{ flex: 1 }} />
          <button onClick={download} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12, padding: '6px 12px', borderRadius: 8, border: '1px solid var(--border)', background: 'var(--surface)', color: 'var(--fg)', cursor: 'pointer', flexShrink: 0 }}><Icon name="download" size={14} />Download original file</button>
          <button onClick={onClose} title="Close" style={{ border: 'none', background: 'transparent', color: 'var(--fg-muted)', cursor: 'pointer', display: 'flex' }}><Icon name="close" size={18} /></button>
        </div>
        <div style={{ overflow: 'auto', padding: isPdf && pdfUrl ? 0 : '14px 18px' }}>
          {isPdf && pdfUrl
            ? <iframe title="pdf-preview" src={pdfUrl} style={{ width: '100%', height: '68vh', border: 'none' }} />
            : <>
              <div style={{ fontSize: 11.5, fontWeight: 600, color: 'var(--fg-muted)', marginBottom: 8 }}>Excerpt the AI used ({doc.page_label || 'related section'}):</div>
              <div style={{ fontSize: 13, lineHeight: 1.7, whiteSpace: 'pre-wrap', color: 'var(--fg)' }}>{doc.text}</div>
              <div style={{ marginTop: 14, fontSize: 11.5, color: 'var(--fg-subtle)' }}>This preview is extracted text. To see the original slides and formatting, click "Download original file".</div>
            </>}
        </div>
      </dialog>
    </div>
  )
}

// Alur proses live gaya Claude/Codex: "teks → berpikir → teks lagi". Narasi model
// (reasoning_delta) tampil sebagai paragraf prosa di alur chat; rentetan aktivitas
// (status/finding) di antara paragraf dilipat jadi satu baris ringkas yang bisa dibuka;
// aktivitas TERAKHIR tetap hidup dengan titik berkedip selama proses berjalan.
function LiveFlow({ feed, fallback, working }) {
  const items = feed.length ? feed : [{ kind: 'status', text: fallback || 'Processing…' }]
  const label = (step) => {
    if (step.kind !== 'finding') return step.text
    return step.text + (step.ok ? ` · ${step.rows} rows` : ' · no data')
  }
  // The feed is append-only, so a block's position IS its identity — bake it into an
  // id at build time instead of keying off the render index.
  const blocks = []
  let acts = []
  const flushActs = (tail) => {
    if (!acts.length) return
    blocks.push({ id: `acts-${blocks.length}`, type: 'acts', items: acts, tail })
    acts = []
  }
  for (const it of items) {
    if (it.kind === 'note') {
      flushActs(false)
      blocks.push({ id: `note-${blocks.length}`, type: 'note', text: it.text })
    } else acts.push(it)
  }
  flushActs(true)
  // aktivitas paling akhir jadi baris "sedang berjalan" (dots), sisanya dilipat
  let current = null
  const lastBlock = blocks[blocks.length - 1]
  if (working && lastBlock?.type === 'acts' && lastBlock.tail) {
    current = lastBlock.items[lastBlock.items.length - 1]
    lastBlock.items = lastBlock.items.slice(0, -1)
    if (!lastBlock.items.length) blocks.pop()
  }
  const actLine = (step, idx) => (
    <div key={idx} style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 12, color: 'var(--fg-subtle)', minWidth: 0, padding: '2px 0' }}>
      <span style={{ display: 'flex', flexShrink: 0, color: step.error ? 'var(--danger)' : 'var(--success)' }}><Icon name={step.error ? 'error' : 'check_circle'} size={13} /></span>
      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>{label(step)}</span>
    </div>
  )
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 9, minWidth: 0 }}>
      {blocks.map(b => b.type === 'note' ? (
        <div key={b.id} style={{ fontSize: 13.5, lineHeight: 1.65, color: 'var(--fg-muted)', whiteSpace: 'pre-wrap' }}>{b.text}</div>
      ) : (
        <details key={b.id} style={{ minWidth: 0 }}>
          <summary style={{ cursor: 'pointer', listStyle: 'none', display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 11.5, color: 'var(--fg-subtle)', padding: '2px 8px', border: '1px solid var(--border)', borderRadius: 8, background: 'var(--surface)' }}>
            <Icon name={b.items.some(s => s.error) ? 'error' : 'check_circle'} size={13} color={b.items.some(s => s.error) ? 'var(--danger)' : 'var(--success)'} />
            {b.items.length} steps run
            <Icon name="expand_more" size={13} />
          </summary>
          <div style={{ padding: '5px 2px 2px 6px' }}>{b.items.map(actLine)}</div>
        </details>
      ))}
      {current && (
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 12.5, color: 'var(--fg-muted)', minWidth: 0 }}>
          <span style={{ display: 'flex', gap: 4, flexShrink: 0 }}>{[0, .2, .4].map(d => <span key={d} style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--fg-subtle)', animation: `dcblink 1s ${d}s infinite` }} />)}</span>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>{label(current)}</span>
        </div>
      )}
    </div>
  )
}

function Message({ m, openDoc, onFeedback, onPick }) {
  if (m.role === 'user') return <UserBubble m={m} />
  if (m.cancelled) return (
    <div style={{ display: 'flex', gap: 13, maxWidth: '88%' }}>
      <div style={botAvatar}><Icon name="stop_circle" size={18} /></div>
      <div style={{ flex: 1, padding: '11px 14px', border: '1px solid var(--border)', borderRadius: 12, background: 'var(--surface-2)', color: 'var(--fg-muted)', fontSize: 13.5 }}>
        Stopped. No further steps ran and no final answer was recorded.
      </div>
    </div>
  )
  if (m.error) return (
    <div style={{ display: 'flex', gap: 13, maxWidth: '88%' }}>
      <div style={botAvatar}><Icon name="error" size={18} /></div>
      <div style={{ flex: 1, padding: '11px 14px', border: '1px solid var(--danger)', borderRadius: 12, background: 'color-mix(in srgb,var(--danger) 8%,transparent)', color: 'var(--danger)', fontSize: 13.5 }}>
        <b>{m.error.code}:</b> {m.error.message}</div>
    </div>
  )
  const sql = m.sql || {}
  const hypotheses = m.hypotheses || m.research?.hypotheses || []
  const ex = extractBlocks(m.answer)   // pull ```chart / ```artifact blocks out of the answer
  return (
    <div style={{ display: 'flex', gap: 13, maxWidth: '88%' }}>
      <div style={botAvatar}><Icon name="auto_awesome" size={18} /></div>
      <div style={{ flex: 1, minWidth: 0 }}>
        {m.blocked && <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12, fontWeight: 600, color: 'var(--danger)', marginBottom: 8 }}><Icon name="block" size={16} />Blocked by gatekeeper</div>}
        <AssumptionNotes assumptions={m.assumptions} onPick={onPick} />
        <Answer text={ex.clean} />
        <ConfirmationOptions confirmation={m.confirmation} onPick={onPick} />
        <HypothesisLedger hypotheses={hypotheses} />

        {sql.ok && (
          <div style={{ marginTop: 12, border: '1px solid var(--border)', borderRadius: 11, overflow: 'hidden', background: 'var(--surface)' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '8px 13px', borderBottom: '1px solid var(--border)', background: 'var(--surface-2)', fontSize: 11.5, fontWeight: 600, color: 'var(--fg-muted)' }}>
              <Icon name="code" size={15} color="var(--accent)" />Generated SQL<span style={{ flex: 1 }} /><span className="mono" style={{ color: 'var(--success)', fontSize: 11 }}>✓ governed</span>
            </div>
            <pre className="mono" style={{ margin: 0, padding: '13px 15px', fontSize: 12.5, lineHeight: 1.65, color: 'var(--fg)', whiteSpace: 'pre-wrap', overflowX: 'auto' }}>{sql.safe_sql}</pre>
          </div>
        )}
        {sql.ok && sql.columns && sql.columns.length > 0 && (
          <div style={{ marginTop: 12, border: '1px solid var(--border)', borderRadius: 11, overflow: 'auto', background: 'var(--surface)' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
              <thead><tr>{sql.columns.map(c => <th key={c} className="mono" style={{ textAlign: 'left', padding: '9px 14px', background: 'var(--surface-2)', fontWeight: 600, color: 'var(--fg-muted)', borderBottom: '1px solid var(--border)', fontSize: 11.5 }}>{c}</th>)}</tr></thead>
              <tbody>{(sql.rows || []).slice(0, 50).map((row, ri) => <tr key={ri}>{row.map((cell, ci) => <td key={ci} className="mono" style={{ padding: '9px 14px', borderBottom: '1px solid var(--border)', textAlign: typeof cell === 'number' ? 'right' : 'left' }}>{fmtCell(cell)}</td>)}</tr>)}</tbody>
            </table>
          </div>
        )}
        <AnswerVisuals blocks={ex} question={m.question} columns={sql.ok ? sql.columns : null} rows={sql.ok ? sql.rows : null} />
        {sql && sql.ok === false && (
          <div style={{ marginTop: 10, fontSize: 12.5, color: 'var(--warn)', display: 'flex', alignItems: 'center', gap: 6 }}><Icon name="gpp_maybe" size={16} />{sql.code}: {sql.error}</div>
        )}
        <AnswerMetaChips m={m} openDoc={openDoc} />
        <ExportActions m={m} />
        <FeedbackControls m={m} onFeedback={onFeedback} />
      </div>
    </div>
  )
}

function UserBubble({ m }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
      <div style={{ maxWidth: '80%', minWidth: 0, background: 'var(--accent)', color: '#fff', padding: '12px 16px', borderRadius: '16px 16px 4px 16px', fontSize: 14, lineHeight: 1.55, overflowWrap: 'anywhere', wordBreak: 'break-word', whiteSpace: 'pre-wrap' }}>
        {(m.pastedBlocks || []).map(block => (
          <div key={block.id} title={block.preview}
            style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '8px 10px', marginBottom: 8, borderRadius: 9, background: 'rgba(255,255,255,.14)', border: '1px solid rgba(255,255,255,.25)' }}>
            <Icon name="description" size={17} />
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 11.5, fontWeight: 700 }}>{block.name}</div>
              <div style={{ fontSize: 10.5, opacity: .8, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {Number(block.chars || 0).toLocaleString('en-US')} characters · {Number(block.lines || 0).toLocaleString('en-US')} lines
              </div>
            </div>
          </div>
        ))}
        {(m.displayText ?? m.text) && <div>{m.displayText ?? m.text}</div>}
      </div>
    </div>
  )
}

// What an answer ASSUMED instead of asking (chat overhaul item 5): a name the data only
// resembles, the brand of a brand analysis, a KPI with several curated meanings. The
// answer's first sentence states the reading; this line shows it with the other readings
// as one-click corrections (each option's `question` is the corrected question).
function AssumptionNotes({ assumptions, onPick }) {
  if (!(assumptions || []).length) return null
  const subject = a => (a.kind === 'brand' ? 'Brand' : `"${a.term}"`)
  const shown = (a, o) => (a.kind === 'metric' && o.descr ? `${o.name} (${o.descr})` : o.name)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 10 }}>
      {assumptions.map((a, i) => {
        const chosen = (a.options || []).find(o => o.id === a.chosen)
        const others = (a.options || []).filter(o => o !== chosen && o.question)
        return (
          <div key={`${a.kind}-${a.term || ''}-${i}`}
            style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--fg-muted)' }}>
            <Icon name="alt_route" size={15} color="var(--accent)" />
            <span>
              {chosen
                ? <>Assumed: {subject(a)} read as <b style={{ color: 'var(--fg)' }}>{shown(a, chosen)}</b>.</>
                : <>{subject(a)} could be read more than one way; the answer says which.</>}
            </span>
            {others.length > 0 && <span>{chosen ? 'Not this?' : 'Read it as:'}</span>}
            {others.map(o => (
              <button key={o.id || o.name} type="button" className="hov-accent" disabled={!onPick}
                title={o.descr || undefined}
                onClick={() => onPick?.(o, { kind: 'assumption' })}
                style={{ padding: '2px 9px', border: '1px solid var(--border)', borderRadius: 999, background: 'var(--surface)', color: 'var(--fg)', fontSize: 12, cursor: onPick ? 'pointer' : 'default' }}>
                {shown(a, o)}
              </button>
            ))}
          </div>
        )
      })}
    </div>
  )
}

function ConfirmationOptions({ confirmation, onPick }) {
  if (!(confirmation?.options || []).length) return null
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 12 }}>
      {confirmation.options.map(option => (
        /* The description is VISIBLE, not a hover title: for metric options it can
         * be the only thing telling two same-named meanings apart. */
        <button key={option.id || option.name} type="button" className="hov-accent"
          disabled={!onPick}
          onClick={() => onPick?.(option, confirmation)}
          style={{ display: 'flex', alignItems: 'flex-start', gap: 8, padding: '9px 13px', border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface)', color: 'var(--fg)', fontSize: 13, fontWeight: 600, cursor: onPick ? 'pointer' : 'default', opacity: onPick ? 1 : .55, textAlign: 'left', maxWidth: 420 }}>
          <Icon name={{ business: 'account_tree', metric: 'functions' }[confirmation.kind] || 'sell'}
            size={16} color="var(--accent)" style={{ marginTop: 1, flexShrink: 0 }} />
          <span style={{ minWidth: 0 }}>
            {option.name}
            {option.descr ? (
              <span style={{ display: 'block', fontSize: 11.5, fontWeight: 400, color: 'var(--fg-muted)', lineHeight: 1.45, wordBreak: 'break-word' }}>{option.descr}</span>
            ) : null}
          </span>
        </button>
      ))}
    </div>
  )
}

function HypothesisLedger({ hypotheses }) {
  if (!hypotheses.length) return null
  return (
    <details style={{ marginTop: 12, border: '1px solid var(--border)', borderRadius: 11, background: 'var(--surface)', overflow: 'hidden' }}>
      <summary style={{ cursor: 'pointer', padding: '9px 13px', background: 'var(--surface-2)', fontSize: 11.5, fontWeight: 650, color: 'var(--fg-muted)', display: 'flex', alignItems: 'center', gap: 7 }}>
        <Icon name="experiment" size={15} color="var(--accent)" />
        Hypothesis ledger · {hypotheses.length} hypotheses
      </summary>
      <div style={{ padding: 9, display: 'flex', flexDirection: 'column', gap: 7 }}>
        {hypotheses.map((hypothesis, index) => (
          <HypothesisCard key={hypothesis.id || hypothesis.statement} hypothesis={hypothesis} index={index} />
        ))}
      </div>
    </details>
  )
}

const HYPOTHESIS_STATUS_COLORS = {
  supported: 'var(--success)',
  rejected: 'var(--danger)',
  partially_supported: 'var(--warn)',
}

function HypothesisCard({ hypothesis, index }) {
  const status = hypothesis.status || 'inconclusive'
  const statusColor = HYPOTHESIS_STATUS_COLORS[status] || 'var(--fg-muted)'
  const evidence = [
    ...(hypothesis.supporting_evidence || []),
    ...(hypothesis.contradicting_evidence || []),
    ...(hypothesis.tested_evidence || []),
  ].filter((value, pos, all) => value && all.indexOf(value) === pos)
  return (
    <div style={{ border: '1px solid var(--border)', borderRadius: 9, padding: '9px 11px', background: 'var(--bg)' }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
        <span className="mono" style={{ fontSize: 10.5, color: 'var(--accent)', fontWeight: 700 }}>{hypothesis.id || `H${index + 1}`}</span>
        <span style={{ flex: 1, fontSize: 12.5, lineHeight: 1.45, fontWeight: 600 }}>{hypothesis.statement}</span>
        <span className="mono" style={{ fontSize: 9.5, color: statusColor, border: `1px solid color-mix(in srgb,${statusColor} 35%,transparent)`, borderRadius: 999, padding: '2px 7px', whiteSpace: 'nowrap' }}>{status.replaceAll('_', ' ')}</span>
      </div>
      {hypothesis.analytical_angle && <div style={{ marginTop: 5, color: 'var(--accent)', fontSize: 10.5 }}>Analytical angle: {hypothesis.analytical_angle}</div>}
      {hypothesis.rationale && <div style={{ marginTop: 5, color: 'var(--fg-muted)', fontSize: 11.5, lineHeight: 1.45 }}>{hypothesis.rationale}</div>}
      {(hypothesis.scope?.tables || []).length > 0 && <div style={{ marginTop: 5, color: 'var(--fg-subtle)', fontSize: 10.5 }}>Scope: {hypothesis.scope.tables.join(' · ')}{hypothesis.scope.time_window ? ` · ${hypothesis.scope.time_window}` : ''}</div>}
      {hypothesis.falsification_test && <div style={{ marginTop: 4, color: 'var(--fg-subtle)', fontSize: 10.5 }}>Falsified or weakened if: {hypothesis.falsification_test}</div>}
      {hypothesis.decision_effect && <div style={{ marginTop: 4, color: 'var(--fg-muted)', fontSize: 10.5 }}>Decision impact: {hypothesis.decision_effect}</div>}
      {evidence.length > 0 && <div style={{ marginTop: 6, display: 'flex', gap: 5, flexWrap: 'wrap' }}>{evidence.map(id => <span key={id} className="mono" style={{ fontSize: 9.5, padding: '2px 6px', borderRadius: 5, background: 'var(--surface-2)', color: 'var(--fg-subtle)' }}>{id}</span>)}</div>}
      {(hypothesis.assumptions || []).length > 0 && <div style={{ marginTop: 6, color: 'var(--warn)', fontSize: 10.5 }}>Assumptions: {hypothesis.assumptions.join(' · ')}</div>}
      {(hypothesis.next_tests || []).length > 0 && <div style={{ marginTop: 4, color: 'var(--fg-subtle)', fontSize: 10.5 }}>Next tests: {hypothesis.next_tests.join(' · ')}</div>}
    </div>
  )
}

function AnswerMetaChips({ m, openDoc }) {
  const cited = m.citations || []
  const mem = m.recalled_memory || []
  const webSources = m.web_sources || []
  const dataSources = m.datasources_used || []
  const unverified = m.unverified_figures || []
  const proposed = m.proposed_figures || []
  const show = !m.confirmation && (m.router?.model || cited.length > 0 || mem.length > 0
    || webSources.length > 0 || dataSources.length > 0 || m.analysis_mode === 'deep'
    || m.analysis_mode === 'brand' || m.analysis_mode === 'brainstorm' || m.engine === 'agentic'
    || m.confidence?.level || unverified.length > 0
    || m.status === 'partial' || m.status === 'no_data')
  if (!show) return null
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10 }}>
      {unverified.length > 0 && (
        // the verifier no longer rewrites the answer: a figure it could not match to
        // this turn's data stays in the text, and this note says so
        <span title={`Not found in the data this answer used: ${unverified.join(', ')}`}
          style={chip('color-mix(in srgb,var(--warn) 12%,transparent)', 'var(--warn)')}>
          <Icon name="error" size={13} />
          {unverified.length === 1
            ? '1 figure could not be matched to the data'
            : `${unverified.length} figures could not be matched to the data`}
        </span>
      )}
      {(m.status === 'partial' || m.status === 'no_data') && (() => {
        // a partial or empty answer must say so where the person reads it — the
        // status otherwise only exists in the API payload and the audit trail
        const partial = m.status === 'partial'
        const miss = (m.missing_requirements || []).join(', ')
        const missSuffix = miss ? `: ${miss}` : ''
        const title = partial
          ? `The answer does not deliver everything the question asked for${missSuffix}`
          : 'The governed query ran but returned no qualifying data'
        return <span title={title}
          style={chip('color-mix(in srgb,var(--warn) 12%,transparent)', 'var(--warn)')}>
          <Icon name={partial ? 'rule' : 'search_off'} size={13} />
          {partial ? `Partial${missSuffix}` : 'No data'}
        </span>
      })()}
      {m.analysis_mode === 'brainstorm' && (
        // ideas wanted: the answer stands on general knowledge, and on the data only
        // when the model went and checked
        <span title={m.confidence?.level === 'brainstorm'
          ? 'Brainstorm: ideas from general knowledge, no company data was checked'
          : 'Brainstorm: ideas checked against the data this answer used'}
          style={chip('var(--accent-soft)', 'var(--accent)')}>
          <Icon name="emoji_objects" size={13} />
          {m.confidence?.level === 'brainstorm' ? 'Ideas, general knowledge' : 'Ideas, checked against data'}
        </span>
      )}
      {proposed.length > 0 && (
        <span title={`Proposed in the ideas, not taken from the data: ${proposed.join(', ')}`}
          style={chip('var(--surface-2)', 'var(--fg-muted)')}>
          <Icon name="edit_note" size={13} />
          {proposed.length === 1 ? '1 proposed figure, not from data' : `${proposed.length} proposed figures, not from data`}
        </span>
      )}
      {m.analysis_mode === 'deep' && (
        <span title={m.research
          ? `Adaptive research agent · ${m.research.rounds || 0} rounds · ${m.research.tool_counts?.run_sql || 0} SQL · stopped: ${m.research.stop_reason || '-'}`
          : 'Deep analysis with evidence evaluation'}
          style={chip('var(--accent-soft)', 'var(--accent)')}>
          <Icon name="travel_explore" size={13} />Deep Research
        </span>
      )}
      {m.analysis_mode === 'brand' && m.brand_analysis && (() => {
        // the brand analysis: which brand and month it stands on, and whether
        // this exact answer came from the cache (same question asked earlier)
        const b = m.brand_analysis
        const tip = `Brand analysis · ${(b.brands || []).join(' & ')} · ${b.period_label || b.period}`
          + (b.flagged ? ` · ${b.flagged} sentence(s) flagged by the figure check` : '')
          + (b.answer_cached ? ' · same answer as the earlier ask (cached)' : '')
        return <span title={tip} style={chip('var(--accent-soft)', 'var(--accent)')}>
          <Icon name="insights" size={13} />Brand analysis{b.answer_cached ? ' · cached' : ''}
        </span>
      })()}
      {m.engine === 'agentic' && (() => {
        // the agentic engine: which tools the model used, in order (open_business:3P,
        // lookup_values, run_sql×2 …) — the comparison against the classic engine
        const steps = (m.plan || []).filter(p => !['conversation_history', 'synthesis',
          'auto_memory', 'agent_loop(agentic)'].includes(p))
        return <span title={`Agentic engine · ${steps.join(' → ') || 'no tool used'}`}
          style={chip('var(--accent-soft)', 'var(--accent)')}>
          <Icon name="explore" size={13} />Agentic · {steps.length} step{steps.length === 1 ? '' : 's'}
        </span>
      })()}
      {m.router?.model && (() => {
        const simple = m.router.purpose === 'answer_simple'   // cheap model was selected
        const c = simple ? 'var(--success)' : 'var(--accent)'
        const others = (m.router.models || []).filter(x => x !== m.router.model)
        const tip = `Answering model: ${m.router.model} — `
          + (simple ? 'cheaper model (a greeting, or the budget for this period is spent)' : 'primary model')
          + (others.length ? ` · other models in this run: ${others.join(', ')}` : '')
        return <span title={tip} className="mono" style={chip(`color-mix(in srgb,${c} 12%,transparent)`, c)}>
          <Icon name={simple ? 'bolt' : 'smart_toy'} size={13} />{m.router.model}</span>
      })()}
      {m.confidence?.level === 'general_knowledge' && (
        // a discussion with no business figure: nothing to be confident about, so say
        // what it stands on instead of showing a Low badge
        <span title="Answered from general knowledge: no company data was needed and no business figure is stated"
          style={chip('var(--surface-2)', 'var(--fg-muted)')}>
          <Icon name="lightbulb" size={13} />General knowledge</span>
      )}
      {m.confidence?.level && !['general_knowledge', 'brainstorm'].includes(m.confidence.level) && (() => {
        const looks = { high: ['var(--success)', 'High', 'verified'], medium: ['var(--warn)', 'Medium', 'gpp_maybe'] }
        const [c, lbl, icon] = looks[m.confidence.level] || ['var(--danger)', 'Low', 'gpp_bad']
        const why = (m.confidence.signals?.checks_failed || []).join(', ')
        const failSuffix = why ? ` (failed: ${why})` : ''
        return <span title={`Confidence ${m.confidence.score}/100 — computed from the verifier + evidence strength${failSuffix}`}
          style={chip(`color-mix(in srgb,${c} 12%,transparent)`, c)}>
          <Icon name={icon} size={13} />Confidence: {lbl}</span>
      })()}
      {(m.documents || []).map(d => (
        <button key={d.id || d.citation_label || d.citation} onClick={() => openDoc?.(d)} title="Open source document"
          className="mono" style={{ ...chip('var(--accent-soft)', 'var(--accent)'), cursor: 'pointer' }}>
          <Icon name="description" size={13} />{d.citation_label || d.citation}<Icon name="open_in_new" size={12} />
        </button>
      ))}
      {dataSources.map(source => (
        <span key={source.id || source.name} title={`Datasource used: ${source.type || ''}`}
          className="mono" style={chip('var(--surface-2)', 'var(--fg-muted)')}>
          <Icon name="database" size={13} />{source.name || source.id}
        </span>
      ))}
      {webSources.map(source => (
        <a key={source.url || source.title} href={source.url} target="_blank" rel="noreferrer"
          title="Open web source" className="mono"
          style={{ ...chip('var(--accent-soft)', 'var(--accent)'), textDecoration: 'none' }}>
          <Icon name="public" size={13} />{source.title || source.url}<Icon name="open_in_new" size={12} />
        </a>
      ))}
      {mem.map(mm => <span key={mm.id || mm.text} style={chip('var(--surface-2)', 'var(--fg-muted)')}><Icon name="cognition" size={13} />{mm.text.slice(0, 40)}</span>)}
    </div>
  )
}

function ExportActions({ m }) {
  const [copyState, setCopyState] = useState('')
  const copyMarkdown = async () => {
    const markdown = buildResponseMarkdown(m)
    try {
      await navigator.clipboard.writeText(markdown)
    } catch {
      const area = document.createElement('textarea')
      area.value = markdown
      area.style.position = 'fixed'
      area.style.opacity = '0'
      document.body.appendChild(area)
      area.select()
      document.execCommand('copy')
      area.remove()
    }
    setCopyState('copied')
    window.setTimeout(() => setCopyState(''), 1800)
  }
  const downloadMarkdown = () => {
    const markdown = buildResponseMarkdown(m)
    const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    const trace = String(m.trace_id || Date.now()).replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 32)
    link.href = url
    link.download = `poros-response-${trace || 'answer'}.md`
    document.body.appendChild(link)
    link.click()
    link.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 0)
  }
  if (!m.answer || m.confirmation) return null
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginTop: 12, paddingTop: 10, borderTop: '1px solid var(--border)' }}>
      <span style={{ fontSize: 11.5, color: 'var(--fg-subtle)', marginRight: 2 }}>Export response:</span>
      <button type="button" onClick={copyMarkdown} title="Copy response as Markdown"
        style={responseActionButton}>
        <Icon name={copyState === 'copied' ? 'check' : 'content_copy'} size={14} />
        {copyState === 'copied' ? 'Copied' : 'Copy MD'}
      </button>
      <button type="button" onClick={downloadMarkdown} title="Download response as Markdown"
        style={responseActionButton}>
        <Icon name="download" size={14} />Download MD
      </button>
    </div>
  )
}

function FeedbackControls({ m, onFeedback }) {
  const [feedbackOpen, setFeedbackOpen] = useState(false)
  const [feedbackBusy, setFeedbackBusy] = useState(false)
  const [feedbackReason, setFeedbackReason] = useState('incorrect')
  const [feedbackComment, setFeedbackComment] = useState('')
  const rateHelpful = async () => {
    setFeedbackBusy(true)
    await onFeedback?.(m, { rating: 'helpful', reason: '', comment: '' })
    setFeedbackOpen(false); setFeedbackBusy(false)
  }
  const rateUnhelpful = async () => {
    setFeedbackBusy(true)
    const r = await onFeedback?.(m, { rating: 'unhelpful', reason: feedbackReason, comment: feedbackComment.trim() })
    if (r?.ok) setFeedbackOpen(false)
    setFeedbackBusy(false)
  }
  if (!m.trace_id || m.confirmation) return null
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <span style={{ fontSize: 11.5, color: 'var(--fg-subtle)', marginRight: 2 }}>Was this answer helpful?</span>
        <button type="button" disabled={feedbackBusy} title="Helpful" onClick={rateHelpful}
          style={feedbackButton(m.feedback?.rating === 'helpful', 'var(--success)')}>
          <Icon name="thumb_up" size={14} />
        </button>
        <button type="button" disabled={feedbackBusy} title="Needs improvement" onClick={() => setFeedbackOpen(v => !v)}
          style={feedbackButton(m.feedback?.rating === 'unhelpful', 'var(--danger)')}>
          <Icon name="thumb_down" size={14} />
        </button>
        {m.feedback && <span style={{ fontSize: 11, color: m.feedback.rating === 'helpful' ? 'var(--success)' : 'var(--danger)' }}>Feedback saved</span>}
      </div>

      {feedbackOpen && (
        <div style={{ marginTop: 8, padding: 10, border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface-2)', display: 'flex', flexDirection: 'column', gap: 8, maxWidth: 520 }}>
          <select value={feedbackReason} onChange={e => setFeedbackReason(e.target.value)}
            style={{ height: 36, border: '1px solid var(--border)', borderRadius: 8, background: 'var(--surface)', color: 'var(--fg)', padding: '0 10px', fontSize: 12.5 }}>
            <option value="incorrect">Wrong answer or data</option>
            <option value="incomplete">Incomplete answer</option>
            <option value="irrelevant">Not relevant</option>
            <option value="unclear">Hard to understand</option>
            <option value="outdated">Outdated data</option>
            <option value="other">Other</option>
          </select>
          <textarea value={feedbackComment} onChange={e => setFeedbackComment(e.target.value)} maxLength={2000}
            placeholder="Describe what needs fixing (optional)…"
            style={{ minHeight: 68, resize: 'vertical', border: '1px solid var(--border)', borderRadius: 8, background: 'var(--surface)', color: 'var(--fg)', padding: '9px 10px', font: 'inherit', fontSize: 12.5, lineHeight: 1.45 }} />
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 7 }}>
            <button type="button" onClick={() => setFeedbackOpen(false)} style={{ border: 0, background: 'transparent', color: 'var(--fg-muted)', padding: '7px 10px', fontSize: 12, fontWeight: 600 }}>Cancel</button>
            <button type="button" disabled={feedbackBusy} onClick={rateUnhelpful}
              style={{ border: 0, borderRadius: 8, background: 'var(--danger)', color: '#fff', padding: '7px 12px', fontSize: 12, fontWeight: 700, opacity: feedbackBusy ? .6 : 1 }}>{feedbackBusy ? 'Sending…' : 'Send feedback'}</button>
          </div>
        </div>
      )}
    </div>
  )
}

const feedbackButton = (active, color) => ({
  width: 30, height: 28, border: `1px solid ${active ? color : 'var(--border)'}`, borderRadius: 8,
  background: active ? `color-mix(in srgb,${color} 12%,transparent)` : 'var(--surface)',
  color: active ? color : 'var(--fg-subtle)', display: 'grid', placeItems: 'center',
  cursor: 'pointer',
})
const responseActionButton = {
  minHeight: 29, padding: '4px 9px', border: '1px solid var(--border)', borderRadius: 8,
  background: 'var(--surface)', color: 'var(--fg-muted)', display: 'inline-flex',
  alignItems: 'center', gap: 5, fontSize: 10.5, fontWeight: 600, cursor: 'pointer',
}

function buildResponseMarkdown(message) {
  const parts = [String(message.answer || '').trim()]
  const sql = message.sql || {}
  if (sql.ok && sql.safe_sql) {
    parts.push(`## Generated SQL\n\n\`\`\`sql\n${sql.safe_sql}\n\`\`\``)
  }

  const sourceLines = []
  const labels = new Set()
  for (const document of message.documents || []) {
    const label = document.citation_label || document.citation || document.filename
    if (label && !labels.has(label)) {
      sourceLines.push(`- ${label}`)
      labels.add(label)
    }
  }
  for (const citation of message.citations || []) {
    const label = typeof citation === 'string'
      ? citation
      : citation?.citation_label || citation?.citation
    if (label && !labels.has(label)) {
      sourceLines.push(`- ${label}`)
      labels.add(label)
    }
  }
  for (const source of message.web_sources || []) {
    const title = source.title || source.label || source.url
    if (!title || labels.has(title)) continue
    sourceLines.push(source.url ? `- [${title}](${source.url})` : `- ${title}`)
    labels.add(title)
  }
  if (sourceLines.length) parts.push(`## Sources\n\n${sourceLines.join('\n')}`)
  if (message.trace_id) parts.push(`<!-- Poros trace_id: ${message.trace_id} -->`)
  return parts.filter(Boolean).join('\n\n').trim() + '\n'
}

// numbers: round + thousands separators so floats like 227093794.93360746 read cleanly
function fmtCell(v) {
  if (typeof v === 'number' && Number.isFinite(v)) {
    const r = Math.abs(v) >= 1000 ? Math.round(v) : Math.round(v * 100) / 100
    return r.toLocaleString('en-US')
  }
  return v == null ? '—' : String(v)
}
// minimal inline markdown: **bold** and `code`
function renderInline(s) {
  return s.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((p, i) => {
    if (p.startsWith('**') && p.endsWith('**')) return <b key={i}>{p.slice(2, -2)}</b>
    if (p.startsWith('`') && p.endsWith('`')) return <code key={i} className="mono" style={{ background: 'var(--surface-2)', padding: '1px 5px', borderRadius: 5, fontSize: 12.5 }}>{p.slice(1, -1)}</code>
    return <span key={i}>{p}</span>
  })
}
// ---- GFM table support (the LLM often answers with markdown tables) ----
// a separator row looks like |---|:--:|--:| — every cell is dashes with optional colons
function isTableSep(s) {
  const t = (s || '').trim()
  if (!t.includes('|') || !t.includes('-')) return false
  const cells = t.replace(/(?:^\||\|$)/g, '').split('|')
  return cells.length > 0 && cells.every(c => /^\s*:?-+:?\s*$/.test(c))
}
function splitRow(s) {
  return (s || '').trim().replace(/(?:^\||\|$)/g, '').split('|').map(c => c.trim())
}
function colAligns(sep) {
  return splitRow(sep).map(c => {
    const l = c.startsWith(':'), r = c.endsWith(':')
    return (l && r) ? 'center' : r ? 'right' : 'left'
  })
}
function MdTable({ header, rows, aligns }) {
  return (
    <div style={{ margin: '8px 0', overflowX: 'auto', border: '1px solid var(--border)', borderRadius: 9 }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
        <thead>
          <tr>{header.map((h, i) => <th key={i} style={{ textAlign: aligns[i] || 'left', padding: '9px 14px', borderBottom: '1px solid var(--border)', background: 'var(--surface-2)', fontWeight: 600, whiteSpace: 'nowrap' }}>{renderInline(h)}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, ri) => <tr key={ri}>{header.map((_, ci) => <td key={ci} style={{ textAlign: aligns[ci] || 'left', padding: '9px 14px', borderBottom: ri === rows.length - 1 ? 'none' : '1px solid var(--border)', verticalAlign: 'top' }}>{renderInline(r[ci] ?? '')}</td>)}</tr>)}
        </tbody>
      </table>
    </div>
  )
}
// table block at line i: a header row followed by a |---|---| separator row.
// Returns the table and the index of the first line after it, or null.
function tableAt(lines, i) {
  if (!lines[i].trim().includes('|') || i + 1 >= lines.length || !isTableSep(lines[i + 1])) return null
  const header = splitRow(lines[i])
  const aligns = colAligns(lines[i + 1])
  const rows = []
  let j = i + 2
  while (j < lines.length && lines[j].trim() && lines[j].includes('|')) { rows.push(splitRow(lines[j])); j++ }
  return { node: <MdTable key={'t' + i} header={header} rows={rows} aligns={aligns} />, next: j }
}
const HEADING_SIZES = [24, 21, 18, 16, 15, 14]
function headingNode(level, body, key) {
  const big = level <= 2
  return React.createElement(
    `h${level}`,
    {
      key,
      style: {
        margin: big ? '18px 0 8px' : '14px 0 6px',
        fontSize: HEADING_SIZES[level - 1],
        lineHeight: 1.3,
        fontWeight: big ? 750 : 700,
        letterSpacing: big ? '-.015em' : 0,
        color: 'var(--fg)',
      },
    },
    renderInline(body),
  )
}
// one non-table line: blank, rule, heading, numbered item, bullet or a plain paragraph line
function lineNode(line, key) {
  const t = line.trim()
  if (!t) return <div key={key} style={{ height: 7 }} />
  if (/^(?:-{3,}|_{3,}|\*{3,})$/.test(t)) return <hr key={key} style={{ border: 0, borderTop: '1px solid var(--border)', margin: '16px 0' }} />
  const heading = t.match(/^(#{1,6})\s+(.+)$/)
  if (heading) return headingNode(heading[1].length, heading[2], key)
  const numbered = t.match(/^(\d+)[.)]\s+(.+)$/)
  if (numbered) {
    return <div key={key} style={{ display: 'flex', gap: 8, margin: '3px 0 3px 2px' }}><span style={{ color: 'var(--accent)', minWidth: 20, fontWeight: 650 }}>{numbered[1]}.</span><span>{renderInline(numbered[2])}</span></div>
  }
  if (/^[-*•]\s+/.test(t)) return <div key={key} style={{ display: 'flex', gap: 8, margin: '2px 0 2px 2px' }}><span style={{ color: 'var(--accent)', flexShrink: 0 }}>•</span><span>{renderInline(t.replace(/^[-*•]\s+/, ''))}</span></div>
  return <div key={key} style={{ marginBottom: 3 }}>{renderInline(line)}</div>
}
// render the assistant answer with tables/bold/bullets/line-breaks instead of raw plain text
export function Answer({ text }) {
  const lines = (text || '').split('\n')
  const out = []
  let i = 0
  while (i < lines.length) {
    const table = tableAt(lines, i)
    if (table) {
      out.push(table.node)
      i = table.next
    } else {
      out.push(lineNode(lines[i], i))
      i++
    }
  }
  return <div style={{ fontSize: 14, lineHeight: 1.6, color: 'var(--fg)' }}>{out}</div>
}

DsRow.propTypes = {
  k: PropTypes.node,
  v: PropTypes.node,
  mono: PropTypes.bool,
}
ChatbotView.propTypes = {
  adminKey: PropTypes.string.isRequired,
  tenants: PropTypes.arrayOf(tenantType).isRequired,
  toast: toastType.isRequired,
}
DocViewer.propTypes = {
  doc: PropTypes.shape({
    id: PropTypes.string,
    filename: PropTypes.string,
    page_label: PropTypes.string,
    citation: PropTypes.string,
    text: PropTypes.string,
  }).isRequired,
  adminKey: PropTypes.string.isRequired,
  toast: toastType.isRequired,
  onClose: PropTypes.func.isRequired,
}
LiveFlow.propTypes = {
  feed: PropTypes.arrayOf(PropTypes.shape({
    kind: PropTypes.string,
    text: PropTypes.string,
    ok: PropTypes.bool,
    rows: PropTypes.number,
    error: PropTypes.bool,
  })).isRequired,
  fallback: PropTypes.string,
  working: PropTypes.bool,
}
Message.propTypes = {
  m: PropTypes.object.isRequired,
  openDoc: PropTypes.func.isRequired,
  onFeedback: PropTypes.func.isRequired,
  onPick: PropTypes.func,
}
UserBubble.propTypes = { m: PropTypes.object.isRequired }
ConfirmationOptions.propTypes = { confirmation: PropTypes.object, onPick: PropTypes.func }
AssumptionNotes.propTypes = { assumptions: PropTypes.array, onPick: PropTypes.func }
HypothesisLedger.propTypes = { hypotheses: PropTypes.array.isRequired }
HypothesisCard.propTypes = { hypothesis: PropTypes.object.isRequired, index: PropTypes.number.isRequired }
AnswerMetaChips.propTypes = { m: PropTypes.object.isRequired, openDoc: PropTypes.func }
ExportActions.propTypes = { m: PropTypes.object.isRequired }
FeedbackControls.propTypes = { m: PropTypes.object.isRequired, onFeedback: PropTypes.func }
MdTable.propTypes = {
  header: PropTypes.arrayOf(PropTypes.node).isRequired,
  rows: PropTypes.arrayOf(PropTypes.arrayOf(PropTypes.node)).isRequired,
  aligns: PropTypes.arrayOf(PropTypes.string).isRequired,
}
Answer.propTypes = { text: PropTypes.string }
const botAvatar = { width: 30, height: 30, borderRadius: 8, background: 'var(--accent-soft)', color: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, marginTop: 2 }
const railLbl = { fontSize: 10.5, fontWeight: 600, letterSpacing: '.07em', color: 'var(--fg-subtle)', textTransform: 'uppercase', padding: '0 4px 6px' }
const railSel = { width: '100%', height: 34, padding: '0 8px', border: '1px solid var(--border)', borderRadius: 8, background: 'var(--bg)', color: 'var(--fg)', fontSize: 12.5 }
const chip = (bg, color) => ({ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 11.5, padding: '4px 9px', borderRadius: 7, background: bg, color, border: '1px solid var(--border)' })
