"""NL->SQL. Default: rule-based ringan (biar jalan tanpa LLM key).
Produksi: set LLM via LiteLLM (pip install litellm + env key) -> ganti llm_generate()."""
import math
import os
import re
from . import semantic_contract


# Shared visualization spec — used by BOTH the agentic tool-loop prompt (sql_agent) and
# the single-shot synthesis prompt (skills.registry._synthesize) so a chart/dashboard can
# be produced regardless of whether the tenant's model can tool-call. Charts/artifacts are
# emitted as fenced ```chart / ```artifact blocks that the frontend (viz.jsx) extracts.
VIZ_INSTRUCTIONS = (
    "- VISUALIZE: when a chart makes the answer clearer (or the user asks for one), include a "
    "```chart block with JSON built from the REAL result numbers (use them verbatim, raw — no "
    "thousands separators). Pick the type that fits the data; you may emit several blocks. Format:\n"
    "    ```chart\n"
    '    {"type":"bar","title":"GMV per Marketplace","labels":["Shopee","TikTok"],"series":[{"name":"GMV","data":[1538394212,7718929177]}]}\n'
    "    ```\n"
    "  TYPES: bar (compare categories) · line / area (trend over time) · pie / doughnut (composition, <=6 "
    "slices) · scatter · geo (Indonesia province heatmap: labels = province names e.g. \"JAWA BARAT\", "
    "series[0].data = values).\n"
    "- INTERACTIVE ARTIFACT — when the user asks for an interactive view / dashboard, include ONE "
    "```artifact block = a SELF-CONTAINED HTML document (<body> plus its own <style>/<script>). EMBED the "
    "actual result rows as a JS const (NEVER invent numbers); make it interactive (e.g. tabs, sortable table, "
    "filter, or charts). NO NETWORK AT ALL: no fetch/XHR and NO external library — the renderer serves the "
    "artifact under `default-src 'none'` and deployed environments have no internet egress, so a CDN "
    "<script>/<link> is silently dropped and every feature built on it dies. Inline all CSS/JS and draw "
    "charts yourself with inline SVG or <canvas>. If you add an export/download button, build the file in "
    "the page (Blob + URL.createObjectURL + <a download>) or a data: URI — never navigator.clipboard, "
    "window.open, or a server round-trip. Keep it focused and close with </html>."
)


def now_context() -> str:
    """Current date line injected into every agent prompt. Relative-date questions
    ('hari ini', 'kemarin', 'minggu ini') must resolve from THIS — and the answer must
    never state a different date. English lowercase day name is included because
    day-of-week ENUM columns (e.g. shift_assignments.day_of_week) use those values.

    DATE ONLY, no clock: this line sits at byte 0 of every agent prompt (sql_agent,
    _llm_sql, synthesis), and prompt caching matches a byte-exact prefix — an HH:MM that
    ticked every minute invalidated the whole ~6.4k-token prompt between questions.
    Nothing resolves an hour-level relative date, so the clock bought nothing."""
    import time as _t
    lt = _t.localtime()
    day_en = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][lt.tm_wday]
    return (f"CURRENT DATE (server): {day_en}, {_t.strftime('%Y-%m-%d', lt)}. "
            "Resolve relative dates ('hari ini'/'today', 'kemarin', 'minggu ini', 'bulan ini') from this, "
            "and NEVER invent a different current date in the answer.")


# What the model may bring itself — shared by every prompt that writes an answer. The
# north star is "numbers only from validated SQL", and that rule stays exactly as strict.
# What it never meant is "every SENTENCE only from the evidence": an analyst who may not
# say how a 9.9 sale, a pack price or a marketplace algorithm usually behaves can only
# read the table back, which is the stiffness users complained about (2026-10-30).
KNOWLEDGE_INSTRUCTIONS = (
    "- YOUR OWN KNOWLEDGE: facts and figures about THIS business come only from the evidence "
    "(query results, documents, memory). Your general knowledge of how commerce works (marketplaces, "
    "campaigns and sale days, pricing and packs, buyer behaviour, ads, creators, fulfilment, retail "
    "practice) is welcome: use it to explain what the numbers mean, why they may have moved and what "
    "is worth trying. Make clear when a point is general knowledge or your own reading rather than "
    "something the data shows, and never attach a number to it that is not in the evidence. "
    "Never promise to check or look something up later (\"I will check ...\"): nothing runs after "
    "this reply. When a check in the data would help, offer it as a question the person can say yes "
    "to, in their language (e.g. \"Want me to check this in the data for a brand and month?\")."
)

# Grounded-analysis spec — shared by the tool-loop and single-shot synthesis prompts.
# Insight/suggestions are REASONING over validated numbers (allowed & encouraged);
# a cause or policy the evidence does not show may be offered as a reading, never as fact.
# No fixed 'Insight:' / 'Saran:' labels (2026-10-30): every answer ending in the same two
# labelled bullets is what made the chat read like a template.
INSIGHT_INSTRUCTIONS = (
    "- INSIGHT & RECOMMENDATION: when the data shows something worth knowing (a pattern, trend, "
    "comparison or outlier VISIBLE in the numbers), point it out, and when it helps, suggest an action "
    "that follows from it. Weave both into the answer in your own words, in the SAME language as the "
    "user's question; there are no fixed labels or sections to fill. Derive both from the evidence "
    "numbers (compare, rank, trend, outlier; you may compute percentages/deltas from them); a cause or "
    "policy the evidence does not show is your reading, so say so instead of stating it as fact; a "
    "recommendation reads as a recommendation, not a fact. When the data is trivial, just answer."
)

