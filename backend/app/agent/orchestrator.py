"""Agent orchestrator: the single pipeline every /v1/chat request flows through."""
import json
import re
import uuid
from .. import (answer_contract, assumptions, business_confirm, business_scope,
                entity_confirm, followup, metric_confirm, request_contract, store,
                llm, memory, web_research, semantic_contract, semantic_resolver)
from ..connectors.factory import SQL_TYPES
from ..skills import Ctx, allowed_skills, run_skill
from ..skills.registry import _evidence_text, _record_usage
from ..context import check_cancelled, set_cancellation_usage, set_chat_session
from ..reports import mbr_engine
from . import analytics, brand_analysis, deep_analysis, kb_agent, research_agent, sql_agent
from .verifier import verify, compute_confidence

# fenced ```chart / ```artifact blocks the frontend renders as charts/dashboards.
_VIZ_RE = re.compile(r"```(?:chart|artifact)\b.*?```", re.S | re.I)
# words that mean the answer likely lives in documents (procedure / how-to / policy),
# so docs are only searched for these — a pure data question ("dashboard penjualan")
# must not pull an unrelated doc citation (which then fails the citation check).
_DOC_CUE = re.compile(
    r"\b(cara|bagaimana|langkah|prosedur|sop|kebijakan|policy|panduan|dokumen|document|"
    r"manual|tutorial|playbook|procedure|petunjuk|guide|how\s*to)\b", re.I)
_DIRECT_INTENTS = {"smalltalk", "ambiguous"}
_SQL_INTENTS = {"data_query", "diagnostic", "prescriptive"}
_FOLLOWUP_EVENT = "followup"          # audit event: what a follow-up rewrite did this turn
_ASKING_BUSINESS = "Menanyakan business yang dimaksud…"   # status line: the turn asks which business
_ENGLISH_DIRECT = re.compile(
    r"\b(hello|hi|hey|thanks|thank\s+you|who\s+are\s+you|what\s+can\s+you\s+do|"
    r"help|check|continue|system|connection)\b", re.I)
_TEST_DIRECT = re.compile(r"^\s*(?:test(?:ing)?|tes|ping)(?:\s+\w+)?[.!?]*\s*$", re.I)
_THANKS_DIRECT = re.compile(r"^\s*(?:terima\s+kasih|makasih|thanks|thank\s+you)[.!?]*\s*$", re.I)


def _requires_sql(intent: str) -> bool:
    """SQL is opt-in by intent. Missing document evidence is never permission to query DB."""
    return intent in _SQL_INTENTS


def _direct_answer(message: str, intent: str) -> str:
    """Deterministic no-data responses for smalltalk and underspecified requests."""
    english = bool(_ENGLISH_DIRECT.search(message or ""))
    if intent == "ambiguous":
        if english:
            return ("I need a little more detail. Please mention the data or document, metric, "
                    "and period you want me to check.")
        return ("Saya perlu sedikit konteks. Sebutkan data atau dokumen, metrik, dan periode "
                "yang ingin diperiksa.")
    if _TEST_DIRECT.fullmatch(message or ""):
        return ("System is active and ready. Ask a question about your data or documents."
                if english else
                "Sistem aktif dan siap. Silakan ajukan pertanyaan tentang data atau dokumen.")
    if _THANKS_DIRECT.fullmatch(message or ""):
        return "You're welcome." if english else "Sama-sama."
    if english:
        return "Hello! I can help analyze governed business data and your uploaded documents."
    return "Halo! Saya dapat membantu menganalisis data bisnis governed dan dokumen yang tersedia."


def _direct_result(tenant_id, trace_id, role, user_id, message, gate):
    """Complete response shape for a turn that intentionally uses no evidence tools."""
    check_cancelled()
    answer = _direct_answer(message, gate.intent)
    plan = ["direct_response" if gate.intent == "smalltalk" else "request_clarification"]
    vr = {"ok": True, "issues": [], "checks": {"direct_response": True}}
    confidence = {"level": "high", "score": 100, "signals": {"direct_response": True}}
    # No pipeline model ran, but SCREENING did and it is billed. Reporting zero here made
    # these turns look free in monitoring while the provider still charged for them.
    store.log_audit(tenant_id, trace_id, "answer", role=role, user_id=user_id,
                    model=gate.engine, status="complete", cost=gate.cost,
                    detail={"answer": answer, "plan": plan,
                            "confidence": "high", "verifier_ok": True,
                            "prompt_tokens": gate.prompt_tokens,
                            "completion_tokens": gate.completion_tokens})
    return {"answer": answer, "blocked": False, "trace_id": trace_id, "plan": plan,
            "status": "complete", "missing_requirements": [], "assumptions": [],
            "unverified_figures": [],
            "gatekeeper": _gate_dict(gate), "captured_memory": None,
            "router": {"purpose": "direct", "complexity": gate.complexity,
                       "model": None, "models": []},
            "sql": None, "citations": [], "recalled_memory": [], "documents": [],
            "verifier": vr, "confidence": confidence, "evidence_summary": [],
            "usage": {"model": gate.engine, "prompt_tokens": gate.prompt_tokens,
                      "completion_tokens": gate.completion_tokens,
                      "cost": round(gate.cost, 6)}}


def _brand_cached_result(tenant_id, trace_id, role, user_id, gate, biz, cached, interpreted,
                         emit, usage=None, plan=None, readings=None):
    """The same brand question asked again within the cache window: the analysis stored
    the first time, so the answer does not drift between two asks. In the brand mode only
    screening ran; in an ordinary turn the agent ran until it called brand_baseline
    (``usage`` = every call of this turn, ``plan`` = its steps so far). The readings the
    answer took are the stored ones (the same question opens the same readings)."""
    check_cancelled()
    answer = cached.get("answer") or ""
    taken = cached.get("assumptions") or assumptions.public(readings)
    plan = [*(plan or []), "brand_analysis(cached)"]
    meta = {**(cached.get("brand_analysis") or {}), "answer_cached": True}
    status = cached.get("status") or "complete"
    confidence = cached.get("confidence") or {"level": "medium", "score": 60, "signals": {}}
    billed = usage or [{"model": gate.engine, "prompt_tokens": gate.prompt_tokens,
                        "completion_tokens": gate.completion_tokens, "cost": gate.cost}]
    spent = {"model": billed[-1]["model"],
             "prompt_tokens": sum(u["prompt_tokens"] for u in billed),
             "completion_tokens": sum(u["completion_tokens"] for u in billed),
             "cost": round(sum(u["cost"] for u in billed), 6)}
    _stream_text(emit, answer)
    store.log_audit(tenant_id, trace_id, "answer", role=role, user_id=user_id,
                    model=spent["model"], status=status, cost=spent["cost"],
                    detail={"answer": answer[:2000], "plan": plan,
                            "confidence": confidence.get("level"), "verifier_ok": True,
                            "analysis_mode": brand_analysis.MODE,
                            "brand_analysis": {"brands": meta.get("brands"),
                                               "period": meta.get("period"),
                                               "answer_cached": True},
                            "prompt_tokens": spent["prompt_tokens"],
                            "completion_tokens": spent["completion_tokens"]})
    return {"answer": answer, "blocked": False, "trace_id": trace_id, "plan": plan,
            "status": status, "missing_requirements": cached.get("missing_requirements") or [],
            "assumptions": taken,
            "unverified_figures": cached.get("unverified_figures") or [],
            "business": {"id": biz["id"], "name": biz["name"]} if biz else None,
            "interpreted_question": interpreted["question"] if interpreted else None,
            "gatekeeper": _gate_dict(gate), "captured_memory": None,
            "router": {"purpose": "brand_analysis", "complexity": gate.complexity,
                       "model": None, "models": _models_used(gate.engine, usage)[1] if usage else [],
                       "analysis_mode": brand_analysis.MODE},
            "analysis_mode": brand_analysis.MODE, "brand_analysis": meta,
            "sql": None, "citations": cached.get("citations") or [], "recalled_memory": [],
            "documents": [], "verifier": cached.get("verifier") or {"ok": True, "issues": [],
                                                                    "checks": {}},
            "confidence": confidence, "evidence_summary": cached.get("evidence_summary") or [],
            "usage": spent}


def _audit_queries(tenant_id, trace_id, role, user_id, steps):
    """One audit event per governed SQL this turn ran."""
    for step in steps or []:
        if not step:
            continue
        store.log_audit(tenant_id, trace_id, "query", role=role, user_id=user_id, model=step.get("engine"),
                        status=("ok" if step.get("ok") else "error"), code=step.get("code"),
                        detail={"safe_sql": step.get("safe_sql") or step.get("generated_sql"),
                                "rows": len(step.get("rows") or []) if step.get("ok") else 0,
                                "estimated_bytes": step.get("estimated_bytes"), "error": step.get("error")})


