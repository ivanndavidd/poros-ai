/* Shared console UI kit.
 *
 * These lived inside ConfigModal.jsx when the config modal was their only user. The
 * Knowledge Base view now edits the same curation layer, so keeping one definition here
 * is what stops the two screens from drifting into two different-looking editors. */
import React, { useEffect } from 'react'
import PropTypes from 'prop-types'
import { Icon } from '../../tos.jsx'

export const scrim = { position: 'fixed', inset: 0, background: 'rgba(8,12,20,.55)', backdropFilter: 'blur(3px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 60, padding: 32 }
export const lbl = { display: 'block', fontSize: 12.5, fontWeight: 500, color: 'var(--fg-muted)', marginBottom: 7 }
export const fieldBox = { width: '100%', height: 40, padding: '0 13px', border: '1px solid var(--border)', borderRadius: 9, background: 'var(--bg)', color: 'var(--fg)', fontSize: 13.5, outline: 'none', fontFamily: "'IBM Plex Mono',monospace" }
export const descInput = { width: '100%', height: 34, padding: '0 11px', border: '1px solid var(--border)', borderRadius: 8, background: 'var(--bg)', color: 'var(--fg)', fontSize: 12.5, outline: 'none' }
export const catLbl = { fontSize: 11.5, fontWeight: 600, color: 'var(--fg-muted)', marginBottom: 8 }
export const btnGhost = { padding: '9px 16px', border: '1px solid var(--border)', borderRadius: 9, background: 'var(--surface)', color: 'var(--fg)', fontSize: 13, fontWeight: 600 }
export const btnPrimary = { padding: '9px 18px', border: 'none', borderRadius: 9, background: 'var(--accent)', color: '#fff', fontSize: 13, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 7 }
export const miniBtn = { display: 'flex', alignItems: 'center', gap: 5, padding: '6px 11px', border: '1px solid var(--border)', borderRadius: 8, background: 'var(--surface)', color: 'var(--fg-muted)', fontSize: 12, fontWeight: 600 }
export const dashed = { display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 7, padding: 11, border: '1px dashed var(--border)', borderRadius: 11, background: 'transparent', color: 'var(--accent)', fontSize: 13, fontWeight: 600 }
export const dashedSm = { display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11.5, fontWeight: 600, padding: '6px 11px', borderRadius: 7, border: '1px dashed var(--border)', background: 'transparent', color: 'var(--accent)' }
export const alert = { marginTop: 12, fontSize: 12.5, padding: '9px 12px', borderRadius: 9, background: 'var(--surface-2)' }
export const cardBox = { border: '1px solid var(--border)', borderRadius: 12, padding: '15px 17px', background: 'var(--bg)' }
export const iconBtn = { width: 32, height: 32, border: '1px solid var(--border)', borderRadius: 8, background: 'var(--surface)', color: 'var(--fg-subtle)', display: 'flex', alignItems: 'center', justifyContent: 'center' }
export const iconLink = { border: 'none', background: 'transparent', color: 'var(--fg-subtle)', display: 'flex' }
export const selBox = { display: 'flex', alignItems: 'center', gap: 8, height: 40, padding: '0 13px', border: '1px solid var(--border)', borderRadius: 9, background: 'var(--surface)', fontSize: 13.5, fontFamily: "'IBM Plex Mono',monospace" }
export const bareSel = { border: 'none', background: 'transparent', color: 'var(--fg)', fontSize: 13, outline: 'none', flex: 1, fontFamily: 'inherit' }
export const hintStyle = { fontSize: 11.5, color: 'var(--fg-subtle)', lineHeight: 1.5, marginTop: 4 }
export const chipStyle = { display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 11.5, padding: '5px 6px 5px 10px', borderRadius: 7, background: 'var(--surface)', border: '1px solid var(--border)', color: 'var(--fg-muted)' }
export const xBtn = { border: 'none', background: 'transparent', color: 'var(--fg-subtle)', display: 'flex', cursor: 'pointer' }
export const sectionHead = { display: 'flex', alignItems: 'center', gap: 8, width: '100%', textAlign: 'left', background: 'transparent', border: 'none', padding: 0, cursor: 'pointer' }
export const sectionWrap = { marginTop: 14, borderTop: '1px solid var(--border)', paddingTop: 13 }
// ONE card shape for every semantic-catalog entry (concepts, metrics, reference values,
// examples). The sections differ in how many fields an entry needs — a concept is a
// server-enforced contract, a metric is one line of hint — but the entries must not LOOK
// like different kinds of thing: same card, first line always [name][content][x].
export const catalogCard = { border: '1px solid var(--border)', borderRadius: 9, padding: 9, background: 'var(--surface)' }

export function Loading() { return <div style={{ display: 'grid', placeItems: 'center', padding: 40 }}><span className="spin" style={{ color: 'var(--accent)' }} /></div> }
export function Toggle({ on, onClick }) { return <button type="button" aria-pressed={on} onClick={onClick} style={{ width: 40, height: 23, padding: 0, border: 0, borderRadius: 20, background: on ? 'var(--accent)' : 'var(--border)', position: 'relative', cursor: 'pointer', transition: '.2s' }}><span style={{ position: 'absolute', top: 2.5, left: on ? 19.5 : 2.5, width: 18, height: 18, borderRadius: '50%', background: '#fff', transition: '.2s', boxShadow: '0 1px 3px rgba(0,0,0,.3)' }} /></button> }
export function Chip({ on, onClick, children }) { return <button type="button" aria-pressed={on} onClick={onClick} className="mono" style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 11.5, padding: '5px 10px', borderRadius: 7, cursor: 'pointer', background: on ? 'var(--accent-soft)' : 'var(--surface)', border: '1px solid ' + (on ? 'var(--accent)' : 'var(--border)'), color: on ? 'var(--accent)' : 'var(--fg-muted)', marginRight: 6, marginBottom: 6 }}>{children}</button> }
export function Sub({ label, children }) { return <div style={{ marginTop: 10 }}><div style={{ fontSize: 11.5, fontWeight: 600, color: 'var(--fg-muted)', marginBottom: 7 }}>{label}</div><div style={{ display: 'flex', flexWrap: 'wrap' }}>{children}</div></div> }