# Number-formatting spec — shared by every narrative prompt (sql_agent tool-loop + synthesis).
# Raw DB output (long decimal tails, no currency, no readable scale) is hard to read; this makes
# the model present figures for humans WITHOUT dropping the exact value (traceability must hold).
NUMBER_FORMAT_INSTRUCTIONS = (
    "- NUMBER FORMATTING (write figures for humans, not raw database output): "
    "(a) ROUND sensibly — never show more than 2 decimal places, and drop the decimals entirely for "
    "values of 1000 or more (write 'Rp2.785.176.381', never '2.785.176.381,1759'). "
    "(b) SEPARATORS in the question's locale (Indonesian: '.' for thousands, ',' for decimals). "
    "(c) CURRENCY: when a figure is monetary (GMV, NMV, revenue, sales, omzet, harga, biaya), prefix the "
    "currency the data uses — 'Rp' for Indonesian rupiah amounts; never invent a currency if it is truly unknown. "
    "(d) READABLE SCALE: for large figures (1 million or more), append an approximate human-readable magnitude "
    "in parentheses, localized to the question language — Indonesian ribu/juta/miliar/triliun, English "
    "thousand/million/billion. E.g. Indonesian 'Rp2.785.176.381 (±2,79 miliar)'; English "
    "'Rp2,785,176,381 (~2.79 billion)'. ALWAYS keep the full exact figure as well — the parenthetical is a "
    "reading aid, never a replacement, and the exact value must stay traceable to the evidence."
)

# Period-comparison spec — shared by every SQL-WRITING prompt (sql_agent tool-loop,
# single-shot _llm_sql, Deep Research). Live Acme cases: 'Sep MTD 1-7' was the ELSE of a
# CASE whose WHERE ran Sep 1 - Oct 7, so it summed all of September (2026-10-07); a
# 'Rata-rata 3 bulan' bucket held a three-month SUM (2026-09-29). period_labels.violation()
# rejects such SQL deterministically; this text is what keeps the model from writing it.
PERIOD_SQL_INSTRUCTIONS = (
    "- PERIOD COMPARISONS: give EVERY time window its own WHEN with explicit start AND end "
    "dates, e.g. CASE WHEN d BETWEEN '2026-10-01' AND '2026-10-07' THEN 'Oct 1-7' WHEN d "
    "BETWEEN '2026-09-01' AND '2026-09-07' THEN 'Sep 1-7' END. Never let ELSE carry a period "
    "label: ELSE takes every row the WHERE lets through, not just the window its label names. "
    "Limit WHERE to exactly the compared windows (OR-ed windows inside parentheses). A label "
    "must name exactly the dates its rows cover — SQL that breaks this is rejected with "
    "PERIOD_LABEL_MISMATCH.\n"
    "- AVERAGES: a figure called average/rata-rata must be computed as one in SQL (SUM(x) / "
    "number of periods, or AVG over a per-period subquery) — never a multi-month SUM under "
    "an average label.\n"
    "- MONTH-TO-DATE (MTD): unless the user names an end date or says today, MTD runs from "
    "day 1 through YESTERDAY (today's data is still coming in), and every compared month uses "
    "the SAME day numbers (Oct 1-7 vs Sep 1-7). State the exact windows in the answer."
)

# ---- measure intent: "how much" (value) vs "how many" (count) ----
# Live feedback (fb_061885f7, Acme): "How much inventory Acme has?" was answered with
# 14.438 UNITS. The admin's rule: 'how much' asks for the asset VALUE in IDR, 'how many'
# asks for the unit COUNT. Both measures often sit side by side in one table (quantity +
# gmv/nmv/price), so the wording of the question is the ONLY signal for which one the user
# wants — the model was picking whichever column it noticed first.
#
# This is a deterministic PREFERENCE between measures that already exist, never a licence
# to invent one: when the asked-about entity has no monetary column at all (live case: the
# IMS inventory tables carry quantity only), the instruction requires saying so out loud
# and labelling the quantity as a quantity, instead of silently answering the wrong unit.
# English splits the two readings cleanly ('how much' vs 'how many'); INDONESIAN DOES NOT —
# a bare 'berapa X' carries no signal at all, and 'berapa banyak' is the ordinary way to say
# 'how many'. So the Indonesian side keys off the explicit money words the user would add
# ('berapa NILAI/RUPIAH/nominal', 'senilai') and the explicit unit words ('berapa UNIT/pcs',
# 'berapa banyak'). A bare 'berapa stok gudang' stays deliberately NEUTRAL: guessing there
# would be inventing an intent the sentence does not carry.
_MEASURE_VALUE_CUE = re.compile(
    r"(?<![a-z])how\s+much(?!\s+(?:time|long|longer|space|room|capacity|percent|percentage|"
    r"often|weight|volume|cbm|kg|ton|tonnage)\b)"
    r"|(?<![a-z])what(?:'s|\s+is)\s+the\s+(?:total\s+)?value\s+of"
    r"|(?<![a-z])total\s+value(?![a-z])"
    r"|berapa\s+(?:total\s+|besar\s+|banyak\s+)?(?:nilai|rupiah|nominal|valuasi)"
    r"|berapa\s+(?:omzet|omset|pendapatan|harga|biaya|revenue)"
    r"|(?:total|nilai)\s+aset|total\s+nilai|senilai|dalam\s+rupiah|dalam\s+idr",
    re.I,
)
_MEASURE_COUNT_CUE = re.compile(
    r"(?<![a-z])how\s+many(?![a-z])"
    r"|(?<![a-z])(?:number|count)\s+of(?![a-z])"
    # 'berapa banyak NILAI/omzet' is a value question wearing count clothing — let the value
    # cue own that span instead of both firing and cancelling each other out.
    r"|berapa\s+(?:banyak|banyaknya)(?!\s+(?:nilai|rupiah|nominal|valuasi|omzet|omset|"
    r"pendapatan|harga|biaya|revenue))"
    r"|berapa\s+(?:unit|pcs|buah|item|biji|karton|koli|dus|kardus)"
    r"|ada\s+berapa"
    r"|(?:jumlah|total)\s+(?:unit|pcs|buah|karton|koli)",
    re.I,
)
# Colloquial Indonesian says 'berapa banyak GMV' where English says 'how much GMV'. When the
# question names an inherently monetary metric, the count cue is NOT evidence that a unit
# count is wanted, so we force nothing and let the catalog/business contract decide.
_MEASURE_MONETARY_TERM = re.compile(
    r"(?<![a-z0-9])(?:gmv|nmv|idr|rupiah|omzet|omset|revenue|pendapatan|nilai|harga|"
    r"biaya|cost|profit|margin|laba)(?![a-z0-9])",
    re.I,
)