def _confirmation_result(tenant_id, trace_id, role, user_id, gate, ask, business=None,
                         usage=None, plan=None):
    """A turn that ends in a question — which business — instead of an answer.

    Only the business is still asked: it is an access boundary, not a reading. A name,
    a brand or a KPI meaning is an open reading the answer states (app/assumptions.py).
    Deliberately shaped like an ordinary answer, with the structured menu alongside it.
    A console renders `confirmation` as buttons; an app that ignores the field still
    shows the question to the one person who can answer it, and typing the business name
    is always the way through (business_confirm.selection_reply).

    ``usage`` = the turn's ctx.usage when a model already ran before the ask (a general
    question whose agent found it needs company data after all); it holds the screening
    entry too. Without it only screening ran and is billed.
    """
    check_cancelled()
    plan = list(plan or []) + ["ask_" + ask["kind"]]
    vr = {"ok": True, "issues": [], "checks": {"awaiting_confirmation": True}}
    confidence = {"level": "high", "score": 100,
                  "signals": {"awaiting_confirmation": True}}
    if usage:
        model = usage[-1].get("model") or gate.engine
        ptok = sum(int(u.get("prompt_tokens") or 0) for u in usage)
        ctok = sum(int(u.get("completion_tokens") or 0) for u in usage)
        cost = sum(float(u.get("cost") or 0.0) for u in usage)
    else:
        model, ptok, ctok, cost = (gate.engine, gate.prompt_tokens,
                                   gate.completion_tokens, gate.cost)
    # Logged as awaiting_confirmation, NOT "ok" (design doc §11.3): a question is not a
    # successful answer, and a dashboard counting "ok" must not count turns that produced
    # none. What ran before the question is still billed.
    store.log_audit(tenant_id, trace_id, "answer", role=role, user_id=user_id,
                    model=model, status="awaiting_confirmation", cost=cost,
                    detail={"answer": ask["question"], "plan": plan,
                            "confidence": "high", "verifier_ok": True,
                            "confirmation_kind": ask["kind"],
                            "options": [o["name"] for o in ask["options"]],
                            "businesses": [o["name"] for o in ask["options"]],
                            "pending_question": ask["pending_question"],
                            "prompt_tokens": ptok, "completion_tokens": ctok})
    return {"answer": ask["question"], "confirmation": ask,
            "status": "awaiting_confirmation", "assumptions": [], "unverified_figures": [],
            "business": {"id": business["id"], "name": business["name"]} if business else None,
            "blocked": False, "trace_id": trace_id, "plan": plan,
            "gatekeeper": _gate_dict(gate), "captured_memory": None,
            "router": {"purpose": "confirmation", "complexity": gate.complexity,
                       "model": None, "models": []},
            "sql": None, "citations": [], "recalled_memory": [], "documents": [],
            "verifier": vr, "confidence": confidence, "evidence_summary": [],
            "usage": {"model": model, "prompt_tokens": ptok, "completion_tokens": ctok,
                      "cost": round(cost, 6)}}


def _viz_blocks(text):
    return _VIZ_RE.findall(text or "")


def _pick_sql_ds(tenant_id, datasource_id, business=None):
    """The datasource to answer from — never one outside the active business.

    Every list here goes through business_scope so the slice is applied once, in one place,
    rather than each caller remembering that businesses exist."""
    all_ds = business_scope.visible_datasources(tenant_id, business)
    sql_ds = [d for d in all_ds if d["type"] in SQL_TYPES]
    if datasource_id:
        m = [d for d in all_ds if d["id"] == datasource_id and d["type"] in SQL_TYPES]
        if m:
            return m[0]
    return sql_ds[0] if sql_ds else None


_ROUTE_TOK = re.compile(r"[a-z0-9]+")

# These are high-impact KPI/domain terms whose answer is unsafe to delegate to a cheap
# model merely because the sentence is short. The budget router may still downgrade when
# the tenant explicitly exhausted its budget, but normal operation uses the Primary model.
_BUSINESS_PRECISION_CUE = re.compile(
    r"(?<![a-z0-9])(?:gmv|nmv|doi|days?[\s_-]+of[\s_-]+inventory|"
    r"cancell?ation[\s_-]+rate|cancel[\s_-]+rate|revenue|pendapatan|omzet|"
    r"inventory[\s_-]+value|nilai[\s_-]+(?:inventory|inventori|persediaan)|"
    r"retention|retensi|sla|1p|3p|third[\s_-]+party)(?![a-z0-9])",
    re.I,
)


_VOCAB_WEIGHT = 0.25    # curated-vocabulary matches count, but 4 are needed to outvote 1 structural
# A sampled dimension VALUE ("Philips", "Shopee") only proves the table CONTAINS such rows —
# many tables do. It must never outweigh a schema identifier or a curated KPI name, which
# state what a datasource IS ABOUT. Live case: "berapa gmv brand philips" tied on 'philips'
# (a reference value of a 3PL complaint table) and fell back to the first datasource, so a
# GMV question was answered from complaint data instead of the curated order table.
_VALUE_WEIGHT = 0.25


def _route_tokens(parts):
    """Lowercased routing tokens. PURE-NUMERIC tokens are excluded: they are data values
    (202606, limits, codes), not vocabulary — live case: '15' from 'naik 15%' matched a
    number in one catalog and tied the score, misrouting a GMV question to inventory."""
    return {t for t in _ROUTE_TOK.findall(" ".join(parts).lower()) if not t.isdigit()}


def _ds_struct_terms(d, mdl):
    """Schema identifiers: datasource name, connection coordinates, tables, columns, dimensions."""
    # Public connection coordinates are useful routing vocabulary (for example a user
    # explicitly says "dataset Datamart"). Never include credentials or arbitrary values.
    conn = d.get("conn") or {}
    struct = [str(d.get("name") or "")]
    struct.extend(
        str(conn.get(key) or "")
        for key in ("project", "project_id", "dataset", "database", "schema")
    )
    for t, cols in (mdl.get("tables") or {}).items():
        struct.append(t)
        struct.extend(cols or [])
    struct.extend(str(k) for k in (mdl.get("reference_values") or {}))
    return struct


def _ds_value_terms(mdl):
    """Sampled dimension VALUES — weak routing evidence (see _VALUE_WEIGHT)."""
    return [
        str(value)
        for values in (mdl.get("reference_values") or {}).values()
        for value in (values or [])
    ]


def _ds_vocab_terms(mdl):
    """Curated wording: MDL descriptions, metric names/definitions, example questions."""
    vocab = [str(v) for v in (mdl.get("descriptions") or {}).values()]
    for m in (mdl.get("metrics") or []):
        vocab.extend(str(m.get(k) or "") for k in ("name", "definition", "note"))
    vocab.extend(str(ex.get("q") or "") for ex in (mdl.get("examples") or []))
    return vocab


def _ds_terms(d):
    """Weighted term map (lowercased token -> weight) describing a datasource.

    STRUCTURAL tokens weigh 1.0. CURATED VOCABULARY weighs _VOCAB_WEIGHT: everyday words
    ('pesanan', 'penjualan') only live in the curation layer — without it a generic sales
    question matches nothing and falls back to the FIRST datasource (live case: 'jumlah
    pesanan Mei 2026' hit the inventory DB, COUNT=0). The low weight keeps a heavily-
    described datasource from outvoting a structural match."""
    mdl = d.get("mdl") or {}
    terms = {t: _VOCAB_WEIGHT for t in _route_tokens(_ds_vocab_terms(mdl))}
    terms.update({t: _VALUE_WEIGHT for t in _route_tokens(_ds_value_terms(mdl))})
    # structural weight wins on overlap
    terms.update({t: 1.0 for t in _route_tokens(_ds_struct_terms(d, mdl))})
    return terms


def _phrase_specificity(phrases, message: str) -> tuple[int, int] | None:
    """Most specific curated phrase occurring in the question, or None.

    Phrase (not token) matching prevents a short curated term such as ``3P`` or ``GMV``
    from matching inside an unrelated word.
    """
    best = None
    for raw in phrases or []:
        tokens = _ROUTE_TOK.findall(str(raw or "").lower())
        if not tokens:
            continue
        pattern = re.compile(
            r"(?<![a-z0-9])" + r"[\s_-]+".join(map(re.escape, tokens))
            + r"(?![a-z0-9])",
            re.I,
        )
        match = pattern.search(message or "")
        if match:
            score = (len(tokens), match.end() - match.start())
            if best is None or score > best:
                best = score
    return best


def _routing_alias_score(datasource: dict, message: str) -> tuple[int, int] | None:
    """Most-specific explicitly curated datasource alias that matches.

    ``routing_aliases`` is the deterministic bridge between business vocabulary and a
    physical datasource: e.g. ``3P`` / ``third party`` -> the tenant's Datamart source.
    """
    return _phrase_specificity(
        (datasource.get("mdl") or {}).get("routing_aliases") or [], message,
    )


def _metric_name_score(datasource: dict, message: str) -> tuple[int, int] | None:
    """Most-specific curated KPI name (MDL ``metrics``) this question asks for.

    A metric name is the tenant declaring which datasource OWNS that KPI, so naming it
    must outrank incidental catalog overlap elsewhere. Metrics are free-text curation
    (unlike validated ``business_concepts``), so they only steer ROUTING here — they
    never redefine what the SQL must compute.
    """
    return _phrase_specificity(
        [
            metric.get("name")
            for metric in ((datasource.get("mdl") or {}).get("metrics") or [])
            if isinstance(metric, dict)
        ],
        message,
    )


