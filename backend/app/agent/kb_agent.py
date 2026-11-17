"""The agentic chat engine: the model reads the question first, then the Knowledge Base.

The classic engine runs a dozen deterministic layers over the RAW message before any model
reads it (which business, which value a name resembles, which brand, which KPI meaning,
which datasource, which 8 tables reach the prompt). When the person's words do not match
the catalog's vocabulary, the model starts from the wrong context. Here the model is
handed the businesses its role may open and a set of tools over the Knowledge Base — the
way an analyst with a governed warehouse connection works:

  open_business   open one granted business (its datasources + curated knowledge)
  ask_user        end the turn with a question only the person can settle
  list_tables     the open business's table index
  get_definitions curated metrics, business concepts and example queries
  lookup_values   how a named value is stored (curated values first, else a governed
                  DISTINCT query the server writes)
  describe_table, search_catalog, profile_table, run_sql, search_docs, brand_baseline
                  the classic tools, over every datasource of the open business

What does NOT move: the gatekeeper screens before this, every query goes through
execute_governed -> govern() + the dry-run cost guard, PII never reaches the model
(masked rows, the text redactor on every tool reply, PII columns refused by
lookup_values), a business outside the role's grants cannot be opened, and the semantic
contracts of the opened slice are enforced on every KPI query.

Switched per tenant: ``config/chat {engine: "classic" | "agentic"}`` (default classic).
"""
from __future__ import annotations

import datetime
import functools
import json
import re

from sqlglot import exp

from .. import assumptions, business_confirm, business_scope, llm, memory, store
from ..connectors.factory import SQL_TYPES, make_connector
from ..context import check_cancelled
from ..nl2sql import (VIZ_INSTRUCTIONS, INSIGHT_INSTRUCTIONS, HYPOTHESIS_INSTRUCTIONS,
                      ADVISOR_INSTRUCTIONS, KNOWLEDGE_INSTRUCTIONS, BRAINSTORM_INSTRUCTIONS,
                      NUMBER_FORMAT_INSTRUCTIONS, measure_instructions, now_context, _ref_val)
from ..reports import mbr_engine
from ..skills.registry import execute_governed, _record_usage, mask_pii_for_llm, TextPiiRedactor
from . import brand_analysis, sql_agent
from .research_agent import can_profile, visible_mdl

CONFIG_SECTION = "chat"
CLASSIC, AGENTIC = "classic", "agentic"
ENGINES = (CLASSIC, AGENTIC)

MAX_TURNS = sql_agent._int_env("CHAT_AGENTIC_MAX_TURNS", 14, 4, 40)
# every tool reply of one turn together: past it, replies are cut and the model is told to
# finish with what it has (browsing a large catalog must not grow the prompt without end)
TOOL_BUDGET = sql_agent._int_env("CHAT_AGENTIC_TOOL_CHARS", 80000, 10000, 400000)
DESCRIBE_CAP = 12000
INDEX_CAP = 200
LOOKUP_LIMIT = 20
LOOKUP_DAYS = 400       # window of a lookup on a partitioned table
LOOKUP_FALLBACKS = 2    # smaller tables tried when a lookup is over the scan limit


def engine(tenant_id: str) -> str:
    """Which engine answers this tenant's chat."""
    cfg = store.get_tenant_config(tenant_id, CONFIG_SECTION) or {}
    name = str(cfg.get("engine") or CLASSIC).strip().lower()
    return name if name in ENGINES else CLASSIC


def _fn(name, description, properties=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {},
                       **({"required": required} if required else {})}}}


OPEN_BUSINESS, ASK_USER = "open_business", "ask_user"

_DS_ARG = {"type": "string", "description": (
    "Datasource id from the table index. Leave it out for the first datasource.")}

OPEN_BUSINESS_TOOL = _fn(
    OPEN_BUSINESS,
    "Open one of the businesses listed under BUSINESSES: its data and curated knowledge "
    "become available to the other tools. Returns its datasources and table index. Open "
    "the business the question belongs to before you look for tables or query.",
    {"business": {"type": "string", "description": "The business name exactly as listed."}},
    ["business"])

ASK_USER_TOOL = _fn(
    ASK_USER,
    "End this turn with a question to the person, when something only they can settle is "
    "unclear (which business when the question fits more than one, which of two similar "
    "names, which period). Give the choices as options. Do not use it for what you can "
    "reasonably assume and state.",
    {"question": {"type": "string", "description": "The question, in the person's language."},
     "options": {"type": "array", "description": "The choices (2 to 8).", "items": {
         "type": "object", "properties": {
             "label": {"type": "string", "description": "A short choice label (for a "
                       "business: its name exactly)."},
             "question": {"type": "string", "description": "The person's question rewritten "
                          "with this choice, sent when they pick it."}},
         "required": ["label"]}}},
    ["question"])

LIST_TABLES_TOOL = _fn(
    "list_tables",
    "The tables of the open business: name, description, grain and column count.",
    {"datasource": _DS_ARG})