MEASURE_VALUE_INSTRUCTIONS = (
    "- MEASURE INTENT ('how much' = VALUE, not count): this question asks for a MONETARY amount. "
    "When the entity has BOTH a value measure and a quantity measure, choose the MONETARY one "
    "(a value/price/gmv/nmv/revenue/harga/omzet column, or a curated value metric) and report it in "
    "the currency the data actually uses — never a unit/piece count, and never a made-up currency. "
    "HONESTY CLAUSE (never break it): if the catalog has NO monetary measure for the entity asked "
    "about, do NOT invent, estimate, or derive a rupiah figure, and do NOT quietly answer in units "
    "as if it were the value. Say plainly that the data holds no value column for it, give the "
    "quantity CLEARLY LABELLED as a quantity (units/pcs) as the closest available fact, and name the "
    "data that would be needed to express it in Rupiah (e.g. unit cost or selling price per item)."
)

MEASURE_COUNT_INSTRUCTIONS = (
    "- MEASURE INTENT ('how many' = COUNT, not value): this question asks for a QUANTITY. When the "
    "entity has BOTH a quantity measure and a value measure, choose the QUANTITY one (a quantity/unit "
    "column, or COUNT/COUNT DISTINCT of the entity) rather than a monetary column, and state the unit "
    "explicitly (units/pcs/orders). If only a monetary measure exists, say so instead of presenting "
    "an amount as if it were a count."
)


def measure_intent(question: str) -> str | None:
    """'value' | 'count' | None — which measure the question's wording asks for.

    A question that asks for BOTH ('how many units and how much value') returns None: there
    is nothing to disambiguate, and forcing either one would drop half the answer."""
    text = question or ""
    value, count = bool(_MEASURE_VALUE_CUE.search(text)), bool(_MEASURE_COUNT_CUE.search(text))
    if count and not value and _MEASURE_MONETARY_TERM.search(text):
        return None                      # 'berapa banyak GMV' — the metric itself is money
    if value == count:
        return None
    return "value" if value else "count"


def measure_instructions(question: str) -> str:
    """The measure-intent prompt block for this question ('' when the wording is neutral)."""
    intent = measure_intent(question)
    if intent == "value":
        return MEASURE_VALUE_INSTRUCTIONS
    if intent == "count":
        return MEASURE_COUNT_INSTRUCTIONS
    return ""


# Diagnostic (WHY-question) spec — injected ONLY for diagnostic intent: candidate causes
# are labelled as hypotheses, each tied to a concrete evidence pattern, with no new figures.
HYPOTHESIS_INSTRUCTIONS = (
    "- DIAGNOSTIC MODE (the user asks WHY a metric moved): explain the likely causes, the strongest "
    "first, wherever they fit in your answer (no fixed section label). HARD RULES: every cause MUST point "
    "to the specific evidence pattern that supports it (which metric/segment/period moved, by how much — "
    "reusing figures already in the evidence); a cause is a hypothesis, so say how sure you are and NEVER "
    "present it as a fact; do NOT introduce any NEW figure for it; when the evidence cannot support any "
    "cause, say so and name the additional data that would confirm one."
)

# Advisor (Level 4) spec — injected for prescriptive ("how do we improve X") and diagnostic
# questions. Upgrades the single optional recommendation into RANKED, impact-tied advice.
# Impact estimates must reuse evidence/derived figures or be expressed as percentages — the
# verifier traces every substantial figure back to the data, so invented rupiah amounts fail.
# The THINKING is the contract; the old mandated 'Analisis:' paragraph + numbered
# 'Rekomendasi (prioritas):' section are gone (2026-10-30) — the answer's shape is the model's.
ADVISOR_INSTRUCTIONS = (
    "- ADVISOR MODE — think like a sharp business analyst, not a number-reporter. A ranking of "
    "current totals is only the STARTING point; the value you add is the reasoning ON TOP of it: go "
    "beyond the headline number by looking for the ANGLES the evidence supports, e.g.: MOMENTUM vs size "
    "(a segment that is smaller today but growing fastest — use derived.growth_by_segment — can matter "
    "more than the current #1); CONCENTRATION/risk (how dependent the metric is on one segment); a GAP or "
    "MISMATCH across datasets (e.g. where demand is vs where it is currently served/stocked — an "
    "opportunity a single table can't show); SEASONALITY or a turning point in the trend. Connect "
    "MULTIPLE result sets into ONE argument when they are present. Be creative about WHICH angle is "
    "decision-relevant, but every angle MUST rest on a figure that is actually in the evidence.\n"
    "  Then say what to do: the moves, RANKED so the one with the biggest expected impact comes first. "
    "For each, make clear the concrete action; the evidence it rests on (metric/segment/period); the "
    "expected impact TIED to a real figure or a percentage derived from the evidence (framed as an "
    "estimate — 'berpotensi'/'diperkirakan' — never a promise); the key ASSUMPTION or TRADE-OFF; and, when "
    "your confidence is limited, say so and name the ONE extra datum that would confirm it. Write it the "
    "way the question calls for (prose, bullets or a short list); there are no fixed headings or "
    "numbered sections to fill. If a needed metric is MISSING or zero (e.g. warehouse capacity total_cbm "
    "is 0/null), do NOT dead-end — state the gap, offer a sensible proxy or the specific data to pull "
    "next, and still give a provisional recommendation. HARD RULE (unchanged): reuse/derive figures from "
    "the evidence only; NEVER invent a new absolute number or present a guess as a measured fact — "
    "creative IDEAS, interpretations and what you know of how the market works are welcome, invented "
    "DATA is not. Skip the advice only when the data truly supports no action."
)