def _phrase_winner(sql_ds: list, message: str, scorer):
    """The single datasource whose curated phrase match is strictly most specific.

    When the same top phrase is curated on several datasources the choice is ambiguous,
    so routing falls through to the next signal instead of silently picking one.
    """
    matched = [
        (score, datasource) for score, datasource in
        ((scorer(datasource, message), datasource) for datasource in sql_ds)
        if score is not None
    ]
    if not matched:
        return None
    top = max(score for score, _datasource in matched)
    winners = [datasource for score, datasource in matched if score == top]
    return winners[0] if len(winners) == 1 else None


def _gate_usage(gate) -> dict | None:
    """One ctx.usage entry for a screening decision, or None when it cost nothing."""
    if not (gate.prompt_tokens or gate.completion_tokens):
        return None
    return {"model": gate.engine.replace("llm:", "").replace("+followup", ""),
            "prompt_tokens": gate.prompt_tokens,
            "completion_tokens": gate.completion_tokens, "cost": gate.cost}


def _contextualize_followup(tenant_id, trace_id, role, user_id, message, history, gate,
                            confirmation, emit):
    """Make an elliptical follow-up self-contained BEFORE the deterministic layers read it.

    Returns ``(message, gate, usage_entries, interpreted)``: the question the rest of the
    turn should use, the screening decision that goes with it, the metering entries the
    step cost, and ``{"original", "question"}`` when a rewrite was adopted (None
    otherwise). Everything that can go wrong — no model, an ungrounded draft, a re-screen
    that blocks or leaves the data lane — returns the turn exactly as it came in.
    """
    if not followup.is_candidate(message, history, gate, confirmation, _SQL_INTENTS,
                                 standalone_cue=_BUSINESS_PRECISION_CUE):
        return message, gate, [], None
    emit({"type": "status", "text": "Melengkapi pertanyaan lanjutan dari percakapan…"})
    draft = followup.rewrite(tenant_id, message, history)
    usage = [draft.usage] if draft.usage else []
    if draft.reason == "unavailable":
        return message, gate, usage, None      # no model: nothing happened, nothing to audit
    if not draft.question:
        store.log_audit(tenant_id, trace_id, _FOLLOWUP_EVENT, role=role, user_id=user_id,
                        model=draft.model, status="kept",
                        detail={"reason": draft.reason, "message": message[:500]})
        return message, gate, usage, None
    detail = {"original": message[:500], "rewritten": draft.question[:500]}
    # The draft is derived from client-supplied history, so it is screened THIS turn —
    # the same rule business_confirm.selection_reply lives by.
    regate = llm.screen(tenant_id, draft.question, history)
    if not (regate.allow and regate.intent in _SQL_INTENTS):
        usage += [entry for entry in (_gate_usage(regate),) if entry]
        store.log_audit(tenant_id, trace_id, _FOLLOWUP_EVENT, role=role, user_id=user_id,
                        model=draft.model, status="discarded",
                        detail={"reason": f"rescreen:{regate.intent}", **detail})
        return message, gate, usage, None
    usage = [entry for entry in (_gate_usage(gate),) if entry] + usage
    store.log_audit(tenant_id, trace_id, _FOLLOWUP_EVENT, role=role, user_id=user_id,
                    model=draft.model, status="rewritten", detail=detail)
    return draft.question, regate, usage, {"original": message, "question": draft.question}


def _semantic_gate(tenant_id, trace_id, role, user_id, message, sql_ds, datasource_id,
                   emit):
    """Cross-datasource semantic resolution for one turn (design doc phase 1).

    Returns ``(analysis, strict)``. Enforcement is per-tenant
    (``config/semantic.strict_metrics``, DEFAULT OFF): a strict tenant's ambiguity or
    authority conflict RAISES SemanticError (409 with the choices) instead of letting
    datasource order pick a meaning; everyone else answers byte-identically and the
    collision is recorded as a ``semantic_collision`` audit event, so an admin can curate
    it away before refusals are switched on. A caller-pinned datasource narrows the scope
    the resolver sees — the pin was already validated against the business slice.
    """
    if not sql_ds:
        return None, False
    in_scope = ([d for d in sql_ds if d.get("id") == datasource_id] or sql_ds
                ) if datasource_id else sql_ds
    analysis = semantic_resolver.analyze(in_scope, message)
    if not (analysis.blocking or analysis.resolved):
        return analysis, False           # nothing curated matched: no config read needed
    strict = semantic_resolver.is_strict(tenant_id)
    if analysis.blocking:
        if strict:
            audit_status = "enforced" if analysis.conflicts else "assumed"
        else:
            audit_status = "observed"
        store.log_audit(tenant_id, trace_id, "semantic_collision", role=role,
                        user_id=user_id, status=audit_status,
                        detail=semantic_resolver.audit_detail(analysis))
        # Only a CONFLICT still refuses under strict — broken replica authority is an
        # admin's problem, no reading a person corrects can fix it. An AMBIGUITY is
        # returned to run_agent, which hands its meanings to the model as an open reading
        # (overhaul item 5).
        if strict and analysis.conflicts:
            emit({"type": "status",
                  "text": "Istilah ini punya lebih dari satu makna terkurasi…"})
            raise semantic_resolver.blocking_error(analysis)
    return analysis, strict


def _route_datasource(tenant_id, message, sql_ds):
    """With MORE THAN ONE SQL datasource, pick the one whose catalog best fits the question.
    Without this the agent always used the FIRST datasource, so a marketplace-sales question
    got answered against an inventory datasource (wrong data).

    Catalog-based (no LLM): score each datasource by the weighted sum of question words that
    match terms DISTINCTIVE to it (present in its catalog but not the others'), so shared/common
    words don't bias the choice and a superset datasource doesn't win by size alone. Structural
    terms weigh 1.0, curated-vocabulary terms _VOCAB_WEIGHT (see _ds_terms). Ties or no
    distinctive match -> the first datasource (previous behavior), so nothing regresses."""
    if len(sql_ds) <= 1:
        return sql_ds[0] if sql_ds else None
    # An explicit business semantic contract outranks fuzzy vocabulary routing.
    # This makes datasource selection stable when a tenant changes LLM providers.
    contracts = semantic_contract.resolve_datasources(sql_ds, message)
    if contracts:
        selected_id = contracts[0].get("datasource_id")
        selected = next((ds for ds in sql_ds if ds.get("id") == selected_id), None)
        if selected:
            return selected
    # Curated business vocabulary outranks fuzzy catalog overlap, most explicit first:
    # a datasource alias ("3P" -> Datamart), then a KPI name ("GMV" -> the datasource
    # that defines that metric). Only a single unambiguous winner short-circuits.
    for scorer in (_routing_alias_score, _metric_name_score):
        winner = _phrase_winner(sql_ds, message, scorer)
        if winner:
            return winner
    q = {t for t in _ROUTE_TOK.findall((message or "").lower()) if not t.isdigit()}
    if not q:
        return sql_ds[0]
    bags = [_ds_terms(d) for d in sql_ds]
    best, best_score = sql_ds[0], 0.0
    for i, d in enumerate(sql_ds):
        others = set().union(*[bags[j].keys() for j in range(len(bags)) if j != i])
        score = sum(w for t, w in bags[i].items() if t in q and t not in others)
        if score > best_score:
            best, best_score = d, score
    return best


_MAX_OTHER_DS = 2       # besides the main one, the agent may query this many more SQL
                        # datasources of the slice (the old planner's bound: 3 in all)


def _semantic_route(analysis):
    """Where a strict tenant's turn routes: the resolved concepts decide, and an open KPI
    phrase adds its first meaning (stable order) — never connection order. A conflict
    routes nowhere (it refused before this point)."""
    if analysis.conflicts:
        return None
    probe = semantic_resolver.Analysis(
        resolved=analysis.resolved + [g["contracts"][0] for g in analysis.ambiguities])
    return probe.route_datasource_id()


def _unlogged(steps, logged):
    """The steps not yet written to the audit trail (the agentic engine audits each query
    the moment it runs)."""
    return [s for s in steps or [] if not any(s is done for done in logged)]


# agentic tool steps that go looking in the company's data (open_business only opens)
def _unfound_terms(session, evidence) -> list:
    """Terms the agent looked up and never found, that no row-returning query filtered on.
    A business name ("1p") is the slice, not a value, and is never one of them."""
    businesses = {kb_agent.squash(c.get("name")) for c in getattr(session, "choices", None) or []}
    missed = [t for t, found in (getattr(session, "terms", None) or {}).items()
              if not found and len(kb_agent.squash(t)) >= 3
              and kb_agent.squash(t) not in businesses]
    if not missed:
        return []
    sqls = kb_agent.squash(" ".join(str(s.get("safe_sql") or s.get("sql") or "")
                                    for s in answer_contract._ok_sets(evidence) if s.get("rows")))
    return [t for t in missed if kb_agent.squash(t) not in sqls]


_DATA_STEPS = {"lookup_values", "run_sql", "describe_table", "list_tables", "get_definitions",
               "search_catalog", "profile_table"}


def _compact_trace(trace):
    """The agentic tool steps for the plan, repeats folded: run_sql, run_sql -> run_sql×2."""
    out = []
    for step in trace or []:
        name, _, count = (out[-1].partition("×") if out else ("", "", ""))
        if name == step:
            out[-1] = f"{step}×{int(count or 1) + 1}"
        else:
            out.append(step)
    return out