GET_DEFINITIONS_TOOL = _fn(
    "get_definitions",
    "The curated definitions of the open business: metrics (how a KPI is computed), "
    "business concepts (the enforced formula and table of a KPI) and example queries. Pass "
    "terms to filter (GMV, NMV, cancel rate, ...), or none for everything.",
    {"terms": {"type": "array", "items": {"type": "string"},
               "description": "Optional business terms to look up."}})

LOOKUP_VALUES_TOOL = _fn(
    "lookup_values",
    "How a value the question names (a brand, client, product, marketplace, warehouse, "
    "status) is stored in one column: returns the stored values containing the term and "
    "their row counts. Use it before filtering on a name you have not seen stored.",
    {"table": {"type": "string", "description": "Exact table name."},
     "column": {"type": "string", "description": "Exact column name."},
     "term": {"type": "string", "description": "The value as the person wrote it."},
     "datasource": _DS_ARG},
    ["table", "column", "term"])

_STATUS = {OPEN_BUSINESS: "Membuka business…",
           "list_tables": "Melihat tabel yang tersedia…",
           "get_definitions": "Membaca definisi metrik…",
           "lookup_values": "Mengecek nilai di data…",
           "describe_table": "Membaca struktur tabel…",
           **sql_agent._STATUS}


class Session:
    """What the turn has open: the business (None = the whole tenant, for a tenant that
    has not adopted businesses), its SQL datasources, and the semantic guard for them."""

    def __init__(self, tenant_id, role, allowed_skills, datasource_id=None,
                 contracts_for=None):
        self.tenant_id, self.role = tenant_id, role
        self.allowed = allowed_skills
        self.datasource_id = datasource_id
        self.contracts_for = contracts_for or (lambda sources: ([], []))
        self.choices = business_scope.choices(tenant_id, role)
        self.business = None
        self.opened = False
        self.sources = []
        self.contracts = []
        self.readings = []
        self.trace = []
        self.looked = False      # a governed lookup ran: the data was searched this turn
        self.terms = {}          # looked-up term -> found anywhere (curated or data)

    @property
    def primary(self):
        if not self.sources:
            return None
        pinned = [d for d in self.sources if d["id"] == self.datasource_id]
        return (pinned or self.sources)[0]

    def menu(self) -> list:
        """The businesses offered to the model: the ones meant for chat (a business kept
        for the MBR report opens only when named), or every grant when none is."""
        return [c for c in self.choices if c.get("in_chat", True)] or self.choices

    def start(self, ref):
        """The scope before the model reads anything. A business the app pinned (still
        validated against the grants), the whole tenant when businesses are not required,
        the only business there is — otherwise nothing: the model picks one."""
        if (ref or "").strip():
            try:
                self.open(business_scope.resolve(self.tenant_id, self.role, ref))
                return
            except business_scope.BusinessError as exc:
                # a pin to a business that no longer exists (deleted or merged into
                # another) reads as no pin: the model picks among the grants and the
                # reply's business re-pins the app's conversation. A business that
                # exists but is not granted stays a refusal.
                if exc.code != "BUSINESS_NOT_FOUND" or _exists(self.tenant_id, ref):
                    raise
        if not business_scope.is_required(self.tenant_id):
            self.open(None)
            return
        if not self.choices:
            business_scope.resolve(self.tenant_id, self.role, None)   # BUSINESS_NOT_GRANTED
        menu = self.menu()
        if len(menu) == 1:
            self.open(store.get_business(menu[0]["id"]))

    def open(self, business):
        sources = ([d for d in business_scope.visible_datasources(self.tenant_id, business)
                    if d["type"] in SQL_TYPES] if "run_sql" in self.allowed else [])
        self.business, self.opened, self.sources = business, True, sources
        self.contracts, self.readings = self.contracts_for(sources) if sources else ([], [])
        if business:
            self.trace.append("open_business:" + business["name"])

    def find(self, name):
        wanted = str(name or "").strip().lower()
        return next((c for c in self.choices
                     if wanted in (c["name"].lower(), c["id"].lower())), None)

    def source(self, ref):
        if not ref:
            return self.primary
        return next((d for d in self.sources if d["id"] == ref), None)


def _exists(tenant_id, ref) -> bool:
    """Whether any business of the tenant (granted or not, paused or not) answers to ref."""
    wanted = str(ref or "").strip().lower()
    return any(wanted in (b["id"].lower(), b["name"].lower())
               for b in store.list_businesses(tenant_id))


def _one_line(text, limit=160):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _bytes(mdl, table):
    size = ((mdl.get("table_metadata") or {}).get(table) or {}).get("estimated_bytes")
    return size if isinstance(size, (int, float)) and size > 0 else None


def _partition(mdl, table):
    return (((mdl.get("table_metadata") or {}).get(table) or {}).get("partitioning")
            or {}).get("field")