/* ---------- reusable styled confirmation dialog (replaces native confirm()) ---------- */
export function ConfirmDialog({ open, icon = 'help', tone = 'accent', title, children,
  confirmLabel = 'Continue', cancelLabel = 'Cancel', onConfirm, onCancel }) {
  useEffect(() => {
    if (!open) return
    const h = (e) => {
      if (e.key === 'Escape') onCancel?.()
      if (e.key === 'Enter') onConfirm?.()
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [open, onConfirm, onCancel])
  if (!open) return null
  const toneC = tone === 'danger' ? 'var(--danger,#e5484d)' : 'var(--accent)'
  const toneBg = tone === 'danger' ? 'color-mix(in srgb,var(--danger,#e5484d) 14%,transparent)' : 'var(--accent-soft)'
  // no onKeyDown on the scrim: the window-level listener above already handles Escape
  return (
    <div style={{ ...scrim, zIndex: 80, padding: 24 }}>
      <div role="alertdialog" aria-modal="true" aria-label={title} className="pop" style={{ width: 'min(440px,100%)', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 16, overflow: 'hidden', boxShadow: '0 30px 80px -20px rgba(0,0,0,.5)', color: 'var(--fg)' }}>
        <div style={{ padding: '22px 22px 18px', display: 'flex', gap: 14 }}>
          <div style={{ width: 42, height: 42, flexShrink: 0, borderRadius: 11, background: toneBg, color: toneC, display: 'flex', alignItems: 'center', justifyContent: 'center' }}><Icon name={icon} size={23} /></div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 700, fontSize: 15.5, marginBottom: 6 }}>{title}</div>
            <div style={{ fontSize: 12.5, color: 'var(--fg-muted)', lineHeight: 1.6 }}>{children}</div>
          </div>
        </div>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, padding: '13px 20px', borderTop: '1px solid var(--border)', background: 'var(--surface-2)' }}>
          <button onClick={onCancel} style={{ ...btnGhost, cursor: 'pointer' }}>{cancelLabel}</button>
          <button onClick={onConfirm} style={{ ...btnPrimary, cursor: 'pointer', background: toneC }}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  )
}