def _baseline_plan(bases) -> str:
    return "brand_baseline(%s)" % (",".join(
        "%s:%s" % (b["brand"], "cached" if b.get("cached") else "fresh")
        for b in bases) or "none")


def _models_used(gate_engine, usage):
    """(answer_model, distinct_models) for the per-answer model badge.

    answer_model = the model of the LAST LLM call this turn — synthesis, the brand analysis
    or the agent loop's final turn (auto-memory runs AFTER this and in a background thread,
    so it never masquerades as the answer model). distinct_models rosters every model the
    turn touched, gatekeeper first (it screens before ctx.usage exists, so its model comes from
    gate.engine), then the answer pipeline in call order."""
    answer_model = usage[-1]["model"] if usage else None
    ge = str(gate_engine or "")
    gate_model = ge.split("llm:", 1)[1] if ge.startswith("llm:") else None
    models = []
    for m in [gate_model] + [u.get("model") for u in usage]:
        if m and m not in models:
            models.append(m)
    return answer_model, models


def _docs_irrelevant(docs):
    """True when the reranker judged EVERY retrieved chunk irrelevant (best score < 0).
    Data-flavored questions ('bagaimana cara meningkatkan GMV?') can trip the _DOC_CUE and
    retrieve unrelated chunks; keeping them would force a doc citation onto a purely
    data-driven answer (citations_present fails -> lower confidence for nothing).
    No reranker signal (offline embedder) -> not irrelevant, behavior unchanged."""
    hits = (docs or {}).get("results") or []
    rs = [h.get("rerank_score") for h in hits if h.get("rerank_score") is not None]
    return bool(rs) and max(rs) < 0


def _merge_docs(evidence, docs):
    """Merge doc hits the agent pulled mid-reasoning into evidence['docs'] (dedup by citation)."""
    cur = evidence.get("docs") or {"ok": True, "skill": "search_docs", "results": [], "citations": []}
    seen = {h.get("citation") for h in cur.get("results", [])}
    for h in docs.get("results", []):
        if h.get("citation") not in seen:
            cur.setdefault("results", []).append(h)
            cur.setdefault("citations", []).append(h.get("citation"))
            seen.add(h.get("citation"))
    evidence["docs"] = cur


def _stream_text(emit, text):
    """Emit the final answer as small paced deltas (type 'answer_delta') so the console reveals
    the text progressively — real over-the-wire streaming. The verifier never rewrites an
    answer (overhaul item 6), so what streams is what the response carries."""
    import re
    import time
    buf, count = "", 0
    for tok in re.findall(r"\S+\s*|\s+", text or ""):
        buf += tok
        count += 1
        if count >= 4:
            emit({"type": "answer_delta", "text": buf})
            buf, count = "", 0
            time.sleep(0.02)
    if buf:
        emit({"type": "answer_delta", "text": buf})


def _business_turn(session, tenant_id, role, business, message):
    """The business decision of this turn: the agentic session's slice when it opened one,
    none when its session opened nothing, else the classic ask-or-use."""
    if session and session.opened:
        return business_confirm.Turn(session.business, message)
    if session:
        return None
    return business_confirm.for_turn(tenant_id, role, business, message)


