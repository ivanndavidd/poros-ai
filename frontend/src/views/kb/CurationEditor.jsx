/* The curation a BUSINESS applies to ONE datasource: authorized tables, schema context,
 * semantic catalog. Plus the datasource's PII list, which is shown here because this is
 * where an admin now works — but saved per-datasource and labelled as shared, because PII
 * is a hard rule and one table must not be sensitive for 1P and open for 3P. */
import React, { useEffect, useState } from 'react'
import PropTypes from 'prop-types'
import { api } from '../../api.js'
import { Icon } from '../../tos.jsx'
import { toastType } from '../../propTypes.js'
import {
  BusinessConceptEditor, Chip, ConfirmDialog, Sub, btnPrimary, catLbl, catalogCard, dashedSm,
  descInput, miniBtn, sectionHead, sectionWrap, withConceptKey, conceptPayload,
  withRowKey, rowPayload, normalizeRoutingAliases, xBtn,
} from './ui.jsx'

const byText = (a, b) => String(a).localeCompare(String(b))
const asRefRows = map => Object.entries(map || {}).map(([dim, values]) => ({
  dim, vals: (values || []).join(', '),
}))
const refsObj = refs => Object.fromEntries(refs.filter(r => (r.dim || '').trim())
  .map(r => [r.dim.trim(), (r.vals || '').split(',').map(s => s.trim()).filter(Boolean)]))
const buildPayload = ({ allowed, desc, metrics, concepts, routingAliases, refs, examples }) => ({
  allowed_tables: allowed, descriptions: desc, metrics: rowPayload(metrics),
  business_concepts: conceptPayload(concepts),
  routing_aliases: normalizeRoutingAliases(routingAliases),
  reference_values: refsObj(refs), examples: rowPayload(examples),
})
const baselinePayload = knowledge => ({
  allowed_tables: knowledge.allowed_tables, descriptions: knowledge.descriptions,
  metrics: knowledge.metrics, business_concepts: knowledge.business_concepts,
  routing_aliases: knowledge.routing_aliases, reference_values: knowledge.reference_values,
  examples: knowledge.examples,
})

/* A description that came from the datasource scan, not from this business.
 *
 * Deliberately NOT the input's placeholder, which is how this first shipped: placeholder
 * text cannot be selected, is clipped at the input's edge (these are full sentences), and
 * reads as an EMPTY field -- while that text is already going to the model. So it is real
 * text that wraps, with a label saying where it came from and that filling the box above
 * overrides it. */
function Inherited({ text, business }) {
  if (!text) return null
  return (
    <div style={{ display: 'flex', gap: 7, marginTop: 6, padding: '7px 9px', borderRadius: 7,
      background: 'var(--surface-2, var(--surface))', border: '1px dashed var(--border)' }}>
      <Icon name="cloud_download" size={14} color="var(--fg-subtle)" style={{ flexShrink: 0, marginTop: 1 }} />
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 10.5, fontWeight: 600, color: 'var(--fg-subtle)', letterSpacing: 0.2, marginBottom: 2 }}>
          FROM DATASOURCE SCAN · already sent to the model
        </div>
        <div style={{ fontSize: 12, color: 'var(--fg-muted)', lineHeight: 1.5, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{text}</div>
        <div style={{ fontSize: 10.5, color: 'var(--fg-subtle)', marginTop: 3 }}>
          Only fill the box above if it means something different for {business}.
        </div>
      </div>
    </div>
  )
}

Inherited.propTypes = { text: PropTypes.string, business: PropTypes.string.isRequired }

/* Catalog entries the slice inherits and this business has not overridden. Named rather
 * than rendered as editors: the admin's job here is to OVERRIDE when a term means something
 * else for their business, not to re-enter what the datasource already says. A bare count
 * would not do -- "12m" with nothing visible reads as someone else's number. */
function InheritedList({ names, business, noun }) {
  if (!names.length) return null
  return (
    <div style={{ display: 'flex', gap: 6, marginTop: 7, fontSize: 11, color: 'var(--fg-subtle)', lineHeight: 1.55 }}>
      <Icon name="cloud_download" size={13} style={{ flexShrink: 0, marginTop: 2 }} />
      <div>
        <b style={{ fontWeight: 600 }}>{names.length} {noun} from the datasource scan</b> — already sent to the model:{' '}
        <span className="mono" style={{ color: 'var(--fg-muted)' }}>{names.slice(0, 6).join(', ')}</span>
        {names.length > 6 && ` +${names.length - 6} more`}. Add one here only to override what it means for {business}.
      </div>
    </div>
  )
}

InheritedList.propTypes = {
  names: PropTypes.arrayOf(PropTypes.string).isRequired,
  business: PropTypes.string.isRequired,
  noun: PropTypes.string.isRequired,
}

/* Inherited catalog entries, each with the definition VISIBLE and an override action.
 *
 * Naming them was not enough: overriding "GMV" meant clicking Add metric and retyping a
 * definition the panel never showed, so the only realistic move was to leave it alone or
 * guess. Timpa copies the entry into this business filled in, which is the only honest way
 * to offer an override -- an admin editing a definition should start from the one in force,
 * not from a blank row. Long lists collapse to four: Acme L2 inherits eleven metrics, and a
 * section that tall buries the editor underneath it. */
