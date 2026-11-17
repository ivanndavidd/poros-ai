// Production is served by FastAPI from the same origin. VITE_API_BASE remains an
// optional build-time override for split deployments; local Vite keeps using :8000.
const BASE = import.meta.env.VITE_API_BASE
  || (import.meta.env.DEV ? 'http://localhost:8000' : '')

async function parse(r) {
  const text = await r.text()
  let body
  try { body = text ? JSON.parse(text) : {} } catch { body = { raw: text } }
  const ok = r.status >= 200 && r.status < 300
  let error = null
  if (!ok) {
    const d = body && body.detail
    if (d && typeof d === 'object') error = { code: d.code || 'ERROR', message: d.message || JSON.stringify(d) }
    else if (typeof d === 'string') error = { code: 'ERROR', message: d }
    else error = { code: 'ERROR', message: (body && body.message) || r.statusText || 'Request failed' }
  }
  return { ok, status: r.status, data: body, error }
}

// DEV-ONLY simulation of the Apps Gateway identity: locally there is no gateway to
// inject X-User-Id, so (in dev) we attach it ourselves from localStorage `dev_user`.
// `import.meta.env.DEV` is statically replaced with `false` by `vite build`, so this whole
// branch is DEAD-CODE-ELIMINATED from production bundles — the browser can NEVER ship
// X-User-Id to prod (only the real gateway sets it there). The backend also ignores the
// header unless APPS_GATEWAY_TRUST_HEADER=1, so a stray dev_user is harmless.
const DEV_USER_KEY = 'dev_user'
const devIdentity = () => {
  if (!import.meta.env.DEV) return {}
  const email = localStorage.getItem(DEV_USER_KEY)
  return email ? { 'X-User-Id': email } : {}
}
export const devAuth = {
  enabled: !!import.meta.env.DEV,                                    // false in any built bundle
  get: () => (import.meta.env.DEV ? localStorage.getItem(DEV_USER_KEY) || '' : ''),
  set: (email) => { if (import.meta.env.DEV) localStorage.setItem(DEV_USER_KEY, (email || '').trim()) },
  clear: () => localStorage.removeItem(DEV_USER_KEY),
}

// Browser sessions use an HttpOnly cookie (`credentials: include`), never localStorage.
// `k` is only the optional break-glass master key and remains in React memory.
const ah = (k, extra) => ({
  ...(k ? { 'X-Admin-Key': k } : {}),
  ...devIdentity(),
  ...(extra || {}),
})
const opts = (input = {}) => ({ credentials: 'include', ...input })
const delay = (ms, signal) => new Promise((resolve, reject) => {
  const timer = setTimeout(resolve, ms)
  if (signal) signal.addEventListener('abort', () => {
    clearTimeout(timer)
    reject(new DOMException('Aborted', 'AbortError'))
  }, { once: true })
})

const cancelledResult = () => ({ ok: false, status: 0, data: {}, error: { code: 'CANCELLED', message: 'Job cancelled' } })
// terminal admin-job states -> the shape waitJob resolves with (null = keep polling)
const jobOutcome = (data) => {
  if (data.status === 'succeeded') return { ok: true, status: 200, data: data.result, job: data }
  if (data.status === 'failed') return { ok: false, status: 400, data, error: data.error || { code: 'JOB_FAILED', message: 'Job failed' } }
  if (data.status === 'cancelled') return { ok: false, status: 409, data, error: { code: 'CANCELLED', message: 'Job cancelled' } }
  return null
}
// terminal consumer-job states -> the shape consumerJob resolves with (null = keep polling)
const consumerOutcome = (data, jobId, onEvent, cancel) => {
  if (data.status === 'succeeded') {
    onEvent?.({ type: 'done', result: data.result })
    return { ok: true, data: data.result, job_id: jobId }
  }
  if (data.status === 'failed') {
    onEvent?.({ type: 'error', code: data.error?.code, error: data.error?.message })
    return { ok: false, error: data.error, job_id: jobId }
  }
  if (data.status === 'cancelled') return cancel()
  return null
}

