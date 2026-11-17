import React, { useState, useEffect, useCallback, useMemo } from 'react'
import PropTypes from 'prop-types'
import { api } from '../api.js'
import { tenantType } from '../propTypes.js'
import { fmtTime } from '../time.js'
import { Icon } from '../tos.jsx'
import { Answer } from './Chatbot.jsx'
import { extractBlocks } from './viz.jsx'

const EV = {
  question: { icon: 'help', c: 'var(--fg-muted)' },
  query: { icon: 'code', c: 'var(--accent)' },
  answer: { icon: 'auto_awesome', c: 'var(--success)' },
  blocked: { icon: 'block', c: 'var(--danger)' },
  memory_capture: { icon: 'psychology', c: 'var(--warn)' },
  feedback: { icon: 'reviews', c: 'var(--warn)' },
  hypothesis: { icon: 'experiment', c: 'var(--accent)' },
  cancelled: { icon: 'stop_circle', c: 'var(--warn)' },
}


function groupByUser(rows) {
  const groups = new Map()
  rows.forEach(row => {
    const userId = (row.user_id || '').trim() || '(legacy/unrecorded user)'
    if (!groups.has(userId)) groups.set(userId, { userId, rows: [], roles: new Set(), traces: new Set(), latest: '' })
    const group = groups.get(userId)
    group.rows.push(row)
    if (row.role) group.roles.add(row.role)
    if (row.trace_id) group.traces.add(row.trace_id)
    if ((row.ts || '') > group.latest) group.latest = row.ts
  })
  return [...groups.values()].sort((a, b) => (b.latest || '').localeCompare(a.latest || ''))
}

function summaryFor(row) {
  const detail = row.detail || {}
  return row.event === 'query'
    ? (detail.safe_sql || detail.error || '')
    : row.event === 'answer'
      ? (detail.answer || '')
      : row.event === 'blocked'
        ? (detail.reasons || []).join(', ')
        : row.event === 'memory_capture'
          ? `${detail.count || 0} memories saved`
          : row.event === 'feedback'
            ? `${detail.rating || ''}${detail.reason ? ` · ${detail.reason}` : ''}${detail.comment ? ` · ${detail.comment}` : ''}`
          : row.event === 'hypothesis'
            ? `${detail.id || ''} ${detail.statement || ''}`
          : row.event === 'cancelled'
            ? 'Stopped by the user before the final answer'
          : (detail.text || '')
}

function groupByTrace(rows) {
  const groups = new Map()
  rows.forEach((row, index) => {
    const traceId = (row.trace_id || '').trim()
    // Legacy events without a trace must not be merged into one fake conversation.
    const key = traceId || `untraced:${index}:${row.ts || ''}`
    if (!groups.has(key)) {
      groups.set(key, {
        key,
        traceId,
        items: [],
        latest: '',
        started: '',
        question: '',
        cost: 0,
      })
    }
    const group = groups.get(key)
    group.items.push({ row, index })
    if (!group.latest || (row.ts || '') > group.latest) group.latest = row.ts || ''
    if (!group.started || (row.ts || '') < group.started) group.started = row.ts || ''
    if (row.event === 'question' && !group.question) group.question = summaryFor(row)
    group.cost += Number(row.cost || 0)
  })
  return [...groups.values()].sort((a, b) => (b.latest || '').localeCompare(a.latest || ''))
}