function InheritedRows({ items, business, noun, onOverride }) {
  const [all, setAll] = useState(false)
  if (!items.length) return null
  const shown = all ? items : items.slice(0, 4)
  return (
    <div style={{ marginBottom: 9, border: '1px dashed var(--border)', borderRadius: 8,
      padding: '8px 9px', background: 'var(--surface-2, var(--surface))' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 7 }}>
        <Icon name="cloud_download" size={13} color="var(--fg-subtle)" />
        <span style={{ fontSize: 10.5, fontWeight: 600, color: 'var(--fg-subtle)', letterSpacing: 0.2 }}>
          {items.length} {noun} FROM DATASOURCE SCAN · ALREADY SENT TO THE MODEL
        </span>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {shown.map(item => (
          <div key={item.key} style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
            <span className="mono" style={{ fontSize: 11.5, color: 'var(--fg-muted)', flex: '0 0 130px', wordBreak: 'break-word', lineHeight: 1.5 }}>{item.name}</span>
            <span className="mono" style={{ fontSize: 11, color: 'var(--fg-subtle)', flex: 1, minWidth: 0, wordBreak: 'break-word', lineHeight: 1.5 }}>{item.detail}</span>
            <button type="button" onClick={() => onOverride(item.value)} title={`Copy into ${business}, then edit`}
              style={{ ...miniBtn, padding: '3px 8px', flexShrink: 0 }}>
              <Icon name="edit" size={13} />Override
            </button>
          </div>
        ))}
      </div>
      {items.length > 4 && (
        <button type="button" onClick={() => setAll(a => !a)} style={{ ...miniBtn, padding: '3px 8px', marginTop: 7 }}>
          {all ? 'Show less' : `Show all (${items.length})`}
        </button>
      )}
      <div style={{ fontSize: 10.5, color: 'var(--fg-subtle)', marginTop: 7, lineHeight: 1.45 }}>
        <b>Override</b> copies the entry into {business} pre-filled — edit it freely; the {business} version wins.
        Anything you don&apos;t override is still sent to the model as-is.
      </div>
    </div>
  )
}

InheritedRows.propTypes = {
  items: PropTypes.arrayOf(PropTypes.shape({
    key: PropTypes.string.isRequired,
    name: PropTypes.string,
    detail: PropTypes.string,
    value: PropTypes.object,
  })).isRequired,
  business: PropTypes.string.isRequired,
  noun: PropTypes.string.isRequired,
  onOverride: PropTypes.func.isRequired,
}

/* Alias collisions / broken replica authority across the WHOLE business, computed
 * server-side over the same scoped views the model reads. Shown here because this editor
 * is where the fix happens: rename one meaning, or mark deliberate replicas with one
 * concept_id + a shared equivalence contract and exactly one primary. */
function SemanticFindings({ items }) {
  if (!items.length) return null
  const one = detail => {
    const label = detail.name || detail.concept_id
    if (!detail.datasource_name) return String(label)
    const table = detail.table ? ` · ${detail.table}` : ''
    return `${label} (${detail.datasource_name}${table})`
  }
  return (
    <div style={{ fontSize: 12, lineHeight: 1.6, padding: '11px 13px', borderRadius: 9,
      border: '1px solid var(--warn)', color: 'var(--fg-muted)', marginBottom: 13 }}>
      <b style={{ color: 'var(--warn)' }}>
        {items.length} semantic collision{items.length === 1 ? '' : 's'} across this business.
      </b>
      {' '}The same term is curated as more than one thing, so which meaning answers would
      depend on datasource order. Give each meaning its own name, or mark deliberate
      replicas with a shared <span className="mono">concept id</span> + equivalence
      contract and exactly one primary. Strict tenants refuse these questions until fixed;
      others get the collision logged in Audit.
      <div style={{ marginTop: 7, display: 'flex', flexDirection: 'column', gap: 4 }}>
        {items.map(f => (
          <div key={f.kind === 'alias_collision' ? `a:${f.alias}` : `c:${f.concept_id}:${f.reason}`}
            className="mono" style={{ fontSize: 11.5, wordBreak: 'break-word' }}>
            {f.kind === 'alias_collision'
              ? <>&ldquo;{f.alias}&rdquo; → {(f.detail || []).map(one).join('  ·  ')}</>
              : <>concept id {f.concept_id}: {f.reason} — {(f.detail || []).map(one).join('  ·  ')}</>}
          </div>
        ))}
      </div>
    </div>
  )
}

SemanticFindings.propTypes = { items: PropTypes.array.isRequired }

const toggled = (arr, v) => (arr.includes(v) ? arr.filter(x => x !== v) : [...arr, v])

function AutoFillButton({ busy, noTables, onClick }) {
  const off = busy || noTables
  return (
    <button onClick={onClick} disabled={off}
      title={noTables ? 'Pick authorized tables first' : undefined}
      style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8, width: '100%', padding: '10px 12px', borderRadius: 9, border: '1px solid var(--accent)', background: 'transparent', color: 'var(--accent)', fontSize: 12.5, fontWeight: 600, cursor: off ? 'default' : 'pointer', opacity: off ? 0.6 : 1 }}>
      <Icon name={busy ? 'hourglass_top' : 'auto_awesome'} size={17} />
      {busy ? 'AI is drafting… (can take a few seconds)' : 'Auto-fill with AI'}
    </button>
  )
}

AutoFillButton.propTypes = {
  busy: PropTypes.bool.isRequired,
  noTables: PropTypes.bool.isRequired,
  onClick: PropTypes.func.isRequired,
}