def run_agent(principal: dict, message: str, datasource_id: str = None, user_id: str = "",
              emit=None, history: list = None, analysis_mode: str = "auto",
              trace_id: str = None, business: str = None,
              confirmation: dict = None, engine: str = None, session_id: str = None) -> dict:
    """One chat turn. ``engine`` overrides the tenant's ``config/chat.engine`` (classic |
    agentic) — for the engine comparison script and tests only. ``session_id`` = the app's
    conversation id, stamped on every audit row of the turn."""
    _emit = emit or (lambda *_: None)   # optional progress callback (SSE streaming)
    set_chat_session(session_id)        # always set: a pooled job thread must not keep the last one
    tenant_id, role = principal["tenant_id"], principal["role"]
    trace_id = trace_id or uuid.uuid4().hex
    # Before the gatekeeper: when the person answered a business confirmation with just
    # the name, this turn is their ORIGINAL question, and that question has to be
    # screened here — it is recovered from client-supplied history.
    asked = message
    business, message = business_confirm.selection_reply(
        tenant_id, role, business, message, history)
    store.log_audit(tenant_id, trace_id, "question", role=role, user_id=user_id,
                    detail={"text": message} if message == asked
                    else {"text": message, "business_reply": asked})
    check_cancelled()

    # 1) GATEKEEPER — screens every prompt
    _emit({"type": "status", "text": "Mengecek keamanan prompt…"})
    # history is passed so an elliptical follow-up ("ya, bandingkan dengan marketplace
    # lain") is not screened as if it were the first thing ever said — the _DIRECT_INTENTS
    # return below fires long before the conversation block at step 2b, so a wrong
    # "ambiguous" here discards the history the client sent.
    gate = llm.screen(tenant_id, message, history)
    check_cancelled()
    if not gate.allow:
        store.log_audit(tenant_id, trace_id, "blocked", role=role, user_id=user_id, model=gate.engine,
                        status="blocked", detail={"reasons": gate.reasons, "intent": gate.intent})
        return {"answer": "This request was blocked by the gatekeeper (possible prompt-injection or unsafe content).",
                "blocked": True, "status": "blocked", "missing_requirements": [],
                "unverified_figures": [],
                "trace_id": trace_id, "gatekeeper": _gate_dict(gate), "evidence": {}, "citations": [],
                "verifier": {"ok": True, "issues": [], "checks": {"blocked_by_gatekeeper": True}},
                "usage": {"model": gate.engine, "prompt_tokens": gate.prompt_tokens,
                          "completion_tokens": gate.completion_tokens,
                          "cost": round(gate.cost, 6)}}

    # The gatekeeper screens SAFETY; its intent no longer picks the path (overhaul item 3).
    # One chat agent answers every turn with the tools this turn may use and decides itself
    # whether to call any: a greeting is answered with none, an unclear message with its
    # own question. The intent still decides (a) which confirmation may END a turn (a
    # greeting is never interrogated), (b) how much of the catalog the prompt carries, (c)
    # the hypothesis / advisor guidance, and (d) the whole path only when no tool-capable
    # model exists: then the deterministic fallback below runs as it always did.
    conversational = gate.intent in _DIRECT_INTENTS
    if conversational and not sql_agent.usable(tenant_id):
        # no model to talk with: the canned reply, before any datasource is touched
        _emit({"type": "status", "text": ("Meminta detail pertanyaan…"
                                          if gate.intent == "ambiguous" else
                                          "Menyiapkan respons…")})
        return _direct_result(tenant_id, trace_id, role, user_id, message, gate)

    # 1a) FOLLOW-UP CONTEXT — "show me for each client" only means something against the
    #     previous turn, yet every deterministic layer below (business ask, entity and
    #     semantic gates, request contract, precision cue, router) reads the raw message;
    #     the conversation block only ever reached the model, which then guessed. The
    #     follow-up is rewritten into a self-contained question from the user's OWN
    #     earlier turns, token-gated and re-screened; on any doubt the turn continues
    #     exactly as typed.
    # The AGENTIC engine (config/chat.engine) skips every layer below that reads the raw
    # message before a model does: the follow-up rewrite, the business pick, value and
    # brand readings, the KPI-meaning gate and the datasource router. Its model reads the
    # question (history included as chat turns) and the Knowledge Base through tools.
    agentic = ((engine or kb_agent.engine(tenant_id)) == kb_agent.AGENTIC
               and sql_agent.usable(tenant_id))
    if agentic:
        followup_usage, interpreted = [], None
    else:
        message, gate, followup_usage, interpreted = _contextualize_followup(
            tenant_id, trace_id, role, user_id, message, history, gate, confirmation, _emit)
    check_cancelled()
    data_intent = _requires_sql(gate.intent)

    role_policy = (store.get_role(tenant_id, role) or {}).get("policy", {})
    allowed = allowed_skills(role_policy)

    def _contracts_for(sources):
        """The semantic guard for the slice the agentic engine opened: the same rules as
        the classic path below (strict: the resolver; otherwise every source's contracts,
        each enforced on its own datasource only)."""
        found, strict = _semantic_gate(tenant_id, trace_id, role, user_id, message, sources,
                                       datasource_id, _emit)
        if strict and found and (found.resolved or found.ambiguities):
            return (found.resolved + metric_confirm.enforceable(found),
                    metric_confirm.readings(found, message) if found.ambiguities else [])
        return semantic_contract.resolve_datasources(sources, message), []

    session = None
    if agentic:
        session = kb_agent.Session(tenant_id, role, allowed, datasource_id, _contracts_for)
        try:
            session.start(business)
        except business_scope.BusinessError:
            if not conversational:
                raise
    # Resolve BEFORE any datasource is listed: the business decides which ones exist for
    # this turn. With several to choose from, the turn ENDS in a question instead of a
    # guess — nobody but the person asking knows whether they meant 1P or 3P. A greeting
    # is never asked: it is answered without any data instead.
    try:
        turn = _business_turn(session, tenant_id, role, business, message)
    except business_scope.BusinessError:
        if not conversational:
            raise
        turn = None
    # A general question ("why do sales usually drop after 11.11?") needs no business to be
    # answered, so it is not asked one up front: the agent answers it with no company data
    # and the question comes only when the agent finds it needs this company's figures
    # (the need_company_data tool). The business is still never guessed. A chosen analysis
    # mode (brand, deep) is a data ask by itself.
    pending_ask = None
    if turn and turn.ask and not conversational:
        # a brainstorm is not asked either: ideas first, the business only once the model
        # finds it needs this company's figures
        if ((gate.needs_data and not getattr(gate, "brainstorm", False))
                or analysis_mode not in (None, "", "auto", "fast")
                or not sql_agent.usable(tenant_id)):
            _emit({"type": "status", "text": _ASKING_BUSINESS})
            return _confirmation_result(tenant_id, trace_id, role, user_id, gate, turn.ask)
        pending_ask = turn.ask
    data_ok = bool(turn) and not turn.ask
    biz = turn.business if data_ok else None
    has_docs = bool(store.list_documents(tenant_id))
    docs_ok = has_docs and "search_docs" in allowed
    if session:
        _sql_ds, datasource = list(session.sources), session.primary
    else:
        _sql_ds = ([d for d in business_scope.visible_datasources(tenant_id, biz)
                    if d["type"] in SQL_TYPES] if data_ok and "run_sql" in allowed else [])
        datasource = _pick_sql_ds(tenant_id, datasource_id, biz) if _sql_ds else None
    mem_cfg = memory.get_cfg(tenant_id)     # the Memory tab config actually drives behavior (v2)

    # 1b) OPEN READINGS instead of ask-backs (overhaul item 5): a name the data only
    #     RESEMBLES ("Philips" vs Philipsome), the brand mode without a brand and (1c) a
    #     KPI phrase with several curated meanings no longer end the turn with a
    #     question. Their candidates go to the model, which takes one, says so in the
    #     answer's first sentence and the person corrects it next turn. Before the router
    #     on purpose: the whole reachable slice is read, not just the source routing picks.
    #     1b-1d follow the TOOL, not the intent label: whenever run_sql is offered the
    #     model may query (a short follow-up the gatekeeper calls ambiguous included).
    readings = list(session.readings) if session else []
    if _sql_ds and not session and entity_confirm.is_enabled(tenant_id):
        readings += entity_confirm.readings(message, _sql_ds)

    # 1b') BRAND MODE — the person chose the brand analysis: the brand's fixed baseline
    #     is read before the agent starts, and the same question within the cache window
    #     gets the same analysis. Without a brand named, the brand is an open reading the
    #     agent settles with brand_baseline. Outside this mode the agent calls the
    #     brand_baseline tool when it judges a question needs it.
    brand_target = None
    # the agentic engine finds a brand itself (brand_baseline); only the brand MODE the
    # person chose still reads the baseline up front
    if _sql_ds and not conversational and (
            not session or analysis_mode in brand_analysis.MODE_ALIASES):
        found = brand_analysis.detect(tenant_id, role, biz, message, analysis_mode)
        if isinstance(found, dict):
            readings.append(found)
            if session:
                session.readings = list(readings)   # the agentic prompt reads the session's
        else:
            brand_target = found
    if brand_target:
        cached = brand_analysis.cached_answer(tenant_id, role, brand_target, message)
        if cached:
            _emit({"type": "status", "text": "Memakai analisa brand yang sama seperti sebelumnya…"})
            return _brand_cached_result(tenant_id, trace_id, role, user_id, gate, biz, cached,
                                        interpreted, _emit, readings=readings)

    # 1c) SEMANTIC RESOLUTION — which curated meaning OWNS the KPI phrase this question
    #     uses, across every datasource this turn can see. Before the router on purpose:
    #     "GMV" curated in two datasources is exactly what used to be settled by
    #     connection order (resolve_datasources(...)[0]).
    analysis, semantic_strict = (None, False)
    if _sql_ds and not session:          # the agentic session ran it when it opened
        analysis, semantic_strict = _semantic_gate(
            tenant_id, trace_id, role, user_id, message, _sql_ds, datasource_id, _emit)
        # For a strict tenant an ambiguous KPI phrase is an open reading of the chat turn
        # (/v1/query keeps its typed refusal — raw SQL has nobody to read an assumption).
        if semantic_strict and analysis and analysis.ambiguities:
            readings += metric_confirm.readings(analysis, message)
    open_meanings = (analysis.ambiguities if semantic_strict and analysis else [])

    # 1d) REQUEST CONTRACT (phase 3) — what this question EXPLICITLY requires the answer
    #     to deliver ("per client", monthly bucket, top-10, a chart), extracted
    #     deterministically and grounded in the visible schema. None for the ordinary
    #     question that states no such requirement — then nothing downstream changes.
    # (agentic: built after the loop, from the business the model opened)
    required = request_contract.build(message, _sql_ds) if _sql_ds and not session else None

    # 2a) DATASOURCE ROUTER — with several SQL datasources, the agent's MAIN datasource is
    #     the one whose tables fit the question (the others stay reachable through
    #     run_sql's `datasource` argument). Skipped when the caller pinned datasource_id or
    #     only one SQL datasource exists. A strict tenant's resolved concept routes
    #     DETERMINISTICALLY (the resolver's pick never depends on datasource order).
    if _sql_ds and not session and not datasource_id and len(_sql_ds) > 1:
        _emit({"type": "status", "text": "Memilih sumber data yang paling sesuai…"})
        routed = None
        if semantic_strict and analysis:
            owner = _semantic_route(analysis)
            routed = next((d for d in _sql_ds if d.get("id") == owner), None)
        if not routed:
            routed = _route_datasource(tenant_id, message, _sql_ds)
        if routed:
            datasource = routed

    if session:
        semantic_contracts = list(session.contracts)
    elif semantic_strict and analysis and (analysis.resolved or open_meanings):
        # Every independently resolved concept rides along (a comparison may span two
        # datasources); execute_governed already filters per executed datasource. The
        # meanings of an open phrase ride too when their tables tell them apart, so the
        # one the model takes is computed exactly as curated.
        semantic_contracts = analysis.resolved + metric_confirm.enforceable(analysis)
    else:
        semantic_contracts = semantic_contract.resolve_datasources(
            [datasource] if datasource else [], message,
        )

    # 2) ROUTER — every analysis answer gets the Primary model (overhaul item 7): the cheap
    #    lane is for screening, a greeting, the follow-up rewrite and history summaries. The
    #    router still drops to it when the tenant's budget for the period is spent.
    purpose = llm.pick_answer_purpose(
        tenant_id, "simple" if gate.intent == "smalltalk" else "complex")
    diagnostic = gate.intent == "diagnostic"     # WHY question -> hypothesis guidance
    advise = gate.intent == "prescriptive"       # how-to-improve -> advisor guidance
    # Deep Research only when the caller asks for it; auto never routes there by regex
    deep = deep_analysis.should_use(analysis_mode) and bool(datasource)
    ctx = Ctx(tenant_id=tenant_id, role=role, role_policy=role_policy, question=message,
              user_id=user_id, datasource=datasource, answer_purpose=purpose,
              diagnostic=diagnostic, advise=advise, deep_analysis=deep,
              brainstorm=bool(getattr(gate, "brainstorm", False)) and not deep,
              semantic_contracts=semantic_contracts, business=biz, trace_id=trace_id,
              assumptions=readings)
    # Screening (and any follow-up rewrite + re-screen) already happened and is billed;
    # ctx.usage only exists now, which is why it used to be omitted from every turn's
    # reported tokens/cost. Recorded FIRST so the "which model wrote the answer" logic
    # (last entry) is unaffected.
    ctx.usage.extend(entry for entry in (*followup_usage, _gate_usage(gate)) if entry)
    set_cancellation_usage(ctx.usage)
    check_cancelled()

    # 2c) BRAND BASELINE (brand mode) — the fixed part of a brand analysis, cached per
    #     brand + month; the agent is told what it already holds so it queries the rest.
    brand_bases = []
    if brand_target:
        _emit({"type": "status", "text": "Menyiapkan analisa tetap %s (%s)…" % (
            " & ".join(brand_target.brands), brand_target.month.label)})
        brand_bases = brand_analysis.baselines(tenant_id, role, brand_target, check_cancelled)
        ctx.focus = brand_analysis.focus_text(brand_target, brand_bases)

    # 2b) CONVERSATION MEMORY (v2) — client-supplied history -> summary + recent turns per
    #     the tenant's Memory config (memory_type/context_window/summarization/max tokens).
    #     The agent loop and the synthesis get it as chat turns (overhaul item 8); the
    #     flattened block serves single-shot SQL, Deep Research and catalog pruning.
    if history:
        conv = memory.build_conversation(tenant_id, history, mem_cfg,
                                         usage_sink=lambda res: _record_usage(ctx, res),
                                         current=asked)
        if conv:
            ctx.conversation = conv.text
            ctx.chat_history = conv
            plan_prefix = ["conversation_history"]
        else:
            plan_prefix = []
    else:
        plan_prefix = []
    if interpreted:
        plan_prefix.insert(0, "contextualize_followup")

    # 3) THE AGENT — one loop over the tools this turn may use.
    plan, evidence = list(plan_prefix), {}
    if ctx.brainstorm:
        plan.append("brainstorm")
    if brand_target:
        plan.append(_baseline_plan(brand_bases))
    # long-term recall is now config-gated: persistent = the store exists at all,
    # cross_session_recall = it participates in chat (defaults keep pre-v2 behavior).
    want_memory = (user_id and "recall_memory" in allowed
                   and mem_cfg.get("persistent") and mem_cfg.get("cross_session_recall"))
    if want_memory:
        _emit({"type": "status", "text": "Menelusuri memori…"})
        plan.append("recall_memory"); evidence["memory"] = run_skill("recall_memory", ctx)
    agent_answer = None
    explored = False            # the agent called a tool: its last text is notes, not the reply
    sql_steps = []
    brand_tool = None
    pipeline_docs = False       # docs gathered OUTSIDE the agent force a merging synthesis
    logged = []                 # queries already audited the moment they ran (agentic)

    def _audit_now(step):
        _audit_queries(tenant_id, trace_id, role, user_id, [step])
        logged.append(step)

    if sql_agent.usable(tenant_id, purpose):
        # an open KPI phrase's meanings may live in other datasources: those come first
        meaning_ds = {c.get("datasource_id") for g in open_meanings for c in g["contracts"]}
        others = ([] if datasource_id or not datasource else
                  sorted((d for d in _sql_ds if d["id"] != datasource["id"]),
                         key=lambda d: d["id"] not in meaning_ds)[:_MAX_OTHER_DS])

        def chat_agent():
            """The one chat loop — the ordinary turn, and Deep Research's fallback when it
            stalls (same datasources, brand tool and progress statuses)."""
            if session:
                plan.append("agent_loop(agentic)")
                out = kb_agent.run(
                    ctx, session, enable_docs=docs_ok, emit=_emit, on_query=_audit_now,
                    seed_brand=(brand_target, brand_bases) if brand_target else None)
                return out.pop("brand_tool"), out
            tool = None
            if (datasource and brand_analysis.enabled()
                    and mbr_engine.has_brand_orders(tenant_id, role, (biz or {}).get("id"))):
                tool = brand_analysis.BrandTool(tenant_id, role, biz, message)
                if brand_target:
                    tool.seed(brand_target, brand_bases)
            plan.append("agent_loop")
            return tool, sql_agent.run_sql_agent(
                ctx, datasource, enable_docs=docs_ok, others=others, brand_tool=tool,
                lean=not data_intent, emit=_emit, ask_tool=bool(pending_ask))

        if deep and research_agent.usable(tenant_id, purpose):
            _emit({"type": "status", "text": "Memulai Deep Research adaptif…"})
            # Deep mode is a general adaptive loop: inspect -> act -> observe -> replan,
            # with an adversarial critic before synthesis. It can discover any authorized
            # datasource/table dynamically and is not constrained to a domain playbook.
            candidates = [datasource] + ([] if datasource_id else
                                         [d for d in _sql_ds if d["id"] != datasource["id"]])
            ar = research_agent.run_research(
                ctx,
                candidates,
                emit=_emit,
                enable_docs=docs_ok,
                enable_web=web_research.enabled(tenant_id),
            )
            research_meta = ar.get("research") or {}
            plan.append(
                "adaptive_research("
                f"{research_meta.get('rounds', 0)} rounds,"
                f"{research_meta.get('tool_counts', {}).get('run_sql', 0)} sql,"
                f"{research_meta.get('tool_counts', {}).get('web_search', 0)} web)"
            )
            # Deep research can end without a single governed query — a model that
            # replies in prose never opens the phase gate, so run_sql is never even
            # offered to it. Returning that no-evidence answer is worse than a plain
            # one, so fall back to the chat agent, which reaches SQL directly (and,
            # failing that, drops to single-shot below). Web search rides on SQL
            # evidence, so this is what unblocks it too.
            if not (ar.get("results") or ar.get("queries")):
                plan.append(
                    "adaptive_research(stalled:%s)"
                    % (research_meta.get("stop_reason") or "no_evidence")
                )
                _emit({"type": "status",
                       "text": "Deep Research tidak menghasilkan bukti — beralih ke SQL governed…"})
                brand_tool, ar = chat_agent()
        else:
            _emit({"type": "status", "text": "Menelusuri pertanyaan…"})
            brand_tool, ar = chat_agent()
        if session:
            # the scope is whatever the model opened: the rest of the turn reads that
            plan.extend(_compact_trace(ar.get("trace")))
            biz, datasource, _sql_ds = session.business, session.primary, list(session.sources)
            semantic_contracts, readings = list(session.contracts), list(session.readings)
            required = request_contract.build(message, _sql_ds) if _sql_ds else None
            if ar.get("ask"):
                _emit({"type": "status", "text": "Menanyakan yang perlu dipastikan…"})
                return _confirmation_result(tenant_id, trace_id, role, user_id, gate, ar["ask"],
                                            business=biz, usage=ctx.usage, plan=plan)
        if pending_ask and (ar.get("needs_business") or not ar.get("answer")):
            # the general question turned out to need this company's data (or no reply
            # came): now the business is asked, with what already ran billed
            _emit({"type": "status", "text": _ASKING_BUSINESS})
            return _confirmation_result(tenant_id, trace_id, role, user_id, gate, pending_ask,
                                        usage=ctx.usage, plan=plan)
        if pending_ask:
            plan.append("business_not_needed")
        if brand_tool is not None and brand_tool.cached:
            # brand_baseline found the analysis written for the same question, brands and
            # month within the cache window: the same answer, like the brand mode's
            _audit_queries(tenant_id, trace_id, role, user_id, _unlogged(ar.get("queries"), logged))
            assumptions.settle(readings, ar.get("queries"), brand_tool.target.brands)
            _emit({"type": "status", "text": "Memakai analisa brand yang sama seperti sebelumnya…"})
            return _brand_cached_result(tenant_id, trace_id, role, user_id, gate, biz,
                                        brand_tool.cached, interpreted, _emit,
                                        usage=ctx.usage, plan=plan, readings=readings)
        if ar.get("queries") or ar.get("research"):
            evidence["sql"] = ar.get("sql"); sql_steps = ar.get("queries", [])
            if ar.get("results"):             # ALL successful sets = distinct evidence
                evidence["sql_results"] = ar["results"]
            if ar.get("coverage", {}).get("required"):
                evidence["coverage"] = ar["coverage"]
            if ar.get("web"):
                evidence["web"] = ar["web"]
            if ar.get("research"):
                evidence["research"] = ar["research"]
        agent_answer = ar.get("answer")
        explored = bool(ar.get("explored"))
        if ar.get("docs"):                    # doc facts the agent pulled itself
            _merge_docs(evidence, ar["docs"])
        if not agent_answer and not ar.get("queries") and conversational:
            # no model reply at all (provider down): the plain reply, never rule-based SQL
            return _direct_result(tenant_id, trace_id, role, user_id, message, gate)
        if not agent_answer and not ar.get("queries") and data_intent and datasource:
            # the loop failed outright (no answer, no query): single-shot SQL instead
            plan.append("run_sql(fallback)"); evidence["sql"] = run_skill("run_sql", ctx)
            sql_steps = [evidence["sql"]] if evidence.get("sql") else []
    elif pending_ask:
        # no agent for this purpose to judge whether data is needed: ask, as before
        _emit({"type": "status", "text": _ASKING_BUSINESS})
        return _confirmation_result(tenant_id, trace_id, role, user_id, gate, pending_ask,
                                    usage=ctx.usage, plan=plan)
    else:
        # No tool-capable model: the deterministic pipeline, where the intent still picks
        # the sources — docs for document/how-to wording, SQL for a data intent.
        want_docs = (gate.intent == "doc_question" or bool(_DOC_CUE.search(message))
                     or bool(brand_target))
        if docs_ok and want_docs:
            _emit({"type": "status", "text": "Menelusuri dokumen…"})
            plan.append("search_docs"); evidence["docs"] = run_skill("search_docs", ctx)
            if gate.intent != "doc_question" and _docs_irrelevant(evidence["docs"]):
                plan[-1] = "search_docs(irrelevant)"; evidence.pop("docs", None)
            pipeline_docs = bool((evidence.get("docs") or {}).get("results"))
        if data_intent and datasource:
            _emit({"type": "status", "text": "Menulis & menjalankan SQL (governed)…"})
            plan.append("run_sql"); evidence["sql"] = run_skill("run_sql", ctx)
            sql_steps = [evidence["sql"]] if evidence.get("sql") else []

    # 3b) A BRAND ANALYSIS the agent reached on its own (it called brand_baseline): the
    #     answer stands on those baselines, and like every brand analysis it reads the
    #     knowledge base's documents too (campaigns, policies).
    if brand_tool is not None and brand_tool.bases and not brand_target:
        brand_target, brand_bases = brand_tool.target, brand_tool.bases
        plan.append(_baseline_plan(brand_bases))
    if (brand_target and docs_ok and "docs" not in evidence
            and not any(p.startswith("search_docs") for p in plan)):
        plan.append("search_docs"); evidence["docs"] = run_skill("search_docs", ctx)
        if _docs_irrelevant(evidence["docs"]):
            plan[-1] = "search_docs(irrelevant)"; evidence.pop("docs", None)
        pipeline_docs = bool((evidence.get("docs") or {}).get("results"))

    # 4) EVIDENCE CHECK / corrective retry — if SQL was blocked/failed but docs exist and
    #    weren't searched yet, lean on docs.
    sql_ev = evidence.get("sql")
    if sql_ev and not sql_ev.get("ok") and docs_ok and "docs" not in evidence:
        plan.append("search_docs(retry)"); evidence["docs"] = run_skill("search_docs", ctx)

    # 4b) WHAT-IF — a hypothetical %-change in the question ("kalau harga naik 10%?") gets
    #     its scenario values computed DETERMINISTICALLY over the derived totals and attached
    #     to the evidence, so the model can state simulated figures the verifier can trace
    #     (an LLM-computed scenario number would otherwise fail figures_traceable).
    wi = analytics.parse_what_if(message)
    if wi and sql_ev and sql_ev.get("ok") and sql_ev.get("derived"):
        sc = analytics.scenario(sql_ev["derived"], wi["signed_pct"], wi.get("phrase", ""))
        if sc:
            sql_ev["derived"]["scenario"] = sc
            plan.append("what_if_scenario")      # the synthesis below narrates it

    # 4c) EXTERNAL VALIDATION — quality-first decisions may validate candidate regions,
    # infrastructure, regulation, and benchmarks with Google Search grounding. Internal
    # SQL remains authoritative; PII-masked evidence only is sent to this optional service.
    if (deep and web_research.enabled(tenant_id) and evidence.get("sql")
            and "web" not in evidence and "research" not in evidence):
        check_cancelled()
        _emit({"type": "status", "text": "Memvalidasi kandidat dengan sumber web resmi…"})
        mdl = (ctx.datasource or {}).get("mdl") or {}
        web = web_research.research(
            tenant_id,
            message,
            _evidence_text(evidence, mdl)[:12000],
        )
        evidence["web"] = web
        web_usage = web.get("usage") or {}
        if web_usage.get("model"):
            ctx.usage.append({
                "model": web_usage["model"],
                "prompt_tokens": int(web_usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(web_usage.get("completion_tokens", 0) or 0),
                "cost": float(web_usage.get("cost", 0.0) or 0.0),
            })
        check_cancelled()
        plan.append(
            f"web_search:{web.get('provider')}({len(web.get('queries') or [])})"
            if web.get("ok")
            else f"web_search:{web.get('provider') or 'none'}(failed)"
        )

    # the request contract is about data: a turn the gatekeeper did not read as a data
    # question that then ran no query (a conversation, a document answer) owes no column
    # (a brainstorm owes ideas, not a breakdown, unless it went and queried)
    if required and (not data_intent or ctx.brainstorm) and not sql_steps:
        required = None
    # a turn that ran SQL is judged as one, whatever the intent label said — and so is an
    # agentic turn that went looking in the data (a lookup, a table read) and came back
    # without a result: "not found" is an outcome of a data question, never a discussion
    searched = bool(session) and (session.looked or any(
        step.split(":")[0] in _DATA_STEPS for step in session.trace))
    needs_sql = (data_intent and not ctx.brainstorm) or bool(sql_steps) or searched

    # 5) SYNTHESIZE — the answer is written AFTER the exploration (overhaul item 7): a
    #    streamed call with the whole evidence, the agent's working notes and room for the
    #    analysis (CHAT_ANSWER_MAX_TOKENS). Only a turn where the agent used no tool (a
    #    greeting, a discussion) keeps its own reply, unless docs/memory gathered outside
    #    the agent must be merged in.
    check_cancelled()
    _emit({"type": "status", "text": "Menyusun jawaban…"})
    pipeline_docs = pipeline_docs or "search_docs(retry)" in plan
    mem_hit = bool(evidence.get("memory", {}).get("results"))
    answer_streamed = False
    brand_out = None
    if brand_target:
        _emit({"type": "status", "text": "Menyusun analisa dan hipotesis…"})
        brand_out = brand_analysis.compose(ctx, brand_target, brand_bases, evidence, message)
    if brand_out:
        answer = brand_out["text"]
        charts = _viz_blocks(agent_answer)      # a chart the agent drew stays with the analysis
        if charts:
            answer += "\n\n" + "\n\n".join(charts)
        plan.append("brand_analysis(%d flagged)" % len(brand_out["flagged"]))
    elif agent_answer and not explored and not pipeline_docs and not mem_hit and not deep:
        answer = agent_answer
    else:
        # emit=_emit makes synthesis STREAM: answer_delta events flow live while the
        # model writes, so the user reads the answer as it is composed instead of
        # staring at "Menyusun jawaban…" until the full completion returns.
        if agent_answer and not deep:
            ctx.exploration = agent_answer
        plan.append("synthesis")
        report = run_skill("generate_report", ctx, evidence=evidence, emit=_emit)
        answer = report["text"]
        answer_streamed = bool(report.get("streamed"))
        if report.get("fallback") and agent_answer and not deep:
            # no model wrote the answer: the agent's own text says more than the template
            answer, answer_streamed = agent_answer, False

    # 6) VERIFY — citations / PII / grounding / figure-traceability / explicit
    #    requirements. It only REPORTS (overhaul item 6): a rewrite used to delete or
    #    reword sentences the person never saw being changed. A figure it cannot match to
    #    the evidence stays in the answer and is named in `unverified_figures`.
    check_cancelled()
    vr = verify(message, answer, evidence, gate, required, brainstorm=ctx.brainstorm)
    if brand_out:
        # its own figure check (every number, against the baseline AND this turn's
        # evidence) is stricter and saw the baseline, so it settles the figure verdict
        vr = brand_analysis.settle_verdict(vr, brand_bases, brand_out)

    # 6a) OUTCOME (phase 3) — what this turn actually DELIVERED, as a typed status
    #     (complete | partial | no_data | error). The confirmation and blocked paths
    #     return their own statuses before this point.
    #     A discussion (no query, no business figure) is complete — unless the question
    #     needed data and none was reachable this turn (no SQL datasource to ask).
    if searched and vr.get("basis") == "general_knowledge":
        vr = {k: v for k, v in vr.items() if k != "basis"}   # not a general-knowledge reply
    # A brainstorm with no query is a discussion too, data open or not: ideas need none.
    discussion = ((vr.get("basis") == "general_knowledge"
                   and (not data_intent or bool(datasource)))
                  or vr.get("basis") == "brainstorm")
    outcome = answer_contract.classify(required, evidence, answer, needs_sql, discussion)
    if searched and outcome["status"] == "error" and (answer or "").strip():
        # it looked and found nothing usable, and said so: no data, not a failure
        outcome = {"status": "no_data", "missing_requirements": []}
    unfound = _unfound_terms(session, evidence) if searched else []
    if unfound and outcome["status"] == "complete":
        # a name the question is about was never found in the data, and no query that
        # returned rows filtered on it: the answer is about something else (staging
        # "Milkita": rows of five other brands, marked complete)
        outcome = {"status": "partial",
                   "missing_requirements": [f"value:{t}" for t in unfound]}
    if brand_out and brand_bases and outcome["status"] in ("error", "no_data"):
        # the fixed baseline is governed SQL too: the analysis stands on it even when
        # this question's own queries found nothing
        missing = answer_contract.missing_requirements(required, evidence, answer)
        outcome = {"status": "partial" if missing else "complete",
                   "missing_requirements": missing}

    # Streaming already happened live during synthesis (answer_streamed), and the text the
    # user watched IS the answer: nothing rewrites it now, so answer_reset only comes from
    # a model fallback inside the synthesis. The other paths (a reply with no tool, brand
    # analysis, template fallback) never streamed, so reveal them as paced deltas.
    if not answer_streamed:
        _stream_text(_emit, answer)

    # 6b) CONFIDENCE — deterministic aggregation of signals already in hand (verifier
    #     checks, unmatched figures, SQL row support, doc rerank strength). No LLM call.
    confidence = compute_confidence(vr, evidence, gate)
    if brand_out:
        mode_used = brand_analysis.MODE
    elif ctx.brainstorm:
        mode_used = "brainstorm"
    else:
        mode_used = "deep" if deep else "fast"
    brand_meta = (brand_analysis.meta(brand_target, brand_bases, brand_out)
                  if brand_out else None)

    # aggregate token/cost usage from all LLM calls in this turn
    total = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0, "model": None}
    for u in ctx.usage:
        total["prompt_tokens"] += u["prompt_tokens"]; total["completion_tokens"] += u["completion_tokens"]
        total["cost"] += u["cost"]; total["model"] = u["model"]
    total["cost"] = round(total["cost"], 6)
    total["web_search_queries"] = len((evidence.get("web") or {}).get("queries") or [])
    # which model actually wrote this answer + the full roster for the UI badge
    answer_model, models_used = _models_used(gate.engine, ctx.usage)
    datasources_used = []
    datasource_results = list(evidence.get("sql_results") or [])
    primary_result = evidence.get("sql")
    if primary_result and not any(result is primary_result for result in datasource_results):
        datasource_results.append(primary_result)
    for result in datasource_results:
        datasource = {
            "id": result.get("datasource_id"),
            "name": result.get("datasource_name"),
            "type": result.get("datasource_type"),
        }
        if datasource["id"] and datasource not in datasources_used:
            datasources_used.append(datasource)

    # which reading the turn took, read from what it RAN (never from what it says)
    assumptions.settle(readings, sql_steps, brand_target.brands if brand_target else ())

    # AUDIT — full per-step trail (each governed SQL + the final answer)
    check_cancelled()
    _audit_queries(tenant_id, trace_id, role, user_id, _unlogged(sql_steps, logged))
    for hypothesis in (evidence.get("research") or {}).get("hypotheses") or []:
        store.log_audit(
            tenant_id, trace_id, "hypothesis",
            role=role, user_id=user_id, status=hypothesis.get("status"),
            detail=hypothesis,
        )
    # A dashboard counting successful answers must not count a partial or empty one as
    # "ok" (doc §11.3) — the audit status IS the delivered outcome now.
    store.log_audit(tenant_id, trace_id, "answer", role=role, user_id=user_id, model=total.get("model"),
                    status=outcome["status"], cost=total.get("cost", 0.0),
                    detail={"answer": (answer or "")[:2000], "plan": plan,
                            "confidence": confidence["level"], "verifier_ok": vr.get("ok"),
                            "unverified_figures": vr.get("unverified_figures") or None,
                            "proposed_figures": vr.get("proposed_figures") or None,
                            "missing_requirements": outcome["missing_requirements"],
                            "request_contract": required,
                            "assumptions": assumptions.audit(readings) or None,
                            "analysis_mode": mode_used,
                            "brand_analysis": ({"brands": brand_meta["brands"],
                                                "period": brand_meta["period"],
                                                "flagged": brand_meta["flagged"]}
                                               if brand_meta else None),
                            "coverage": evidence.get("coverage"),
                            "research": {
                                "completed": (evidence.get("research") or {}).get("completed"),
                                "stop_reason": (evidence.get("research") or {}).get("stop_reason"),
                                "tool_counts": (evidence.get("research") or {}).get("tool_counts"),
                                "layers": (evidence.get("research") or {}).get("layers"),
                                "hypotheses": (evidence.get("research") or {}).get("hypotheses"),
                                "profiles": (evidence.get("research") or {}).get("profiles"),
                                "semantic_checks": (
                                    evidence.get("research") or {}
                                ).get("semantic_checks"),
                                "tool_trace": [
                                    {
                                        "round": item.get("round"),
                                        "tool": item.get("tool"),
                                        "status": item.get("status"),
                                    }
                                    for item in (evidence.get("research") or {}).get("tool_trace", [])
                                ],
                            } if evidence.get("research") else None,
                            "datasources_used": datasources_used,
                            "semantic_contracts": semantic_contracts,
                            "prompt_tokens": total["prompt_tokens"], "completion_tokens": total["completion_tokens"]})

    # 7) AUTO-MEMORY (v2) — the AI writes its own durable notes about this user.
    #    Fire-and-forget thread (no reply latency); sync in tests via MEMORY_CAPTURE_SYNC=1.
    captured = None
    check_cancelled()
    if user_id and (answer or "").strip() and mem_cfg.get("auto_memory") and mem_cfg.get("persistent"):
        plan.append("auto_memory")
        captured = memory.schedule_capture(principal, user_id, message, answer, trace_id, mem_cfg)

    web_sources = (evidence.get("web") or {}).get("sources") or []
    citations = (
        evidence.get("docs", {}).get("citations", [])
        + [source.get("url") for source in web_sources if source.get("url")]
    )
    sql_view = _sql_view(sql_ev, evidence.get("sql_results"))
    # a FAILED exploratory SQL attempt is pure noise when the docs already answered the
    # question — it stays in the audit trail (logged above) but is dropped from the reply.
    if sql_view and not sql_view.get("ok") and citations and (answer or "").strip():
        sql_view = None
    result = {"answer": answer, "blocked": False, "trace_id": trace_id, "plan": plan, "gatekeeper": _gate_dict(gate),
            "engine": kb_agent.AGENTIC if session else kb_agent.CLASSIC,
            # what this turn delivered: complete | partial | no_data | error — a question
            # answered without its requested breakdown must SAY so, not read as complete
            "status": outcome["status"],
            "missing_requirements": outcome["missing_requirements"],
            # figures in the answer the verifier could not match to this turn's evidence:
            # they stay in the text, and the clients say how many (overhaul item 6)
            "unverified_figures": vr.get("unverified_figures") or [],
            # a brainstorm's own proposals (a discount to test, a voucher value): not
            # from the data, and the clients say so
            "proposed_figures": vr.get("proposed_figures") or [],
            # readings the turn took instead of asking (item 5): the answer's first
            # sentence says which; each other option's `question` is the correction
            "assumptions": assumptions.public(readings),
            # which slice answered — the console shows it and keeps sending it, and an
            # auto-selected single business is otherwise invisible to the caller
            "business": {"id": biz["id"], "name": biz["name"]} if biz else None,
            # the self-contained question a follow-up was answered AS (None = as typed),
            # so a client can show what the turn was taken to mean
            "interpreted_question": interpreted["question"] if interpreted else None,
            "captured_memory": captured,        # list when MEMORY_CAPTURE_SYNC=1; None = scheduled in background
            "router": {"purpose": purpose, "complexity": gate.complexity,
                       "model": answer_model, "models": models_used,
                       "analysis_mode": mode_used},
            "analysis_mode": mode_used,
            "brand_analysis": brand_meta,
            "coverage": evidence.get("coverage"),
            "research": evidence.get("research"),
            "hypotheses": (evidence.get("research") or {}).get("hypotheses") or [],
            "semantic_contracts": semantic_contracts,
            "datasources_used": datasources_used,
            "web_sources": web_sources,
            "sql": sql_view, "citations": citations,
            "recalled_memory": evidence.get("memory", {}).get("results", []),
            "documents": evidence.get("docs", {}).get("results", []),
            "verifier": vr, "confidence": confidence,
            "evidence_summary": _evidence_summary(evidence), "usage": total}
    if brand_out and result["status"] in ("complete", "partial"):
        brand_analysis.remember_answer(tenant_id, role, brand_target, message, result)
    return result