function AuditRow({ tenantId, row, index, open, setOpen, showTrace = true }) {
  const ev = EV[row.event] || EV.question
  const detail = row.detail || {}
  const summary = summaryFor(row)
  const rowKey = `${tenantId}:${row.trace_id || index}:${row.event}:${index}`
  const expanded = open === rowKey

  return (
    <div style={{ border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface)', padding: '10px 13px' }}>
      <button type="button" onClick={() => setOpen(expanded ? null : rowKey)}
        aria-expanded={expanded}
        style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', width: '100%', padding: 0, border: 0, background: 'transparent', color: 'inherit', textAlign: 'left' }}>
        <Icon name={ev.icon} size={16} color={ev.c} />
        <span style={{ fontWeight: 600, fontSize: 12.5, textTransform: 'capitalize', flex: '0 0 92px' }}>{(row.event || '').replace('_', ' ')}</span>
        <span className="mono" style={{ fontSize: 11, color: 'var(--fg-subtle)', flex: '0 0 132px' }}>{fmtTime(row.ts)}</span>
        <span style={{ fontSize: 11.5, color: 'var(--fg-muted)', flex: '0 0 90px' }}>{row.role || '—'}</span>
        {row.model ? <span className="mono" style={{ fontSize: 10.5, color: 'var(--fg-subtle)', padding: '2px 7px', borderRadius: 6, background: 'var(--surface-2)', flexShrink: 0 }}>{row.model}</span> : null}
        {row.status ? <span style={{ fontSize: 10.5, fontWeight: 600, flexShrink: 0, color: row.status === 'ok' ? 'var(--success)' : 'var(--danger)' }}>{row.code || row.status}</span> : null}
        <span style={{ flex: 1, minWidth: 0, fontSize: 12, color: 'var(--fg-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontFamily: row.event === 'query' ? "'IBM Plex Mono',monospace" : 'inherit' }}>{summary}</span>
        {row.cost ? <span className="mono" style={{ fontSize: 11, color: 'var(--fg-subtle)', flexShrink: 0 }}>${row.cost}</span> : null}
        {showTrace && <span className="mono" style={{ fontSize: 10, color: 'var(--fg-subtle)', flexShrink: 0 }}>{(row.trace_id || '').slice(0, 6)}</span>}
        <Icon name={expanded ? 'expand_less' : 'expand_more'} size={16} color="var(--fg-subtle)" />
      </button>
      {expanded && <pre className="mono" style={{ margin: '10px 0 0', padding: '10px 12px', background: 'var(--surface-2)', borderRadius: 8, fontSize: 11.5, whiteSpace: 'pre-wrap', overflowX: 'auto', color: 'var(--fg)', lineHeight: 1.55 }}>{JSON.stringify(detail, null, 2)}</pre>}
    </div>
  )
}

function SessionCard({ tenantId, userId, session, expanded, onToggle, open, setOpen }) {
  const title = session.question || `Conversation ${session.traceId ? session.traceId.slice(0, 8) : 'without a trace'}`
  const chronological = [...session.items].reverse()

  return (
    <section style={{ border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface)', overflow: 'hidden' }}>
      <button type="button" onClick={onToggle}
        style={{ width: '100%', border: 0, padding: '11px 12px', background: 'transparent', color: 'var(--fg)', display: 'flex', alignItems: 'center', gap: 10, textAlign: 'left', cursor: 'pointer' }}>
        <span style={{ width: 30, height: 30, borderRadius: 8, display: 'grid', placeItems: 'center', background: 'var(--accent-soft)', color: 'var(--accent)', flexShrink: 0 }}>
          <Icon name="forum" size={17} />
        </span>
        <span style={{ flex: 1, minWidth: 0 }}>
          <span style={{ display: '-webkit-box', WebkitBoxOrient: 'vertical', WebkitLineClamp: 2, overflow: 'hidden', overflowWrap: 'anywhere', fontSize: 12.5, fontWeight: 650, lineHeight: 1.4 }}>
            {title}
          </span>
          <span style={{ display: 'flex', flexWrap: 'wrap', gap: '3px 8px', marginTop: 4, color: 'var(--fg-subtle)', fontSize: 10.5 }}>
            <span>{fmtTime(session.started)}</span>
            <span className="mono">trace {session.traceId ? session.traceId.slice(0, 8) : 'not recorded'}</span>
          </span>
        </span>
        {session.cost > 0 && <span className="mono" style={{ fontSize: 10.5, color: 'var(--fg-subtle)', flexShrink: 0 }}>${session.cost.toFixed(6)}</span>}
        <span style={{ padding: '3px 8px', borderRadius: 999, background: 'var(--accent-soft)', color: 'var(--accent)', fontSize: 10.5, fontWeight: 600, flexShrink: 0 }}>
          {session.items.length} events
        </span>
        <Icon name={expanded ? 'expand_less' : 'expand_more'} size={17} color="var(--fg-subtle)" />
      </button>
      {expanded && (
        <div style={{ borderTop: '1px solid var(--border)', padding: 8, background: 'var(--surface-2)', display: 'flex', flexDirection: 'column', gap: 6 }}>
          {chronological.map(({ row, index }) => (
            <AuditRow
              key={`${session.key}:${row.event}:${index}`}
              tenantId={`${tenantId}:${userId}:${session.key}`}
              row={row}
              index={index}
              open={open}
              setOpen={setOpen}
              showTrace={false}
            />
          ))}
        </div>
      )}
    </section>
  )
}

// ---- conversations: the audit trail as chat sessions (what a reviewer reads) ----
const STATUS = {
  complete: { label: 'Complete', c: 'var(--success)' },
  partial: { label: 'Partial', c: 'var(--warn)' },
  no_data: { label: 'No data', c: 'var(--warn)' },
  awaiting_confirmation: { label: 'Asked back', c: 'var(--accent)' },
  blocked: { label: 'Blocked', c: 'var(--danger)' },
  error: { label: 'Error', c: 'var(--danger)' },
  cancelled: { label: 'Stopped', c: 'var(--fg-subtle)' },
}
const REASONS = { incorrect: 'Incorrect', incomplete: 'Incomplete', irrelevant: 'Irrelevant', unclear: 'Unclear', outdated: 'Outdated', other: 'Other' }

const pill = (c) => ({ display: 'inline-flex', alignItems: 'center', gap: 4, padding: '2px 8px', borderRadius: 999, fontSize: 11, fontWeight: 600, color: c, background: `color-mix(in srgb, ${c} 12%, transparent)`, whiteSpace: 'nowrap' })
const meta = { fontSize: 11, color: 'var(--fg-subtle)', whiteSpace: 'nowrap' }

function seconds(from, to) {
  const a = Date.parse(from), b = Date.parse(to)
  return Number.isFinite(a) && Number.isFinite(b) && b >= a ? Math.round((b - a) / 1000) : null
}

function sessionFlags(session) {
  const counts = {}
  session.turns.forEach(t => { if (t.status && t.status !== 'complete') counts[t.status] = (counts[t.status] || 0) + 1 })
  return counts
}

function SessionItem({ session, active, onClick }) {
  const first = session.turns[0]?.question || '(no question)'
  const flags = sessionFlags(session)
  return (
    <button type="button" onClick={onClick} aria-current={active ? 'true' : undefined}
      style={{ width: '100%', textAlign: 'left', border: 0, borderBottom: '1px solid var(--border)', padding: '11px 14px', cursor: 'pointer', background: active ? 'var(--accent-soft)' : 'transparent', color: 'var(--fg)', display: 'flex', flexDirection: 'column', gap: 5 }}>
      <span style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
        <span className="mono" style={{ flex: 1, minWidth: 0, fontSize: 11.5, color: 'var(--fg-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{session.user_id || 'unknown user'}</span>
        <span style={meta}>{fmtTime(session.latest)}</span>
      </span>
      <span style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.4, display: '-webkit-box', WebkitBoxOrient: 'vertical', WebkitLineClamp: 2, overflow: 'hidden', overflowWrap: 'anywhere' }}>{first}</span>
      <span style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
        <span style={meta}>{session.turns.length} {session.turns.length === 1 ? 'question' : 'questions'}</span>
        {session.feedback.helpful > 0 && <span style={pill('var(--success)')}><Icon name="thumb_up" size={12} />{session.feedback.helpful}</span>}
        {session.feedback.unhelpful > 0 && <span style={pill('var(--danger)')}><Icon name="thumb_down" size={12} />{session.feedback.unhelpful}</span>}
        {Object.entries(flags).map(([k, n]) => <span key={k} style={pill((STATUS[k] || STATUS.error).c)}>{n} {(STATUS[k] || { label: k }).label.toLowerCase()}</span>)}
      </span>
    </button>
  )
}

function FeedbackNote({ fb }) {
  const good = fb.rating === 'helpful'
  const c = good ? 'var(--success)' : 'var(--danger)'
  return (
    <div style={{ marginTop: 8, padding: '9px 12px', borderRadius: 10, border: `1px solid color-mix(in srgb, ${c} 35%, var(--border))`, background: `color-mix(in srgb, ${c} 7%, var(--surface))`, display: 'flex', gap: 9, alignItems: 'flex-start' }}>
      <Icon name={good ? 'thumb_up' : 'thumb_down'} size={16} color={c} />
      <div style={{ minWidth: 0, fontSize: 12.5, lineHeight: 1.5 }}>
        <div><b style={{ color: c }}>{good ? 'Helpful' : 'Not helpful'}</b>{fb.reason ? ` · ${REASONS[fb.reason] || fb.reason}` : ''}<span style={{ ...meta, marginLeft: 8 }}>{fb.user_id} · {fmtTime(fb.updated)}</span></div>
        {fb.comment && <div style={{ marginTop: 3, color: 'var(--fg)', overflowWrap: 'anywhere' }}>“{fb.comment}”</div>}
      </div>
    </div>
  )
}

function TurnDetails({ turn, tenantKey }) {
  const [open, setOpen] = useState(null)
  const facts = [
    ['Plan', (turn.plan || []).join(' → ')],
    ['Model', turn.model],
    ['Confidence', turn.confidence],
    ['Missing', (turn.missing_requirements || []).join(', ')],
    ['Unverified figures', (turn.unverified_figures || []).join(', ')],
    ['Proposed figures', (turn.proposed_figures || []).join(', ')],
    ['Data sources', (turn.datasources_used || []).map(d => d.name || d.id).join(', ')],
    ['Trace', turn.trace_id],
  ].filter(([, v]) => v)
  return (
    <div style={{ marginTop: 8, padding: 10, borderRadius: 10, background: 'var(--surface-2)', border: '1px solid var(--border)', display: 'flex', flexDirection: 'column', gap: 6 }}>
      <dl style={{ margin: '0 0 4px', display: 'grid', gridTemplateColumns: 'max-content minmax(0,1fr)', gap: '4px 12px', fontSize: 12 }}>
        {facts.map(([k, v]) => (
          <React.Fragment key={k}>
            <dt style={{ color: 'var(--fg-subtle)' }}>{k}</dt>
            <dd className={k === 'Plan' || k === 'Trace' ? 'mono' : undefined} style={{ margin: 0, overflowWrap: 'anywhere', fontSize: k === 'Plan' || k === 'Trace' ? 11.5 : 12 }}>{v}</dd>
          </React.Fragment>
        ))}
      </dl>
      {turn.events.map((row, index) => {
        const key = `${tenantKey}:${turn.trace_id}:${index}`
        return <EventLine key={key} row={row} expanded={open === key} onToggle={() => setOpen(open === key ? null : key)} />
      })}
    </div>
  )
}

function EventLine({ row, expanded, onToggle }) {
  const ev = EV[row.event] || EV.question
  const bad = row.status && !['ok', 'complete', 'helpful'].includes(row.status)
  const code = row.event === 'query' && row.status === 'error'
    ? ((row.detail?.error || '').match(/^[A-Z_]{4,}/)?.[0] || row.code || 'refused')
    : (row.code || row.status)
  return (
    <div style={{ border: '1px solid var(--border)', borderRadius: 8, background: 'var(--surface)' }}>
      <button type="button" onClick={onToggle} aria-expanded={expanded}
        style={{ width: '100%', display: 'grid', gridTemplateColumns: '18px 70px minmax(0,1fr) auto 16px', gap: 8, alignItems: 'center', padding: '6px 9px', border: 0, background: 'transparent', color: 'inherit', textAlign: 'left', cursor: 'pointer' }}>
        <Icon name={ev.icon} size={15} color={ev.c} />
        <span style={{ fontSize: 12, fontWeight: 600, textTransform: 'capitalize' }}>{(row.event || '').replace('_', ' ')}</span>
        <span style={{ fontSize: 11.5, color: 'var(--fg-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontFamily: row.event === 'query' ? "'IBM Plex Mono',monospace" : 'inherit' }}>{summaryFor(row)}</span>
        {code ? <span style={{ fontSize: 10.5, fontWeight: 600, color: bad ? 'var(--danger)' : 'var(--success)' }}>{code}</span> : <span />}
        <Icon name={expanded ? 'expand_less' : 'expand_more'} size={15} color="var(--fg-subtle)" />
      </button>
      {expanded && <pre className="mono" style={{ margin: 0, padding: '9px 11px', borderTop: '1px solid var(--border)', fontSize: 11.5, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', lineHeight: 1.5, color: 'var(--fg)' }}>{JSON.stringify(row.detail, null, 2)}</pre>}
    </div>
  )
}

function Turn({ turn, tenantKey }) {
  const [details, setDetails] = useState(false)
  const st = STATUS[turn.status] || (turn.status ? { label: turn.status, c: 'var(--fg-subtle)' } : null)
  const took = seconds(turn.ts, turn.answered)
  const answer = extractBlocks(turn.answer || '')
  const charts = (answer.charts?.length || 0) + (answer.artifacts?.length || 0)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 3 }}>
        <div style={{ maxWidth: '78%', background: 'var(--accent)', color: '#fff', padding: '10px 14px', borderRadius: '16px 16px 4px 16px', fontSize: 14, lineHeight: 1.55, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{turn.question}</div>
        <span style={meta}>{fmtTime(turn.ts)}</span>
      </div>
      <div style={{ display: 'flex', gap: 11, maxWidth: '90%' }}>
        <div style={{ width: 28, height: 28, borderRadius: 8, background: 'var(--accent-soft)', color: 'var(--accent)', display: 'grid', placeItems: 'center', flexShrink: 0, marginTop: 2 }}><Icon name="auto_awesome" size={16} /></div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ padding: '11px 14px', borderRadius: '4px 16px 16px 16px', background: 'var(--surface)', border: '1px solid var(--border)' }}>
            {turn.answer
              ? <Answer text={answer.clean} />
              : <div style={{ fontSize: 13, color: 'var(--fg-subtle)' }}>No answer was recorded for this question.</div>}
            {charts > 0 && <div style={{ ...meta, marginTop: 6 }}><Icon name="bar_chart" size={13} /> {charts} chart{charts > 1 ? 's' : ''} shown to the user (not drawn here)</div>}
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center', marginTop: 6 }}>
            {st && <span style={pill(st.c)}>{st.label}</span>}
            {turn.confidence && <span style={meta}>Confidence {turn.confidence}</span>}
            {turn.queries > 0 && <span style={meta}>{turn.queries} {turn.queries === 1 ? 'query' : 'queries'}{turn.failed_queries ? `, ${turn.failed_queries} refused or failed` : ''}</span>}
            {took != null && <span style={meta}>{took} s</span>}
            {turn.cost > 0 && <span className="mono" style={meta}>${turn.cost.toFixed(4)}</span>}
            <button type="button" onClick={() => setDetails(!details)} aria-expanded={details}
              style={{ marginLeft: 'auto', border: '1px solid var(--border)', background: 'var(--surface)', color: 'var(--fg-muted)', borderRadius: 8, padding: '3px 9px', fontSize: 11.5, fontWeight: 600, cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 4 }}>
              <Icon name={details ? 'expand_less' : 'list_alt'} size={14} />{details ? 'Hide details' : `Details · ${turn.events.length} events`}
            </button>
          </div>
          {turn.feedback.map(fb => <FeedbackNote key={`${fb.user_id}:${fb.updated}:${fb.rating}`} fb={fb} />)}
          {details && <TurnDetails turn={turn} tenantKey={tenantKey} />}
        </div>
      </div>
    </div>
  )
}

function Conversation({ session, tenantId }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0, height: '100%' }}>
      <div style={{ padding: '12px 18px', borderBottom: '1px solid var(--border)', display: 'flex', flexWrap: 'wrap', gap: '4px 14px', alignItems: 'baseline' }}>
        <span className="mono" style={{ fontSize: 13, fontWeight: 700 }}>{session.user_id || 'unknown user'}</span>
        <span style={meta}>{session.roles.join(', ') || 'role not recorded'}</span>
        <span style={meta}>{fmtTime(session.started)} – {fmtTime(session.latest)}</span>
        <span style={meta}>{session.turns.length} {session.turns.length === 1 ? 'question' : 'questions'}</span>
        {session.cost > 0 && <span className="mono" style={meta}>${session.cost.toFixed(4)}</span>}
        <span className="mono" style={{ ...meta, marginLeft: 'auto' }}>{session.session_id ? `session ${session.session_id}` : 'no session id sent (single question)'}</span>
      </div>
      <div style={{ flex: 1, overflowY: 'auto', padding: '18px 22px 30px', display: 'flex', flexDirection: 'column', gap: 26 }}>
        {session.turns.map(turn => <Turn key={turn.trace_id} turn={turn} tenantKey={`${tenantId}:${session.key}`} />)}
      </div>
    </div>
  )
}

function ConversationsView({ adminKey, tenants, toast }) {
  const [tenantId, setTenantId] = useState(tenants[0]?.id || '')
  const [sessions, setSessions] = useState([])
  const [selected, setSelected] = useState(null)
  const [user, setUser] = useState('')
  const [filter, setFilter] = useState('all')
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!tenants.some(t => t.id === tenantId)) setTenantId(tenants[0]?.id || '')
  }, [tenants, tenantId])

  const load = useCallback(async () => {
    if (!tenantId) { setSessions([]); return }
    setLoading(true)
    try {
      const res = await api.get(`/admin/tenants/${tenantId}/conversations?limit=100`, adminKey)
      if (res.ok) setSessions(res.data || [])
      else { setSessions([]); toast.bad("Couldn't load the conversations") }
    } finally {
      setLoading(false)
    }
  }, [tenantId, adminKey, toast])

  useEffect(() => { load() }, [load])

  const users = useMemo(() => [...new Set(sessions.map(s => s.user_id).filter(Boolean))].sort((a, b) => a.localeCompare(b)), [sessions])
  const shown = useMemo(() => {
    const q = search.trim().toLowerCase()
    return sessions.filter(s => {
      if (user && s.user_id !== user) return false
      if (filter === 'feedback' && !(s.feedback.helpful + s.feedback.unhelpful)) return false
      if (filter === 'unhelpful' && !s.feedback.unhelpful) return false
      if (filter === 'issues' && !Object.keys(sessionFlags(s)).length) return false
      return !q || s.turns.some(t => `${t.question} ${t.answer}`.toLowerCase().includes(q))
    })
  }, [sessions, user, filter, search])
  const current = shown.find(s => `${s.user_id}:${s.key}` === selected) || shown[0] || null

  const ctl = { height: 34, padding: '0 10px', border: '1px solid var(--border)', borderRadius: 9, background: 'var(--surface)', color: 'var(--fg)', fontSize: 12.5 }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, flex: 1, minHeight: 0, padding: '0 34px 24px' }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
        <select id="audit-tenant" aria-label="Tenant" value={tenantId} onChange={e => { setTenantId(e.target.value); setSelected(null) }} style={ctl}>
          {tenants.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
        </select>
        <select id="audit-user" aria-label="User" value={user} onChange={e => setUser(e.target.value)} style={ctl}>
          <option value="">All users</option>
          {users.map(u => <option key={u} value={u}>{u}</option>)}
        </select>
        <select id="audit-filter" aria-label="Show" value={filter} onChange={e => setFilter(e.target.value)} style={ctl}>
          <option value="all">All sessions</option>
          <option value="feedback">With feedback</option>
          <option value="unhelpful">Rated not helpful</option>
          <option value="issues">Partial, no data, blocked or error</option>
        </select>
        <input id="audit-search" type="search" placeholder="Search questions and answers" value={search} onChange={e => setSearch(e.target.value)} style={{ ...ctl, flex: '1 1 220px', minWidth: 180 }} />
        <span style={{ fontSize: 12, color: 'var(--fg-muted)' }}>{loading ? 'Loading…' : `${shown.length} of ${sessions.length} sessions`}</span>
        <button onClick={load} disabled={loading} style={{ ...ctl, display: 'inline-flex', alignItems: 'center', gap: 6, fontWeight: 600, color: 'var(--fg-muted)', cursor: loading ? 'wait' : 'pointer' }}><Icon name="refresh" size={16} />Refresh</button>
      </div>
      <div style={{ flex: 1, minHeight: 480, display: 'grid', gridTemplateColumns: 'minmax(260px, 340px) minmax(0, 1fr)', border: '1px solid var(--border)', borderRadius: 14, overflow: 'hidden', background: 'var(--bg)' }}>
        <div style={{ borderRight: '1px solid var(--border)', overflowY: 'auto', background: 'var(--surface)' }}>
          {shown.length === 0 && !loading && <div style={{ padding: 24, fontSize: 12.5, color: 'var(--fg-subtle)', textAlign: 'center' }}>No conversations match.</div>}
          {shown.map(s => {
            const key = `${s.user_id}:${s.key}`
            return <SessionItem key={key} session={s} active={current && key === `${current.user_id}:${current.key}`} onClick={() => setSelected(key)} />
          })}
        </div>
        <div style={{ minWidth: 0, minHeight: 0 }}>
          {current
            ? <Conversation key={`${current.user_id}:${current.key}`} session={current} tenantId={tenantId} />
            : <div style={{ padding: 40, color: 'var(--fg-subtle)', textAlign: 'center', fontSize: 13 }}>Pick a conversation on the left.</div>}
        </div>
      </div>
    </div>
  )
}