def _size(mdl, table) -> str:
    """'12.5 GB, not partitioned': what a full read of the table costs, so the model can
    prefer a smaller table before the scan limit refuses a query."""
    size = _bytes(mdl, table)
    if size is None:
        return ""
    text = f"{size / 1e9:.1f} GB" if size >= 1e8 else f"{max(size / 1e6, 0.1):.1f} MB"
    field_ = _partition(mdl, table)
    return text + (f", partitioned by {field_}" if field_ else ", not partitioned")


def _table_line(mdl, table, desc, grain) -> str:
    extra = f" [grain: {_one_line(grain[table], 80)}]" if grain.get(table) else ""
    size = _size(mdl, table)
    extra += f" [{size}]" if size else ""
    return f"- {table}" + (f" — {_one_line(desc[table])}" if desc.get(table) else "") + extra


def _curated_names(mdl) -> list:
    lines = []
    names = [str(m.get("name")) for m in mdl.get("metrics") or []
             if isinstance(m, dict) and m.get("name")]
    if names:
        lines.append("  curated metrics: " + "; ".join(names[:40]))
    concepts = [str(c.get("name")) for c in mdl.get("business_concepts") or []
                if c.get("name")]
    if concepts:
        lines.append("  business concepts (enforced): " + "; ".join(concepts[:40]))
    return lines


def _index(ctx, session) -> str:
    """The open business's datasources and table index: every readable table with its one
    line description (no keyword pruning — the model reads and chooses)."""
    if not session.sources:
        return "No database is available in this scope."
    lines, shown = [], 0
    for ds in session.sources:
        mdl = visible_mdl(ctx, ds)
        tables = list(mdl.get("tables") or {})
        lines.append(f"DATASOURCE {ds['id']} ({ds.get('name') or ds['id']}): "
                     f"{len(tables)} tables")
        desc = mdl.get("descriptions") or {}
        grain = mdl.get("grain") or {}
        for table in tables[:max(0, INDEX_CAP - shown)]:
            shown += 1
            lines.append(_table_line(mdl, table, desc, grain))
        lines += _curated_names(mdl)
    total = sum(len(visible_mdl(ctx, d).get("tables") or {}) for d in session.sources)
    if total > shown:
        lines.append(f"… {total - shown} more tables: use search_catalog to find them.")
    return "\n".join(lines)


def _businesses_block(session) -> str:
    if not session.choices:
        return ""
    current = (session.business or {}).get("id")
    lines = ["=== BUSINESSES this person may open (only these) ==="]
    for c in session.menu():
        mark = " [open]" if c["id"] == current else ""
        lines.append(f"- {c['name']}{mark}" + (f": {_one_line(c['descr'], 300)}"
                                                if c.get("descr") else ""))
    if session.business and all(c["id"] != current for c in session.menu()):
        lines.append(f"- {session.business['name']} [open]")
    return "\n".join(lines) + "\n"


_HEAD = (
    "You are Poros, the company's analytics assistant. You work like an analyst with direct, "
    "governed access to the company's data warehouse through tools. Talk with the person the "
    "way a capable analyst colleague would.\n"
    "HOW TO WORK:\n"
    "1. Understand the question first: what is asked, about what, for which period, and what "
    "shape of answer fits. A greeting, a thank-you or a general business question needs no "
    "tool: answer it directly.\n"
    "2. When the answer needs this company's data, decide which BUSINESS it belongs to from "
    "the business descriptions and open it with open_business. When it fits more than one "
    "and nothing in the question or the conversation tells them apart, call ask_user with "
    "those businesses as options instead of guessing. When the question names a business "
    "other than the open one, open that one.\n"
    "3. Find what answers it: read the table index, call get_definitions for the curated "
    "meaning of a KPI (GMV, NMV, cancel rate, ...) and describe_table for a table's columns "
    "before you query it. search_catalog finds tables by business term. Choose the table "
    "whose description and definitions fit the question (the curated, complete source), "
    "never a table just because it is small: a small table may hold only part of the "
    "data. The index shows each table's size for one case only: when a query is refused "
    "for its scan, read fewer columns or a smaller table that holds the SAME data (on a "
    "table that is not partitioned a date filter does not make the scan smaller).\n"
    "4. When the question names a value (a brand, client, product, marketplace, warehouse), "
    "check how it is stored with lookup_values before filtering on it, take the stored value "
    "that fits and say which one you used. A name missing from one column is NOT missing "
    "from the data: try the other columns that hold names (brand, sub brand, client, store, "
    "seller, stock owner, product, marketplace, warehouse) in the tables that fit, a shorter "
    "spelling, and the other businesses that could hold it (open_business) before you say "
    "the data does not have it; then say where you looked.\n"
    "5. Query with run_sql, read the result, and query again when something is off or "
    "missing.\n"
    "6. Ask with ask_user only for what the person alone can settle; otherwise take the most "
    "sensible reading and state it in the answer's first sentence.\n")