def _evidence_summary(evidence):
    """Consolidated 'Evidence: [...]' bundle for the response — one entry per source the
    answer stands on (each governed query, each cited document, each recalled memory).
    Pure reshaping of data already gathered this turn."""
    out = []
    results = [r for r in (evidence.get("sql_results") or []) if r and r.get("ok")]
    prim = evidence.get("sql")
    if prim and prim.get("ok") and not any(r is prim for r in results):
        results.append(prim)
    for r in results:
        out.append({"type": "sql", "safe_sql": r.get("safe_sql"),
                    "row_count": len(r.get("rows") or []),
                    "datasource_id": r.get("datasource_id"),
                    "datasource_name": r.get("datasource_name")})
    for d in (evidence.get("docs") or {}).get("results") or []:
        entry = {"type": "document", "label": d.get("citation_label") or d.get("citation")}
        if d.get("rerank_score") is not None:
            entry["score"] = round(d["rerank_score"], 3)
        out.append(entry)
    for m in (evidence.get("memory") or {}).get("results") or []:
        out.append({"type": "memory", "text": (m.get("text") or "")[:100]})
    for source in (evidence.get("web") or {}).get("sources") or []:
        out.append({"type": "web", "label": source.get("label"), "url": source.get("url")})
    return out


def _gate_dict(gate):
    return {"allow": gate.allow, "intent": gate.intent, "complexity": gate.complexity,
            "injection": gate.injection, "pii_request": gate.pii_request,
            "reasons": gate.reasons, "engine": gate.engine,
            "brainstorm": bool(getattr(gate, "brainstorm", False))}