export function BusinessConceptEditor({ concept, tables, tableNames, onUpdate, onDelete }) {
  const columns = tables[concept.table] || []
  const updateSynonyms = value => onUpdate({
    synonyms: value.split(',').map(item => item.trim()).filter(Boolean),
  })
  return (
    <div style={catalogCard}>
      <div style={{ display: 'flex', gap: 7, marginBottom: 7 }}>
        <input value={concept.name || ''} onChange={e => onUpdate({ name: e.target.value })} style={{ ...descInput, flex: '0 0 130px' }} placeholder="concept (sales)" />
        <input value={(concept.synonyms || []).join(', ')} onChange={e => updateSynonyms(e.target.value)} style={{ ...descInput, flex: 1 }} placeholder="synonyms: penjualan, omzet" />
        <button onClick={onDelete} style={xBtn}><Icon name="close" size={15} /></button>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(170px,1.5fr) minmax(110px,1fr) minmax(110px,1fr) 90px', gap: 7, marginBottom: 7 }}>
        <select value={concept.table || ''} onChange={e => onUpdate({ table: e.target.value, metric: '', formula: '', time_dimension: '' })} style={descInput}>
          <option value="">Select a table…</option>
          {tableNames.map(table => <option key={table} value={table}>{table}</option>)}
        </select>
        <select value={concept.metric || ''} onChange={e => onUpdate({ metric: e.target.value, formula: '' })} style={descInput}>
          <option value="">Metric column…</option>
          {columns.map(column => <option key={column} value={column}>{column}</option>)}
        </select>
        <select value={concept.time_dimension || ''} onChange={e => onUpdate({ time_dimension: e.target.value })} style={descInput}>
          <option value="">Time column (optional)…</option>
          {columns.map(column => <option key={column} value={column}>{column}</option>)}
        </select>
        <select value={concept.aggregation || 'SUM'} onChange={e => onUpdate({ aggregation: e.target.value })} style={descInput}>
          {['SUM', 'AVG', 'COUNT', 'COUNT_DISTINCT', 'MIN', 'MAX'].map(value => <option key={value}>{value}</option>)}
        </select>
      </div>
      <input value={concept.formula || ''}
        onChange={e => onUpdate({ formula: e.target.value, metric: e.target.value.trim() ? '' : concept.metric })}
        style={{ ...descInput, marginBottom: 7, fontFamily: "'IBM Plex Mono',monospace" }}
        placeholder="Optional aggregate formula, e.g. SUM(COALESCE(new_gmv, gmv))" />
      <input value={concept.description || ''} onChange={e => onUpdate({ description: e.target.value })} style={{ ...descInput, marginBottom: 7 }} placeholder="What it means and when to use it" />
      {/* v2 identity (optional): an explicit id makes this concept nameable across
        * datasources; role + equivalence contract mark DELIBERATE physical replicas of
        * one meaning (exactly one primary). Never inferred from the display name. */}
      <div style={{ display: 'flex', gap: 7 }}>
        <input value={concept.concept_id || ''} onChange={e => onUpdate({ concept_id: e.target.value })}
          title="Optional stable id (lowercase). Two concepts with the same NAME are still two different meanings; the id is what marks them as the same one."
          style={{ ...descInput, flex: '0 0 170px', fontFamily: "'IBM Plex Mono',monospace" }} placeholder="concept id (optional)" />
        <select value={concept.source_binding?.role || ''}
          title="Replica role — only for the same concept_id living on several datasources"
          onChange={e => onUpdate({
            source_binding: e.target.value
              ? { role: e.target.value, equivalence_contract_id: concept.source_binding?.equivalence_contract_id || '' }
              : null,
          })} style={{ ...descInput, flex: '0 0 130px' }}>
          <option value="">not a replica</option>
          <option value="primary">primary source</option>
          <option value="fallback">fallback source</option>
        </select>
        <input value={concept.source_binding?.equivalence_contract_id || ''}
          disabled={!concept.source_binding?.role}
          onChange={e => onUpdate({
            source_binding: { role: concept.source_binding?.role || 'primary', equivalence_contract_id: e.target.value },
          })}
          title="Shared contract id every replica of this concept must name — exactly one of them is primary"
          style={{ ...descInput, flex: 1, fontFamily: "'IBM Plex Mono',monospace", opacity: concept.source_binding?.role ? 1 : 0.5 }}
          placeholder="equivalence contract id" />
      </div>
    </div>
  )
}

// Stable row identity for the business-concept editors. An array index as React key makes
// the wrong row survive a delete: remove the middle concept and the row below inherits its
// DOM state (a half-typed name stays on screen against the wrong concept). The content
// cannot serve as the key either — it changes on every keystroke, which would remount the
// row and steal focus mid-word. So each row carries a client-only `_key`, stripped again
// before the curation is compared or saved.
let conceptKeySeq = 0
export const withConceptKey = concept => ({ ...concept, _key: `bc${++conceptKeySeq}` })
export const conceptPayload = list => list.map(concept => {
  const copy = { ...concept }
  delete copy._key
  return copy
})

// Same contract for the other editable row lists (metrics / dimensions / examples):
// a client-only key at load or add time, stripped before compare/save.
let rowKeySeq = 0
export const withRowKey = row => ({ ...row, _key: `row${++rowKeySeq}` })
export const rowPayload = list => list.map(row => {
  const copy = { ...row }
  delete copy._key
  return copy
})

export function normalizeRoutingAliases(values) {
  const seen = new Set()
  const aliases = []
  for (const raw of values) {
    const value = raw.trim().slice(0, 120)
    const key = value.toLowerCase()
    if (!value || seen.has(key)) continue
    seen.add(key)
    aliases.push(value)
    if (aliases.length === 50) break
  }
  return aliases
}

Toggle.propTypes = { on: PropTypes.bool, onClick: PropTypes.func.isRequired }
Chip.propTypes = { on: PropTypes.bool, onClick: PropTypes.func, children: PropTypes.node }
Sub.propTypes = { label: PropTypes.node.isRequired, children: PropTypes.node }
ConfirmDialog.propTypes = {
  open: PropTypes.bool,
  icon: PropTypes.string,
  tone: PropTypes.string,
  title: PropTypes.string.isRequired,
  children: PropTypes.node,
  confirmLabel: PropTypes.string,
  cancelLabel: PropTypes.string,
  onConfirm: PropTypes.func.isRequired,
  onCancel: PropTypes.func.isRequired,
}
BusinessConceptEditor.propTypes = {
  concept: PropTypes.object.isRequired,
  tables: PropTypes.object.isRequired,
  tableNames: PropTypes.arrayOf(PropTypes.string).isRequired,
  onUpdate: PropTypes.func.isRequired,
  onDelete: PropTypes.func.isRequired,
}