def _system_prompt(ctx, session, docs_tool, brand_tool) -> str:
    question = ctx.question
    extra = ""
    if docs_tool:
        extra += ("- You may call search_docs to look up a fact/target/policy in the tenant's "
                  "documents; when you use document content, cite its EXACT bracketed label "
                  "(e.g. [report.pptx — Slide 3]) inline in your answer.\n")
    if brand_tool:
        extra += ("- brand_baseline gives a brand's fixed key numbers for one month; call it "
                  "when a question analyses how a brand did or why, then query only what it "
                  "does not cover.\n")
    if getattr(ctx, "diagnostic", False):
        extra += HYPOTHESIS_INSTRUCTIONS + "\n"
    if getattr(ctx, "brainstorm", False):
        extra += BRAINSTORM_INSTRUCTIONS + "\n"
    elif getattr(ctx, "advise", False) or getattr(ctx, "diagnostic", False):
        extra += ADVISOR_INSTRUCTIONS + "\n"
    measure = measure_instructions(question)
    if measure:
        extra += measure + "\n"
    if session.opened:
        scope = ("=== OPEN: business %s ===\n" % session.business["name"]
                 if session.business else "=== OPEN: the company's data ===\n")
        context = scope + _index(ctx, session)
    else:
        context = ("No business is open yet: never state a figure about this company before "
                   "you opened one and queried it.")
    return (now_context() + "\n" + _HEAD + sql_agent.DATA_RULES + sql_agent._NOTES_RULE
            + "- Let the SHAPE follow the question: a one-number ask gets a short direct "
            "answer; an analytical one gets what it needs (comparisons, what drives the "
            "result, what it means, what to do). Reply ENTIRELY in the SAME language as the "
            "user's question (the whole answer), even when the schema is in another "
            "language; use **bold** for key figures.\n"
            + extra + KNOWLEDGE_INSTRUCTIONS + "\n" + INSIGHT_INSTRUCTIONS + "\n"
            + NUMBER_FORMAT_INSTRUCTIONS + "\n" + VIZ_INSTRUCTIONS + "\n\n"
            + _businesses_block(session) + "\n" + context)


def _tools(session, ctx, enable_docs, brand_tool) -> list:
    tools = []
    if len(session.choices) > 1 or not session.opened:
        tools.append(OPEN_BUSINESS_TOOL)
    tools.append(ASK_USER_TOOL)
    if session.sources:
        many = len(session.sources) > 1
        data = [LIST_TABLES_TOOL, GET_DEFINITIONS_TOOL, LOOKUP_VALUES_TOOL,
                sql_agent.RUN_SQL_TOOL, sql_agent.DESCRIBE_TABLE_TOOL]
        if can_profile(ctx):
            data.append(sql_agent.PROFILE_TABLE_TOOL)
        for tool in data:
            props = tool["function"]["parameters"]["properties"]
            tools.append(sql_agent._with_datasource(tool, _DS_ARG)
                         if many and "datasource" not in props else tool)
        tools.append(sql_agent.SEARCH_CATALOG_TOOL)
    if enable_docs:
        tools.append(sql_agent.SEARCH_DOCS_TOOL)
    if brand_tool is not None:
        tools.append(brand_analysis.TOOL)
    return tools


# ---- the new tools ----
def _open_reply(ctx, session, args, state) -> str:
    hit = session.find(args.get("business"))
    if not hit:
        names = ", ".join(c["name"] for c in session.choices) or "none"
        return f"No business by that name is available to this person. Available: {names}."
    session.open(store.get_business(hit["id"]))
    state.sync(ctx, session)
    if not session.sources:
        return "Opened business %s, but it holds no database this person may query." % hit["name"]
    return ("Opened business %s. Its table index is now in the context above; the data "
            "tools work on it." % hit["name"])


def _ask(session, args, message) -> dict:
    """The confirmation payload for an ask_user call. Options that are all businesses this
    person may open become the business menu (the shape every client already renders);
    anything else is a `clarify` question whose options carry the rewritten question."""
    question = str(args.get("question") or "").strip()
    options = [o for o in (args.get("options") or []) if isinstance(o, dict)
               and str(o.get("label") or "").strip()][:8]
    businesses = [session.find(o.get("label")) for o in options]
    if options and len(options) > 1 and all(businesses):
        return business_confirm.question_for(
            [{k: b[k] for k in ("id", "name", "descr")} for b in businesses], message)
    menu = [{"id": str(i + 1), "name": str(o["label"]).strip()[:120], "descr": "",
             "question": (str(o.get("question") or "").strip()
                          or f"{message} ({str(o['label']).strip()})")[:2000]}
            for i, o in enumerate(options)]
    text = question or message
    if menu:
        text += "\n\n" + "\n".join(f"- **{o['name']}**" for o in menu)
    return {"kind": "clarify", "question": text, "options": menu,
            "pending_question": message}


