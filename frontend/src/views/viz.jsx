// Auto-visualization for governed chat results: bar/pie/line charts + Indonesia geo
// choropleth. Ported from the TenantOS design (Tenant Platform.dc.html + indonesia-map.js).
import React, { useMemo, useId, useState, useRef, useEffect } from 'react'
import * as echarts from 'echarts'
import { Icon } from '../tos.jsx'
import '../indonesia-map.js'   // side-effect: registers the <indonesia-choropleth> custom element

const PIE_COLORS = ['#1e3a8a', '#2563eb', '#3b82f6', '#60a5fa', '#93c5fd', '#bfdbfe']

// compact large numbers for chart labels: 368084791 -> "368M"
function compact(n) {
  const x = Number(n); if (!isFinite(x)) return String(n)
  const a = Math.abs(x)
  if (a >= 1e9) return (x / 1e9).toFixed(a >= 1e10 ? 0 : 1) + 'B'
  if (a >= 1e6) return (x / 1e6).toFixed(a >= 1e7 ? 0 : 1) + 'M'
  if (a >= 1e3) return (x / 1e3).toFixed(a >= 1e4 ? 0 : 1) + 'K'
  return String(Math.round(x))
}

// ---- province detection (mirrors indonesia-map.js norm()) ----
const PROV_ALIAS = { PROBANTEN: 'BANTEN', 'BANGKA BELITUNG': 'KEPULAUAN BANGKA BELITUNG', 'NUSATENGGARA BARAT': 'NUSA TENGGARA BARAT' }
function normProv(s) {
  const v = (s || '').toString().toUpperCase().replace(/[.]/g, ' ')
    .replace(/^(PROVINSI|PROV\.?|DAERAH ISTIMEWA|DI|DKI)\s+/, '').replace(/\s+/g, ' ').trim()
  return PROV_ALIAS[v] || v
}
const PROVINCES = new Set(['ACEH', 'SUMATERA UTARA', 'SUMATERA BARAT', 'RIAU', 'KEPULAUAN RIAU', 'JAMBI',
  'SUMATERA SELATAN', 'KEPULAUAN BANGKA BELITUNG', 'BENGKULU', 'LAMPUNG', 'JAKARTA', 'JAWA BARAT',
  'JAWA TENGAH', 'YOGYAKARTA', 'JAWA TIMUR', 'BANTEN', 'BALI', 'NUSA TENGGARA BARAT', 'NUSA TENGGARA TIMUR',
  'KALIMANTAN BARAT', 'KALIMANTAN TENGAH', 'KALIMANTAN SELATAN', 'KALIMANTAN TIMUR', 'KALIMANTAN UTARA',
  'SULAWESI UTARA', 'SULAWESI TENGAH', 'SULAWESI SELATAN', 'SULAWESI TENGGARA', 'GORONTALO', 'SULAWESI BARAT',
  'MALUKU', 'MALUKU UTARA', 'PAPUA', 'PAPUA BARAT'])

// the user may ask for several chart types in one message ("line pie dan bar") — return them all
function intentCharts(q) {
  const t = (q || '').toLowerCase(); const out = []
  if (/\b(bar|batang|column)\b/.test(t)) out.push('bar')
  if (/(pie|lingkaran|donut|donat|\bpai\b|komposisi|proporsi|persentase|porsi|share)/.test(t)) out.push('pie')
  if (/(line|garis|tren|trend|harian|over time|pergerakan|seiring)/.test(t)) out.push('line')
  return out
}
const wantsGeo = (q) => /(geo|heat\s?map|heatmap|peta|wilayah|provinsi|map|sebaran|distribusi)/.test((q || '').toLowerCase())

