import React, { useState, useEffect, useCallback } from 'react'
import { api } from '../api.js'
import { Icon, nfmt, money } from '../tos.jsx'

const PALETTE = ['#2563eb', '#3b82f6', '#60a5fa', '#93c5fd', '#6366f1', '#0ea5e9', '#94a3b8', '#bfdbfe']

export default function MonitoringView({ adminKey }) {
  const [m, setM] = useState(null)
  const [range, setRange] = useState('all')
  const [userMetric, setUserMetric] = useState('tokens')
  const [loading, setLoading] = useState(false)
  const load = useCallback(async () => {
    setLoading(true)
    const r = await api.get(`/admin/monitoring?range=${encodeURIComponent(range)}`, adminKey)
    if (r.ok) setM(r.data)
    setLoading(false)
  }, [adminKey, range])
  useEffect(() => { load() }, [load])

  if (!m) return <div style={{ display: 'grid', placeItems: 'center', flex: 1 }}><span className="spin" style={{ color: 'var(--accent)' }} /></div>

  const kpis = [
    { label: 'Total tenants', value: String(m.total_tenants), icon: 'groups', sub: 'all tenants' },
    { label: 'Active tenants', value: String(m.active_tenants), icon: 'check_circle', sub: m.total_tenants ? Math.round(m.active_tenants / m.total_tenants * 100) + '% active' : '—' },
    { label: 'Total tokens', value: nfmt(m.total_tokens), icon: 'bolt', sub: 'in + out' },
    { label: 'Total requests', value: nfmt(m.total_requests), icon: 'sync', sub: 'all actions' },
    { label: 'Est. cost', value: money(m.total_cost), icon: 'payments', sub: 'metered' },
  ]
  const models = (m.by_model || []).filter(x => x.tokens > 0).slice(0, 6)
  const modelTotal = models.reduce((a, x) => a + x.tokens, 0) || 1
  let acc = 0
  const stops = models.map((x, i) => { const from = acc / modelTotal * 100; acc += x.tokens; const to = acc / modelTotal * 100; return `${PALETTE[i % PALETTE.length]} ${from}% ${to}%` }).join(',')
  const donut = stops ? `conic-gradient(${stops})` : 'conic-gradient(var(--surface-2) 0 100%)'

  const days = m.by_day || []
  const maxDayTok = Math.max(...days.map(d => d.tokens), 1)
  const tenants = m.per_tenant || []
  const maxTenTok = Math.max(...tenants.map(t => t.tokens), 1)
  const users = m.per_user || []
  const metric = {
    tokens: { label: 'Tokens', value: u => Number(u.tokens || 0), format: nfmt, unit: () => 'tokens' },
    requests: { label: 'Requests', value: u => Number(u.requests || 0), format: nfmt, unit: value => value === 1 ? 'request' : 'requests' },
    cost: { label: 'Cost', value: u => Number(u.cost || 0), format: money, unit: () => '' },
  }[userMetric]
  const rankedUsers = [...users].sort((a, b) => metric.value(b) - metric.value(a))
  const maxUserValue = Math.max(...rankedUsers.map(metric.value), 1)
  const metricText = value => [metric.format(value), metric.unit(value)].filter(Boolean).join(' ')

  return (
    <div style={{ flex: 1, overflowY: 'auto', opacity: loading ? .72 : 1, transition: 'opacity .15s' }} className="fade">
      <header style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 20, padding: '26px 34px 20px' }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, letterSpacing: '-.02em' }}>Monitoring Dashboard</h1>
          <p style={{ color: 'var(--fg-muted)', marginTop: 4, fontSize: 13.5 }}>Usage across models, tokens, tenants, and consumer users.</p>
        </div>
        <div style={{ display: 'flex', gap: 3, padding: 3, border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface)' }}>
          {[['all', 'All'], ['30d', '30d'], ['7d', '7d']].map(([id, lb]) => (
            <button key={id} onClick={() => setRange(id)} style={{ padding: '6px 14px', border: 'none', borderRadius: 8, fontSize: 12.5, fontWeight: range === id ? 600 : 500, background: range === id ? 'var(--surface-2)' : 'transparent', color: range === id ? 'var(--fg)' : 'var(--fg-subtle)' }}>{lb}</button>
          ))}
        </div>
      </header>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(180px,1fr))', gap: 14, padding: '6px 34px 0' }}>
        {kpis.map(k => (
          <div key={k.label} style={card}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span style={{ fontSize: 12.5, color: 'var(--fg-muted)' }}>{k.label}</span>
              <Icon name={k.icon} size={19} color="var(--accent)" />
            </div>
            <div className="mono" style={{ fontSize: 27, fontWeight: 700, letterSpacing: '-.02em', marginTop: 10 }}>{k.value}</div>
            <div style={{ fontSize: 12, color: 'var(--fg-subtle)', marginTop: 6 }}>{k.sub}</div>
          </div>
        ))}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.25fr', gap: 16, padding: '16px 34px 0' }}>
        <div style={card}>
          <div style={{ fontWeight: 600, fontSize: 15, marginBottom: 4 }}>Model usage by type</div>
          <div style={{ fontSize: 12.5, color: 'var(--fg-muted)', marginBottom: 18 }}>Token distribution per model</div>
          {models.length === 0 ? <Empty icon="donut_large" text="No model usage yet." /> : (
            <div style={{ display: 'flex', alignItems: 'center', gap: 26 }}>
              <div style={{ width: 140, height: 140, borderRadius: '50%', flexShrink: 0, background: donut, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <div style={{ width: 88, height: 88, borderRadius: '50%', background: 'var(--surface)', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center' }}>
                  <div className="mono" style={{ fontSize: 20, fontWeight: 700 }}>{nfmt(m.total_tokens)}</div>
                  <div style={{ fontSize: 10.5, color: 'var(--fg-subtle)' }}>tokens</div>
                </div>
              </div>
              <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 11 }}>
                {models.map((x, i) => (
                  <div key={x.model} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                    <span style={{ width: 10, height: 10, borderRadius: 3, background: PALETTE[i % PALETTE.length], flexShrink: 0 }} />
                    <span className="mono" style={{ flex: 1, fontSize: 12.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{x.model}</span>
                    <span style={{ fontSize: 13, color: 'var(--fg-muted)' }}>{nfmt(x.tokens)}</span>
                    <span style={{ fontSize: 13, fontWeight: 600, width: 42, textAlign: 'right' }}>{Math.round(x.tokens / modelTotal * 100)}%</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        <div style={card}>
          <div style={{ fontWeight: 600, fontSize: 15, marginBottom: 4 }}>Token usage trend</div>
          <div style={{ fontSize: 12.5, color: 'var(--fg-muted)', marginBottom: 18 }}>
            {range === '7d' ? 'Last 7 days' : range === '30d' ? 'Last 30 days' : 'Last 14 days of activity'}
          </div>
          {days.length === 0 ? <Empty icon="show_chart" text="No activity yet." /> : (
            <div style={{ display: 'flex', alignItems: 'flex-end', gap: 6, height: 148 }}>
              {days.map((b, i) => (
                <div key={i} title={`${b.day}: ${nfmt(b.tokens)} tokens`} style={{ flex: 1, height: Math.max(6, b.tokens / maxDayTok * 100) + '%', background: 'linear-gradient(to top,var(--accent),color-mix(in srgb,var(--accent) 45%,transparent))', borderRadius: '5px 5px 2px 2px', minHeight: 6 }} />
              ))}
            </div>
          )}
        </div>
      </div>

      <div style={{ ...card, margin: '16px 34px 0' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 18 }}>
          <div><div style={{ fontWeight: 600, fontSize: 15 }}>Token usage per tenant</div><div style={{ fontSize: 12.5, color: 'var(--fg-muted)', marginTop: 3 }}>Ranked by token usage</div></div>
          <span className="mono" style={{ fontSize: 12, color: 'var(--fg-subtle)' }}>{tenants.length} tenants</span>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 15 }}>
          {tenants.map((u, i) => (
            <div key={u.id}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 7 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
                  <span style={{ fontWeight: 600, fontSize: 13.5 }}>{u.name}</span>
                  <span className="mono" style={{ fontSize: 11, color: 'var(--fg-subtle)', padding: '2px 7px', borderRadius: 6, background: 'var(--surface-2)' }}>{u.model}</span>
                </div>
                <span className="mono" style={{ fontSize: 13, fontWeight: 600 }}>{nfmt(u.tokens)}</span>
              </div>
              <div style={{ height: 9, borderRadius: 6, background: 'var(--surface-2)', overflow: 'hidden' }}>
                <div style={{ height: '100%', width: Math.max(2, u.tokens / maxTenTok * 100) + '%', background: PALETTE[i % PALETTE.length], borderRadius: 6 }} />
              </div>
            </div>
          ))}
          {tenants.length === 0 && <Empty icon="leaderboard" text="No tenant data yet." />}
        </div>
      </div>

      <div style={{ ...card, margin: '16px 34px 40px' }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16, marginBottom: 18, flexWrap: 'wrap' }}>
          <div>
            <div style={{ fontWeight: 600, fontSize: 15 }}>Usage per user</div>
            <div style={{ fontSize: 12.5, color: 'var(--fg-muted)', marginTop: 3 }}>
              Consumer users across all tenants, ranked for the selected period
            </div>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span className="mono" style={{ fontSize: 12, color: 'var(--fg-subtle)' }}>{users.length} users</span>
            <div style={{ display: 'flex', gap: 3, padding: 3, border: '1px solid var(--border)', borderRadius: 9, background: 'var(--surface-2)' }}>
              {['tokens', 'requests', 'cost'].map(id => (
                <button key={id} onClick={() => setUserMetric(id)} style={{
                  padding: '5px 10px', border: 'none', borderRadius: 7, fontSize: 11.5,
                  fontWeight: userMetric === id ? 600 : 500,
                  background: userMetric === id ? 'var(--surface)' : 'transparent',
                  color: userMetric === id ? 'var(--fg)' : 'var(--fg-muted)',
                  boxShadow: userMetric === id ? '0 1px 2px rgba(15,23,42,.08)' : 'none',
                }}>{id === 'tokens' ? 'Tokens' : id === 'requests' ? 'Requests' : 'Cost'}</button>
              ))}
            </div>
          </div>
        </div>
        {rankedUsers.length === 0 ? <Empty icon="person_search" text="No consumer user activity in this period." /> : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14, maxHeight: 520, overflowY: 'auto', paddingRight: 4 }}>
            {rankedUsers.map((u, i) => {
              const value = metric.value(u)
              return (
                <div key={`${u.tenant_id}:${u.user_id}`}>
                  <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 16, marginBottom: 7 }}>
                    <div style={{ minWidth: 0 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0, flexWrap: 'wrap' }}>
                        <span title={u.user_id} style={{ fontWeight: 600, fontSize: 13.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 420 }}>{u.user_id}</span>
                        <span style={{ fontSize: 10.5, color: 'var(--accent)', padding: '2px 7px', borderRadius: 999, background: 'color-mix(in srgb,var(--accent) 9%,transparent)' }}>{u.tenant_name}</span>
                      </div>
                      <div style={{ color: 'var(--fg-subtle)', fontSize: 11.5, marginTop: 3 }}>
                        {u.roles} · {nfmt(u.requests)} requests · {nfmt(u.tokens)} tokens · {money(u.cost)}
                      </div>
                    </div>
                    <span className="mono" style={{ fontSize: 13, fontWeight: 650, whiteSpace: 'nowrap' }}>{metricText(value)}</span>
                  </div>
                  <div style={{ height: 10, borderRadius: 6, background: 'var(--surface-2)', overflow: 'hidden' }}>
                    <div title={`${u.user_id}: ${metricText(value)}`} style={{
                      height: '100%', width: Math.max(value > 0 ? 2 : 0, value / maxUserValue * 100) + '%',
                      background: `linear-gradient(90deg,${PALETTE[i % PALETTE.length]},color-mix(in srgb,${PALETTE[i % PALETTE.length]} 65%,white))`,
                      borderRadius: 6,
                    }} />
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

const card = { background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 14, padding: 20 }
function Empty({ icon, text }) { return <div style={{ textAlign: 'center', color: 'var(--fg-subtle)', padding: '24px 0' }}><Icon name={icon} size={28} /><div style={{ marginTop: 6, fontSize: 13 }}>{text}</div></div> }
