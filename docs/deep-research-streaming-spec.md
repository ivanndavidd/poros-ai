# Spec: Stream Deep-Research reasoning as readable text ("efek Claude")

Status: DONE (2026-07-29, implemented in a Claude Code session — and extended beyond this
spec: the final answer now streams token-live from the provider during synthesis via
`gateway.complete(on_delta=...)` → `answer_delta` SSE events; `answer_reset` clears a
shown draft when a fallback/repair replaces it. This spec's `reasoning_delta` narration
is implemented in `research_agent.py` (per-round prose + hypothesis transitions + critic
notes) with a live panel in `Chatbot.jsx`. Tests: `tests/test_answer_streaming.py`,
`tests/test_adaptive_research.py::test_reasoning_narration_streams_during_research`.)

## Goal
During **Deep Research**, show the agent's **reasoning/findings as readable prose that flows
in real time** while the research runs — approximating the Claude/ChatGPT "thinking out loud"
feel. Today the research phase only emits terse status labels
("Deep Research: … (putaran 7)"); the user wants readable analysis text **accumulating during**
the (long) research so it never feels like a silent wait.

## Why this layer (important context)
Poros Deep Research = `agent/research_agent.py` runs **many rounds** of governed tool-calls
(SQL / docs / profiling), and only AFTER research does `agent/orchestrator.py` synthesize the
final answer. So the **final answer cannot stream during research** (it does not exist yet).
BUT every research round already has model **reasoning** (why it runs a query, what a hypothesis
test showed). Streaming **that reasoning** is the achievable "text during process".

Do NOT try to stream the final answer earlier, and do NOT reduce research quality — the user
explicitly wants to keep depth. This is purely an **observability/UX** addition.

## Plumbing that ALREADY exists (reuse it — do not rebuild)
- **SSE**: `routers/consumer.py::chat_stream` forwards ANY event dict put on `emit=q.put` as
  `data: {json}\n\n`. New event types reach the client with **no router change**.
- **Orchestrator** threads an `emit` callback through the pipeline and already emits
  `{"type":"status"}`, `{"type":"finding"}`, `{"type":"answer_delta"}` (answer streaming was
  added in a prior session — keep it; it powers normal-mode answer streaming).
- **Frontend** `views/Chatbot.jsx` consumes via `api.consumerStream('/v1/chat_stream', …, onEvent)`
  and already handles `status` (→ `liveTrace` log), `finding` (→ `liveTrace`), `answer_delta`
  (→ `streamingText` live bubble), `done`/`error`/`cancelled`. You just add ONE more branch.

## Backend — `backend/app/agent/research_agent.py`
1. Find the **round loop** that currently emits the "Deep Research: … (putaran N)" status
   (grep for the status text / `emit(` / `putaran`). The agent's per-round LLM response and each
   hypothesis's status transition are available there (see the `Ledger` / hypotheses structures).
2. Per round, emit a NEW readable event, e.g.:
   ```python
   emit({"type": "reasoning_delta", "text": narration})
   ```
   `narration` = 1–3 sentences: what hypothesis it is testing + what the last result showed +
   what it will check next. **Derive it from text the agent ALREADY produced** (its reasoning
   field / hypothesis rationale). **Do NOT add extra paid LLM calls just to narrate.**
3. HARD RULES (non-negotiable — this repo is a governed analytics plane):
   - Reasoning text is **process narration**, not new factual claims. Never stream invented
     numbers; any figure mentioned mid-stream must already exist in a validated ledger result,
     otherwise speak qualitatively.
   - PII stays masked (research already operates on PII-masked rows — keep it that way).
   - Keep it cheap: truncate each delta (~300 chars); no new network/model calls.
4. Cancellation: the loop already calls `check_cancelled()`; nothing extra needed.

## Frontend — `frontend/src/views/Chatbot.jsx`
1. Add state: `const [reasoning, setReasoning] = useState('')`.
2. In the `onEvent` handler add:
   ```js
   else if (ev.type === 'reasoning_delta') setReasoning(r => (r + ' ' + (ev.text || '')).slice(-4000))
   ```
   Reset `setReasoning('')` at ask-start (next to the existing `setLiveTrace([])`/`setStreamingText('')`)
   and on `done`/`error`/`cancelled`.
3. While `thinking`, render a **live reasoning panel** (muted / smaller / italic, its own
   `max-height` + `overflow:auto`, auto-scroll to bottom) showing `reasoning`. Place it under the
   trace log. Keep it visually distinct from the final answer bubble (this is "the model working",
   not the answer).
4. On `done`, `reasoning` clears; the final answer renders as today via `appendMsg`.

## Acceptance
- Ask a Deep-Research question → within a few seconds, readable analysis text starts flowing in
  the reasoning panel and keeps updating each round; the wait no longer feels silent.
- Final answer still renders at the end, unchanged; confidence/SQL/citations intact.
- **No new paid LLM calls**; no ungrounded figures shown mid-stream; cancellation still works.
- Normal (non-deep) mode unaffected (it already streams the answer via `answer_delta`).

## Files
- `backend/app/agent/research_agent.py` — emit `reasoning_delta` per round.
- `frontend/src/views/Chatbot.jsx` — `reasoning` state + handler + live panel.
- (optional) `backend/tests/` — assert `reasoning_delta` is emitted on a deep run (offline,
  monkeypatched gateway/rag), mirroring the existing agent-loop tests.

## Cleanup nit (from the prior session)
`Chatbot.jsx` still has a dormant "reveal" path (`Answer({text, stream})` + an unused `stream`
prop on `Message`) left over after answer streaming moved to `answer_delta`. Safe to remove while
you are in this file.
