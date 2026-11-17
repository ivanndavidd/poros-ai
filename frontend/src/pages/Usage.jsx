import React, { useState, useEffect, useCallback } from 'react'
import { api } from '../api.js'
import { Card, CardH, Button, Stat, Badge, BarChart, LineChart, Empty, Loading } from '../components/ui.jsx'

const money = v => '$' + (v || 0).toFixed(4)
const num = v => (v || 0).toLocaleString()

export default function Usage({ adminKey, tenantId }) {
  const [u, setU] = useState(null)
  const load = useCallback(async () => {
    if (!tenantId) return
    const r = await api.get(`/admin/tenants/${tenantId}/usage`, adminKey)
    if (r.ok) setU(r.data)
  }, [adminKey, tenantId])
  useEffect(() => { setU(null); load() }, [tenantId])

  if (u === null) return <Loading />
  const budget = u.budget_usd
  const budgetSpent = u.budget_spent_usd ?? u.cost
  const pct = budget ? Math.min(100, (budgetSpent / budget) * 100) : null
  const statusTone = { OK: 'ok', BLOCKED: 'warn', CLAC_DENIED: 'bad', MODEL_NOT_FOUND: 'bad', ERROR: 'bad' }

  return (
    <>
      <div className="spread" style={{ marginBottom: 14 }}>
        <p className="page-intro" style={{ margin: 0 }}>Per-tenant requests, tokens, cost-per-model and budget vs spend.</p>
        <Button variant="ghost sm" onClick={load}>↻ Refresh</Button>
      </div>

      <div className="grid cols-4">
        <Stat k="Requests" v={num(u.requests)} icon="📨" />
        <Stat k="Rows returned" v={num(u.rows_returned)} icon="🧾" />
        <Stat k="Total tokens" v={num(u.total_tokens)} d={`${num(u.prompt_tokens)} in · ${num(u.completion_tokens)} out`} icon="🔢" />
        <Stat k="Cost" v={money(u.cost)} d={budget ? `${money(budgetSpent)} this month of ${money(budget)}` : 'no budget set'} icon="💲" />
      </div>

      {pct !== null && (
        <Card style={{ marginTop: 14 }}>
          <CardH title="Budget vs spend" sub={`${pct.toFixed(1)}% used`} />
          <div className="bar-track" style={{ height: 14 }}>
            <div className="bar-fill" style={{ width: pct + '%', background: pct > 90 ? 'var(--bad)' : pct > 70 ? 'var(--warn)' : 'linear-gradient(90deg,var(--brand),#16b894)' }} />
          </div>
          <div className="spread" style={{ marginTop: 6, fontSize: 12 }}><span className="muted">{money(budgetSpent)} spent this month</span><span className="muted">{money(budget)} budget</span></div>
        </Card>)}

      <div className="grid cols-2" style={{ marginTop: 14 }}>
        <Card>
          <CardH title="Requests over time" sub="last 14 days" />
          <LineChart data={(u.by_day || []).map(d => ({ label: d.day.slice(5), value: d.requests }))} />
        </Card>
        <Card>
          <CardH title="Cost per model" />
          {(u.by_model || []).filter(m => m.cost > 0 || m.model !== '(none)').length === 0
            ? <Empty icon="🤖" title="No model cost yet">Configure models and run chats to see cost.</Empty>
            : <BarChart data={(u.by_model || []).map(m => ({ label: m.model, value: m.cost }))} fmt={money} color="var(--accent)" />}
        </Card>
      </div>

      <Card style={{ marginTop: 14 }}>
        <CardH title="By status" />
        <div className="pill-row">{(u.by_status || []).map(s => <Badge key={s.status} tone={statusTone[s.status] || 'gray'} dot>{s.status}: {s.n}</Badge>)}</div>
      </Card>

      <Card>
        <CardH title="Recent activity" />
        {(u.recent || []).length === 0 ? <Empty icon="📭" title="No activity yet">Run a query from the Chatbot page.</Empty> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Time</th><th>Role</th><th>Action</th><th>Rows</th><th>Model</th><th>Tokens</th><th>Cost</th><th>Status</th></tr></thead>
            <tbody>{u.recent.map((r, i) => (
              <tr key={i}>
                <td className="muted">{r.ts}</td><td>{r.role}</td><td>{r.action}</td><td>{r.rows}</td>
                <td>{r.model || <span className="muted">—</span>}</td>
                <td>{num((r.prompt_tokens || 0) + (r.completion_tokens || 0))}</td>
                <td>{r.cost ? money(r.cost) : <span className="muted">—</span>}</td>
                <td><Badge tone={statusTone[r.status] || 'gray'}>{r.status}</Badge></td>
              </tr>))}</tbody>
          </table></div>)}
      </Card>
    </>
  )
}
