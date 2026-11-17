"""The chat agent: one tool-loop for every chat turn (overhaul item 3).

The model gets the tools this turn may use and decides itself which to call: `run_sql`
(governed + cost-guarded + executed by the backend, rows or the error fed back so it
can SELF-CORRECT), `describe_table`, `search_catalog` + `profile_table` (shared with Deep
Research: a business term whose table is not in the pruned catalog is searched for before
the data is called missing), `search_docs`, `brand_baseline`. A greeting or an
unclear message is answered with no tool at all. The gatekeeper screens safety before
this; its intent no longer picks a path. Without a SQL datasource (or the run_sql
right) the loop runs without the data tools. When several SQL datasources are in the
business slice, run_sql / describe_table take a `datasource` id, and each query is
governed against THAT source (what the bounded diagnostic planner used to do).

Returns the last SUCCESSFUL query as the primary SQL evidence (drives the UI
table/chart) plus every success and every attempt. Falls back to single-shot run_sql
when the tenant has no tool-capable model (caller handles that).

Overhaul item 7: the loop explores, it does not write the answer. A turn that used a
tool ends with the model's working notes (``explored`` True); the orchestrator then
writes the answer in a streamed synthesis call with room for the whole analysis. A turn
that needed no tool (a greeting) is answered by the loop directly. Turns and per-turn
tokens come from CHAT_AGENT_MAX_TURNS (10) and CHAT_AGENT_TURN_TOKENS (4096).
"""
from __future__ import annotations

import json
import os

from .. import assumptions, discovery, llm, memory
from ..nl2sql import (catalog_for_prompt, catalog_index, describe_tables, now_context,
                      rank_tables, VIZ_INSTRUCTIONS, INSIGHT_INSTRUCTIONS,
                      HYPOTHESIS_INSTRUCTIONS, ADVISOR_INSTRUCTIONS, KNOWLEDGE_INSTRUCTIONS,
                      NUMBER_FORMAT_INSTRUCTIONS, PERIOD_SQL_INSTRUCTIONS,
                      BRAINSTORM_INSTRUCTIONS, brainstorm_temperature, measure_instructions)
from ..skills.registry import (execute_governed, _record_usage, mask_pii_for_llm,
                               TextPiiRedactor, find_documents)
from .research_agent import can_profile, catalog_search, visible_mdl



def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


MAX_TURNS = _int_env("CHAT_AGENT_MAX_TURNS", 10, 2, 30)
# a cap, not a spend: reasoning models think inside it, and 1,500 left them too little room
# for a turn that writes SQL after reading a large result
TURN_TOKENS = _int_env("CHAT_AGENT_TURN_TOKENS", 4096, 1000, 32000)

SEARCH_DOCS_TOOL = {
    "type": "function",
    "function": {
        "name": "search_docs",
        "description": ("Search the tenant's uploaded documentation (SOPs, decks, reports) for a fact, "
                        "target, or policy relevant to the analysis. Returns excerpts with bracketed "
                        "citation labels you MUST copy verbatim when you use their content."),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to look for in the documents."}},
            "required": ["query"],
        },
    },
}

DESCRIBE_TABLE_TOOL = {
    "type": "function",
    "function": {
        "name": "describe_table",
        "description": ("Get the full schema (columns, descriptions, grain rules, valid dimension "
                        "values, example queries) for tables listed by name only in the context. "
                        "Call this BEFORE querying such a table — never guess its columns."),
        "parameters": {
            "type": "object",
            "properties": {"tables": {"type": "array", "items": {"type": "string"},
                                      "description": "Exact table names to describe (max 5)."}},
            "required": ["tables"],
        },
    },
}

RUN_SQL_TOOL = {
    "type": "function",
    "function": {
        "name": "run_sql",
        "description": ("Execute ONE read-only SQL SELECT to fetch real numbers. The query is "
                        "validated (SELECT-only, allowlist, no PII, cost dry-run) then run. Use the "
                        "exact column names and metric definitions from the context. Returns result "
                        "rows (JSON) on success, or an ERROR string you must read, fix, and retry."),
        "parameters": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "A valid read-only SELECT statement."}},
            "required": ["sql"],
        },
    },
}