# Brainstorm — the person wants ideas to think with ("kasih ide campaign 12.12", "gimana kalau
# ..."), not a report (owner, 2026-11-05: "brainstorming seperti claude chat biasa"). Replaces
# the advisor contract for that turn, whose "every angle rests on a figure in the evidence"
# pushed the model to query first and think second. Figures about THIS company still come
# only from run_sql; a PROPOSED figure (a discount to test, a voucher value) is a proposal.
BRAINSTORM_INSTRUCTIONS = (
    "- BRAINSTORM: the person wants ideas to think with, like a conversation with a sharp colleague. "
    "Give several genuinely different directions (include at least one less obvious one), build on "
    "anything the person already suggested, and say briefly for each why it could work and what could "
    "go wrong or what it costs. Your general knowledge of commerce, marketplaces, campaigns, content "
    "and buyers is the main material here. Keep the shape light (short headed list or prose, no "
    "report), and end with one question or next step that moves the thinking on. Figures you PROPOSE "
    "(a discount to test, a voucher value, a target to aim for) are welcome when written as proposals "
    "('misalnya diskon 20%', 'try a IDR 10,000 voucher'); facts ABOUT THIS COMPANY (its sales, GMV, "
    "stock, conversion, best channel) come only from data you queried in this turn: when you did not "
    "check, say so and offer to. Query the data only when a fact would make the ideas sharper (which "
    "channel or product sells most), never just to have a number."
)


def brainstorm_temperature() -> float:
    """Sampling temperature for a brainstorm turn: ideas need some spread (env
    BRAINSTORM_TEMPERATURE, default 0.7; a reasoning model that refuses it runs at its own)."""
    try:
        return max(0.0, min(1.0, float(os.environ.get("BRAINSTORM_TEMPERATURE", 0.7))))
    except (TypeError, ValueError):
        return 0.7


# Cross-source reconciliation — injected only when BOTH SQL figures and document excerpts are present.
CROSS_SOURCE_INSTRUCTIONS = (
    "- CROSS-SOURCE: when both SQL figures and document facts are present, reconcile them explicitly — "
    "if a document states a target/policy/benchmark, compare the SQL actual against it (variance, "
    "on/off track) citing BOTH the SQL value and the document's bracketed label; a causal link neither "
    "source states is your reading, so present it as one."
)


def _column_prompt_line(table: str, column: str, descriptions: dict,
                        column_metadata: dict) -> str:
    key = f"{table}.{column}"
    metadata = column_metadata.get(key) or {}
    description = descriptions.get(key) or metadata.get("description")
    technical = []
    if metadata.get("data_type"):
        technical.append(str(metadata["data_type"]))
    nullable = metadata.get("nullable")
    if nullable is False:
        technical.append("NOT NULL")
    elif nullable is True:
        technical.append("nullable")
    suffix = f" [{', '.join(technical)}]" if technical else ""
    return f"    - {column}{suffix}" + (f": {description}" if description else "")


def schema_for_prompt(mdl: dict) -> str:
    """Render the (allowed) schema + admin descriptions as semantic context for NL->SQL.

    `mdl['descriptions']` keys: a bare table name -> the table's description, and
    'table.column' -> that column's description. Only allowed_tables are exposed to the
    model (so it never even sees governed-out tables like internal_costs)."""
    desc = mdl.get("descriptions") or {}
    column_metadata = mdl.get("column_metadata") or {}
    allowed = set(mdl.get("allowed_tables", []))
    out = []
    for t, cols in mdl.get("tables", {}).items():
        if allowed and t not in allowed:
            continue
        td = desc.get(t)
        out.append(f"- {t}" + (f": {td}" if td else ""))
        for c in cols:
            out.append(_column_prompt_line(t, c, desc, column_metadata))
    return "\n".join(out) or "(no tables)"


