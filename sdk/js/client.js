/**
 * JavaScript/TypeScript-friendly SDK for the Poros (AI Distribution
 * Administrator) consumer API (/v1/*). Works in the browser and Node 18+ (global fetch).
 * A consumer app only needs an API key.
 *
 *   import { AdminClient } from "./client.js"
 *   const c = new AdminClient("http://localhost:8000", "sk_...")
 *   const res = await c.chat("total sales per region?", { userId: "u1" })
 *   console.log(res.answer, res.citations)
 */
/** `businesses` is filled on the BUSINESS_* refusals: the slices this caller may pick
 *  from. Show them to the user and resend with `business` — retrying without one is
 *  never the fix, it does not widen what the role may read.
 *  `concepts` is filled on the SEMANTIC_* refusals (strict tenants): the curated
 *  meanings ({id, name, descr}) one KPI phrase could refer to — ask again naming one. */
export class ApiError extends Error {
  constructor(code, message, status, businesses, concepts) {
    super(`${code}: ${message}`)
    this.code = code; this.message = message; this.status = status
    this.businesses = businesses || []
    this.concepts = concepts || []
  }
}

export class AdminClient {
  constructor(baseUrl, apiKey) {
    this.base = baseUrl.replace(/\/$/, '')
    this.apiKey = apiKey
  }

  async _post(path, body, jwt) {
    const headers = { 'Content-Type': 'application/json', 'X-API-Key': this.apiKey }
    if (jwt) headers['Authorization'] = 'Bearer ' + jwt   // end-user identity + role (verified server-side)
    const r = await fetch(this.base + path, { method: 'POST', headers, body: JSON.stringify(body) })
    const data = await r.json().catch(() => ({}))
    if (!r.ok) {
      const d = data?.detail
      if (d && typeof d === 'object') {
        throw new ApiError(d.code || 'ERROR', d.message || 'Request failed', r.status, d.businesses, d.concepts)
      }
      throw new ApiError('ERROR', typeof d === 'string' ? d : 'Request failed', r.status)
    }
    return data
  }

  async _jobRequest(method, path, jwt) {
    const headers = { 'X-API-Key': this.apiKey }
    if (jwt) headers.Authorization = 'Bearer ' + jwt
    const r = await fetch(this.base + path, { method, headers })
    const data = await r.json().catch(() => ({}))
    if (!r.ok) {
      const d = data?.detail
      throw new ApiError(d?.code || 'ERROR', d?.message || 'Request failed', r.status, d?.businesses, d?.concepts)
    }
    return data
  }

  /** Agent pipeline over DB + Docs + Memory; returns a governed, cited answer.
   *  Pass `jwt` (a signed end-user token) to act as that end-user's role/identity.
   *  Pass `business` (id or name, e.g. "3P") to answer from that slice only; a tenant
   *  may require it, and then a request without one is refused WITH the choices.
   *  A result with `status === 'awaiting_confirmation'` is a QUESTION (which business),
   *  not an answer: show `result.confirmation.options` and resend
   *  `confirmation.pending_question` with `business` set. `result.assumptions` lists the
   *  readings taken instead of asking; a correction is `chat(option.question)`.
   *  `confirmation` is accepted for older clients and unused. */
  chat(message, { userId, datasourceId, analysisMode = 'auto', history, jwt, business,
    confirmation } = {}) {
    return this._post('/v1/chat', {
      message,
      user_id: userId,
      datasource_id: datasourceId,
      analysis_mode: analysisMode,
      history,
      business,
      confirmation,
    }, jwt)
  }

  /** Gateway-safe chat/deep research. Returns immediately with a persistent job id. */
  startChat(message, { datasourceId, analysisMode = 'auto', history, jwt, business,
    confirmation } = {}) {
    return this._post('/v1/chat/jobs', {
      message,
      datasource_id: datasourceId,
      analysis_mode: analysisMode,
      history,
      business,
      confirmation,
    }, jwt)
  }

  getChatJob(jobId, { after = 0, jwt } = {}) {
    return this._jobRequest('GET', `/v1/chat/jobs/${encodeURIComponent(jobId)}?after=${after}`, jwt)
  }

  cancelChatJob(jobId, { jwt } = {}) {
    return this._jobRequest('DELETE', `/v1/chat/jobs/${encodeURIComponent(jobId)}`, jwt)
  }

  /** Start and poll until complete; each HTTP request stays short. */
  async chatAsync(message, { datasourceId, analysisMode = 'auto', history, jwt, business,
    confirmation, pollMs = 900, onEvent } = {}) {
    const started = await this.startChat(
      message, { datasourceId, analysisMode, history, jwt, business, confirmation })
    let after = 0
    for (;;) {
      const state = await this.getChatJob(started.job_id, { after, jwt })
      for (const event of state.events || []) {
        after = Math.max(after, Number(event.seq || 0))
        if (onEvent) onEvent(event)
      }
      if (state.status === 'succeeded') return state.result
      if (state.status === 'failed') {
        throw new ApiError(state.error?.code || 'JOB_FAILED', state.error?.message || 'Job failed', 400)
      }
      if (state.status === 'cancelled') {
        throw new ApiError('CANCELLED', 'Job cancelled', 409)
      }
      await new Promise(resolve => setTimeout(resolve, pollMs))
    }
  }

  /** Rate one chat result. Re-submitting updates this user's feedback for the trace. */
  feedback(traceId, { rating, reason = '', comment = '', jwt } = {}) {
    return this._post('/v1/feedback', { trace_id: traceId, rating, reason, comment }, jwt)
  }

  /** Run a governed SQL SELECT (allowlist + RLAC/CLAC), inside `business` if given. */
  query(sql, { datasourceId, jwt, business } = {}) {
    return this._post('/v1/query', { sql, datasource_id: datasourceId, business }, jwt)
  }

  /** Doc-RAG retrieval scoped to the (effective) role; returns citations. */
  retrieve(query, { topK = 5, datasourceId, jwt } = {}) {
    return this._post('/v1/retrieve', { query, top_k: topK, datasource_id: datasourceId }, jwt)
  }
}

export default AdminClient