# Offered only on a general question asked before any business was picked: the turn runs
# with no company data, and this is how the agent says it needs some after all. The person
# is then asked which business; the agent never picks one.
NEED_DATA_TOOL = {
    "type": "function",
    "function": {
        "name": "need_company_data",
        "description": ("Call when a good answer needs this company's own figures or records "
                        "(its sales, brands, clients, stock, orders). The person is then asked "
                        "which business to read, and the question is answered from that data."),
        "parameters": {"type": "object", "properties": {}},
    },
}

SEARCH_CATALOG_TOOL = {
    "type": "function",
    "function": {
        "name": "search_catalog",
        "description": ("Search the catalog of every table you may query for the ones that fit a "
                        "business term (ads, ROAS, traffic, conversion, stock, ...). Use it when no "
                        "table in the context fits, BEFORE saying the data is missing. Returns table "
                        "names, descriptions and a preview of their columns."),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string",
                                     "description": "The business terms to look for."}},
            "required": ["query"],
        },
    },
}

PROFILE_TABLE_TOOL = {
    "type": "function",
    "function": {
        "name": "profile_table",
        "description": ("Look at a small sample of one table to learn what its columns hold "
                        "(empty share, typical values, ranges). For understanding a column only: "
                        "every figure in the answer still comes from run_sql."),
        "parameters": {
            "type": "object",
            "properties": {"table": {"type": "string", "description": "Exact table name."},
                           "columns": {"type": "array", "items": {"type": "string"},
                                       "description": "Optional exact columns (max 24)."}},
            "required": ["table"],
        },
    },
}

_DATASOURCE_ARG = {"type": "string", "description": (
    "Id of the datasource to use, from OTHER DATASOURCES. Leave it out for the main one.")}


def _with_datasource(tool: dict, arg: dict = None) -> dict:
    """The tool with an optional `datasource` argument (several SQL datasources)."""
    fn = tool["function"]
    params = fn["parameters"]
    return {"type": "function", "function": {
        **fn, "parameters": {**params, "properties": {**params["properties"],
                                                       "datasource": arg or _DATASOURCE_ARG}}}}


def _others_block(others: list, question: str) -> str:
    """The other SQL datasources of the slice: id, name and the tables that fit the
    question best (names only; describe_table with that id gives the schema)."""
    lines = ["- OTHER DATASOURCES you may also query: pass the id as `datasource` to "
             "run_sql and describe_table, and call describe_table before querying one of "
             "their tables (each query is governed against that datasource):"]
    for d in others:
        mdl = d.get("mdl") or {}
        tables = rank_tables(mdl, question or "", limit=12) or list(mdl.get("tables") or {})[:12]
        lines.append(f"  - {d['id']} ({d.get('name') or d['id']}): tables "
                     + (", ".join(tables) or "none"))
    return "\n".join(lines) + "\n"


_CHAT_HEAD = (
    "You are Poros, the company's analytics assistant. Talk with the person the way a capable "
    "analyst colleague would. Decide yourself what each message needs: a greeting, a thank-you, "
    "a question about what you can do or a general business question needs no tool, so answer it "
    "directly; when a request is unclear, say what you understood and ask what you need to know. "
    "Use the tools when the answer depends on this company's data or documents.\n")

# the loop explores; the answer is written after it, from these notes and every result
_NOTES_RULE = (
    "- After you used any tool, finish with short working notes instead of the reply: the "
    "figures that answer the question, what they suggest, which table and columns they came "
    "from, the reading you took and what is still open. The person's answer is written next "
    "from your notes and every result, with room for the whole analysis. When you used no "
    "tool, reply to the person directly.\n")