def _list_reply(ctx, session, args) -> str:
    target = session.source(args.get("datasource"))
    if target is None:
        return "Unknown datasource. Use one of: " + ", ".join(d["id"] for d in session.sources)
    mdl = visible_mdl(ctx, target)
    desc, grain = mdl.get("descriptions") or {}, mdl.get("grain") or {}
    rows = [{"table": t, "description": _one_line(desc.get(t), 300),
             "grain": _one_line(grain.get(t), 200), "columns": len(cols or []),
             "size": _size(mdl, t)}
            for t, cols in (mdl.get("tables") or {}).items()]
    return json.dumps({"datasource": target["id"], "tables": rows[:INDEX_CAP],
                       "more": max(0, len(rows) - INDEX_CAP)}, ensure_ascii=False)


def _matches(terms, *texts) -> bool:
    if not terms:
        return True
    blob = " ".join(str(t or "") for t in texts).lower()
    return any(term in blob for term in terms)


def _replica(concept) -> dict:
    """A concept curated on several datasources as copies of the same data: which copy
    this one is. The primary is the source to use; a fallback answers the same KPI when
    the primary cannot (a scan limit, missing months)."""
    binding = concept.get("source_binding") or {}
    if not binding.get("equivalence_contract_id"):
        return {}
    return {"copy": binding.get("role") or "",
            "same_data_as": binding["equivalence_contract_id"],
            "note": ("primary source of this KPI" if binding.get("role") == "primary" else
                     "fallback copy: use it when the primary source cannot answer")}


def _formula(concept) -> str:
    return concept.get("formula") or "%s(%s)" % (concept.get("aggregation", "SUM"),
                                                 concept.get("metric", ""))


def _ds_definitions(ds, mdl, terms):
    """One datasource's curated definitions matching the terms (None = nothing matches)."""
    metrics = [{"name": m.get("name"), "definition": m.get("definition"),
                "note": m.get("note")}
               for m in mdl.get("metrics") or [] if isinstance(m, dict)
               and _matches(terms, m.get("name"), m.get("definition"), m.get("note"))]
    concepts = [{"name": c.get("name"), "synonyms": c.get("synonyms"),
                 "table": c.get("table"),
                 "formula": _formula(c),
                 "time_dimension": c.get("time_dimension"),
                 "description": c.get("description"),
                 **_replica(c)}
                for c in mdl.get("business_concepts") or []
                if _matches(terms, c.get("name"), " ".join(c.get("synonyms") or []),
                            c.get("description"))]
    examples = [{"question": e.get("q"), "sql": e.get("sql")}
                for e in mdl.get("examples") or []
                if _matches(terms, e.get("q"), e.get("sql"))][:8]
    if not (metrics or concepts or examples):
        return None
    return {"datasource": ds["id"], "metrics": metrics[:40],
            "business_concepts": concepts[:40], "examples": examples}


def _definitions_reply(ctx, session, args) -> str:
    terms = [str(t).strip().lower() for t in (args.get("terms") or []) if str(t).strip()]
    out = [found for found in (_ds_definitions(ds, visible_mdl(ctx, ds), terms)
                               for ds in session.sources) if found]
    if session.contracts:
        out.append({"enforced_for_this_question": [
            {"name": c.get("name"), "table": c.get("table"), "datasource": c.get("datasource_id"),
             "formula": _formula(c),
             **_replica(c)}
            for c in session.contracts]})
    return json.dumps(out or "no curated definitions match", ensure_ascii=False, default=str)


def _pii(mdl, policy, table, column) -> bool:
    blocked = {str(p).lower() for p in mdl.get("pii_columns") or []}
    blocked |= {str(p).lower() for p in (policy or {}).get("denied_columns") or []}
    return column.lower() in blocked or f"{table}.{column}".lower() in blocked


def squash(text) -> str:
    """Letters and digits only, case folded: 'JUB-SIDO' and 'jub sido' are one name."""
    return re.sub(r"[^0-9a-z]", "", str(text or "").lower())


def _term(text) -> str:
    """The term as a LIKE needle: wildcards and quotes dropped, case folded."""
    return re.sub(r"[%_'\"\\]", "", str(text or "")).strip().lower()[:80]


def lookup_sql(table: str, column: str, term: str, dialect: str, since=None) -> str:
    """The server-written lookup: stored values of one column containing the term, busiest
    first. Built as a sqlglot tree (identifiers quoted per dialect, the needle a literal).
    ``since`` = (partition column, 'YYYY-MM-DD'): the recent window of a partitioned table,
    the only filter that lowers its scan."""
    col = exp.column(column, quoted=True)
    # words joined by a wildcard: "jub sido" finds "JUB-SIDO" and "Jub_Sido" too
    needle = exp.Literal.string("%" + "%".join(term.split()) + "%")
    cond = exp.Like(this=exp.Lower(this=exp.cast(col, "TEXT")), expression=needle)
    if since:
        cond = exp.and_(cond, exp.GTE(this=exp.column(since[0], quoted=True),
                                      expression=exp.Literal.string(since[1])))
    query = (exp.select(exp.alias_(col, "value"), exp.alias_(exp.Count(this=exp.Star()), "n"))
             .from_(exp.to_table(table))
             .where(cond)
             .group_by(col)
             .order_by(exp.Ordered(this=exp.column("n"), desc=True))
             .limit(LOOKUP_LIMIT))
    return query.sql(dialect=dialect)