# ---- catalog pruning (latency/cost): send only question-relevant tables to the LLM ----
# A big warehouse catalog (Acme L2: 104 tables ≈ 105k chars ≈ 26k tokens) was rendered into
# EVERY LLM call of the sql-agent loop — prompt processing dominated answer latency and cost.
# Pruning is DETERMINISTIC (no extra LLM call, no added latency): score each table by
# vocabulary overlap with the question, keep the top-K, list the rest by name only. Recovery
# paths when the selection misses: the omitted-name index (the model still sees what exists),
# the agent's describe_table tool (fetches any omitted table's schema mid-loop), and the
# error-feedback retry loop (govern() validates against the FULL mdl, never the pruned view).
_TOKEN_RE = re.compile(r"[a-z0-9]+")
# question words that carry no table signal (Indonesian + English analytics chatter)
_PRUNE_STOP = {
    "yang", "dari", "untuk", "dengan", "pada", "dan", "atau", "apa", "berapa", "bagaimana",
    "kenapa", "mengapa", "adalah", "tolong", "coba", "buatkan", "tampilkan", "berikan",
    "bandingkan", "dibanding", "dibandingkan", "bulan", "tahun", "hari", "minggu", "total",
    "jumlah", "data", "per", "tiap", "setiap", "antara", "seluruh", "semua", "ini", "itu",
    "the", "and", "for", "with", "from", "what", "how", "why", "many", "much", "show",
    "give", "make", "compare", "month", "year", "week", "day", "all", "this", "that",
    "januari", "februari", "maret", "april", "mei", "juni", "juli", "agustus", "september",
    "oktober", "november", "desember", "january", "february", "march", "june", "july",
    "august", "october", "december",
}


def _q_tokens(text: str) -> set:
    """Signal tokens from the question: alnum, >=3 chars, not a stopword, not pure digits
    (a number like '15' from 'naik 15%' is a VALUE, not vocabulary — same rule as the
    datasource router)."""
    return {t for t in _TOKEN_RE.findall((text or "").lower())
            if len(t) >= 3 and not t.isdigit() and t not in _PRUNE_STOP}


def _score_tables(mdl: dict, qtok: set, visible: list) -> dict:
    """Vocabulary-overlap score per table: name parts x3, column parts x2, curated
    reference VALUES x3 (e.g. 'Shopee' names a dimension value, a strong signal),
    description words x1 — PLUS a curation bonus: when a curated metric's name or a
    golden example's question overlaps the user's question, the tables that metric/
    example actually uses get a strong boost. Many tables may carry an `nmv` column,
    but the curated 'NMV' metric names the CANONICAL one — curation must win ties."""
    # Tenant/company prefixes such as "acme" occur in much of a catalog. Treating such
    # tokens as relevance made open-ended questions rank arbitrary tables by catalog
    # order. Remove table-name tokens shared by >=25% of visible tables (minimum 3).
    counts = {}
    for table in visible:
        for token in set(_TOKEN_RE.findall(str(table).lower())):
            counts[token] = counts.get(token, 0) + 1
    common_cutoff = max(3, math.ceil(len(visible) * 0.25))
    common_name_tokens = {token for token, count in counts.items() if count >= common_cutoff}
    signal_qtok = set(qtok) - common_name_tokens

    desc = mdl.get("descriptions") or {}
    refs = mdl.get("reference_values") or {}
    tabs = mdl.get("tables") or {}
    all_tables = list(tabs)
    bonus = {}
    for contract in mdl.get("business_concepts") or []:
        aliases = [contract.get("name"), *(contract.get("synonyms") or [])]
        if any(_q_tokens(str(alias or "")) and _q_tokens(str(alias or "")) <= qtok
               for alias in aliases):
            table = contract.get("table")
            if table in visible:
                bonus[table] = bonus.get(table, 0) + 100
    for m in mdl.get("metrics") or []:
        if qtok & set(_TOKEN_RE.findall(str(m.get("name", "")).lower())):
            for t in _mentioned_tables(str(m.get("definition", "")), all_tables):
                bonus[t] = bonus.get(t, 0) + 4
    for e in mdl.get("examples") or []:
        if qtok & _q_tokens(str(e.get("q", ""))):
            for t in _mentioned_tables(str(e.get("sql", "")), all_tables):
                bonus[t] = bonus.get(t, 0) + 3
    scores = {}
    for t in visible:
        cols = tabs.get(t) or []
        name_parts = set(_TOKEN_RE.findall(t.lower()))
        col_parts = set()
        for c in cols:
            col_parts |= set(_TOKEN_RE.findall(str(c).lower()))
        dtext = " ".join([str(desc.get(t) or "")] +
                         [str(desc.get(f"{t}.{c}") or "") for c in cols]).lower()
        desc_parts = set(_TOKEN_RE.findall(dtext))
        ref_parts = set()
        col_names = {str(c).lower() for c in cols}
        for dim, vals in refs.items():
            dt, _, dc = str(dim).rpartition(".")
            if (dt and dt != t) or (not dt and str(dim).lower() not in col_names):
                continue
            for v in (vals or [])[:60]:
                ref_parts |= set(_TOKEN_RE.findall(str(v).lower()))
        scores[t] = (3 * len(signal_qtok & name_parts) + 2 * len(signal_qtok & col_parts)
                     + 3 * len(signal_qtok & ref_parts) + len(signal_qtok & desc_parts)
                     + bonus.get(t, 0))
    return scores


def _env_int(name: str, default: int) -> int:
    """Env int with a safe fallback — a malformed value must degrade, never crash a query."""
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _mentioned_tables(text: str, all_tables: list) -> list:
    """Tables referenced in `text` as WHOLE identifiers — substring matching would let
    'sales' claim every mention of 'sales_target' (identifier chars around the name
    disqualify the match; '_' counts as an identifier char)."""
    low = (text or "").lower()
    out = []
    for t in all_tables:
        tl = t.lower()
        if tl in low and re.search(r"(?<![a-z0-9_])" + re.escape(tl) + r"(?![a-z0-9_])", low):
            out.append(t)
    return out


# Public alias. business_scope needs the SAME whole-identifier rule to decide whether an
# inherited metric or example belongs to a slice; a second copy of that regex is how
# 'sales' ends up claiming 'sales_target' in one of them and not the other.
mentioned_tables = _mentioned_tables