DATA_RULES = (
    "WHEN YOU USE DATA:\n"
    "- Numbers MUST come from the run_sql tool — never invent them.\n"
    "- Use ONLY the tables/columns + metric definitions in the context; never invent columns.\n"
    "- NEVER query INFORMATION_SCHEMA or any system catalog — the schema is fully described in the "
    "context and such queries are always rejected.\n"
    "- Filter using the VALID DIMENSION VALUES exactly; follow GRAIN/DEDUP rules to avoid "
    "double-counting.\n"
    "- ONE simple SELECT per run_sql call. Do NOT use UNION ALL to combine different "
    "breakdowns/shapes.\n"
    "- If run_sql returns an ERROR, READ it, FIX the SQL, and call run_sql again.\n"
    "- Be efficient: once you have what you need, STOP calling tools.\n"
    + PERIOD_SQL_INSTRUCTIONS + "\n"
    "- The result table is ALREADY shown to the user, so never repeat every row or re-print the "
    "result as a markdown table.\n")


def _system_prompt(mdl: dict | None, diagnostic: bool = False, docs_tool: bool = False,
                   advise: bool = False, question: str = None, deep: bool = False,
                   others: list = None, brand_tool: bool = False, lean: bool = False,
                   profile: bool = False, ask_tool: bool = False,
                   brainstorm: bool = False) -> str:
    """The chat agent's instructions. ``mdl`` None = no SQL this turn; ``lean`` = names-only
    catalog (a turn the gatekeeper read as conversation or a document question: the full
    catalog would be thousands of tokens the model most likely never reads)."""
    extra = _extra_rules(mdl, diagnostic, docs_tool, advise, question, others, brand_tool,
                         profile, brainstorm)
    rules, catalog = _data_rules(mdl, ask_tool, lean, question, deep)
    return (
        now_context() + "\n"
        + _CHAT_HEAD + rules
        + (_NOTES_RULE if mdl is not None or docs_tool or brand_tool else "")
        + "- Let the SHAPE follow the question: a one-number ask gets a short direct "
        "answer; an analytical one gets what it needs (comparisons, what drives the result, what it "
        "means, what to do). Reply ENTIRELY in the SAME language as the user's question (the whole "
        "answer), even when the schema is in another language; use **bold** for key figures.\n"
        + extra
        + KNOWLEDGE_INSTRUCTIONS + "\n"
        + INSIGHT_INSTRUCTIONS + "\n"
        + NUMBER_FORMAT_INSTRUCTIONS
        + ("\n" + VIZ_INSTRUCTIONS if mdl is not None else "")
        + catalog
    )


def _extra_rules(mdl, diagnostic, docs_tool, advise, question, others, brand_tool,
                 profile, brainstorm=False) -> str:
    """The rules that depend on what this turn offers: docs, data, other datasources, the
    brand baseline, diagnostic/advice mode and the value-vs-count measure."""
    extra = ""
    if docs_tool:
        extra += ("- You may call search_docs to look up a fact/target/policy in the tenant's documents; "
                  "when you use document content, cite its EXACT bracketed label (e.g. "
                  "[report.pptx — Slide 3]) inline in your answer.\n")
    if mdl is not None:
        extra += ("- If a needed table is listed by name only, call describe_table for its "
                  "schema BEFORE querying it.\n"
                  "- Map business terms to columns with what you know of the business: ROAS = "
                  "ads GMV / ads cost, conversion = orders / visitors, AOV = value / orders, "
                  "and so on. When no table in the context fits, call search_catalog (then "
                  "describe_table" + (" or profile_table" if profile else "")
                  + ") BEFORE you say the data is missing.\n"
                  "- When you used data, name in one short sentence, in business words, which "
                  "table and columns the figures come from (e.g. \"from the ads table: ads cost "
                  "and ads GMV\").\n")
    if others:
        extra += _others_block(others, question)
    if brand_tool:
        extra += ("- brand_baseline gives a brand's fixed key numbers for one month; call it when "
                  "a question analyses how a brand did or why, then query only what it does not "
                  "cover.\n")
    if diagnostic:
        extra += HYPOTHESIS_INSTRUCTIONS + "\n"
    if brainstorm:
        extra += BRAINSTORM_INSTRUCTIONS + "\n"
    elif advise or diagnostic:
        extra += ADVISOR_INSTRUCTIONS + "\n"
    # 'how much' (value) vs 'how many' (count) decides WHICH measure column to SUM — this
    # must reach the SQL-writing prompt, not only the narrative.
    measure = measure_instructions(question)
    if measure:
        extra += measure + "\n"
    return extra


