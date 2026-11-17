"""Auto-memory — the AI writes its own notes about the user (Memory v2).

After every successful /v1/chat turn, one cheap LLM pass reads (question, answer)
and extracts 0-3 DURABLE facts/preferences about the USER — not about query
results. Each candidate is deduped against the user's existing memories
(embedding cosine + case-insensitive text) and stored with source='auto' so the
admin UI can tell it apart from hand-written notes.

Runs in a daemon thread by default so it never adds latency to the reply; set
env MEMORY_CAPTURE_SYNC=1 (tests) to run inline. Gated per tenant config:
auto_memory AND persistent, and it needs a JWT-verified user_id.

PII note: extraction is instructed to skip identifiers (phone/emails/ID numbers);
recalled memory later passes through the same evidence path whose PII masking
already guards LLM prompts.
"""
import contextvars, json, math, os, threading
from .. import store, llm
from ..rag.embeddings import embed_query
from .conversation import get_cfg
from .recall import MEMORY_KINDS

_MAX_ITEMS = 3
_DEDUP_COS = 0.90

_SYS = ("You maintain long-term memory notes about a chat user. From the exchange, extract at most "
        f"{_MAX_ITEMS} DURABLE facts or preferences about the USER themself — their job/scope "
        "(e.g. 'PIC gudang Jakarta'), lasting preferences ('prefers answers as tables'), or standing "
        "context ('handles the Acme project'). NEVER store: one-off query results or numbers, anything "
        "that changes week to week, or personal identifiers (phone, email, ID numbers, addresses). "
        "Write each note in the user's language, third person, <=160 chars, self-contained. "
        'Reply with ONLY a JSON array: [{"kind":"fact|preference|context","text":"..."}] — '
        "return [] when the exchange reveals nothing durable (this is the COMMON case).")


def _cos(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _parse(raw: str) -> list:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`").strip()
        if s.lower().startswith("json"):
            s = s[4:].strip()
    try:
        items = json.loads(s)
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    out = []
    for it in items[:_MAX_ITEMS]:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()[:200]
        if len(text) < 8:                        # too short to be a real durable note
            continue
        kind = str(it.get("kind") or "fact").strip().lower()
        out.append({"kind": kind if kind in MEMORY_KINDS else "fact", "text": text})
    return out


def auto_capture(principal: dict, user_id: str, question: str, answer: str,
                 trace_id: str = "", cfg: dict = None) -> list:
    """Extract + dedup + store. Returns the list of stored {id, kind, text}.
    Never raises — memory capture must not be able to break anything."""
    tenant_id = principal["tenant_id"]
    try:
        cfg = cfg or get_cfg(tenant_id)
        if not (user_id and cfg.get("auto_memory") and cfg.get("persistent") and llm.available()):
            return []
        # max_tokens must cover a reasoning model's INTERNAL thinking tokens too — at 300,
        # gpt-5.6-luna returned empty text and auto-memory silently captured nothing.
        # Hard rule: PII never reaches a model. This prompt carries the WHOLE turn — the
        # user's question and the assistant's answer, the richest PII surface in the
        # pipeline. Lazy import: app.skills.registry imports app.memory, so a module-level
        # import here would be circular.
        from ..skills.registry import TextPiiRedactor
        _r = TextPiiRedactor()
        res = llm.complete(tenant_id, "answer_simple",
                           [{"role": "system", "content": _SYS},
                            {"role": "user", "content": f"User said: {_r.redact(question)[:2000]}\n\n"
                                                        f"Assistant answered: {_r.redact(answer or '')[:2000]}"}],
                           temperature=0, max_tokens=1500)
        items = _parse(res.text)
        # meter the extraction call (it runs post-reply, outside the turn's usage total)
        try:
            store.log_usage(tenant_id, principal.get("key", ""), principal.get("role", ""), "memory_capture",
                            0, "OK", res.model, res.prompt_tokens, res.completion_tokens, res.cost,
                            user_id=user_id, trace_id=trace_id)
        except Exception:
            pass
        if not items:
            return []
        existing = store.list_memories(tenant_id, user_id)
        stored = []
        for it in items:
            emb = embed_query(tenant_id, it["text"])
            dup = any(m["text"].strip().lower() == it["text"].strip().lower()
                      or _cos(emb, m["embedding"]) >= _DEDUP_COS for m in existing)
            if dup:
                continue
            mid = store.add_memory(tenant_id, user_id, it["kind"], it["text"], emb, source="auto")
            rec = {"id": mid, "kind": it["kind"], "text": it["text"], "embedding": emb}
            stored.append({"id": mid, "kind": it["kind"], "text": it["text"]})
            existing.append(rec)                 # dedup within the same batch too
        if stored:
            try:
                store.log_audit(tenant_id, trace_id or "-", "memory_capture", role=principal.get("role", ""),
                                user_id=user_id, status="ok",
                                detail={"stored": [{"kind": s["kind"], "text": s["text"]} for s in stored]})
            except Exception:
                pass
        return stored
    except Exception:
        return []


def schedule_capture(principal: dict, user_id: str, question: str, answer: str,
                     trace_id: str = "", cfg: dict = None):
    """Fire-and-forget by default (zero added latency); inline when
    MEMORY_CAPTURE_SYNC=1 so tests are deterministic."""
    if os.getenv("MEMORY_CAPTURE_SYNC") == "1":
        return auto_capture(principal, user_id, question, answer, trace_id, cfg)
    # copy the current context so the request's X-Request-Id (a ContextVar) is visible inside
    # the thread — otherwise the capture LLM call would lose the correlation id (threads don't
    # inherit contextvars the way asyncio tasks do).
    ctx = contextvars.copy_context()
    threading.Thread(target=lambda: ctx.run(auto_capture, principal, user_id, question, answer, trace_id, cfg),
                     daemon=True).start()
    return None
