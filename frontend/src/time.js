// Timestamps arrive from the backend as UTC ("2026-08-13T03:47:18Z"). Five views used to
// print them raw — `value.replace('T', ' ').slice(0, 19)` — so a Jakarta admin saw 03:47 on
// an entry they created at 10:47 and had no way to know the difference was a timezone.
//
// Rows written before store.py started stamping the Z are UTC too; they just do not say so.
// `asDate` appends the marker when it is missing, which is why old entries display correctly
// rather than shifting by the viewer's offset — without it, JS reads an offset-less ISO
// string as LOCAL time and those rows would stay 7 hours out forever.

const HAS_ZONE = /(?:Z|[+-]\d{2}:?\d{2})$/i
const HAS_TIME = /T\d{2}:\d{2}/

/** Parse a backend timestamp into a Date, treating a missing offset as UTC.
 *
 * Only a value that carries a TIME gets the Z appended. A date-only string
 * ("2026-09-13", how API-key expiry is stored) is already defined as UTC by the language
 * spec, and gluing a Z onto it produces "2026-09-13Z" — which V8 happens to accept and
 * other engines are free to reject as Invalid Date. Tested, not assumed.
 */
export function asDate(value) {
  if (!value) return null
  const raw = String(value).trim()
  const iso = HAS_ZONE.test(raw) || !HAS_TIME.test(raw) ? raw : raw + 'Z'
  const d = new Date(iso)
  return isNaN(d.getTime()) ? null : d
}

/** "13 Aug 2026, 10.47.18" in the viewer's own timezone. Falls back to the raw string. */
export function fmtTime(value) {
  const d = asDate(value)
  if (!d) return value ? String(value).replace('T', ' ').slice(0, 19) : ''
  return d.toLocaleString('en-US', {
    day: '2-digit', month: 'short', year: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

/** Same, without seconds — for lists where the exact second is noise. */
export function fmtShort(value) {
  const d = asDate(value)
  if (!d) return value ? String(value).replace('T', ' ').slice(0, 16) : '—'
  return d.toLocaleString('en-US', {
    day: '2-digit', month: 'short', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

/** Date only, e.g. for API-key expiry. */
export function fmtDay(value) {
  const d = asDate(value)
  if (!d) return value ? String(value) : '—'
  return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' })
}