def _data_rules(mdl, ask_tool, lean, question, deep) -> tuple[str, str]:
    """(rules, catalog block): no data open yet, no database at all, or the slice's
    schema (names only when ``lean``)."""
    if mdl is None and ask_tool:
        return ("- No business data is open yet in this turn: never state a figure about this "
                "business. Answer from general knowledge only when the question is general. "
                "When a good answer needs this company's own figures or records, call "
                "need_company_data instead of answering (the person then picks which business "
                "to read).\n"), ""
    if mdl is None:
        return ("- You have no database access in this conversation: never state a figure about "
                "this business; when a question needs data, say so and what would answer it.\n"), ""
    if lean:
        context = catalog_index(mdl)
    else:
        context = catalog_for_prompt(mdl, question=question, keep_n=16 if deep else None)
    return DATA_RULES, "\n\n=== CONTEXT (schema + metric catalog) ===\n" + context


def usable(tenant_id: str, purpose: str = "answer_complex") -> bool:
    """Whether the resolved model chain can use Poros's tool-loop contract.

    The gateway uses native function calls when available and structured JSON actions
    otherwise, so model choice no longer changes the SQL-agent flow.
    """
    return llm.available() and llm.supports_tools(tenant_id, purpose)


def _tools(ds, others, enable_docs, brand_tool, profile=False, ask_tool=False) -> list:
    tools = [NEED_DATA_TOOL] if ask_tool and ds is None else []
    if ds is not None:
        data = [RUN_SQL_TOOL, DESCRIBE_TABLE_TOOL] + ([PROFILE_TABLE_TOOL] if profile else [])
        tools += [_with_datasource(t) for t in data] if others else data
        tools.append(SEARCH_CATALOG_TOOL)
    if enable_docs:
        tools.append(SEARCH_DOCS_TOOL)
    if brand_tool is not None:
        from .brand_analysis import TOOL as BRAND_TOOL
        tools.append(BRAND_TOOL)
    return tools


_STATUS = {"run_sql": "Menulis & menjalankan SQL (governed)…",
           "search_docs": "Menelusuri dokumen…",
           "search_catalog": "Menelusuri katalog data…",
           "profile_table": "Melihat isi tabel…",
           "brand_baseline": "Menyiapkan analisa tetap brand…"}


def run_sql_agent(ctx, ds: dict | None, max_turns: int = None, enable_docs: bool = False,
                  others: list = None, brand_tool=None, lean: bool = False,
                  emit=None, ask_tool: bool = False) -> dict:
    """Run the chat agent. Returns {ok, sql: <primary sql_ev|None>, results: [all
    successes], queries: [...], docs: {results, citations}|None, answer: str|None}. ALL
    successful result sets are kept (not only the last): a two-query period comparison
    needs both sets as distinct evidence for the synthesis prompt AND the verifier.

    ``ds`` None runs the loop without the data tools; ``others`` are further SQL
    datasources of the slice (run_sql takes their id); ``enable_docs`` adds search_docs;
    ``brand_tool`` (brand_analysis.BrandTool) adds brand_baseline and collects what it
    fetched. ``emit`` gets one progress status the first time each tool is used.
    ``ask_tool`` (with ``ds`` None) adds need_company_data; calling it ends the loop with
    ``needs_business`` True and no answer."""
    emit = emit or (lambda *_: None)
    announced = set()
    others, by_id = _datasources(ds, others)
    redactor = TextPiiRedactor()
    profile = ds is not None and can_profile(ctx)
    ask_tool = ask_tool and ds is None
    tools = _tools(ds, others, enable_docs, brand_tool, profile, ask_tool)
    messages = _opening(ctx, ds, others, redactor, enable_docs=enable_docs,
                        brand_tool=brand_tool, lean=lean, profile=profile, ask_tool=ask_tool)
    found = Findings()
    answer = None
    explored = False      # a tool was called: the final text is notes, not the reply
    needs_business = False
    turns = max_turns or MAX_TURNS

    for turn in range(turns):
        res = _model_turn(ctx, messages, tools, last=turn == turns - 1)
        if res is None:
            break
        if not res.tool_calls:                       # model wrote its final text → done
            answer = res.text or answer
            break
        if _asks_for_business(res, ask_tool):
            needs_business = True                    # the person picks the business next
            answer = None
            break
        explored = True

        # echo the assistant tool-call turn back (OpenAI format) so the model keeps context
        messages.append(_echo_calls(res))
        messages.extend(_call_reply(ctx, tc, found, ds, by_id, announced, emit,
                                    profile=profile, enable_docs=enable_docs,
                                    brand_tool=brand_tool, redactor=redactor,
                                    engine=f"llm:{res.model}")
                        for tc in res.tool_calls)
        if _brand_reused(brand_tool):
            break                     # the same brand analysis was written before: reuse it

    return found.result(answer, explored, needs_business=needs_business)