// Derive a single best-fit visualization from a governed SQL result.
// Returns { kind:'geo', data, title, max } | { kind:'chart', type, unit, data } | null.
export function deriveViz(question, columns, rows) {
  if (!columns || !rows || !rows.length) return null
  const idxs = columns.map((_, i) => i)
  const isNum = (i) => rows.every(r => r[i] != null && r[i] !== '' && !isNaN(Number(r[i])))
  const numIdxs = idxs.filter(isNum)
  if (!numIdxs.length) return null
  // prefer a sales-like measure column when several numerics exist, else the first numeric
  const measureRe = /sales|gmv|revenue|amount|penjualan|omzet|value|total/i
  const numIdx = numIdxs.find(i => measureRe.test(columns[i])) ?? numIdxs[0]
  let labelIdx = idxs.find(i => i !== numIdx && !isNum(i))
  if (labelIdx == null) labelIdx = idxs.find(i => i !== numIdx)
  if (labelIdx == null) return null

  const labels = rows.map(r => String(r[labelIdx]))
  const values = rows.map(r => Number(r[numIdx]))
  const colName = (columns[labelIdx] || '').toLowerCase()
  const reqTypes = intentCharts(question)   // chart types the user explicitly asked for

  // geo — province-like label column, the default when NO explicit chart type was requested
  const provHits = labels.filter(l => PROVINCES.has(normProv(l))).length
  const isProvince = /\b(prov|provinsi|region|wilayah|propinsi)\b/.test(colName) || provHits >= Math.max(2, Math.ceil(rows.length * 0.4))
  if (isProvince && reqTypes.length === 0 && (wantsGeo(question) || provHits >= 2 || /prov|region|wilayah/.test(colName))) {
    const data = {}; rows.forEach(r => { data[String(r[labelIdx])] = Number(r[numIdx]) })
    return { kind: 'geo', data, title: `${columns[numIdx]} per ${columns[labelIdx]}`, max: Math.max(...values, 1) }
  }

  // chart — render every requested type (or one best-fit) from the SAME single result set
  const isDate = /\b(date|day|month|year|tanggal|bulan|hari|periode|tahun|time|waktu)\b/.test(colName) ||
    labels.every(l => /^\d{4}[-/]\d{2}/.test(l))
  const types = reqTypes.length ? reqTypes : [isDate ? 'line' : 'bar']
  const data = labels.map((l, i) => ({ label: l, value: values[i] }))
  return { kind: 'chart', types, unit: columns[numIdx], data }
}