def _sql_view(sql_ev, all_results=None):
    if not sql_ev:
        return None
    if sql_ev.get("ok"):
        view = {"ok": True, "engine": sql_ev["engine"], "generated_sql": sql_ev["generated_sql"],
                "safe_sql": sql_ev["safe_sql"], "columns": sql_ev["columns"], "rows": sql_ev["rows"],
                "datasource_id": sql_ev.get("datasource_id"),
                "datasource_name": sql_ev.get("datasource_name"),
                "datasource_type": sql_ev.get("datasource_type")}
        if sql_ev.get("derived"):
            view["derived"] = sql_ev["derived"]
        # additive: when the agent ran SEVERAL successful queries, expose each set so the
        # UI/consumer can show a comparison. Rows capped per set to bound the payload;
        # the primary table above stays uncapped (existing behavior).
        extra = [r for r in (all_results or []) if r and r.get("ok") and r is not sql_ev]
        if extra:
            view["results"] = [{"safe_sql": r["safe_sql"], "columns": r["columns"],
                                "rows": r["rows"][:200], "row_count": len(r["rows"]),
                                "datasource_id": r.get("datasource_id"),
                                "datasource_name": r.get("datasource_name"),
                                "datasource_type": r.get("datasource_type"),
                                **({"derived": r["derived"]} if r.get("derived") else {})}
                               for r in extra]
        return view
    return {"ok": False, "engine": sql_ev.get("engine"), "generated_sql": sql_ev.get("generated_sql"),
            "code": sql_ev.get("code"), "error": sql_ev.get("error")}