def _lookup_run(ctx, target, mdl, table, column, term, on_probe) -> dict:
    field_ = _partition(mdl, table)
    since = None
    if field_ and field_ in (mdl.get("tables") or {}).get(table, []):
        since = (field_, (datetime.date.today()
                          - datetime.timedelta(days=LOOKUP_DAYS)).isoformat())
    dialect = make_connector(target["type"], target["conn"]).dialect
    out = execute_governed(ctx, target, lookup_sql(table, column, term, dialect, since),
                           "kb:lookup_values", probe=True)
    on_probe(out)
    return out


def _smaller_tables(ctx, session, table, column, mdl) -> list:
    """Other readable tables holding the same column, smallest first and smaller than
    ``table`` (a product table holds the product names an order table is too big to scan
    for)."""
    ceiling = _bytes(mdl, table)
    found = []
    for ds in session.sources:
        view = visible_mdl(ctx, ds)
        for other, cols in (view.get("tables") or {}).items():
            match = next((str(c) for c in cols or [] if str(c).lower() == column.lower()), None)
            size = _bytes(view, other)
            if (other != table and match and size is not None
                    and (ceiling is None or size < ceiling)
                    and not _pii(view, ctx.role_policy, other, match)):
                found.append((size, ds, view, other, match))
    found.sort(key=lambda item: item[0])
    return found[:LOOKUP_FALLBACKS]


def _lookup_column(ctx, mdl, args):
    """(table, column) a lookup reads, or the reason it cannot (a str)."""
    tables = {t.lower(): t for t in mdl.get("tables") or {}}
    table = tables.get(str(args.get("table") or "").strip().lower())
    if not table:
        return "Unknown table. Use list_tables or search_catalog for the exact name."
    columns = {str(c).lower(): str(c) for c in mdl["tables"][table] or []}
    column = columns.get(str(args.get("column") or "").strip().lower())
    if not column:
        return f"Unknown column in {table}. Use describe_table for its columns."
    if _pii(mdl, ctx.role_policy, table, column):
        return "That column holds personal data: its values are never looked up."
    return table, column


def _lookup_scan(ctx, session, hit, term, on_probe):
    """Run the lookup on hit = (target, mdl, table, column). Returns (result, the hit it
    read, note)."""
    target, mdl, table, column = hit
    out = _lookup_run(ctx, target, mdl, table, column, term, on_probe)
    if out["ok"] or out.get("code") != "COST_LIMIT":
        return out, hit, None
    # the column is too big to scan in this table: the same column in a smaller table
    # (the product table for product names) usually holds the same names
    for _size_, ds, view, other, col in _smaller_tables(ctx, session, table, column, mdl):
        retry = _lookup_run(ctx, ds, view, other, col, term, on_probe)
        if retry["ok"]:
            return retry, (ds, view, other, col), (
                f"{table} is too large to scan for this column, so the lookup read "
                f"the same column in {other}")
    return out, hit, None


def _lookup_found(ctx, session, out, hit, term, note) -> dict:
    target, mdl, table, column = hit
    cols, rows = mask_pii_for_llm(out["columns"], out["rows"][:LOOKUP_LIMIT], target["mdl"],
                                  out.get("safe_sql") or "", out.get("dialect"))
    values = [dict(zip(cols, row)) for row in rows]
    reply = {"source": "data", "table": table, "column": column, "values": values}
    if note:
        reply["note"] = note
    if values:
        session.terms[term] = True
    else:
        reply["values"] = ("no stored value contains that term in this column: this column "
                           "is not the whole data")
        reply.update(_elsewhere(ctx, session, mdl, term, table, column))
    return reply


def _lookup_reply(ctx, session, args, redactor, on_probe) -> str:
    target = session.source(args.get("datasource"))
    if target is None:
        return "Unknown datasource. Use one of: " + ", ".join(d["id"] for d in session.sources)
    mdl = visible_mdl(ctx, target)
    picked = _lookup_column(ctx, mdl, args)
    if isinstance(picked, str):
        return picked
    table, column = picked
    term = _term(args.get("term"))
    if len(squash(term)) < 2:
        return "Give the term to look for (at least 2 letters or digits)."
    session.terms.setdefault(term, False)
    curated = [v for v in (mdl.get("reference_values") or {}).get(column) or []
               if squash(term) in squash(v)]
    if curated:
        session.terms[term] = True
        return redactor.redact(json.dumps({"source": "curated values", "column": column,
                                           "values": [_ref_val(v) for v in curated[:40]]},
                                          ensure_ascii=False))
    out, hit, note = _lookup_scan(ctx, session, (target, mdl, table, column), term, on_probe)
    if not out["ok"]:
        return (f"ERROR {out.get('code')}: "
                f"{redactor.redact_error(out.get('error'))[:400]}")
    session.looked = True
    reply = _lookup_found(ctx, session, out, hit, term, note)
    return redactor.redact(json.dumps(reply, ensure_ascii=False, default=str))