// ---- chart primitives (match the design) ----
const full = (v) => { const x = Number(v); return (Math.abs(x) >= 1000 ? Math.round(x) : Math.round(x * 100) / 100).toLocaleString('en-US') }
function Tip({ style, label, value }) {
  return (
    <div style={{ position: 'absolute', transform: 'translate(-50%,-100%)', background: 'var(--surface-2)', border: '1px solid var(--border)', borderRadius: 8, padding: '6px 9px', fontSize: 11.5, whiteSpace: 'nowrap', pointerEvents: 'none', boxShadow: '0 6px 18px -8px rgba(0,0,0,.3)', zIndex: 6, ...style }}>
      <div style={{ fontWeight: 600 }}>{label}</div>
      <div className="mono" style={{ color: 'var(--accent)', marginTop: 1 }}>{full(value)}</div>
    </div>
  )
}
function BarChart({ bars, mx }) {
  const [hi, setHi] = useState(null)
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', gap: 14, height: 162, paddingBottom: 6 }}>
      {bars.map((b, i) => (
        <div key={i} onMouseEnter={() => setHi(i)} onMouseLeave={() => setHi(null)} style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'flex-end', height: '100%', gap: 8, cursor: 'default' }}>
          <span className="mono" style={{ fontSize: 11.5, fontWeight: 600, color: hi === i ? 'var(--accent)' : 'var(--fg-muted)', whiteSpace: 'nowrap' }}>{hi === i ? full(b.value) : compact(b.value)}</span>
          <div style={{ width: '100%', height: Math.round(b.value / mx * 100) + '%', background: hi === i ? 'var(--accent)' : 'linear-gradient(to top,var(--accent),color-mix(in srgb,var(--accent) 55%,transparent))', borderRadius: '6px 6px 3px 3px', minHeight: 6, transition: 'background .12s' }} />
          <span title={b.label} style={{ fontSize: 11.5, color: 'var(--fg-subtle)', maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{b.label}</span>
        </div>
      ))}
    </div>
  )
}
function PieChart({ bars }) {
  const total = bars.reduce((a, b) => a + b.value, 0) || 1
  let acc = 0; const stops = []
  const slices = bars.map((b, i) => {
    const pct = b.value / total * 100, c = PIE_COLORS[i % PIE_COLORS.length]
    const start = acc, end = acc + pct; acc = end
    stops.push(`${c} ${start.toFixed(2)}% ${end.toFixed(2)}%`)
    return { label: b.label, value: b.value, pct: Math.round(pct), color: c }
  })
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 26, padding: '4px 0 8px', flexWrap: 'wrap' }}>
      <div style={{ width: 148, height: 148, borderRadius: '50%', flexShrink: 0, background: `conic-gradient(${stops.join(',')})`, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ width: 84, height: 84, borderRadius: '50%', background: 'var(--surface)', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center' }}>
          <div style={{ fontSize: 11, color: 'var(--fg-subtle)' }}>Total</div>
          <div className="mono" style={{ fontSize: 15, fontWeight: 700 }}>{compact(total)}</div>
        </div>
      </div>
      <div style={{ flex: 1, minWidth: 160, display: 'flex', flexDirection: 'column', gap: 10 }}>
        {slices.map((s, i) => (
          <div key={i} title={full(s.value)} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span style={{ width: 11, height: 11, borderRadius: 3, background: s.color, flexShrink: 0 }} />
            <span style={{ flex: 1, fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.label}</span>
            <span className="mono" style={{ fontSize: 12, color: 'var(--fg-muted)' }}>{compact(s.value)}</span>
            <span className="mono" style={{ fontSize: 13, fontWeight: 700, width: 40, textAlign: 'right' }}>{s.pct}%</span>
          </div>
        ))}
      </div>
    </div>
  )
}
function LineChart({ bars, mx }) {
  const gid = useId()
  const [hi, setHi] = useState(null)
  const n = bars.length, W = 320, H = 120, pad = 6, PXH = 150
  const xs = bars.map((_, i) => pad + (W - 2 * pad) * (n > 1 ? i / (n - 1) : 0))
  const ys = bars.map(b => H - pad - (H - 2 * pad) * (b.value / mx))
  const pts = xs.map((x, i) => `${x.toFixed(1)},${ys[i].toFixed(1)}`)
  const linePath = 'M' + pts.join(' L')
  const lineArea = `M${xs[0].toFixed(1)},${H - pad} L${pts.join(' L')} L${xs[n - 1].toFixed(1)},${H - pad} Z`
  const labels = [bars[0].label, bars[Math.floor(n / 2)].label, bars[n - 1].label]
  const onMove = (e) => {
    const rect = e.currentTarget.getBoundingClientRect()
    const px = (e.clientX - rect.left) / rect.width * W
    let best = 0, bd = Infinity
    xs.forEach((x, i) => { const d = Math.abs(x - px); if (d < bd) { bd = d; best = i } })
    setHi(best)
  }
  return (
    <div style={{ position: 'relative' }}>
      <svg viewBox={`0 0 ${W} ${H}`} onMouseMove={onMove} onMouseLeave={() => setHi(null)} style={{ width: '100%', height: PXH, display: 'block', overflow: 'visible' }}>
        <defs><linearGradient id={gid} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="var(--accent)" stopOpacity="0.28" /><stop offset="100%" stopColor="var(--accent)" stopOpacity="0" /></linearGradient></defs>
        <path d={lineArea} fill={`url(#${gid})`} />
        <path d={linePath} fill="none" stroke="var(--accent)" strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" />
        {hi != null && <line x1={xs[hi]} y1={pad} x2={xs[hi]} y2={H - pad} stroke="var(--fg-subtle)" strokeWidth="0.8" strokeDasharray="3 3" />}
        {xs.map((x, i) => <circle key={i} cx={x} cy={ys[i]} r={hi === i ? 4.2 : 2.6} fill={hi === i ? 'var(--accent)' : 'var(--surface)'} stroke="var(--accent)" strokeWidth="1.8" />)}
      </svg>
      {hi != null && <Tip style={{ left: `${(xs[hi] / W * 100).toFixed(2)}%`, top: `${(ys[hi] / H * PXH - 6).toFixed(1)}px` }} label={bars[hi].label} value={bars[hi].value} />}
      <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 8, padding: '0 2px' }}>
        {labels.map((l, i) => <span key={i} style={{ fontSize: 11.5, color: 'var(--fg-subtle)' }}>{l}</span>)}
      </div>
    </div>
  )
}
const TYPE_ICON = { bar: 'bar_chart', pie: 'pie_chart', line: 'show_chart' }
// shape the series per chart type: lines tolerate many points (downsample); bar/pie cap to top-N
function prepBars(type, data) {
  if (type === 'line') {
    if (data.length > 240) { const step = Math.ceil(data.length / 180); return data.filter((_, i) => i % step === 0 || i === data.length - 1) }
    return data
  }
  if (data.length > 16) return [...data].sort((a, b) => b.value - a.value).slice(0, 16)
  return data
}
function Chart({ type, unit, data }) {
  const bars = prepBars(type, data)
  const mx = Math.max(...bars.map(b => b.value), 1)
  const capped = bars.length < data.length
  return (
    <div style={{ marginTop: 12, border: '1px solid var(--border)', borderRadius: 11, background: 'var(--surface)', padding: '16px 18px 12px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 11.5, fontWeight: 600, color: 'var(--fg-muted)', marginBottom: 16 }}>
        <Icon name={TYPE_ICON[type] || 'insights'} size={16} color="var(--accent)" />{unit}
        {capped && <span style={{ color: 'var(--fg-subtle)', fontWeight: 500 }}>· top {bars.length}</span>}
      </div>
      {type === 'pie' ? <PieChart bars={bars} /> : type === 'line' ? <LineChart bars={bars} mx={mx} /> : <BarChart bars={bars} mx={mx} />}
    </div>
  )
}
function GeoMap({ data, title, max }) {
  // <indonesia-choropleth> is a vanilla custom element; React just sets its attributes.
  return (
    <div style={{ marginTop: 12 }}>
      {React.createElement('indonesia-choropleth', {
        data: JSON.stringify(data), title, 'max-label': String(max),
        style: { display: 'block', width: '100%' },
      })}
    </div>
  )
}

export default function AutoViz({ question, columns, rows }) {
  const viz = useMemo(() => deriveViz(question, columns, rows), [question, columns, rows])
  if (!viz) return null
  if (viz.kind === 'geo') return <GeoMap data={viz.data} title={viz.title} max={viz.max} />
  return <>{viz.types.map((t, i) => <Chart key={t + i} type={t} unit={viz.unit} data={viz.data} />)}</>
}

// ============ LLM-driven visuals: ```chart specs (ECharts) + ```artifact (HTML) ============
const ECH_PALETTE = ['#2563eb', '#16aeea', '#24a36b', '#f4a63a', '#ef4f4f', '#6d5bd0', '#0d47c4', '#1aa6b7']

function echOption(spec, c) {
  const type = (spec.type || 'bar').toLowerCase()
  const labels = spec.labels || []
  const series = spec.series || []
  const axisLabel = { color: c.muted, fontSize: 11 }
  if (type === 'pie' || type === 'doughnut') {
    const s0 = series[0] || {}
    return {
      color: ECH_PALETTE, tooltip: { trigger: 'item' },
      legend: { bottom: 0, textStyle: { color: c.muted, fontSize: 11 } },
      series: [{ type: 'pie', radius: type === 'doughnut' ? ['45%', '70%'] : '68%', center: ['50%', '46%'],
        data: labels.map((l, i) => ({ name: l, value: Number((s0.data || [])[i]) })),
        label: { color: c.fg, fontSize: 11 } }],
    }
  }
  const et = type === 'scatter' ? 'scatter' : (type === 'line' || type === 'area') ? 'line' : 'bar'
  return {
    color: ECH_PALETTE,
    tooltip: { trigger: 'axis', axisPointer: { type: et === 'bar' ? 'shadow' : 'line' } },
    legend: series.length > 1 ? { top: 0, textStyle: { color: c.muted, fontSize: 11 } } : undefined,
    grid: { left: 8, right: 14, top: series.length > 1 ? 28 : 12, bottom: 8, containLabel: true },
    xAxis: { type: 'category', data: labels, axisLabel, axisLine: { lineStyle: { color: c.border } }, axisTick: { show: false } },
    yAxis: { type: 'value', axisLabel, splitLine: { lineStyle: { color: c.border } } },
    series: series.map(s => ({
      name: s.name, type: et, data: (s.data || []).map(Number), smooth: et === 'line',
      areaStyle: type === 'area' ? { opacity: 0.18 } : undefined,
      itemStyle: { borderRadius: et === 'bar' ? [4, 4, 0, 0] : 0 },
      symbolSize: et === 'scatter' ? 9 : 5, showSymbol: et === 'line',
    })),
  }
}

export function EChart({ spec }) {
  const ref = useRef(null)
  const key = JSON.stringify(spec)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const cs = getComputedStyle(el)
    const c = { muted: cs.getPropertyValue('--fg-muted').trim() || '#5a6475', border: cs.getPropertyValue('--border').trim() || '#e4e8ee', fg: cs.getPropertyValue('--fg').trim() || '#0f1729' }
    const chart = echarts.init(el)
    chart.setOption(echOption(spec, c))
    const ro = new ResizeObserver(() => chart.resize())
    ro.observe(el)
    return () => { ro.disconnect(); chart.dispose() }
  }, [key])
  return (
    <div style={{ marginTop: 12, border: '1px solid var(--border)', borderRadius: 11, background: 'var(--surface)', padding: '14px 16px 10px' }}>
      {spec.title && <div style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 12, fontWeight: 600, color: 'var(--fg-muted)', marginBottom: 8 }}><Icon name="insights" size={15} color="var(--accent)" />{spec.title}</div>}
      <div ref={ref} style={{ width: '100%', height: 300 }} />
    </div>
  )
}