_IDENTIFIER = r"([a-z_]\w*)"
_DECLARED_TABLE_RE = re.compile(rf"\b(?:from|dari)\s+`?{_IDENTIFIER}`?", re.I | re.ASCII)
_AGG_COLUMN_RE = re.compile(
    rf"\b(?:sum|avg|min|max|count)\s*\(\s*(?:distinct\s+)?`?{_IDENTIFIER}`?",
    re.I | re.ASCII,
)


def _metric_applicable(metric: dict, tables: dict) -> bool:
    """Whether a curated metric still refers to the current scanned schema.

    Rescans intentionally preserve admin curation, but a removed table/column must not
    remain prompt-visible and steer the model toward stale data.
    """
    definition = str((metric or {}).get("definition") or "")
    table_names = {str(table).lower() for table in tables}
    declared = [name.lower() for name in _DECLARED_TABLE_RE.findall(definition)]
    if declared and not any(name in table_names for name in declared):
        return False
    aggregate_columns = [name.lower() for name in _AGG_COLUMN_RE.findall(definition)]
    if aggregate_columns:
        live_columns = {
            str(column).lower()
            for columns in tables.values()
            for column in (columns or [])
        }
        if not any(column in live_columns for column in aggregate_columns):
            return False
    return True


def _kept_reference_values(mdl: dict, kept: set, columns: set) -> dict:
    result = {}
    for dimension, references in (mdl.get("reference_values") or {}).items():
        name = str(dimension)
        belongs = name.split(".")[0] in kept if "." in name else name.lower() in columns
        if belongs:
            result[dimension] = references
    return result


def _kept_metrics(mdl: dict, kept: set, tables: dict) -> list:
    result = []
    all_tables = list(tables)
    for metric in mdl.get("metrics") or []:
        if not _metric_applicable(metric, tables):
            continue
        text = f"{metric.get('name', '')} {metric.get('definition', '')} {metric.get('note', '')}"
        mentioned = _mentioned_tables(text, all_tables)
        if not mentioned or any(table in kept for table in mentioned):
            result.append(metric)
    return result


def _kept_examples(mdl: dict, kept: set, tables: dict) -> list:
    all_tables = list(tables)
    return [
        example for example in (mdl.get("examples") or [])
        if any(table in kept for table in
               _mentioned_tables(str(example.get("sql", "")), all_tables))
    ]


def _mdl_view(mdl: dict, kept) -> dict:
    """Shallow mdl copy restricted to `kept` tables: descriptions/grain/reference_values/
    metrics/examples are filtered along; PII/allowed_tables etc. stay untouched (governance
    ALWAYS runs on the full mdl — this view only shapes the prompt)."""
    kset = set(kept)
    tabs = mdl.get("tables") or {}
    view = dict(mdl)
    view["tables"] = {t: c for t, c in tabs.items() if t in kset}
    desc = mdl.get("descriptions") or {}
    view["descriptions"] = {k: v for k, v in desc.items()
                            if k in kset or str(k).split(".")[0] in kset}
    view["grain"] = {t: v for t, v in (mdl.get("grain") or {}).items() if t in kset}
    cols_kept = {str(c).lower() for t in kset for c in (tabs.get(t) or [])}
    view["reference_values"] = _kept_reference_values(mdl, kset, cols_kept)
    view["metrics"] = _kept_metrics(mdl, kset, tabs)
    # examples are attributed by their SQL only — prose questions contain common words
    # ("target sales") that would falsely claim table names like `sales`
    view["examples"] = _kept_examples(mdl, kset, tabs)
    view["business_concepts"] = [
        concept for concept in (mdl.get("business_concepts") or [])
        if concept.get("table") in kset
    ]
    view["table_metadata"] = {
        table: value for table, value in (mdl.get("table_metadata") or {}).items()
        if table in kset
    }
    view["column_metadata"] = {
        key: value for key, value in (mdl.get("column_metadata") or {}).items()
        if str(key).split(".", 1)[0] in kset
    }
    view["relationships"] = [
        relation for relation in (mdl.get("relationships") or [])
        if relation.get("from_table") in kset and relation.get("to_table") in kset
    ]
    return view


def rank_table_scores(mdl: dict, text: str, limit: int = 12) -> list[tuple[str, int]]:
    """Return ``(table, score)`` pairs in deterministic relevance order.

    Exposing the scores lets a cross-datasource planner compare candidates instead of
    selecting whichever datasource happened to be listed first.
    """
    tabs = mdl.get("tables") or {}
    allowed = set(mdl.get("allowed_tables") or [])
    visible = [t for t in tabs if not allowed or t in allowed]
    qtok = _q_tokens(text)
    if not qtok:
        return []
    scores = _score_tables(mdl, qtok, visible)
    order = {t: i for i, t in enumerate(visible)}
    ranked = sorted(
        (t for t in visible if scores[t] > 0),
        key=lambda t: (-scores[t], order[t]),
    )
    return [(table, scores[table]) for table in ranked[:max(1, int(limit))]]


def rank_tables(mdl: dict, text: str, limit: int = 12) -> list[str]:
    """Return relevant tables in deterministic score order for planner context."""
    return [table for table, _score in rank_table_scores(mdl, text, limit)]