export default function AuditView({ adminKey, tenants, toast }) {
  const [tab, setTab] = useState('conversations')
  const tabBtn = (id, label, icon) => (
    <button type="button" onClick={() => setTab(id)} aria-pressed={tab === id}
      style={{ height: 34, padding: '0 12px', border: '1px solid var(--border)', borderRadius: 9, cursor: 'pointer', fontSize: 12.5, fontWeight: 600, display: 'inline-flex', alignItems: 'center', gap: 6, background: tab === id ? 'var(--accent-soft)' : 'var(--surface)', color: tab === id ? 'var(--accent)' : 'var(--fg-muted)' }}>
      <Icon name={icon} size={16} />{label}
    </button>
  )
  return (
    <div style={{ flex: 1, overflowY: tab === 'events' ? 'auto' : 'hidden', display: 'flex', flexDirection: 'column' }} className="fade">
      <header style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', flexWrap: 'wrap', gap: 14, padding: '26px 34px 14px' }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, letterSpacing: '-.02em' }}>Audit Log</h1>
          <p style={{ color: 'var(--fg-muted)', marginTop: 4, fontSize: 13.5 }}>
            {tab === 'conversations'
              ? 'Every chat session as the user saw it, with feedback. Open a reply’s details for its queries and checks.'
              : 'Every recorded event, grouped by tenant, user and question.'}
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          {tabBtn('conversations', 'Conversations', 'forum')}
          {tabBtn('events', 'All events', 'list_alt')}
        </div>
      </header>
      {tab === 'conversations'
        ? <ConversationsView adminKey={adminKey} tenants={tenants} toast={toast} />
        : <RawEventsView adminKey={adminKey} tenants={tenants} toast={toast} />}
    </div>
  )
}