// The tables this slice reads (allowlist ∩ the business's pick), the ones the search shows,
// and how many carry a description (own or inherited) / only an inherited one.
function tableStats({ ceiling, allowed, query, scanned, desc, inhDesc }) {
  const ctxTables = allowed.length ? ceiling.filter(t => allowed.includes(t)) : []
  const q = query.trim().toLowerCase()
  const shownTables = q ? ctxTables.filter(t => t.toLowerCase().includes(q)) : ctxTables
  const described = key => desc[key] || inhDesc[key]
  const anyColumn = (t, test) => (scanned[t] || []).some(c => test(`${t}.${c}`))
  const describedTables = ctxTables.filter(t => described(t) || anyColumn(t, described)).length
  const inheritedTables = ctxTables.filter(
    t => !desc[t] && (inhDesc[t] || anyColumn(t, key => inhDesc[key]))).length
  return { ctxTables, shownTables, describedTables, inheritedTables }
}
// state updater: a blank description removes the key instead of storing ""
const withDescription = (key, v) => prev => {
  const next = { ...prev }
  if (v.trim()) next[key] = v
  else delete next[key]
  return next
}

export default function CurationEditor({
  tenantId, business, snapshot, adminKey, toast, onSaved,
}) {
  const ds = snapshot.datasource
  const knowledge = snapshot.knowledge
  // What this slice gets from the WAREHOUSE without curating anything: BigQuery's own
  // table/column descriptions and sampled dimension values, already filtered to this
  // business's tables by the server. Read-only — shown as placeholders so an admin can see
  // what the model reads and type over it, without the business silently adopting a copy
  // it then has to maintain.
  const inhDesc = snapshot.inherited?.descriptions || {}
  const inhRefs = snapshot.inherited?.reference_values || {}
  const inh = snapshot.inherited || {}
  const [allowed, setAllowed] = useState(knowledge.allowed_tables)
  const [desc, setDesc] = useState(knowledge.descriptions)
  const [metrics, setMetrics] = useState(knowledge.metrics.map(withRowKey))
  const [concepts, setConcepts] = useState(knowledge.business_concepts.map(withConceptKey))
  const [routingAliases, setRoutingAliases] = useState(knowledge.routing_aliases)
  const [refs, setRefs] = useState(asRefRows(knowledge.reference_values).map(withRowKey))
  const [examples, setExamples] = useState(knowledge.examples.map(withRowKey))
  const [openCols, setOpenCols] = useState({})
  const [scOpen, setScOpen] = useState(false)
  const [scQuery, setScQuery] = useState('')
  const [catOpen, setCatOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [enriching, setEnriching] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)

  // Re-seed when the tree selects a different (business, datasource) pair: the component is
  // reused, so without this the previous pair's half-edited state would show under the new
  // heading and the next Save would write it to the wrong scope.
  const apply = next => {
    setAllowed(next.allowed_tables)
    setDesc(next.descriptions)
    setMetrics(next.metrics.map(withRowKey))
    setConcepts(next.business_concepts.map(withConceptKey))
    setRoutingAliases(next.routing_aliases)
    setRefs(asRefRows(next.reference_values).map(withRowKey))
    setExamples(next.examples.map(withRowKey))
  }
  useEffect(() => { apply(knowledge); setScQuery(''); setOpenCols({}) },
    [business.id, ds.id])                                   // eslint-disable-line react-hooks/exhaustive-deps

  const aliasValues = () => normalizeRoutingAliases(routingAliases)
  const payload = () => buildPayload({ allowed, desc, metrics, concepts, routingAliases, refs, examples })
  const dirty = JSON.stringify(payload()) !== JSON.stringify(baselinePayload(knowledge))

  const toggle = (arr, set, v) => set(toggled(arr, v))
  const setD = (key, v) => setDesc(withDescription(key, v))

  const base = `/admin/tenants/${tenantId}/businesses/${business.id}/datasources/${ds.id}`
  const save = async () => {
    setSaving(true)
    const r = await api.patch(`${base}/knowledge`, adminKey, payload())
    setSaving(false)
    if (!r.ok) return toast.bad(r.error?.message || 'Couldn’t save')
    const dropped = r.data.dropped_tables || []
    if (dropped.length) {
      toast.warn(`${dropped.length} tables skipped (outside the datasource allowlist): `
        + dropped.slice(0, 3).join(', '))
    } else {
      toast.ok(`${business.name} knowledge saved`)
    }
    onSaved()
  }
  const autoEnrich = async () => {
    setEnriching(true)
    const started = await api.post(
      `/admin/jobs/tenants/${tenantId}/businesses/${business.id}/datasources/${ds.id}`
      + '/auto-enrich', adminKey, {})
    const r = started.ok ? await api.waitJob(started.data.status_url, adminKey) : started
    setEnriching(false)
    if (!r.ok) return toast.bad(r.error?.message || 'Auto-enrich failed')
    const added = r.data.added || {}
    if (r.data.knowledge) { apply(r.data.knowledge); setScOpen(true); setCatOpen(true) }
    toast.ok(`AI added: ${added.descriptions || 0} descriptions · ${added.metrics || 0} metrics `
      + `· ${added.reference_values || 0} dimensions · ${added.examples || 0} examples`)
    onSaved()
  }

  // The picker offers the DATASOURCE's allowlist, not every scanned table: a business can
  // only ever narrow, so showing more would offer choices the save has to drop again.
  const ceiling = ds.allowed_tables || []
  const scannedCount = Object.keys(ds.tables || {}).length
  const { ctxTables, shownTables, describedTables, inheritedTables } = tableStats({
    ceiling, allowed, query: scQuery, scanned: ds.tables || {}, desc, inhDesc,
  })
  // Everything the slice inherits and this business has NOT overridden — the entries an
  // admin would otherwise read as missing. Names matched case-blind, the same way the
  // server shadows an inherited definition with an override.
  const notOwned = (inherited, own, keyOf) => {
    const taken = new Set((own || []).map(x => (keyOf(x) || '').trim().toLowerCase()))
    return (inherited || []).filter(x => {
      const k = (keyOf(x) || '').trim()
      return k && !taken.has(k.toLowerCase())
    })
  }
  const sqlKey = e => (e.sql || '').replace(/\s+/g, ' ').trim()
  const inheritedDims = Object.entries(inhRefs)
    .filter(([dim]) => !refs.some(r => (r.dim || '').trim().toLowerCase() === dim.toLowerCase()))
    .map(([dim, values]) => ({
      key: `d:${dim}`, name: dim, detail: (values || []).join(', '),
      value: { dim, vals: (values || []).join(', ') },
    }))
  const inheritedMetrics = notOwned(inh.metrics, metrics, m => m.name).map(m => ({
    key: `m:${m.name}`, name: m.name, detail: m.definition || '', value: m,
  }))
  const inheritedConcepts = notOwned(inh.business_concepts, concepts, c => c.name).map(c => ({
    key: `c:${c.name}`, name: c.name,
    detail: [c.table, c.formula || c.metric].filter(Boolean).join(' · '), value: c,
  }))
  const inheritedExamples = notOwned(inh.examples, examples, sqlKey).map(e => ({
    key: `q:${sqlKey(e)}`, name: e.q || '(no question)', detail: e.sql || '', value: e,
  }))
  const inheritedAliases = notOwned(
    (inh.routing_aliases || []).map(a => ({ name: a })), aliasValues().map(a => ({ name: a })),
    a => a.name).map(a => a.name)
  // The header count is what the MODEL sees, not what this business typed: a panel reading
  // 0 next to a prompt carrying the whole catalog is the bug this fixes.
  const catCount = aliasValues().length + inheritedAliases.length + concepts.length
    + inheritedConcepts.length + metrics.length + inheritedMetrics.length + refs.length
    + inheritedDims.length + examples.length + inheritedExamples.length

  return (
    <div>
      <SemanticFindings items={snapshot.semantic_findings || []} />
      {!ceiling.length && (
        <div style={{ fontSize: 12, lineHeight: 1.6, padding: '11px 13px', borderRadius: 9,
          border: '1px solid var(--warn)', color: 'var(--fg-muted)', marginBottom: 13 }}>
          <b style={{ color: 'var(--warn)' }}>This datasource doesn&apos;t allow any tables yet.</b>
          {' '}The connection-level allowlist is empty, and that means <b>every</b> query is
          refused — not that everything is allowed. Fill the datasource allowlist first, then
          businesses can pick from it.
        </div>
      )}
      <Sub label={`Authorized tables — ${allowed.length}/${ceiling.length} used by ${business.name}`}>
        {ceiling.map(t => (
          <Chip key={t} on={allowed.includes(t)} onClick={() => toggle(allowed, setAllowed, t)}>{t}</Chip>
        ))}
      </Sub>
      {/* Without this line the count reads as "this datasource has N tables" and an admin
        * looking for a table that was scanned but never allowed has nowhere to go. */}
      {scannedCount > ceiling.length && (
        <div style={{ fontSize: 10.5, color: 'var(--fg-subtle)', marginTop: 6, lineHeight: 1.5 }}>
          The {ds.name} connection allows <b>{ceiling.length} of {scannedCount}</b> scanned
          tables. To offer more tables to businesses, check them first under
          {' '}<b>Authorized tables (connection)</b> at the bottom of this page.
        </div>
      )}

      <div style={sectionWrap}>
        <AutoFillButton busy={enriching} noTables={!allowed.length} onClick={() => setConfirmOpen(true)} />
        <div style={{ fontSize: 10.5, color: 'var(--fg-subtle)', marginTop: 6, lineHeight: 1.5 }}>
          The tenant&apos;s model reads the schema + sample data of the tables picked above only
          (PII values redacted), then drafts knowledge for <b>{business.name}</b>. The
          datasource catalog and other businesses are untouched.
        </div>
      </div>

      <div style={sectionWrap}>
        <button onClick={() => setScOpen(o => !o)} style={sectionHead}>
          <Icon name="schema" size={16} color="var(--accent)" />
          <span style={{ fontSize: 11.5, fontWeight: 600, color: 'var(--fg-muted)' }}>Schema context — descriptions the AI reads (NL→SQL)</span>
          <span style={{ flex: 1 }} />
          <span
            title={inheritedTables ? `${inheritedTables} of them come from the datasource scan — the model already reads them, no need to retype` : undefined}
            style={{ fontSize: 11, color: describedTables ? 'var(--accent)' : 'var(--fg-subtle)' }}
          >{describedTables}/{ctxTables.length} described{inheritedTables ? ` (${inheritedTables} from scan)` : ''}</span>
          <Icon name={scOpen ? 'expand_less' : 'expand_more'} size={19} color="var(--fg-subtle)" />
        </button>
        {scOpen && (
          <div style={{ marginTop: 11 }}>
            <div style={{ fontSize: 11, color: 'var(--fg-subtle)', marginBottom: 10, lineHeight: 1.5 }}>
              Optional, but this is what the model reads. Only the tables picked above
              ({ctxTables.length}) appear here and reach the model. Descriptions from the
              datasource scan show as gray text — those are already sent; fill a box only
              if {business.name} means something different.
            </div>
            <div style={{ position: 'relative', marginBottom: 11 }}>
              <Icon name="search" size={17} color="var(--fg-subtle)" style={{ position: 'absolute', left: 11, top: '50%', transform: 'translateY(-50%)', pointerEvents: 'none' }} />
              <input value={scQuery} onChange={e => setScQuery(e.target.value)} style={{ ...descInput, height: 38, padding: '0 34px' }} placeholder={`Search tables… (${ctxTables.length} tables)`} />
              {scQuery && <button onClick={() => setScQuery('')} style={{ ...xBtn, position: 'absolute', right: 9, top: '50%', transform: 'translateY(-50%)' }}><Icon name="close" size={16} /></button>}
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 9, maxHeight: 380, overflowY: 'auto', paddingRight: 2 }}>
              {shownTables.map(t => {
                const cols = ds.tables?.[t] || []
                return (
                  <div key={t} style={{ border: '1px solid var(--border)', borderRadius: 9, padding: '10px 11px', background: 'var(--surface)' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                      <Icon name="table_chart" size={15} color="var(--accent)" />
                      <span className="mono" style={{ fontWeight: 600, fontSize: 12.5 }}>{t}</span>
                      <span style={{ flex: 1 }} />
                      <button onClick={() => setOpenCols(o => ({ ...o, [t]: !o[t] }))} style={{ ...miniBtn, padding: '4px 9px' }}><Icon name={openCols[t] ? 'expand_less' : 'expand_more'} size={15} />{cols.length} cols</button>
                    </div>
                    <input value={desc[t] || ''} onChange={e => setD(t, e.target.value)} style={descInput} placeholder={inhDesc[t] ? `Override the description for ${business.name} (optional)` : `What does ${t} hold for ${business.name}?`} />
                    <Inherited text={inhDesc[t]} business={business.name} />
                    {openCols[t] && (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 8 }}>
                        {cols.map(c => (
                          <div key={c} style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
                            <span className="mono" title={c} style={{ fontSize: 11.5, color: 'var(--fg-muted)', width: 116, flexShrink: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', paddingTop: 7 }}>{c}</span>
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <input value={desc[`${t}.${c}`] || ''} onChange={e => setD(`${t}.${c}`, e.target.value)} style={{ ...descInput, height: 30, fontSize: 12 }} placeholder={inhDesc[`${t}.${c}`] ? `Override for ${business.name} (optional)` : 'column description (optional)'} />
                              <Inherited text={inhDesc[`${t}.${c}`]} business={business.name} />
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )
              })}
              {ctxTables.length === 0 && <div style={{ fontSize: 12, color: 'var(--fg-subtle)', padding: '14px 4px', textAlign: 'center' }}>Pick authorized tables above first.</div>}
              {ctxTables.length > 0 && shownTables.length === 0 && <div style={{ fontSize: 12, color: 'var(--fg-subtle)', padding: '14px 4px', textAlign: 'center' }}>No tables match “{scQuery}”.</div>}
            </div>
          </div>
        )}
      </div>

      <div style={sectionWrap}>
        <button onClick={() => setCatOpen(o => !o)} style={sectionHead}>
          <Icon name="menu_book" size={16} color="var(--accent)" />
          <span style={{ fontSize: 11.5, fontWeight: 600, color: 'var(--fg-muted)' }}>Semantic catalog — metrics · dimension values · SQL examples (NL→SQL)</span>
          <span style={{ flex: 1 }} />
          <span
            title="r routing · c concepts · m metrics · d dimensions · q examples — including entries inherited from the datasource scan"
            style={{ fontSize: 11, color: catCount ? 'var(--accent)' : 'var(--fg-subtle)' }}
          >{aliasValues().length + inheritedAliases.length}r · {concepts.length + inheritedConcepts.length}c · {metrics.length + inheritedMetrics.length}m · {refs.length + inheritedDims.length}d · {examples.length + inheritedExamples.length}q</span>
          <Icon name={catOpen ? 'expand_less' : 'expand_more'} size={19} color="var(--fg-subtle)" />
        </button>
        {catOpen && (
          <div style={{ marginTop: 11, display: 'flex', flexDirection: 'column', gap: 16 }}>
            <div>
              <div style={catLbl}>Datasource routing — business terms that always pick this datasource</div>
              <InheritedList names={inheritedAliases} business={business.name} noun="routing aliases" />
              <div style={{ fontSize: 10.5, color: 'var(--fg-subtle)', marginBottom: 8, lineHeight: 1.45 }}>Comma-separated. These aliases win over table/column name matching.</div>
              <input value={routingAliases.join(', ')}
                onChange={e => setRoutingAliases(e.target.value.split(',').map(v => v.trimStart()))}
                style={descInput} placeholder="3P, third party, IMS client" />
            </div>
            <div>
              <div style={catLbl}>Business concepts — business terms locked to a canonical table and metric</div>
              <InheritedRows items={inheritedConcepts} business={business.name} noun="CONCEPTS"
                onOverride={c => setConcepts([...concepts, withConceptKey({
                  name: '', table: '', metric: '', formula: '',
                  time_dimension: '', aggregation: 'SUM', description: '', ...c,
                  // last, not spread: a stored concept may carry synonyms as null, and the
                  // editor maps over this array on its first render.
                  synonyms: Array.isArray(c.synonyms) ? c.synonyms : [],
                })])} />
              <div style={{ fontSize: 10.5, color: 'var(--fg-subtle)', marginBottom: 8, lineHeight: 1.45 }}>
                These are <b>enforced by the server</b>, not just prompt hints: fill them in
                so the model cannot answer a metric from any other table. Use a column +
                aggregation, or a full formula like <span className="mono">SUM(COALESCE(new_gmv, gmv))</span>.
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 9 }}>
                {concepts.map((concept, i) => (
                  <BusinessConceptEditor key={concept._key} concept={concept}
                    tables={ds.tables || {}} tableNames={ctxTables}
                    onUpdate={patch => setConcepts(concepts.map((item, j) => j === i ? { ...item, ...patch } : item))}
                    onDelete={() => setConcepts(concepts.filter((_, j) => j !== i))} />
                ))}
                <button onClick={() => setConcepts([...concepts, withConceptKey({ name: '', synonyms: [], table: '', metric: '', formula: '', time_dimension: '', aggregation: 'SUM', description: '' })])} style={dashedSm}><Icon name="add" size={15} />Add business concept</button>
              </div>
            </div>
            <div>
              <div style={catLbl}>Metrics — standard KPI definitions (e.g. GMV → <span className="mono">SUM(gmv)</span>)</div>
              <InheritedRows items={inheritedMetrics} business={business.name} noun="METRICS"
                onOverride={m => setMetrics([...metrics, withRowKey({ name: m.name || '', definition: m.definition || '' })])} />
              <div style={{ display: 'flex', flexDirection: 'column', gap: 7 }}>
                {metrics.map((m, i) => (
                  <div key={m._key} style={catalogCard}>
                    <div style={{ display: 'flex', gap: 7 }}>
                      <input value={m.name || ''} onChange={e => setMetrics(metrics.map((x, j) => j === i ? { ...x, name: e.target.value } : x))} style={{ ...descInput, flex: '0 0 130px' }} placeholder="name (GMV)" />
                      <input value={m.definition || ''} onChange={e => setMetrics(metrics.map((x, j) => j === i ? { ...x, definition: e.target.value } : x))} style={{ ...descInput, flex: 1, fontFamily: "'IBM Plex Mono',monospace" }} placeholder="definition (SUM(gmv))" />
                      <button onClick={() => setMetrics(metrics.filter((_, j) => j !== i))} style={xBtn}><Icon name="close" size={15} /></button>
                    </div>
                  </div>
                ))}
                <button onClick={() => setMetrics([...metrics, withRowKey({ name: '', definition: '' })])} style={dashedSm}><Icon name="add" size={15} />Add metric</button>
              </div>
            </div>
            <div>
              <div style={catLbl}>Reference values — valid dimension values (comma-separated; the model filters with these)</div>
              {/* These drive catalog pruning and the entity confirmation, so an admin
                * seeing an empty list needs to know the scan already supplies some —
                * otherwise the sensible-looking move is to retype them by hand. */}
              <InheritedRows items={inheritedDims} business={business.name} noun="DIMENSIONS"
                onOverride={r => setRefs([...refs, withRowKey(r)])} />
              <div style={{ display: 'flex', flexDirection: 'column', gap: 7 }}>
                {refs.map((r, i) => (
                  <div key={r._key} style={catalogCard}>
                    <div style={{ display: 'flex', gap: 7 }}>
                      <input value={r.dim || ''} onChange={e => setRefs(refs.map((x, j) => j === i ? { ...x, dim: e.target.value } : x))} style={{ ...descInput, flex: '0 0 130px' }} placeholder="dimension (marketplace)" />
                      <input value={r.vals || ''} onChange={e => setRefs(refs.map((x, j) => j === i ? { ...x, vals: e.target.value } : x))} style={{ ...descInput, flex: 1 }} placeholder="Shopee, Tokopedia, TikTok" />
                      <button onClick={() => setRefs(refs.filter((_, j) => j !== i))} style={xBtn}><Icon name="close" size={15} /></button>
                    </div>
                  </div>
                ))}
                <button onClick={() => setRefs([...refs, withRowKey({ dim: '', vals: '' })])} style={dashedSm}><Icon name="add" size={15} />Add dimension</button>
              </div>
            </div>
            <div>
              <div style={catLbl}>Few-shot examples — question → SQL pairs the model imitates</div>
              <InheritedRows items={inheritedExamples} business={business.name} noun="EXAMPLES"
                onOverride={e => setExamples([...examples, withRowKey({ q: e.q || '', sql: e.sql || '' })])} />
              <div style={{ display: 'flex', flexDirection: 'column', gap: 9 }}>
                {examples.map((e, i) => (
                  <div key={e._key} style={catalogCard}>
                    <div style={{ display: 'flex', gap: 7, marginBottom: 6 }}>
                      <input value={e.q || ''} onChange={ev => setExamples(examples.map((x, j) => j === i ? { ...x, q: ev.target.value } : x))} style={{ ...descInput, flex: 1 }} placeholder="question" />
                      <button onClick={() => setExamples(examples.filter((_, j) => j !== i))} style={xBtn}><Icon name="close" size={15} /></button>
                    </div>
                    <textarea value={e.sql || ''} onChange={ev => setExamples(examples.map((x, j) => j === i ? { ...x, sql: ev.target.value } : x))} rows={2} style={{ width: '100%', border: '1px solid var(--border)', borderRadius: 8, background: 'var(--bg)', color: 'var(--fg)', fontSize: 12, fontFamily: "'IBM Plex Mono',monospace", padding: '7px 10px', outline: 'none', resize: 'vertical', boxSizing: 'border-box' }} placeholder="SELECT …" />
                  </div>
                ))}
                <button onClick={() => setExamples([...examples, withRowKey({ q: '', sql: '' })])} style={dashedSm}><Icon name="add" size={15} />Add example</button>
              </div>
            </div>
          </div>
        )}
      </div>

      {dirty && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, justifyContent: 'flex-end', marginTop: 14 }}>
          <span style={{ fontSize: 11.5, color: 'var(--warn)' }}>Unsaved changes</span>
          <button onClick={() => apply(knowledge)} style={{ ...miniBtn, cursor: 'pointer' }}>Discard</button>
          <button onClick={save} disabled={saving} style={{ ...btnPrimary, cursor: saving ? 'wait' : 'pointer', opacity: saving ? 0.7 : 1 }}>
            {saving ? 'Saving…' : `Save ${business.name} knowledge`}
          </button>
        </div>
      )}

      <ConfirmDialog open={confirmOpen} icon="auto_awesome"
        title={`Auto-fill ${business.name} knowledge?`}
        confirmLabel="Auto-fill" cancelLabel="Cancel"
        onCancel={() => setConfirmOpen(false)}
        onConfirm={() => { setConfirmOpen(false); autoEnrich() }}>
        The tenant&apos;s model reads the <b style={{ color: 'var(--fg)' }}>schema + sample data</b> of
        the {allowed.length} selected tables, then drafts descriptions, metrics, dimension
        values & SQL examples.
        <ul style={{ margin: '10px 0 0', paddingLeft: 18, display: 'flex', flexDirection: 'column', gap: 5 }}>
          <li><b style={{ color: 'var(--fg)' }}>PII values are redacted</b> — never sent to the AI.</li>
          <li>Only fills <b style={{ color: 'var(--fg)' }}>what&apos;s still empty</b> — your edits are never overwritten.</li>
          <li>Writes to business <b style={{ color: 'var(--fg)' }}>{business.name}</b> only.</li>
          <li>On BigQuery, sampling incurs a small <b style={{ color: 'var(--fg)' }}>scan cost</b>.</li>
        </ul>
      </ConfirmDialog>
    </div>
  )
}