_NAME_COLUMN = re.compile(r"brand|client|store|shop|seller|owner|merchant|principal|"
                          r"product|item|sku|marketplace|channel|platform|warehouse|account",
                          re.I)


def _curated_hits(mdl, term, skip=None) -> list:
    """Columns whose curated values contain the term (free: no query)."""
    hits = []
    for col, values in (mdl.get("reference_values") or {}).items():
        if str(col).lower() == (skip or "").lower():
            continue
        found = [_ref_val(v) for v in values or [] if squash(term) in squash(v)]
        if found:
            hits.append({"column": col, "values": found[:5]})
    return hits


def _elsewhere(ctx, session, mdl, term, table, column) -> dict:
    """Where else a missed name may live: other name columns of the same table, curated
    values of this business under other columns, and other businesses whose curated values
    hold it. Only curated values are read here (no query); the model looks itself."""
    out = {}
    others = [c for c in (mdl.get("tables") or {}).get(table) or []
              if _NAME_COLUMN.search(str(c)) and str(c).lower() != column.lower()
              and not _pii(mdl, ctx.role_policy, table, str(c))]
    if others:
        out["other_name_columns_in_this_table"] = others[:12]
    here = [h for ds in session.sources for h in _curated_hits(visible_mdl(ctx, ds), term,
                                                                  skip=column)]
    if here:
        out["curated_in_this_business"] = here[:8]
    current = (session.business or {}).get("id")
    elsewhere = []
    for choice in session.choices:
        if choice["id"] == current:
            continue
        business = store.get_business(choice["id"])
        sources = [d for d in business_scope.visible_datasources(ctx.tenant_id, business)
                   if d["type"] in SQL_TYPES]
        if any(_curated_hits(visible_mdl(ctx, ds), term) for ds in sources):
            elsewhere.append(choice["name"])
    if elsewhere:
        out["curated_in_other_businesses"] = elsewhere
    out["next"] = ("look in another name column, a shorter spelling, or open another "
                   "business that could hold it before concluding it is not in the data")
    return out


class _State:
    """Turn state the loop keeps in step with the session (ctx fields the tools read)."""

    def __init__(self, tenant_id, role, message):
        self.tenant_id, self.role, self.message = tenant_id, role, message
        self.brand_tool = None
        self.brand_for = object()

    def sync(self, ctx, session):
        ctx.business = session.business
        ctx.datasource = session.primary
        ctx.semantic_contracts = session.contracts
        ctx.assumptions = session.readings
        biz_id = (session.business or {}).get("id")
        if self.brand_for != biz_id:
            self.brand_for = biz_id
            self.brand_tool = None
            if (session.sources and brand_analysis.enabled()
                    and mbr_engine.has_brand_orders(self.tenant_id, self.role, biz_id)):
                self.brand_tool = brand_analysis.BrandTool(
                    self.tenant_id, self.role, session.business, self.message)

    @property
    def brand_cached(self) -> bool:
        return self.brand_tool is not None and self.brand_tool.cached


def _start_state(ctx, session, seed_brand) -> _State:
    state = _State(ctx.tenant_id, ctx.role, ctx.question)
    state.sync(ctx, session)
    if seed_brand and state.brand_tool is not None:
        state.brand_tool.seed(*seed_brand)
    return state


def _first_message(ctx, session, redactor) -> str:
    user_msg = redactor.redact(ctx.question)
    if getattr(ctx, "focus", ""):
        user_msg = f"{redactor.redact(ctx.focus)}\n\n{user_msg}"
    readings = assumptions.prompt_block(session.readings)
    if readings:
        user_msg = f"{redactor.redact(readings)}\n\n{user_msg}"
    return user_msg


def _system_message(ctx, session, state, redactor, enable_docs, history) -> dict:
    return {"role": "system",
            "content": redactor.redact(_system_prompt(ctx, session, enable_docs,
                                                      state.brand_tool is not None)
                                       + (memory.HISTORY_RULE if history else ""))}


def _offered(session, ctx, enable_docs, state, last) -> list:
    """The tools this turn offers: none on the last turn, so the model answers."""
    tools = _tools(session, ctx, enable_docs, state.brand_tool)
    return [] if last else tools


def _asked(session, res, question):
    """The confirmation of an ask_user call among the model's tool calls, else None."""
    asked = next((tc for tc in res.tool_calls if tc.get("name") == ASK_USER), None)
    if not asked:
        return None
    return _ask(session, asked.get("arguments") or {}, question)