def pruned_view(mdl: dict, question: str, keep_n: int = None):
    """(mdl_view, dropped_table_names) for the prompt. Returns (mdl, []) — no pruning —
    when disabled, the catalog is already small, the question has no signal tokens, or no
    table scores above zero (no signal = keep everything, correctness over latency)."""
    if os.getenv("CATALOG_PRUNE", "1").lower() not in ("1", "true", "yes"):
        return mdl, []
    tabs = mdl.get("tables") or {}
    allowed = set(mdl.get("allowed_tables") or [])
    visible = [t for t in tabs if not allowed or t in allowed]
    keep_n = max(
        1,
        int(keep_n) if keep_n is not None else _env_int("CATALOG_KEEP_TABLES", 8),
    )
    if len(visible) <= keep_n:
        return mdl, []
    qtok = _q_tokens(question)
    if not qtok:
        return mdl, []
    scores = _score_tables(mdl, qtok, visible)
    order = {t: i for i, t in enumerate(visible)}
    ranked = sorted((t for t in visible if scores[t] > 0), key=lambda t: (-scores[t], order[t]))
    if not ranked:
        return mdl, []
    required = [
        table for table in semantic_contract.required_tables(
            semantic_contract.resolve_mdl(mdl, question)
        )
        if table in visible
    ]
    kept = list(dict.fromkeys(required + ranked))[:max(keep_n, len(required))]
    dropped = [t for t in visible if t not in set(kept)]
    return _mdl_view(mdl, kept), dropped


def _ref_val(v) -> str:
    """Render one reference value for a prompt: length-capped + newline-stripped so a
    hostile/oversized DB value can't sprawl instructions into the prompt."""
    return str(v)[:80].replace("\n", " ").replace("\r", " ")


def describe_tables(mdl: dict, names: list) -> str:
    """Schema + curated context for specific tables, rendered from the FULL mdl — backs the
    agent's describe_table tool so a pruned-out table stays reachable mid-loop."""
    if isinstance(names, str):                    # models emit {"tables": "a, b"} often enough
        names = [n for n in re.split(r"[,\s]+", names) if n]
    tabs = mdl.get("tables") or {}
    allowed = set(mdl.get("allowed_tables") or [])
    lower = {t.lower(): t for t in tabs if not allowed or t in allowed}
    all_tables = list(tabs)
    out = []
    for n in list(names or [])[:5]:
        t = lower.get(str(n).strip().lower())
        if not t:
            valid = list(lower.values())
            shown = ", ".join(valid[:200]) + (f", … +{len(valid) - 200} more" if len(valid) > 200 else "")
            out.append(f"Unknown table '{str(n)[:80]}'. Valid tables: " + shown)
            continue
        view = _mdl_view(mdl, [t])
        block = ["TABLES & COLUMNS:", schema_for_prompt(view)]
        for label, key in (("GRAIN / DEDUP RULES:", "grain"),):
            if view.get(key):
                block.append(label)
                block.extend(f"- {k}: {v}" for k, v in view[key].items())
        if view.get("reference_values"):
            block.append("VALID DIMENSION VALUES:")
            block.extend(f"- {d}: " + ", ".join(_ref_val(v) for v in (vals or [])[:40])
                         for d, vals in view["reference_values"].items())
        # curated metrics that USE this table — otherwise a pruned-out table's metric
        # definitions would be unrecoverable through this recovery path
        table_metrics = [m for m in (mdl.get("metrics") or [])
                         if t in _mentioned_tables(f"{m.get('name', '')} {m.get('definition', '')} "
                                                   f"{m.get('note', '')}", all_tables)]
        if table_metrics:
            block.append("METRICS:")
            block.extend(f"- {m.get('name', '')}: {m.get('definition', '')}" for m in table_metrics)
        table_contracts = [
            contract for contract in (mdl.get("business_concepts") or [])
            if contract.get("table") == t
        ]
        if table_contracts:
            block.append("BUSINESS SEMANTIC CONTRACTS:")
            for contract in table_contracts:
                aliases = ", ".join(
                    [str(contract.get("name") or ""), *(contract.get("synonyms") or [])]
                )
                definition = contract.get("formula") or (
                    f"{contract.get('aggregation', 'SUM')}({contract.get('metric', '')})"
                )
                block.append(
                    f"- {aliases}: {definition}; time={contract.get('time_dimension', '')}"
                )
        for e in (view.get("examples") or [])[:2]:
            block.append(f"EXAMPLE — Q: {e.get('q', '')}\nSQL: {e.get('sql', '')}")
        out.append("\n".join(block))
    return "\n\n".join(out) or "no tables requested"


def catalog_index(mdl: dict, limit: int = 200) -> str:
    """Names-only catalog: the readable tables and the curated metric names. For a chat
    turn that probably needs no data (a greeting, a document question) the full catalog
    is thousands of tokens the model never reads; describe_table gives a table's schema
    when the turn does need it."""
    tabs = mdl.get("tables") or {}
    allowed = set(mdl.get("allowed_tables") or [])
    names = [t for t in tabs if not allowed or t in allowed]
    shown = ", ".join(names[:limit]) + (f", … +{len(names) - limit} more"
                                        if len(names) > limit else "")
    parts = ["TABLES (names only; call describe_table for a table's columns BEFORE "
             "querying it): " + (shown or "none")]
    metrics = [str(m.get("name")) for m in mdl.get("metrics") or []
               if isinstance(m, dict) and m.get("name")]
    if metrics:
        parts.append("CURATED METRICS: " + "; ".join(metrics[:40]))
    return "\n".join(parts)