def _model_turn(ctx, messages, tools, last: bool):
    """One model call of the loop (no tools on the last turn: it must answer); None when
    the provider is down."""
    try:
        res = llm.complete_tools(ctx.tenant_id, ctx.answer_purpose, messages,
                                 [] if last else tools,
                                 temperature=loop_temperature(ctx), max_tokens=TURN_TOKENS)
    except llm.LLMUnavailable:
        return None
    _record_usage(ctx, res)
    return res


def loop_temperature(ctx) -> float:
    """0 for an analysis turn (same SQL for the same question); a brainstorm gets spread."""
    return brainstorm_temperature() if getattr(ctx, "brainstorm", False) else 0


def _datasources(ds, others) -> tuple[list, dict]:
    """The other SQL datasources (never the main one; none without a main one) and every
    datasource the tools may name, by id."""
    others = [d for d in (others or []) if ds is not None and d.get("id") != ds.get("id")]
    by_id = {d.get("id"): d for d in ([ds] if ds is not None else []) + others}
    return others, by_id


def _user_message(ctx, redactor) -> str:
    user_msg = redactor.redact(ctx.question)
    # a brand analysis turn: the brand as stored, the month, and the baseline it already has
    if getattr(ctx, "focus", ""):
        user_msg = f"{redactor.redact(ctx.focus)}\n\n{user_msg}"
    # readings the server found open instead of asking: the model takes one and says so
    readings = assumptions.prompt_block(getattr(ctx, "assumptions", None))
    if readings:
        user_msg = f"{redactor.redact(readings)}\n\n{user_msg}"
    return user_msg


def _opening(ctx, ds, others, redactor, *, enable_docs, brand_tool, lean, profile,
             ask_tool) -> list:
    """The loop's first messages: the system prompt, the chat so far and this turn."""
    # the chat so far arrives as real turns before this one (overhaul item 8)
    history = getattr(ctx, "chat_history", None)
    user_msg = _user_message(ctx, redactor)
    # catalog pruning signal: the question + the WHOLE conversation block (a follow-up like
    # "gabungkan keduanya dalam satu dashboard" carries no table vocabulary on its own —
    # earlier turns do; the block is already bounded upstream)
    prune_q = " ".join(filter(None, [ctx.question, getattr(ctx, "conversation", "") or ""]))
    messages = [{"role": "system", "content": redactor.redact(
                    _system_prompt(visible_mdl(ctx, ds) if ds is not None else None,
                                   diagnostic=bool(getattr(ctx, "diagnostic", False)),
                                   docs_tool=enable_docs,
                                   advise=bool(getattr(ctx, "advise", False)),
                                   question=prune_q,
                                   deep=bool(getattr(ctx, "deep_analysis", False)),
                                   others=[{**d, "mdl": visible_mdl(ctx, d)} for d in others],
                                   brand_tool=brand_tool is not None,
                                   lean=lean, profile=profile, ask_tool=ask_tool,
                                   brainstorm=bool(getattr(ctx, "brainstorm", False)))
                    + (memory.HISTORY_RULE if history else ""))}]
    messages += memory.as_messages(history, redactor.redact, {"role": "user", "content": user_msg})
    return messages