export function Artifact({ html }) {
  const [full, setFull] = useState(false)
  const safeHtml = useMemo(() => {
    const policy = "default-src 'none'; connect-src 'none'; img-src data: blob:; media-src data: blob:; font-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; object-src 'none'; frame-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'"
    const meta = `<meta http-equiv="Content-Security-Policy" content="${policy}"><meta name="referrer" content="no-referrer">`
    const source = String(html || '')
    if (/<head[\s>]/i.test(source)) return source.replace(/<head([^>]*)>/i, `<head$1>${meta}`)
    return `<!doctype html><html><head>${meta}</head><body>${source}</body></html>`
  }, [html])
  // sandbox tokens beyond allow-scripts exist so the artifact's OWN toolbar works: Chrome
  // blocks every download started inside a sandboxed frame unless allow-downloads is set
  // (an Export button building a Blob + <a download> silently did nothing), window.print()
  // needs allow-modals, and requestFullscreen() needs the fullscreen permission policy.
  // allow-same-origin stays OFF — the frame keeps its opaque origin, so this does not widen
  // the artifact's reach into the console (CSP above still blocks network + external code).
  const frame = (h) => <iframe srcDoc={safeHtml} sandbox="allow-scripts allow-downloads allow-modals" allow="fullscreen" referrerPolicy="no-referrer" title="artifact" style={{ width: '100%', height: h, border: 'none', display: 'block', background: '#fff' }} />
  return (
    <div style={{ marginTop: 12, border: '1px solid var(--border)', borderRadius: 11, overflow: 'hidden', background: 'var(--surface)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '8px 13px', borderBottom: '1px solid var(--border)', background: 'var(--surface-2)', fontSize: 11.5, fontWeight: 600, color: 'var(--fg-muted)' }}>
        <Icon name="dashboard" size={15} color="var(--accent)" />Interactive artifact<span style={{ flex: 1 }} />
        <span style={{ fontSize: 10.5, color: 'var(--fg-subtle)' }}>sandboxed</span>
        <button onClick={() => setFull(true)} title="Fullscreen" style={{ border: 'none', background: 'transparent', cursor: 'pointer', color: 'var(--fg-subtle)', display: 'flex', padding: 2 }}><Icon name="open_in_full" size={16} /></button>
      </div>
      {frame(420)}
      {full && (
        <div onClick={e => { if (e.target === e.currentTarget) setFull(false) }}
          onKeyDown={e => { if (e.key === 'Escape') setFull(false) }}
          style={{ position: 'fixed', inset: 0, zIndex: 70, background: 'rgba(8,12,20,.55)', backdropFilter: 'blur(3px)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 28 }}>
          <div style={{ width: 'min(1100px,96vw)', height: '90vh', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 14, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '10px 14px', borderBottom: '1px solid var(--border)', fontSize: 12.5, fontWeight: 600, color: 'var(--fg-muted)' }}>
              <Icon name="dashboard" size={16} color="var(--accent)" />Interactive artifact<span style={{ flex: 1 }} />
              <button onClick={() => setFull(false)} style={{ border: '1px solid var(--border)', background: 'var(--surface)', borderRadius: 8, padding: '5px 9px', cursor: 'pointer', color: 'var(--fg-muted)', display: 'flex' }}><Icon name="close" size={17} /></button>
            </div>
            <div style={{ flex: 1 }}>{frame('100%')}</div>
          </div>
        </div>
      )}
    </div>
  )
}

// Defense-in-depth: some models (e.g. deepseek-reasoner) can't do structured function-calling
// and instead emit a tool call as PLAIN TEXT (`<｜…｜tool_calls｜>` / `<invoke name=…>` markup).
// That must never reach the user. Drop the whole contiguous scaffolding block (incl. the SQL
// nested inside it), then tidy any stray pipe-tokens / function-call XML left behind.
function stripToolCallMarkup(s) {
  let out = String(s || '')
  const SIG = /tool_calls|invoke\s+name=|parameter\s+name=|<｜|<\|/i
  if (!SIG.test(out)) return out
  const lines = out.split('\n')
  let first = -1, last = -1
  lines.forEach((ln, i) => { if (SIG.test(ln)) { if (first < 0) first = i; last = i } })
  if (first >= 0) lines.splice(first, last - first + 1)
  out = lines.join('\n')
  out = out.replace(/<\/?[｜|][^>\n]*[｜|]>/g, '')
           .replace(/<\/?(?:invoke|parameter|tool_call|tool_calls|function_calls)\b[^>]*>/gi, '')
  return out
}

// pull ```chart {json} and ```artifact <html> fenced blocks out of the answer text
export function extractBlocks(text) {
  const charts = [], artifacts = []
  let clean = String(text || '')
  clean = clean.replace(/```chart\s*([\s\S]*?)```/g, (_m, body) => {
    try { charts.push(JSON.parse(body.trim())) } catch { /* ignore malformed */ }
    return ''
  })
  clean = clean.replace(/```(?:artifact|html)\s*([\s\S]*?)```/g, (_m, body) => {
    const h = body.trim(); if (h) artifacts.push(h); return ''
  })
  clean = stripToolCallMarkup(clean)   // scrub any leaked plain-text tool-call markup
  return { clean: clean.trim(), charts, artifacts }
}

// Render LLM-emitted charts/artifacts; if none, fall back to auto-derive from the SQL result.
export function AnswerVisuals({ blocks, question, columns, rows }) {
  const { charts, artifacts } = blocks
  if (!charts.length && !artifacts.length) return <AutoViz question={question} columns={columns} rows={rows} />
  return (
    <>
      {charts.map((spec, i) => (spec.type || '').toLowerCase() === 'geo'
        ? <GeoMap key={'c' + i} title={spec.title || 'Map'} data={Object.fromEntries((spec.labels || []).map((l, j) => [l, Number((spec.series?.[0]?.data || [])[j])]))} max={Math.max(1, ...((spec.series?.[0]?.data) || []).map(Number))} />
        : <EChart key={'c' + i} spec={spec} />)}
      {artifacts.map((h, i) => <Artifact key={'a' + i} html={h} />)}
    </>
  )
}