CurationEditor.propTypes = {
  tenantId: PropTypes.string.isRequired,
  business: PropTypes.shape({ id: PropTypes.string, name: PropTypes.string }).isRequired,
  snapshot: PropTypes.shape({
    knowledge: PropTypes.object.isRequired,
    datasource: PropTypes.object.isRequired,
    inherited: PropTypes.shape({
      descriptions: PropTypes.object,
      reference_values: PropTypes.object,
      metrics: PropTypes.array,
      business_concepts: PropTypes.array,
      routing_aliases: PropTypes.array,
      examples: PropTypes.array,
    }),
    semantic_findings: PropTypes.array,
  }).isRequired,
  adminKey: PropTypes.string.isRequired,
  toast: toastType.isRequired,
  onSaved: PropTypes.func.isRequired,
}

/* PII lives on the DATASOURCE, not the business. Editing it here is a convenience; the
 * banner exists so nobody reads it as a per-business setting. */
export function ConnectionTablesEditor({ snapshot, businesses, adminKey, toast, onSaved }) {
  const ds = snapshot.datasource
  const scanned = Object.keys(ds.tables || {}).sort((a, b) => String(a).localeCompare(String(b)))
  const [allowed, setAllowed] = useState(ds.allowed_tables || [])
  const [saving, setSaving] = useState(false)
  const [confirm, setConfirm] = useState(null)
  useEffect(() => { setAllowed(ds.allowed_tables || []) }, [ds.id])   // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = JSON.stringify([...allowed].sort(byText)) !== JSON.stringify([...(ds.allowed_tables || [])].sort(byText))

  // Businesses that curate a table this save would REVOKE. Narrowing the connection is
  // silently effective -- the scoped view re-narrows at answer time -- so a business would
  // just stop seeing a table with nothing said. That is the same failure the rescan warning
  // exists for, so it gets the same treatment: named up front, before saving.
  const losers = (() => {
    const removed = (ds.allowed_tables || []).filter(t => !allowed.includes(t))
    if (!removed.length) return []
    return (businesses || []).map(b => ({
      name: b.name,
      tables: removed.filter(t => (b.scopes?.[ds.id]?.allowed_tables || []).includes(t)),
    })).filter(x => x.tables.length)
  })()

  const write = async () => {
    setSaving(true)
    // allowed_tables REPLACES only that field; descriptions, metrics and PII are untouched.
    const r = await api.patch(`/admin/datasources/${ds.id}/mdl`, adminKey, { allowed_tables: allowed })
    setSaving(false)
    setConfirm(null)
    if (!r.ok) return toast.bad(r.error?.message || 'Couldn’t save the connection allowlist')
    toast.ok(`${allowed.length} of ${scanned.length} tables allowed for connection ${ds.name}`)
    onSaved()
  }
  const save = () => (losers.length ? setConfirm(losers) : write())

  return (
    <div style={sectionWrap}>
      <div style={{ fontSize: 11, color: 'var(--fg-subtle)', lineHeight: 1.55, marginBottom: 4 }}>
        <Icon name="lock" size={13} color="var(--warn)" style={{ verticalAlign: '-2px', marginRight: 4 }} />
        This list belongs to <b style={{ color: 'var(--fg-muted)' }}>datasource {ds.name}</b> and
        is the <b style={{ color: 'var(--fg-muted)' }}>ceiling for every business</b>: a business
        can only pick from the tables checked here. Newly scanned tables start unchecked —
        allowing a table here doesn&apos;t make any business use it; that&apos;s still picked
        per business above.
      </div>
      <Sub label={`Authorized tables (connection) — ${allowed.length}/${scanned.length} allowed`}>
        {scanned.map(t => (
          <Chip key={t} on={allowed.includes(t)}
            onClick={() => setAllowed(allowed.includes(t) ? allowed.filter(x => x !== t) : [...allowed, t])}>
            {t}
          </Chip>
        ))}
      </Sub>
      {!scanned.length && (
        <div style={{ fontSize: 11.5, color: 'var(--fg-subtle)', marginTop: 8 }}>
          No scanned tables yet. Run a rescan in Tenant Management → Database.
        </div>
      )}
      {dirty && (
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 10 }}>
          <button onClick={() => setAllowed(ds.allowed_tables || [])} style={{ ...miniBtn, cursor: 'pointer' }}>Discard</button>
          <button onClick={save} disabled={saving} style={{ ...btnPrimary, cursor: saving ? 'wait' : 'pointer' }}>
            {saving ? 'Saving…' : 'Save connection allowlist'}
          </button>
        </div>
      )}
      <ConfirmDialog open={!!confirm} icon="warning" tone="warn"
        title="These tables are in use by businesses"
        confirmLabel="Yes, revoke" cancelLabel="Cancel"
        onCancel={() => setConfirm(null)} onConfirm={write}>
        Revoking at the connection level immediately removes the table from every business
        using it — the curation stays stored, but it no longer answers questions:
        <div style={{ marginTop: 8 }}>
          {(confirm || []).map(x => (
            <div key={x.name} style={{ fontSize: 12, marginTop: 4 }}>
              <b>{x.name}</b> — <span className="mono">{x.tables.join(', ')}</span>
            </div>
          ))}
        </div>
      </ConfirmDialog>
    </div>
  )
}