def _assistant_message(res) -> dict:
    return {"role": "assistant", "content": res.text or "",
            "tool_calls": [{"id": tc["id"], "type": "function",
                            "function": {"name": tc["name"],
                                         "arguments": json.dumps(tc["arguments"])}}
                           for tc in res.tool_calls]}


def _tool_content(ctx, session, state, found, name, args, redactor, enable_docs, on_query,
                  model) -> str:
    if name not in (OPEN_BUSINESS,):
        session.trace.append(name)
    before = len(found.queries)
    content = _reply(ctx, session, state, found, name, args, redactor, enable_docs,
                     on_query, model)
    for step in found.queries[before:]:
        on_query(step)
    return content


def _call_tools(ctx, session, state, found, res, messages, system, redactor, enable_docs,
                emit, on_query, announced, spent) -> int:
    """One turn's tool calls, their replies appended to messages. Returns the tool output
    spent so far."""
    messages.append(_assistant_message(res))
    for tc in res.tool_calls:
        name, args = tc.get("name"), tc.get("arguments") or {}
        if name in _STATUS and name not in announced:
            announced.add(name)
            emit({"type": "status", "text": _STATUS[name]})
        content = _tool_content(ctx, session, state, found, name, args, redactor,
                                enable_docs, on_query, res.model)
        spent += len(content)
        if spent > TOOL_BUDGET:
            content = (content[:2000] + "\n… (the tool output budget of this turn is "
                       "spent: finish with what you have)")
        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": content})
        if name == OPEN_BUSINESS:
            # the scope changed: the context (businesses, table index) follows it
            messages[0] = system()
    return spent


def run(ctx, session, *, enable_docs=False, emit=None, on_query=None, seed_brand=None,
        max_turns: int = None) -> dict:
    """Run the agentic loop. Returns run_sql_agent's shape plus ``ask`` (a confirmation
    that ends the turn), ``trace`` (the tool steps, for the plan) and ``brand_tool``.

    ``on_query`` gets every governed query the moment it ran (lookups included), so the
    audit trail holds a query even when the turn is cancelled after it. ``seed_brand`` =
    (target, bases) of a brand mode turn whose baseline was read before the loop."""
    emit = emit or (lambda *_: None)
    on_query = on_query or (lambda *_: None)
    state = _start_state(ctx, session, seed_brand)
    redactor = TextPiiRedactor()
    history = getattr(ctx, "chat_history", None)
    user_msg = _first_message(ctx, session, redactor)
    system = functools.partial(_system_message, ctx, session, state, redactor, enable_docs,
                               history)
    messages = [system()]
    messages += memory.as_messages(history, redactor.redact, {"role": "user", "content": user_msg})
    found = sql_agent.Findings()
    answer, explored, ask, spent = None, False, None, 0
    announced = set()
    turns = max_turns or MAX_TURNS
    for turn in range(turns):
        check_cancelled()
        last = turn == turns - 1 or spent > TOOL_BUDGET
        tools = _offered(session, ctx, enable_docs, state, last)
        try:
            res = llm.complete_tools(ctx.tenant_id, ctx.answer_purpose, messages, tools,
                                     temperature=sql_agent.loop_temperature(ctx),
                                     max_tokens=sql_agent.TURN_TOKENS)
        except llm.LLMUnavailable:
            break
        _record_usage(ctx, res)
        if not res.tool_calls:
            answer = res.text or answer
            break
        ask = _asked(session, res, ctx.question)
        if ask is not None:
            session.trace.append(ASK_USER)
            break
        explored = True
        spent = _call_tools(ctx, session, state, found, res, messages, system, redactor,
                            enable_docs, emit, on_query, announced, spent)
        if state.brand_cached:
            break
    state.sync(ctx, session)
    return found.result(answer, explored, needs_business=False, ask=ask,
                        trace=list(session.trace), brand_tool=state.brand_tool)


def _reply(ctx, session, state, found, name, args, redactor, enable_docs, on_query,
           model) -> str:
    if name == OPEN_BUSINESS:
        return _open_reply(ctx, session, args, state)
    if name in ("list_tables", "get_definitions", "lookup_values") and not session.sources:
        return "Open a business first (open_business)."
    if name == "list_tables":
        return redactor.redact(_list_reply(ctx, session, args))
    if name == "get_definitions":
        return redactor.redact(_definitions_reply(ctx, session, args))
    if name == "lookup_values":
        return _lookup_reply(ctx, session, args, redactor, on_query)
    by_id = {d["id"]: d for d in session.sources}
    content = sql_agent.tool_reply(
        ctx, name, args, found, session.primary, by_id, profile=can_profile(ctx),
        enable_docs=enable_docs, brand_tool=state.brand_tool, redactor=redactor,
        engine=f"llm:{model}", describe_cap=DESCRIBE_CAP)
    if content is None and name in ("run_sql", "describe_table", "profile_table",
                                    "search_catalog") and not session.sources:
        return "Open a business first (open_business)."
    return "unknown tool" if content is None else content
