import React, { useState, useEffect, useCallback, useMemo } from 'react'
import { api } from '../api.js'
import { fmtTime } from '../time.js'
import { Icon } from '../tos.jsx'

const REASON_LABEL = {
  incorrect: 'Incorrect answer or data',
  incomplete: 'Incomplete answer',
  irrelevant: 'Not relevant',
  unclear: 'Hard to understand',
  outdated: 'Outdated data',
  other: 'Other',
}


function groupByTrace(rows) {
  const groups = new Map()
  rows.forEach(row => {
    const key = row.trace_id
    if (!groups.has(key)) groups.set(key, {
      trace_id: key,
      question: row.question,
      answer: row.answer,
      updated: row.updated,
      feedback: [],
    })
    const group = groups.get(key)
    group.feedback.push(row)
    if ((row.updated || '') > (group.updated || '')) group.updated = row.updated
  })
  return [...groups.values()].sort((a, b) => (b.updated || '').localeCompare(a.updated || ''))
}

function FeedbackCard({ group, expanded, onToggle }) {
  const negative = group.feedback.filter(x => x.rating === 'unhelpful').length
  const positive = group.feedback.length - negative
  return (
    <article style={{ border: `1px solid ${negative ? 'color-mix(in srgb,var(--danger) 35%,var(--border))' : 'var(--border)'}`, borderRadius: 12, background: 'var(--surface)', overflow: 'hidden' }}>
      <button type="button" onClick={onToggle}
        style={{ width: '100%', border: 0, padding: '12px 14px', background: 'transparent', color: 'var(--fg)', display: 'flex', alignItems: 'center', gap: 10, textAlign: 'left', cursor: 'pointer' }}>
        <span style={{ width: 30, height: 30, borderRadius: 8, display: 'grid', placeItems: 'center', background: negative ? 'color-mix(in srgb,var(--danger) 10%,transparent)' : 'color-mix(in srgb,var(--success) 10%,transparent)', color: negative ? 'var(--danger)' : 'var(--success)', flexShrink: 0 }}>
          <Icon name={negative ? 'thumb_down' : 'thumb_up'} size={16} />
        </span>
        <span style={{ flex: 1, minWidth: 0 }}>
          <span style={{ display: 'block', fontWeight: 600, fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{group.question}</span>
          <span className="mono" style={{ display: 'block', marginTop: 3, fontSize: 10.5, color: 'var(--fg-subtle)' }}>{group.trace_id.slice(0, 12)} · {fmtTime(group.updated)}</span>
        </span>
        {positive > 0 && <span style={pill('var(--success)')}><Icon name="thumb_up" size={12} />{positive}</span>}
        {negative > 0 && <span style={pill('var(--danger)')}><Icon name="thumb_down" size={12} />{negative}</span>}
        <Icon name={expanded ? 'expand_less' : 'expand_more'} size={18} color="var(--fg-subtle)" />
      </button>

      {expanded && (
        <div style={{ borderTop: '1px solid var(--border)', padding: 14 }}>
          <div style={qaBlock('help', 'var(--accent-soft)', 'var(--accent)')}>
            <div style={qaLabel}><Icon name="help" size={14} />Question</div>
            <div style={qaText}>{group.question}</div>
          </div>
          <div style={{ ...qaBlock('auto_awesome', 'var(--surface-2)', 'var(--fg-muted)'), marginTop: 9 }}>
            <div style={qaLabel}><Icon name="auto_awesome" size={14} />Poros answer</div>
            <div style={qaText}>{group.answer}</div>
          </div>

          <div style={{ marginTop: 13, fontSize: 11, fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', color: 'var(--fg-subtle)' }}>User feedback</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 7, marginTop: 8 }}>
            {group.feedback.map(item => {
              const bad = item.rating === 'unhelpful'
              return (
                <div key={item.id} style={{ display: 'grid', gridTemplateColumns: 'auto minmax(120px,.55fr) minmax(160px,1fr) auto', alignItems: 'start', gap: 10, padding: '10px 11px', borderRadius: 9, background: 'var(--surface-2)', fontSize: 12 }}>
                  <Icon name={bad ? 'thumb_down' : 'thumb_up'} size={16} color={bad ? 'var(--danger)' : 'var(--success)'} />
                  <div>
                    <div className="mono" style={{ fontWeight: 600, color: 'var(--fg)' }}>{item.user_id}</div>
                    <div style={{ color: 'var(--fg-subtle)', marginTop: 2 }}>{item.role || '—'} · {fmtTime(item.updated)}</div>
                  </div>
                  <div>
                    {item.reason && <div style={{ fontWeight: 600, color: bad ? 'var(--danger)' : 'var(--fg-muted)' }}>{REASON_LABEL[item.reason] || item.reason}</div>}
                    <div style={{ color: item.comment ? 'var(--fg-muted)' : 'var(--fg-subtle)', marginTop: item.reason ? 3 : 0, lineHeight: 1.45 }}>{item.comment || 'No comment'}</div>
                  </div>
                  <span style={{ ...pill(bad ? 'var(--danger)' : 'var(--success)'), justifySelf: 'end' }}>{bad ? 'Needs improvement' : 'Helpful'}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </article>
  )
}

export default function FeedbackView({ adminKey, tenants, toast }) {
  const [rowsByTenant, setRowsByTenant] = useState({})
  const [collapsed, setCollapsed] = useState({})
  const [expanded, setExpanded] = useState(null)
  const [filter, setFilter] = useState('all')
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    if (!tenants.length) return setRowsByTenant({})
    setLoading(true)
    try {
      const results = await Promise.all(tenants.map(async tenant => ({
        tenant,
        response: await api.get(`/admin/tenants/${tenant.id}/feedback?limit=500`, adminKey),
      })))
      const next = {}
      const failed = []
      results.forEach(({ tenant, response }) => {
        if (response.ok) next[tenant.id] = response.data
        else { next[tenant.id] = []; failed.push(tenant.name) }
      })
      setRowsByTenant(next)
      if (failed.length) toast.bad(`Couldn't load feedback for: ${failed.join(', ')}`)
    } finally {
      setLoading(false)
    }
  }, [adminKey, tenants, toast])

  useEffect(() => { load() }, [load])

  const totals = useMemo(() => {
    const all = Object.values(rowsByTenant).flat()
    const helpful = all.filter(x => x.rating === 'helpful').length
    return { all: all.length, helpful, unhelpful: all.length - helpful }
  }, [rowsByTenant])

  return (
    <div style={{ flex: 1, overflowY: 'auto' }} className="fade">
      <header style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 20, padding: '26px 34px 20px' }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, letterSpacing: '-.02em' }}>Response Feedback</h1>
          <p style={{ color: 'var(--fg-muted)', marginTop: 4, fontSize: 13.5 }}>Consumer feedback, grouped by tenant and question–answer pair.</p>
        </div>
        <button onClick={load} disabled={loading} style={refreshBtn(loading)}><Icon name="refresh" size={16} />Refresh</button>
      </header>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,minmax(150px,220px))', gap: 12, padding: '0 34px 16px' }}>
        <Kpi label="Total feedback" value={totals.all} icon="reviews" color="var(--accent)" />
        <Kpi label="Helpful" value={totals.helpful} icon="thumb_up" color="var(--success)" />
        <Kpi label="Needs improvement" value={totals.unhelpful} icon="thumb_down" color="var(--danger)" />
      </div>

      <div style={{ display: 'flex', gap: 4, padding: '0 34px 14px' }}>
        {[['all', 'All'], ['unhelpful', 'Needs improvement'], ['helpful', 'Helpful']].map(([id, label]) => (
          <button key={id} onClick={() => setFilter(id)} style={{ padding: '7px 12px', border: '1px solid var(--border)', borderRadius: 9, background: filter === id ? 'var(--accent-soft)' : 'var(--surface)', color: filter === id ? 'var(--accent)' : 'var(--fg-muted)', fontSize: 12, fontWeight: 600 }}>{label}</button>
        ))}
      </div>

      <div style={{ padding: '0 34px 40px', display: 'flex', flexDirection: 'column', gap: 14 }}>
        {tenants.map(tenant => {
          const allRows = rowsByTenant[tenant.id] || []
          const rows = filter === 'all' ? allRows : allRows.filter(x => x.rating === filter)
          const groups = groupByTrace(rows)
          const isCollapsed = !!collapsed[tenant.id]
          return (
            <section key={tenant.id} style={{ border: '1px solid var(--border)', borderRadius: 14, background: 'var(--surface-2)', overflow: 'hidden' }}>
              <button type="button" onClick={() => setCollapsed(c => ({ ...c, [tenant.id]: !c[tenant.id] }))}
                style={{ width: '100%', border: 0, padding: '13px 15px', background: 'transparent', color: 'var(--fg)', display: 'flex', alignItems: 'center', gap: 11, textAlign: 'left', cursor: 'pointer' }}>
                <span style={{ width: 34, height: 34, borderRadius: 9, display: 'grid', placeItems: 'center', background: 'var(--accent-soft)', color: 'var(--accent)' }}><Icon name={tenant.icon || 'domain'} size={18} /></span>
                <span style={{ flex: 1 }}><b style={{ fontSize: 13.5 }}>{tenant.name}</b><span className="mono" style={{ display: 'block', fontSize: 10.5, color: 'var(--fg-subtle)', marginTop: 2 }}>{tenant.id}</span></span>
                <span style={{ ...pill(rows.some(x => x.rating === 'unhelpful') ? 'var(--danger)' : 'var(--accent)'), minWidth: 92, justifyContent: 'center' }}>{rows.length} feedback</span>
                <Icon name={isCollapsed ? 'expand_more' : 'expand_less'} size={18} color="var(--fg-subtle)" />
              </button>
              {!isCollapsed && (
                <div style={{ borderTop: '1px solid var(--border)', padding: groups.length ? 10 : 18, display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {groups.length
                    ? groups.map(group => <FeedbackCard key={group.trace_id} group={group} expanded={expanded === `${tenant.id}:${group.trace_id}`} onToggle={() => setExpanded(expanded === `${tenant.id}:${group.trace_id}` ? null : `${tenant.id}:${group.trace_id}`)} />)
                    : <div style={{ color: 'var(--fg-subtle)', fontSize: 12.5, textAlign: 'center' }}>{filter === 'all' ? 'No feedback for this tenant yet.' : 'No feedback matches this filter.'}</div>}
                </div>
              )}
            </section>
          )
        })}
        {!loading && tenants.length === 0 && <div style={{ color: 'var(--fg-subtle)', padding: 40, textAlign: 'center', border: '1px dashed var(--border)', borderRadius: 14 }}>No tenants to show yet.</div>}
      </div>
    </div>
  )
}

function Kpi({ label, value, icon, color }) {
  return <div style={{ border: '1px solid var(--border)', borderRadius: 12, background: 'var(--surface)', padding: '13px 15px', display: 'flex', alignItems: 'center', gap: 12 }}><Icon name={icon} size={19} color={color} /><div><div className="mono" style={{ fontSize: 20, fontWeight: 700 }}>{value}</div><div style={{ fontSize: 11.5, color: 'var(--fg-muted)' }}>{label}</div></div></div>
}
const pill = color => ({ display: 'inline-flex', alignItems: 'center', gap: 5, padding: '4px 8px', borderRadius: 999, background: `color-mix(in srgb,${color} 10%,transparent)`, color, fontSize: 10.5, fontWeight: 700, whiteSpace: 'nowrap' })
const qaBlock = (_icon, background, color) => ({ borderRadius: 9, padding: '10px 12px', background, color })
const qaLabel = { display: 'flex', alignItems: 'center', gap: 6, fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.05em', marginBottom: 5 }
const qaText = { color: 'var(--fg)', fontSize: 12.5, lineHeight: 1.55, whiteSpace: 'pre-wrap' }
const refreshBtn = loading => ({ height: 38, padding: '0 14px', border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface)', color: 'var(--fg-muted)', fontSize: 13, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 6, cursor: loading ? 'wait' : 'pointer', opacity: loading ? .65 : 1 })