def catalog_for_prompt(mdl: dict, question: str = None, keep_n: int = None) -> str:
    """Full semantic catalog for the NL->SQL prompt: schema + descriptions PLUS the
    curated layer — metric definitions, valid dimension values, grain/dedup rules, and
    few-shot example queries. Adapted for TenantOS's CURATED data: canonical SAFE_CAST
    expressions are OPTIONAL (columns are already typed); the leverage here is metrics,
    reference values, dedup grain and golden examples. Everything is optional — an empty
    catalog degrades to just `schema_for_prompt`.

    With `question`, a large catalog is PRUNED to the question-relevant tables (top-K by
    vocabulary overlap) + a names-only index of the rest — see pruned_view()."""
    contracts = semantic_contract.resolve_mdl(mdl, question or "") if question else []
    dropped = []
    if question:
        mdl, dropped = pruned_view(mdl, question, keep_n=keep_n)
    parts = []
    contract_text = semantic_contract.prompt_block(contracts)
    if contract_text:
        parts.extend([contract_text, ""])
    parts.extend(["TABLES & COLUMNS:", schema_for_prompt(mdl)])

    metrics = [
        metric for metric in (mdl.get("metrics") or [])
        if _metric_applicable(metric, mdl.get("tables") or {})
    ]
    if metrics:
        parts.append("\nMETRICS — use these exact definitions, do not invent formulas:")
        for m in metrics:
            line = f"- {m.get('name', '')}: {m.get('definition', '')}".rstrip()
            if m.get("note"):
                line += f"  ({m['note']})"
            parts.append(line)

    grain = mdl.get("grain") or {}
    if grain:
        parts.append("\nGRAIN / DEDUP RULES (important to avoid double-counting):")
        for t, note in grain.items():
            parts.append(f"- {t}: {note}")

    table_metadata = mdl.get("table_metadata") or {}
    technical_lines = []
    for table, metadata in table_metadata.items():
        details = []
        partitioning = metadata.get("partitioning") or {}
        if partitioning.get("field"):
            details.append(f"partitioned by {partitioning['field']}")
        if metadata.get("clustering"):
            details.append("clustered by " + ", ".join(map(str, metadata["clustering"])))
        if metadata.get("primary_key"):
            details.append("declared primary key " + ", ".join(map(str, metadata["primary_key"])))
        if details:
            technical_lines.append(f"- {table}: " + "; ".join(details))
    if technical_lines:
        parts.append("\nTECHNICAL TABLE METADATA:")
        parts.extend(technical_lines)

    relationships = mdl.get("relationships") or []
    if relationships:
        parts.append("\nDECLARED RELATIONSHIPS — use only when relevant; preserve the stated join keys:")
        for relation in relationships[:40]:
            parts.append(
                f"- {relation.get('from_table')}.{relation.get('from_column')} -> "
                f"{relation.get('to_table')}.{relation.get('to_column')} "
                f"({relation.get('cardinality') or 'relationship'})"
            )

    refs = mdl.get("reference_values") or {}
    if refs:
        parts.append("\nVALID DIMENSION VALUES — filter using these EXACT values, never guess:")
        for dim, vals in refs.items():
            shown = ", ".join(_ref_val(v) for v in (vals or [])[:40])
            parts.append(f"- {dim}: {shown}")

    examples = mdl.get("examples") or []
    if examples:
        parts.append("\nEXAMPLE QUERIES — when a question matches, imitate the pattern:")
        for e in examples[:8]:
            parts.append(f"Q: {e.get('q', '')}\nSQL: {e.get('sql', '')}")

    if dropped:
        shown = ", ".join(dropped[:200]) + (f", … +{len(dropped) - 200} more" if len(dropped) > 200 else "")
        parts.append(
            "\nOTHER TABLES exist but are NOT detailed above (names only): " + shown + "\n"
            "Prefer the detailed tables. Only if the question clearly needs one of these, get its "
            "schema first (describe_table tool when available) — never guess its columns.")

    return "\n".join(parts)


def rule_based(message: str, mdl: dict) -> str:
    m = message.lower()
    tabs = mdl.get("allowed_tables", [])
    pii = {p.lower() for p in mdl.get("pii_columns", [])}
    if "orders" in tabs and any(w in m for w in ("total","penjualan","sales","omzet","gmv","amount","region","wilayah")):
        return "SELECT region, SUM(amount) AS total FROM orders GROUP BY region ORDER BY total DESC"
    if "customers" in tabs and any(w in m for w in ("pelanggan","customer","nama","name")):
        # Avoid projecting PII columns (e.g. name/email) so the default non-PII role
        # isn't dead-ended by CLAC; fall back to a non-PII aggregate.
        return ("SELECT name, region FROM customers" if "name" not in pii
                else "SELECT region, COUNT(*) AS jumlah_pelanggan FROM customers GROUP BY region ORDER BY jumlah_pelanggan DESC")
    if "orders" in tabs and any(w in m for w in ("order","pesanan","jumlah")):
        return "SELECT COUNT(*) AS jumlah_order FROM orders"
    return f"SELECT * FROM {tabs[0]} LIMIT 10" if tabs else "SELECT 1"

def to_sql(message: str, mdl: dict):
    """Return (sql, engine_used)."""
    if os.getenv("LITELLM_MODEL") and (os.getenv("OPENAI_API_KEY") or os.getenv("LITELLM_API_KEY")):
        try:
            return _llm(message, mdl), "llm"
        except Exception as e:
            # Surface the misconfiguration instead of silently downgrading.
            import logging
            logging.getLogger("nl2sql").warning("LLM NL->SQL failed, falling back to rule-based: %s", e)
    return rule_based(message, mdl), "rule-based"

def _llm(message: str, mdl: dict) -> str:
    import litellm
    schema = schema_for_prompt(mdl)
    prompt = (f"Translate the question to a single safe SQL SELECT over these tables:\n{schema}\n"
              f"Only output SQL.\nQuestion: {message}")
    r = litellm.completion(model=os.getenv("LITELLM_MODEL","gpt-4o-mini"),
                           messages=[{"role":"user","content":prompt}])
    sql = r["choices"][0]["message"]["content"].strip().strip("`")
    return sql[3:].strip() if sql.lower().startswith("sql") else sql