ConnectionTablesEditor.propTypes = {
  snapshot: PropTypes.object.isRequired,
  businesses: PropTypes.array,
  adminKey: PropTypes.string,
  toast: toastType.isRequired,
  onSaved: PropTypes.func.isRequired,
}


export function PiiEditor({ snapshot, adminKey, toast, onSaved }) {
  const ds = snapshot.datasource
  const allCols = [...new Set(Object.values(ds.tables || {}).flat())]
    .sort((a, b) => String(a).localeCompare(String(b)))
  const [pii, setPii] = useState(ds.pii_columns || [])
  const [saving, setSaving] = useState(false)
  useEffect(() => { setPii(ds.pii_columns || []) }, [ds.id])   // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = JSON.stringify([...pii].sort(byText)) !== JSON.stringify([...(ds.pii_columns || [])].sort(byText))
  const save = async () => {
    setSaving(true)
    const r = await api.patch(`/admin/datasources/${ds.id}/mdl`, adminKey, { pii_columns: pii })
    setSaving(false)
    if (!r.ok) return toast.bad(r.error?.message || 'Couldn’t save PII')
    toast.ok(`${pii.length} PII columns saved for ${ds.name}`)
    onSaved()
  }
  return (
    <div style={sectionWrap}>
      <div style={{ fontSize: 11, color: 'var(--fg-subtle)', lineHeight: 1.55, marginBottom: 4 }}>
        <Icon name="lock" size={13} color="var(--warn)" style={{ verticalAlign: '-2px', marginRight: 4 }} />
        This list belongs to <b style={{ color: 'var(--fg-muted)' }}>datasource {ds.name}</b> and
        applies to <b style={{ color: 'var(--fg-muted)' }}>every business</b>. Columns here are
        never sent to the model, whatever the role.
      </div>
      <Sub label={`PII columns (CLAC) — ${pii.length} marked`}>
        {allCols.map(c => (
          <Chip key={c} on={pii.includes(c)}
            onClick={() => setPii(pii.includes(c) ? pii.filter(x => x !== c) : [...pii, c])}>
            {pii.includes(c) ? '🔒 ' : ''}{c}
          </Chip>
        ))}
      </Sub>
      {dirty && (
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 10 }}>
          <button onClick={() => setPii(ds.pii_columns || [])} style={{ ...miniBtn, cursor: 'pointer' }}>Discard</button>
          <button onClick={save} disabled={saving} style={{ ...btnPrimary, cursor: saving ? 'wait' : 'pointer' }}>
            {saving ? 'Saving…' : 'Save datasource PII'}
          </button>
        </div>
      )}
    </div>
  )
}

PiiEditor.propTypes = {
  snapshot: PropTypes.shape({ datasource: PropTypes.object.isRequired }).isRequired,
  adminKey: PropTypes.string.isRequired,
  toast: toastType.isRequired,
  onSaved: PropTypes.func.isRequired,
}