function RawEventsView({ adminKey, tenants, toast }) {
  const [rowsByTenant, setRowsByTenant] = useState({})
  const [collapsed, setCollapsed] = useState({})
  const [collapsedUsers, setCollapsedUsers] = useState({})
  const [expandedSessions, setExpandedSessions] = useState({})
  const [open, setOpen] = useState(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    if (!tenants.length) {
      setRowsByTenant({})
      return
    }
    setLoading(true)
    try {
      const results = await Promise.all(tenants.map(async tenant => ({
        tenant,
        response: await api.get(`/admin/tenants/${tenant.id}/audit?limit=500`, adminKey),
      })))
      const next = {}
      const failed = []
      results.forEach(({ tenant, response }) => {
        if (response.ok) next[tenant.id] = response.data
        else {
          next[tenant.id] = []
          failed.push(tenant.name)
        }
      })
      setRowsByTenant(next)
      if (failed.length) toast.bad(`Couldn't load the audit log for: ${failed.join(', ')}`)
    } finally {
      setLoading(false)
    }
  }, [adminKey, tenants, toast])

  useEffect(() => { load() }, [load])

  const totals = useMemo(() => {
    const rows = Object.values(rowsByTenant).reduce((sum, items) => sum + items.length, 0)
    const active = Object.values(rowsByTenant).filter(items => items.length > 0).length
    const users = new Set()
    const sessions = new Set()
    Object.entries(rowsByTenant).forEach(([tenantId, items]) => {
      items.forEach((row, index) => {
        const userId = (row.user_id || '').trim() || '(unknown)'
        users.add(`${tenantId}:${userId}`)
        sessions.add(`${tenantId}:${userId}:${(row.trace_id || '').trim() || `untraced:${index}`}`)
      })
    })
    return { rows, active, users: users.size, sessions: sessions.size }
  }, [rowsByTenant])

  const toggleTenant = (tenantId) => {
    setCollapsed(current => ({ ...current, [tenantId]: !current[tenantId] }))
  }

  return (
    <div>
      <header style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 20, padding: '0 34px 10px' }}>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <span style={{ fontSize: 12.5, color: 'var(--fg-muted)' }}>
            {loading ? 'Loading…' : `${totals.rows} events · ${totals.sessions} sessions · ${totals.users} users · ${totals.active}/${tenants.length} tenants active`}
          </span>
          <button onClick={load} disabled={loading} style={{ height: 38, padding: '0 14px', border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface)', color: 'var(--fg-muted)', fontSize: 13, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 6, cursor: loading ? 'wait' : 'pointer', opacity: loading ? .65 : 1 }}><Icon name="refresh" size={16} />Refresh</button>
        </div>
      </header>

      <div style={{ padding: '6px 34px 40px', display: 'flex', flexDirection: 'column', gap: 14 }}>
        {!loading && tenants.length === 0 && (
          <div style={{ color: 'var(--fg-subtle)', padding: '40px 0', textAlign: 'center', border: '1px dashed var(--border)', borderRadius: 14 }}>No tenants to show yet.</div>
        )}

        {tenants.map(tenant => {
          const rows = rowsByTenant[tenant.id] || []
          const userGroups = groupByUser(rows)
          const isCollapsed = !!collapsed[tenant.id]
          const latest = rows[0]?.ts
          return (
            <section key={tenant.id} style={{ border: '1px solid var(--border)', borderRadius: 14, background: 'var(--surface-2)', overflow: 'hidden' }}>
              <button type="button" onClick={() => toggleTenant(tenant.id)}
                style={{ width: '100%', border: 0, padding: '13px 15px', background: 'transparent', color: 'var(--fg)', display: 'flex', alignItems: 'center', gap: 11, textAlign: 'left', cursor: 'pointer' }}>
                <span style={{ width: 34, height: 34, borderRadius: 9, display: 'grid', placeItems: 'center', background: 'var(--accent-soft)', color: 'var(--accent)', flexShrink: 0 }}>
                  <Icon name={tenant.icon || 'domain'} size={18} />
                </span>
                <span style={{ minWidth: 0, flex: 1 }}>
                  <span style={{ display: 'block', fontSize: 13.5, fontWeight: 700 }}>{tenant.name}</span>
                  <span className="mono" style={{ display: 'block', marginTop: 2, fontSize: 10.5, color: 'var(--fg-subtle)' }}>{tenant.id}</span>
                </span>
                {latest && <span style={{ fontSize: 11, color: 'var(--fg-subtle)' }}>Last activity {fmtTime(latest)}</span>}
                <span style={{ minWidth: 62, textAlign: 'center', padding: '4px 9px', borderRadius: 999, background: 'var(--surface)', color: 'var(--fg-muted)', fontSize: 11, fontWeight: 600 }}>
                  {userGroups.length} users
                </span>
                <span style={{ minWidth: 84, textAlign: 'center', padding: '4px 9px', borderRadius: 999, background: rows.length ? 'var(--accent-soft)' : 'var(--surface)', color: rows.length ? 'var(--accent)' : 'var(--fg-subtle)', fontSize: 11, fontWeight: 600 }}>
                  {rows.length} events
                </span>
                <Icon name={isCollapsed ? 'expand_more' : 'expand_less'} size={18} color="var(--fg-subtle)" />
              </button>

              {!isCollapsed && (
                <div style={{ borderTop: '1px solid var(--border)', padding: rows.length ? '10px' : '18px', display: 'flex', flexDirection: 'column', gap: 9 }}>
                  {rows.length === 0
                    ? <div style={{ color: 'var(--fg-subtle)', fontSize: 12.5, textAlign: 'center' }}>No activity for this tenant yet.</div>
                    : userGroups.map(group => {
                      const userKey = `${tenant.id}:${group.userId}`
                      const userCollapsed = !!collapsedUsers[userKey]
                      const sessions = groupByTrace(group.rows)
                      return (
                        <div key={group.userId} style={{ border: '1px solid var(--border)', borderRadius: 11, background: 'var(--bg)', overflow: 'hidden' }}>
                          <button type="button" onClick={() => setCollapsedUsers(current => ({ ...current, [userKey]: !current[userKey] }))}
                            style={{ width: '100%', border: 0, padding: '10px 12px', background: 'transparent', color: 'var(--fg)', display: 'flex', alignItems: 'center', gap: 10, textAlign: 'left', cursor: 'pointer' }}>
                            <span style={{ width: 30, height: 30, borderRadius: '50%', display: 'grid', placeItems: 'center', background: 'color-mix(in srgb,var(--accent) 10%,transparent)', color: 'var(--accent)', flexShrink: 0 }}>
                              <Icon name="person" size={17} />
                            </span>
                            <span style={{ flex: 1, minWidth: 0 }}>
                              <span className="mono" style={{ display: 'block', fontSize: 12.5, fontWeight: 700, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{group.userId}</span>
                              <span style={{ display: 'block', fontSize: 10.5, color: 'var(--fg-subtle)', marginTop: 2 }}>{[...group.roles].join(', ') || 'role not recorded'} · last active {fmtTime(group.latest)}</span>
                            </span>
                            <span style={{ padding: '3px 8px', borderRadius: 999, background: 'var(--surface-2)', color: 'var(--fg-muted)', fontSize: 10.5, fontWeight: 600 }}>{sessions.length} sessions</span>
                            <span style={{ padding: '3px 8px', borderRadius: 999, background: 'var(--accent-soft)', color: 'var(--accent)', fontSize: 10.5, fontWeight: 600 }}>{group.rows.length} events</span>
                            <Icon name={userCollapsed ? 'expand_more' : 'expand_less'} size={17} color="var(--fg-subtle)" />
                          </button>
                          {!userCollapsed && (
                            <div style={{ borderTop: '1px solid var(--border)', padding: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
                              {sessions.map(session => {
                                const sessionKey = `${userKey}:${session.key}`
                                return (
                                  <SessionCard
                                    key={session.key}
                                    tenantId={tenant.id}
                                    userId={group.userId}
                                    session={session}
                                    expanded={!!expandedSessions[sessionKey]}
                                    onToggle={() => setExpandedSessions(current => ({ ...current, [sessionKey]: !current[sessionKey] }))}
                                    open={open}
                                    setOpen={setOpen}
                                  />
                                )
                              })}
                            </div>
                          )}
                        </div>
                      )
                    })}
                </div>
              )}
            </section>
          )
        })}
      </div>
    </div>
  )
}

// ---- props validation: the shapes the audit endpoints return ----
const auditEventType = PropTypes.shape({
  event: PropTypes.string,
  ts: PropTypes.string,
  role: PropTypes.string,
  user_id: PropTypes.string,
  model: PropTypes.string,
  status: PropTypes.string,
  code: PropTypes.string,
  cost: PropTypes.number,
  trace_id: PropTypes.string,
  // free-form per event; `error` is the only key read outside summaryFor
  detail: PropTypes.shape({ error: PropTypes.string }),
})
const feedbackType = PropTypes.shape({
  rating: PropTypes.string,
  reason: PropTypes.string,
  comment: PropTypes.string,
  user_id: PropTypes.string,
  updated: PropTypes.string,
})
const turnType = PropTypes.shape({
  trace_id: PropTypes.string,
  ts: PropTypes.string,
  answered: PropTypes.string,
  question: PropTypes.string,
  answer: PropTypes.string,
  status: PropTypes.string,
  model: PropTypes.string,
  confidence: PropTypes.string,
  cost: PropTypes.number,
  queries: PropTypes.number,
  failed_queries: PropTypes.number,
  plan: PropTypes.arrayOf(PropTypes.string),
  missing_requirements: PropTypes.arrayOf(PropTypes.string),
  unverified_figures: PropTypes.arrayOf(PropTypes.string),
  proposed_figures: PropTypes.arrayOf(PropTypes.string),
  datasources_used: PropTypes.arrayOf(PropTypes.shape({ id: PropTypes.string, name: PropTypes.string })),
  events: PropTypes.arrayOf(auditEventType).isRequired,
  feedback: PropTypes.arrayOf(feedbackType).isRequired,
})
const conversationType = PropTypes.shape({
  key: PropTypes.string.isRequired,
  session_id: PropTypes.string,
  user_id: PropTypes.string,
  roles: PropTypes.arrayOf(PropTypes.string).isRequired,
  started: PropTypes.string,
  latest: PropTypes.string,
  cost: PropTypes.number,
  feedback: PropTypes.shape({ helpful: PropTypes.number, unhelpful: PropTypes.number }).isRequired,
  turns: PropTypes.arrayOf(turnType).isRequired,
})
const viewProps = {
  adminKey: PropTypes.string,
  tenants: PropTypes.arrayOf(tenantType).isRequired,
  toast: PropTypes.func.isRequired,      // callable with .ok/.bad/.warn (tos.jsx)
}

AuditRow.propTypes = {
  tenantId: PropTypes.string.isRequired,
  row: auditEventType.isRequired,
  index: PropTypes.number.isRequired,
  open: PropTypes.string,
  setOpen: PropTypes.func.isRequired,
  showTrace: PropTypes.bool,
}
SessionCard.propTypes = {
  tenantId: PropTypes.string.isRequired,
  userId: PropTypes.string.isRequired,
  session: PropTypes.shape({
    key: PropTypes.string.isRequired,
    traceId: PropTypes.string,
    question: PropTypes.string,
    started: PropTypes.string,
    cost: PropTypes.number,
    items: PropTypes.arrayOf(PropTypes.shape({ row: auditEventType, index: PropTypes.number })).isRequired,
  }).isRequired,
  expanded: PropTypes.bool,
  onToggle: PropTypes.func.isRequired,
  open: PropTypes.string,
  setOpen: PropTypes.func.isRequired,
}
SessionItem.propTypes = { session: conversationType.isRequired, active: PropTypes.bool, onClick: PropTypes.func.isRequired }
FeedbackNote.propTypes = { fb: feedbackType.isRequired }
TurnDetails.propTypes = { turn: turnType.isRequired, tenantKey: PropTypes.string.isRequired }
EventLine.propTypes = { row: auditEventType.isRequired, expanded: PropTypes.bool, onToggle: PropTypes.func.isRequired }
Turn.propTypes = { turn: turnType.isRequired, tenantKey: PropTypes.string.isRequired }
Conversation.propTypes = { session: conversationType.isRequired, tenantId: PropTypes.string.isRequired }
ConversationsView.propTypes = viewProps
AuditView.propTypes = viewProps
RawEventsView.propTypes = viewProps