def _asks_for_business(res, ask_tool) -> bool:
    return ask_tool and any(tc.get("name") == "need_company_data" for tc in res.tool_calls)


def _brand_reused(brand_tool) -> bool:
    return brand_tool is not None and brand_tool.cached


def _echo_calls(res) -> dict:
    return {
        "role": "assistant", "content": res.text or "",
        "tool_calls": [{"id": tc["id"], "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                       for tc in res.tool_calls],
    }


def _call_reply(ctx, tc, found, ds, by_id, announced, emit, *, profile, enable_docs,
                brand_tool, redactor, engine) -> dict:
    """The tool message for one call; the first use of each tool announces a status."""
    name, args = tc.get("name"), tc.get("arguments") or {}
    if name in _STATUS and name not in announced:
        announced.add(name)
        emit({"type": "status", "text": _STATUS[name]})
    content = tool_reply(ctx, name, args, found, ds, by_id, profile=profile,
                         enable_docs=enable_docs, brand_tool=brand_tool,
                         redactor=redactor, engine=engine)
    return {"role": "tool", "tool_call_id": tc["id"],
            "content": "unknown tool" if content is None else content}


class Findings:
    """What a chat loop gathered: every governed query, the successful sets (the last one
    drives the UI table/chart) and the document hits."""

    def __init__(self):
        self.primary = None       # last SUCCESSFUL execute → the result shown in the UI
        self.successes = []       # EVERY successful execute, in order (evidence)
        self.doc_hits = []        # search_docs results (deduped) — merged upstream
        self.last_fail = None
        self.queries = []

    def result(self, answer, explored, **extra) -> dict:
        return {"ok": bool(self.primary), "sql": self.primary or self.last_fail,
                "results": self.successes, "queries": self.queries, "answer": answer,
                "explored": explored, **extra,
                "docs": ({"results": self.doc_hits,
                          "citations": [h["citation"] for h in self.doc_hits]}
                         if self.doc_hits else None)}


def _docs_reply(ctx, args, found, redactor) -> str:
    query = (args.get("query") or ctx.question).strip()
    try:
        hits = find_documents(ctx, query, 4)
    except Exception:
        hits = []
    seen = {h["citation"] for h in found.doc_hits}
    found.doc_hits.extend(h for h in hits if h["citation"] not in seen)
    return json.dumps([{"label": h.get("citation_label") or h["citation"],
                        "text": redactor.redact((h.get("text") or "")[:400])} for h in hits]
                      or "no matching documents", ensure_ascii=False, default=str)


def _brand_reply(args, brand_tool, redactor) -> str:
    try:
        content = brand_tool.call(args)
    except Exception as exc:
        if type(exc).__name__ == "PipelineCancelled":
            raise
        content = "The brand baseline could not be read; use run_sql."
    return redactor.redact(content)


def _profile_reply(ctx, target, args, redactor) -> str:
    # PII columns are left out by profile_relation; the values it shows still go
    # through the text redactor like every other tool result
    try:
        content = json.dumps(discovery.profile_relation(
            target, visible_mdl(ctx, target), str(args.get("table") or ""),
            columns=args.get("columns") or None, sample_size=200),
            ensure_ascii=False, default=str)[:6000]
    except Exception as exc:
        content = f"Could not profile that table: {str(exc)[:300]}"
    return redactor.redact(content)


def sql_reply(ctx, target, sql, found, redactor, engine) -> str:
    """Run one governed query for the model and say what came back: masked rows, the
    empty-result hint or the error to fix. Every attempt is kept in ``found``."""
    out = execute_governed(ctx, target, sql, engine)
    found.queries.append(out)
    if out["ok"] and out.get("rows"):
        found.primary = out
        found.successes.append(out)
        # HARD RULE: PII never reaches the model — redact PII columns in the tool
        # result fed back to the LLM (the API response `primary` keeps real values),
        # with the PII list of the datasource that produced it.
        mcols, mrows = mask_pii_for_llm(
            out["columns"], out["rows"][:50], target["mdl"],
            out.get("safe_sql") or "", out.get("dialect"),
        )
        payload = {"row_count": len(out["rows"]), "columns": mcols, "rows": mrows}
        if out.get("derived"):
            # pre-computed analytics (already PII-masked at compute time) — the model
            # uses these instead of doing its own arithmetic over the rows.
            payload["derived"] = out["derived"]
        return json.dumps(payload, ensure_ascii=False, default=str)
    found.last_fail = out
    if out["ok"]:
        return ("EMPTY_RESULT: the SQL executed successfully but returned no rows. "
                "Do not treat this as evidence that data is unavailable. Inspect the "
                "table's actual date/value coverage, broaden or remove speculative "
                "filters, and call run_sql again.")
    # The driver's message is the one piece of tool output that is NOT already
    # masked: a constraint violation or type error routinely quotes the offending
    # ROW VALUE, so an unredacted error is a PII channel straight into the prompt.
    # Same treatment as the two sibling sites in skills/registry.py.
    return (f"ERROR {out.get('code')}: "
            f"{redactor.redact_error(out.get('error'))[:500]}. "
            + (COST_HINT if out.get("code") == "COST_LIMIT" else
               "Fix the SQL and call run_sql again."))


# A query over the scan limit is not a dead end: the same answer is usually one cheaper
# query away, and the model stopped at the first refusal (staging, 1P per marketplace).
COST_HINT = ("The question is still answerable: make the query cheaper and run it again, "
             "following the error above (a date filter lowers the scan only on a table "
             "partitioned by that column). Select only the columns the answer needs; a long "
             "text column such as a product name costs the most, so filter on a short id or "
             "code column when one holds the same thing (find the ids in a smaller table, "
             "e.g. the product table). Aggregate in the query, or use a smaller table that "
             "holds the same measure (a monthly/daily, product or curated table: the index "
             "shows table sizes). Only after a cheaper query is refused too, say the data is "
             "too large to read this way and what would make it possible.")


def tool_reply(ctx, name, args, found, ds, by_id, *, profile, enable_docs, brand_tool,
               redactor, engine, describe_cap: int = None) -> str | None:
    """The reply to one call of a chat tool (search_docs, brand_baseline, search_catalog,
    describe_table, profile_table, run_sql); None = not a tool this turn offers. ``ds`` is
    the main datasource (None = no data tools), ``by_id`` every datasource the tools may
    name; shared by the classic loop and the agentic engine (kb_agent)."""
    if name == "search_docs" and enable_docs:
        return _docs_reply(ctx, args, found, redactor)
    if name == "brand_baseline" and brand_tool is not None:
        return _brand_reply(args, brand_tool, redactor)
    if name == "search_catalog" and ds is not None:
        hits = catalog_search(ctx, list(by_id.values()), {"query": args.get("query"), "limit": 8})
        return redactor.redact(json.dumps(hits, ensure_ascii=False, default=str)[:8000])
    if not _offers_data_tool(name, ds, profile):
        return None
    return _data_tool_reply(ctx, name, args, found, ds, by_id, redactor, engine, describe_cap)


def _offers_data_tool(name, ds, profile) -> bool:
    """run_sql / describe_table always with a datasource; profile_table only when allowed."""
    return (name in ("run_sql", "describe_table", "profile_table") and ds is not None
            and (name != "profile_table" or profile))


def _data_tool_reply(ctx, name, args, found, ds, by_id, redactor, engine, describe_cap) -> str:
    target = by_id.get(args.get("datasource") or ds.get("id"))
    if target is None:
        return "Unknown datasource. Use one of: " + ", ".join(map(str, by_id))
    if name == "describe_table":
        return _describe_reply(ctx, target, args, redactor, describe_cap)
    if name == "profile_table":
        return _profile_reply(ctx, target, args, redactor)
    return sql_reply(ctx, target, args.get("sql", ""), found, redactor, engine)


def _describe_reply(ctx, target, args, redactor, describe_cap) -> str:
    text = describe_tables(visible_mdl(ctx, target), args.get("tables") or [])
    if describe_cap and len(text) > describe_cap:
        text = (text[:describe_cap] + "\n… (cut here: describe fewer tables at a time "
                "for the rest)")
    return redactor.redact(text)