export const api = {
  base: BASE,
  get: (p, k) => fetch(BASE + p, opts({ headers: ah(k) })).then(parse).catch(netErr),
  post: (p, k, b) => fetch(BASE + p, opts({ method: 'POST', headers: ah(k, { 'Content-Type': 'application/json' }), body: JSON.stringify(b) })).then(parse).catch(netErr),
  patch: (p, k, b) => fetch(BASE + p, opts({ method: 'PATCH', headers: ah(k, { 'Content-Type': 'application/json' }), body: JSON.stringify(b) })).then(parse).catch(netErr),
  put: (p, k, b) => fetch(BASE + p, opts({ method: 'PUT', headers: ah(k, { 'Content-Type': 'application/json' }), body: JSON.stringify(b) })).then(parse).catch(netErr),
  del: (p, k) => fetch(BASE + p, opts({ method: 'DELETE', headers: ah(k) })).then(parse).catch(netErr),
  upload: (p, k, form) => fetch(BASE + p, opts({ method: 'POST', headers: ah(k), body: form })).then(parse).catch(netErr),
  waitJob: async (statusUrl, key, onUpdate, { signal, interval = 900 } = {}) => {
    let after = 0
    for (;;) {
      if (signal?.aborted) return cancelledResult()
      const separator = statusUrl.includes('?') ? '&' : '?'
      const current = await api.get(`${statusUrl}${separator}after=${after}`, key)
      if (!current.ok) return current
      for (const event of (current.data.events || [])) {
        after = Math.max(after, Number(event.seq || 0))
        onUpdate?.(event, current.data)
      }
      onUpdate?.({ type: 'snapshot', progress: current.data.progress, phase: current.data.phase }, current.data)
      const outcome = jobOutcome(current.data)
      if (outcome) return outcome
      try { await delay(interval, signal) } catch { return cancelledResult() }
    }
  },
  consumer: (p, key, b, jwt) => fetch(BASE + p, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-API-Key': key, ...(jwt ? { Authorization: 'Bearer ' + jwt } : {}) }, body: JSON.stringify(b) }).then(parse).catch(netErr),
  consumerGet: (p, key, jwt) => fetch(BASE + p, { headers: { 'X-API-Key': key, ...(jwt ? { Authorization: 'Bearer ' + jwt } : {}) } }).then(parse).catch(netErr),
  consumerDelete: (p, key, jwt) => fetch(BASE + p, { method: 'DELETE', headers: { 'X-API-Key': key, ...(jwt ? { Authorization: 'Bearer ' + jwt } : {}) } }).then(parse).catch(netErr),
  consumerJob: async (p, key, body, jwt, onEvent, { signal, interval = 650 } = {}) => {
    const started = await api.consumer(p, key, body, jwt)
    if (!started.ok) {
      onEvent?.({ type: 'error', code: started.error?.code, error: started.error?.message })
      return started
    }
    const jobId = started.data.job_id
    let after = 0
    const cancel = async () => {
      if (jobId) await api.consumerDelete(`/v1/chat/jobs/${jobId}`, key, jwt)
      onEvent?.({ type: 'cancelled' })
      return { ok: false, cancelled: true, job_id: jobId }
    }
    for (;;) {
      if (signal?.aborted) return cancel()
      const current = await api.consumerGet(`/v1/chat/jobs/${jobId}?after=${after}`, key, jwt)
      if (!current.ok) {
        onEvent?.({ type: 'error', code: current.error?.code, error: current.error?.message })
        return current
      }
      for (const event of (current.data.events || [])) {
        after = Math.max(after, Number(event.seq || 0))
        onEvent?.(event)
      }
      const outcome = consumerOutcome(current.data, jobId, onEvent, cancel)
      if (outcome) return outcome
      try { await delay(interval, signal) } catch { return cancel() }
    }
  },
  // SSE stream. Abort the supplied signal to stop the server-side pipeline.
  consumerStream: async (p, key, b, jwt, onEvent, { signal } = {}) => {
    let cancelledSent = false
    const cancelled = () => {
      if (!cancelledSent) {
        cancelledSent = true
        onEvent({ type: 'cancelled' })
      }
      return { cancelled: true }
    }
    let r
    try {
      r = await fetch(BASE + p, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': key, ...(jwt ? { Authorization: 'Bearer ' + jwt } : {}) },
        body: JSON.stringify(b),
        signal,
      })
    } catch (e) {
      if (e?.name === 'AbortError' || signal?.aborted) return cancelled()
      onEvent({ type: 'error', error: 'Cannot reach the API (' + (e?.message || 'network') + ')' })
      return { cancelled: false }
    }
    if (!r.ok || !r.body) {
      const j = await r.json().catch(() => ({})); const d = j && j.detail
      onEvent({ type: 'error', code: d && d.code, error: (d && (d.message || d)) || r.statusText || 'Request failed' }); return { cancelled: false }
    }
    const reader = r.body.getReader(), dec = new TextDecoder(); let buf = ''
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        let idx
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2)
          const line = chunk.split('\n').find(l => l.startsWith('data:'))
          if (line) { try { onEvent(JSON.parse(line.slice(5).trim())) } catch { /* ignore */ } }
        }
      }
    } catch (e) {
      if (e?.name === 'AbortError' || signal?.aborted) return cancelled()
      onEvent({ type: 'error', error: 'Stream disconnected (' + (e?.message || 'network') + ')' })
      return { cancelled: false }
    } finally {
      try { reader.releaseLock() } catch { /* already released */ }
    }
    if (signal?.aborted) return cancelled()
    return { cancelled: false }
  },
}

function netErr(e) {
  return { ok: false, status: 0, data: {}, error: { code: 'NETWORK', message: 'Cannot reach the API (' + (e?.message || 'network error') + '). Is the backend running on ' + BASE + '?' } }
}
