"""MBR narration (plan phase 4): the model writes the story, the data keeps the numbers.

    report model (every figure already computed) -> one model call per page: headline,
    takeaway, labelled insights, speaker framing -> every figure the model wrote is checked
    against the numbers that page showed it -> one repair round -> the month's synthesis
    (story, drivers, foresight, asks, strategy) -> one review pass over the storyline ->
    written back into the report model the writers read.

Rules this module keeps:
  * the model never computes a figure on its own. Each number it writes must be a number
    the page sent it (rounded the way it is written), or a change, difference, share or
    ratio of two numbers that sit together in the data: the same table row, the same
    column, the same sentence. Anything else is rejected, repaired once, then dropped in
    favour of the rule-written text;
  * PII never reaches the model: values of PII-flagged columns (found by lineage, the same
    way the chat masks result rows) are replaced before anything is sent, and the text
    redactor runs over every string;
  * narration is an addition, never a dependency. No model configured, a failed call, the
    tenant budget, the time budget or MBR_NARRATION=0 -> the page keeps its rule-written
    text and the package ships;
  * claims stay labelled INTERPRETATION / INFERENCE / RECOMMENDATION, with a confidence
    and, for an inference, the strongest alternative reading (the the analyst standard).
"""
from __future__ import annotations

import contextvars
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from ..context import PipelineCancelled
from ..llm import gateway
from ..skills import registry

log = logging.getLogger(__name__)

PURPOSE = "answer_complex"           # the tenant's Primary model, as auto-enrich uses
SKIP_GROUPS = ("Appendix",)
MAX_TABLES, MAX_ROWS, MAX_COLS = 3, 12, 8
LABELS = ("INTERPRETATION", "INFERENCE", "RECOMMENDATION")
CONFIDENCE = ("high", "medium", "low")
LIMITS = {"headline": 130, "takeaway": 240, "insight": 220, "framing": 260,
          "message": 320, "story": 150, "item": 240}


def _switched_off() -> bool:
    return os.getenv("MBR_NARRATION", "1").strip().lower() in ("0", "false", "off", "no")


def enabled(tenant_id: str) -> bool:
    if _switched_off():
        return False
    return gateway.is_configured(tenant_id, PURPOSE)


def _int_env(name, default, lo, hi) -> int:
    try:
        return max(lo, min(hi, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


# ---------------------------------------------------------------- figures
_SCALE = {"k": 1e3, "thousand": 1e3, "rb": 1e3, "ribu": 1e3,
          "m": 1e6, "mn": 1e6, "mio": 1e6, "million": 1e6, "juta": 1e6, "jt": 1e6,
          "b": 1e9, "bn": 1e9, "billion": 1e9, "miliar": 1e9, "milyar": 1e9,
          "t": 1e12, "tn": 1e12, "trillion": 1e12, "triliun": 1e12}
_UNIT = (r"%|percent(?:age points?)?|pp\b|points?\b|x\b|times\b|"
         + "|".join(sorted((re.escape(k) + r"\b" for k in _SCALE), key=len, reverse=True)))
_FIGURE = re.compile(r"(?<![\w.,])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?:\s*(" + _UNIT + r"))?",
                     re.I)
# whole month words only: "45 marketplaces" or "12 declines" must stay figures to trace
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
          r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?")
_NOT_FIGURES = (
    re.compile(r"\b\d{1,2}\s+" + _MONTH + r"(?:\s+\d{4})?", re.I),     # 9 Sep, 24 Feb 2025
    re.compile(r"\b(?!may\b)" + _MONTH + r"\s+\d{1,2}(?:,?\s+\d{4})?\b", re.I),  # Sep 9
    # 9.9, 11.11 sale days; "up 9.9%" or "IDR 1.1 bn" are figures and stay
    re.compile(r"(?<!IDR )(?<!Rp )\b(\d{1,2})\.\1\b(?!\s*(?:%|percent|pp\b|points?\b|x\b|"
               r"times\b|[kmbt]n?\b|mio\b|million|billion|thousand|trillion|juta|jt\b|rb\b|"
               r"ribu|miliar|milyar|triliun))", re.I),
    re.compile(r"\b(?:19|20)\d{2}\b"),                                  # years
    re.compile(r"\b[A-F]\d{1,2}\b|\b(?:Summary|Appendix) \d+\b"),       # page refs: C2
    re.compile(r"SKU\s*\.\.\s*\d+", re.I),                              # SKU ..1234
    re.compile(r"\btop\s+\d+\b", re.I),                                 # top 25
)


@dataclass
class Figure:
    text: str
    value: float
    tol: float
    kind: str                 # plain | pct | pp | mult


def _unit_kind(unit: str) -> tuple[str, float]:
    """(kind, scale) of the unit written after a number ('' when there is none)."""
    if unit.startswith("%") or unit.startswith("percent"):
        return ("pp" if "point" in unit else "pct"), 1.0
    if unit in ("pp", "point", "points"):
        return "pp", 1.0
    if unit in ("x", "times"):
        return "mult", 1.0
    if unit:
        return "plain", _SCALE.get(unit, 1.0)
    return "plain", 1.0


def figures(text: str) -> list[Figure]:
    """Numbers written in ``text`` with the value they stand for and the rounding they
    were written with ('IDR 1.23 bn' -> 1.23e9 +- 5e6)."""
    s = str(text or "")
    for pat in _NOT_FIGURES:
        s = pat.sub(" ", s)
    out = []
    for m in _FIGURE.finditer(s):
        whole, frac, unit = m.group(1), m.group(2) or "", (m.group(3) or "").lower()
        value = float(whole.replace(",", "") + frac)
        step = 10 ** -(len(frac) - 1) if frac else 1.0
        kind, scale = _unit_kind(unit)
        out.append(Figure(m.group(0).strip(), value * scale, 0.5 * step * scale, kind))
    return out


def _exempt(f: Figure) -> bool:
    """Counting words ('3 products', 'two of the top 5') are not figures to trace."""
    return f.kind == "plain" and f.value <= 10 and float(f.value).is_integer() and \
        "." not in f.text


def _pair_derived(kind, a, b):
    """What a figure of ``kind`` may state about two numbers that sit together."""
    if kind == "pct":
        for x, y in ((a, b), (b, a)):
            if y:
                yield abs(x - y) / y * 100       # change
                yield x / y * 100                # share
        yield abs(a - b)                         # gap between two shares
    elif kind == "pp":
        yield abs(a - b)
    elif kind == "mult":
        if a and b:
            yield a / b
            yield b / a
    else:
        yield abs(a - b)
        yield a + b


class Ground:
    """The numbers one prompt showed the model, in groups of numbers that sit together."""

    def __init__(self):
        self.values: list[float] = []
        self.groups: list[list[float]] = []

    def add(self, nums):
        nums = [abs(float(n)) for n in nums if isinstance(n, (int, float))
                and not isinstance(n, bool) and math.isfinite(float(n))]
        if not nums:
            return
        self.values.extend(nums)
        if len(nums) > 1:
            self.groups.append(nums)

    def add_text(self, text):
        self.add([f.value for f in figures(text)])

    def _derived(self, kind):
        for g in self.groups:
            for i, a in enumerate(g):
                for b in g[i + 1:]:
                    yield from _pair_derived(kind, a, b)

    def traced(self, f: Figure) -> bool:
        if _exempt(f):
            return True
        tol = lambda g: max(f.tol, 1e-9 + 0.0005 * abs(g))      # noqa: E731
        if any(abs(f.value - g) <= tol(g) for g in self.values):
            return True
        return any(abs(f.value - d) <= max(f.tol, 1e-9) for d in self._derived(f.kind))


# ---------------------------------------------------------------- text checks
_DASH = re.compile(r"\s{1,10}[-‒–—―]\s{1,10}|[‒–—―]")
_TECH = re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b|\bSQL\b|\b[Ll][23]\b|"
                   r"\b20\d{2}(?:0[1-9]|1[0-2])\b")


# "INTERPRETATION, high confidence: ...", "RECOMMENDATION: ...", "High confidence: ..."
_LEAD = re.compile(r"^(?:(?P<label>FACT|INTERPRETATION|INFERENCE|RECOMMENDATION)\b[\s,]*"
                   r"(?:\(?(?P<c1>high|medium|low) confidence\)?)?|"
                   r"(?P<c2>high|medium|low) confidence)\s*:\s*", re.I)
_ALT_TAIL = re.compile(r"\bAlternative(?: reading| explanation)?:\s{0,5}(?P<alt>\S.*)$", re.I)
# the same label written at the END: "... (INTERPRETATION, high confidence.)", "(RECOMMENDATION.)"
_LABEL_TAIL = re.compile(
    r"\s*\((?=\s*(?:FACT|INTERPRETATION|INFERENCE|RECOMMENDATION|high|medium|low)\b)\s*"
    r"(?:(?P<label>FACT|INTERPRETATION|INFERENCE|RECOMMENDATION)\b[\s,;]*)?"
    r"(?:(?P<c>high|medium|low) confidence)?\s*\.?\s*\)\s*\.?$", re.I)
# page codes written into a text: "... cancelled. (Summary 1, D1, D3)"; cards carry them
_PAGE = r"(?:Summary \d+|Appendix \d+|[A-F]\d{1,2})"
_PAGE_TAIL = re.compile(r"\s*\((?:pages?:?\s*)?(?P<pages>" + _PAGE + r"(?:\s*(?:,|and|&)\s*"
                        + _PAGE + r")*)\)\s*\.?$", re.I)


def _tails(t: str) -> tuple[str, str | None, list]:
    """(text without a trailing label or page list, confidence it named, page codes)."""
    conf, pages = None, []
    for _ in range(3):
        m = _LABEL_TAIL.search(t)
        if m and (m.group("label") or m.group("c")):
            conf = conf or (m.group("c") or "").lower() or None
            t = t[:m.start()].rstrip()
            continue
        m = _PAGE_TAIL.search(t)
        if m:
            pages = [x.strip() for x in re.split(r",|&|\band\b", m.group("pages"))] + pages
            t = t[:m.start()].rstrip()
            continue
        break
    return t, conf, pages


def tidy(text) -> str:
    t = re.sub(r"\s+", " ", _DASH.sub(", ", str(text or ""))).strip()
    return _tails(_LEAD.sub("", t, count=1))[0]


def lead_confidence(text) -> str | None:
    """The confidence a model wrote into a text (at its start or its end) instead of
    in its field."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    m = _LEAD.match(t)
    conf = (m.group("c1") or m.group("c2")) if m else None
    return conf.lower() if conf else _tails(_LEAD.sub("", t, count=1))[1]


def tail_pages(text) -> list:
    """Page codes a model wrote at the end of a text ("(Summary 1, D1)")."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return _tails(_LEAD.sub("", t, count=1))[2]


def _cut(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    head = text[:n]
    end = max(head.rfind(". "), head.rfind("; "))
    return head[:end + 1].strip() if end > n * 0.5 else head.rsplit(" ", 1)[0].rstrip(",;:") + "..."


def check(text, ground: Ground, limit: int) -> tuple[str, list[str]]:
    """(clean text, problems). An empty text is a problem only for required fields."""
    t = tidy(text)
    problems = []
    bad = [f.text for f in figures(t) if not ground.traced(f)]
    if bad:
        problems.append("figures not in this page's data: " + ", ".join(dict.fromkeys(bad)))
    tech = _TECH.findall(t)
    if tech:
        problems.append("technical names, say it in business words: "
                        + ", ".join(dict.fromkeys(tech)))
    if len(t) > limit:
        problems.append(f"longer than {limit} characters")
    return t, problems


# ---------------------------------------------------------------- PII
class _Mask:
    """PII values of this report's query results (by column lineage) plus the regex
    redactor for anything that looks like contact data."""

    def __init__(self, run):
        self.values = _pii_values(run)
        big = sorted(self.values, key=len, reverse=True)[:2000]
        self.pattern = (re.compile("|".join(re.escape(v) for v in big), re.I) if big else None)
        self.redactor = registry.TextPiiRedactor()
        self.lock = threading.Lock()

    def cell(self, v):
        if isinstance(v, str):
            if v.strip().lower() in self.values:
                return "[PII]"
            return self.text(v)
        return v

    def text(self, s) -> str:
        s = str(s or "")
        if self.pattern is not None:
            s = self.pattern.sub("[PII]", s)
        with self.lock:
            return self.redactor.redact(s)


def _pii_values(run) -> set:
    out = set()
    ref_of = getattr(run, "ref", None)
    for rec in (getattr(run, "queries", None) or {}).values():
        if not rec.get("ok") or not rec.get("rows") or not callable(ref_of):
            continue
        _add_masked_values(out, rec, ref_of(rec.get("table")))
    return out


def _add_masked_values(out: set, rec: dict, ref):
    """Adds to ``out`` the values of one query result that the PII masking replaces."""
    mdl = ((ref.datasource or {}).get("mdl") if ref else None) or {}
    if not mdl.get("pii_columns"):
        return
    cols = list(rec.get("columns") or (rec["rows"][0].keys() if rec["rows"] else []))
    rows = [[r.get(c) for c in cols] for r in rec["rows"]]
    _, masked = registry.mask_pii_for_llm(cols, rows, mdl, rec.get("sql") or "",
                                          getattr(ref, "dialect", None))
    for orig, mrow in zip(rows, masked):
        _add_changed(out, orig, mrow)


def _add_changed(out: set, orig: list, mrow: list):
    for a, b in zip(orig, mrow):
        if a != b and isinstance(a, str) and len(a.strip()) >= 3:
            out.add(a.strip().lower())


# ---------------------------------------------------------------- what the model sees
def _round(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return v
    if not math.isfinite(float(v)):
        return None
    return round(v) if abs(v) >= 1000 else round(float(v), 2)


def _context(model: dict) -> dict:
    inp = model.get("inputs") or {}
    out = {"brand": model.get("brand"), "review_month": model.get("period_label"),
           "business": (model.get("business") or {}).get("name"),
           "data_as_of": model.get("data_as_of"),
           "currency": "IDR (Indonesian rupiah)"}
    if inp.get("takeover_date"):
        out["acme_took_over_the_store_on"] = inp["takeover_date"]
    if (inp.get("target") or {}).get("nmv"):
        out["nmv_target_review_month"] = inp["target"]["nmv"]
    if (inp.get("next_month_target") or {}).get("nmv"):
        out["nmv_target_next_month"] = inp["next_month_target"]["nmv"]
    if inp.get("campaigns"):
        out["campaigns"] = [c.get("name") for c in inp["campaigns"]][:8]
    if inp.get("focus"):
        out["brand_focus"] = inp["focus"]
    return out


def _context_ground(ctx: dict, ground: Ground):
    ground.add([v for k, v in ctx.items() if k.startswith("nmv_target")])


def page_card(sec: dict, mask: _Mask, ground: Ground, max_tables: int = MAX_TABLES,
              max_rows: int = MAX_ROWS) -> dict:
    """The page as the model sees it; every number in it lands in ``ground``."""
    card = {"page": sec.get("ref"), "title": sec.get("title"), "status": sec.get("status"),
            "computed_headline": mask.text(sec.get("headline") or "")}
    ground.add_text(card["title"])                   # "13 month trend"
    ground.add_text(card["computed_headline"])
    card["facts"] = _card_facts(sec, mask, ground)
    tables = []
    for t in (sec.get("tables") or [])[:max_tables]:
        if t.get("rows"):
            tables.append(_card_table(t, mask, ground, max_rows))
    card["tables"] = tables
    card["missing"] = [{"what": mask.text(m.get("what")), "why": mask.text(m.get("why"))}
                       for m in (sec.get("missing") or [])[:4]]
    return card


def _card_facts(sec: dict, mask: _Mask, ground: Ground) -> list:
    facts = []
    for f in sec.get("facts") or []:
        if f.get("by") == "ai":
            continue
        text = mask.text(f.get("text"))
        facts.append({"label": f.get("label", "FACT"), "text": text})
        ground.add_text(text)
    return facts


def _card_table(t: dict, mask: _Mask, ground: Ground, max_rows: int) -> dict:
    rows = t["rows"]
    cols = list(t.get("deck_columns") or range(len(t.get("columns") or [])))[:MAX_COLS]
    names = [str((t.get("columns") or [])[i]) for i in cols
             if i < len(t.get("columns") or [])]
    sent = []
    for r in rows[:max_rows]:
        sent.append(_card_row(r, cols, mask, ground))
    for j in range(len(names)):                  # a column is a group too
        ground.add([row[j] for row in sent if j < len(row)
                    and isinstance(row[j], (int, float))])
    entry = {"title": t.get("title"), "columns": names, "rows": sent}
    if len(rows) > len(sent):
        entry["rows_not_shown"] = len(rows) - len(sent)
    return entry


def _card_row(r, cols: list, mask: _Mask, ground: Ground) -> list:
    cells = [mask.cell(_round(r[i])) if i < len(r) else None for i in cols]
    ground.add([c for c in cells if isinstance(c, (int, float))])
    for c in cells:
        if isinstance(c, str):
            ground.add_text(c)
    return cells


# ---------------------------------------------------------------- prompts
SYSTEM = """You write the narrative of a Monthly Business Review deck that Acme, the company \
that runs the brand's online store, presents to the brand. The reader is a senior executive.

How to write:
- A headline is the answer, not the topic: one conclusion a manager remembers tomorrow \
("NMV grew on more buyers, not higher prices"), never a label ("Sales trend").
- Think in this order: answer, evidence, why it happened, so what, what is likely next, \
what to do.
- Look for tension between measures (sales up while order value falls, growth while \
cancellations rise, target met while a hero product runs out) and name it when it matters.
- Good news: the metric, the improvement, why it matters, what it enables next. Bad news: \
plainly and diplomatically, then the ask.
- Acme speaks as the brand's business consultant; frame problems as asks to the brand.
- When the context gives the brand's focus (brand_focus), every move and ask follows it; \
never push a product or range the brand has deprioritised.
- Plain English, short sentences, punchy but professional.

Numbers (hard rules, checked by a program; a sentence that breaks one is deleted):
- Use only numbers that appear in the data you are given, rounded the way you write them.
- You may state a change, difference, share or ratio of two numbers only when both sit in \
the same table row, the same table column or the same sentence of the data.
- Never estimate, extrapolate or invent a figure, date or target. When a number you would \
want is not there, say it in words ("well above", "about half") or leave it out.
- Money as "IDR 1.23 bn", "IDR 456.7 m" or "IDR 12,345". A change as "from X to Y (up 12.3%)".
- Percentages with one decimal ("93.0%", not "92.98%"), even when the data shows more.

Claims:
- Label every insight: INTERPRETATION (what the evidence most reasonably means), \
INFERENCE (a cause, scenario or forecast beyond what is observed), RECOMMENDATION (an \
action). Never write an inference as if it were a fact.
- Give each insight a confidence (high, medium, low); give every INFERENCE the strongest \
alternative explanation.
- When part of a page's data is missing, say what cannot be concluded yet.

Style:
- No dash characters as punctuation (no spaced hyphen, no en or em dash). Ranges as \
"10 to 23 Aug".
- Never mention tables, columns, queries, SQL, names with underscores or data layers; \
speak in business terms.
- Never put a label, a confidence or page codes inside a text, at its start or its end \
("INTERPRETATION:", "(high confidence)", "(Summary 1, D1)"): the JSON fields carry them, \
the text is what the reader sees.
- Reply with one JSON object only."""

PAGE_ASK = """Report context: {context}

Page {ref}, "{title}". The data on this page:
{card}

Reply with this JSON:
{{"headline": "conclusion led slide title, at most 120 characters",
 "takeaway": "the so what for the brand, or the ask, in one or two sentences, at most 220 characters",
 "insights": [{{"label": "INTERPRETATION or INFERENCE or RECOMMENDATION", "text": "at most 200 characters", "confidence": "high, medium or low", "alternative": "strongest alternative explanation, for an INFERENCE"}}],
 "framing": "speaker note: why this page matters, one sentence",
 "message": "speaker note: what to say on this page, at most two sentences"}}
Give two or three insights."""

SYNTHESIS_ASK = """Report context: {context}

Every page of the review, in deck order (headline, takeaway, key facts, insights, what is \
missing):
{pages}

Write the synthesis of the month: the one message for the executive summary, the few \
drivers that explain it, what is likely next, the asks of the brand and the strategy for \
next month. Each item names the pages (page codes such as "A1", "C2") it rests on.
Reply with this JSON:
{{"story": "the one dominant message of the month, at most 140 characters",
 "summary": [{{"text": "a key point, at most 200 characters", "pages": ["A1"]}}],
 "tension": "the most material tension between measures, or empty",
 "drivers": [{{"text": "why it changed, at most 200 characters", "confidence": "high, medium or low", "alternative": "strongest alternative explanation", "pages": ["C1"]}}],
 "foresight": [{{"text": "what is likely next and what it depends on, at most 200 characters", "confidence": "high, medium or low", "alternative": "what would make it wrong", "pages": ["F1"]}}],
 "asks": [{{"text": "an ask of the brand, with the reason, at most 200 characters", "pages": ["D2"]}}],
 "strategy": [{{"action": "a move for next month, at most 90 characters", "why": "the evidence, at most 120 characters", "impact": "what it should change, at most 90 characters", "assumption": "what must hold true, at most 90 characters", "pages": ["C3"]}}],
 "decisions": [{{"text": "a decision to take this week, at most 160 characters", "owner": "who decides: the brand, Acme, or both", "when": "by when, for example this week or before the next campaign", "unlocks": "what the decision unlocks, at most 90 characters", "pages": ["C4"]}}]}}
Three to five summary points, two to four drivers, one or two foresight items, two to four \
asks, two to four strategy moves, two or three decisions for this week (the meeting closes \
on them: each has one owner, a date and what it unlocks)."""

REVIEW_ASK = """You are the reviewing manager. Read the storyline of this Monthly Business \
Review and find at most six real problems: two pages that contradict each other, a \
headline without a so what, a claim stronger than its evidence (a cause stated as fact), a \
material tension the story ignores, a takeaway that repeats its headline.

Report context: {context}

Storyline:
{storyline}

For each problem give a better replacement text when the fix is a rewrite of a headline, \
a takeaway or the story (same number rules as before: only numbers already in the \
storyline). Reply with this JSON:
{{"problems": [{{"page": "page code or SYNTHESIS", "field": "headline, takeaway or story", "problem": "what is wrong", "replacement": "the rewritten text, or empty"}}]}}
An empty list is a valid answer when the storyline holds together."""


# ---------------------------------------------------------------- model calls
@dataclass
class Tally:
    calls: int = 0
    failed: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    model: str = ""
    errors: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    stop: bool = False                      # budget refused or provider down: no more calls
    in_a_row: int = 0

    def add(self, res):
        with self.lock:
            self.in_a_row = 0
            self.calls += 1
            self.prompt_tokens += int(res.prompt_tokens or 0)
            self.completion_tokens += int(res.completion_tokens or 0)
            self.cost += float(res.cost or 0.0)
            self.model = res.model or self.model

    def fail(self, what, exc, call=True):
        with self.lock:
            self.failed += 1
            if call:
                self.in_a_row += 1
                if self.in_a_row >= 3 and not self.calls:
                    self.stop = True
            if len(self.errors) < 10:
                self.errors.append(f"{what}: {type(exc).__name__}: {str(exc)[:200]}")


def _ask(tenant_id, messages, tally: Tally, what: str, max_tokens: int):
    """One model call -> (parsed JSON or None, reply text). Never raises except Stop."""
    if tally.stop:
        return None, ""
    try:
        res = gateway.complete(tenant_id, PURPOSE, messages, max_tokens=max_tokens,
                               timeout=_int_env("MBR_NARRATION_CALL_TIMEOUT_SEC", 240, 30, 900))
    except PipelineCancelled:
        raise
    except Exception as exc:                 # LLMUnavailable, BudgetExceeded, provider error
        tally.fail(what, exc)
        if type(exc).__name__ == "BudgetExceeded":
            tally.stop = True
        return None, ""
    tally.add(res)
    data = gateway._json_object(res.text)
    if data is None:
        tally.fail(what, ValueError("reply was not a JSON object"), call=False)
    return data, res.text or ""


def _converse(tenant_id, prompt, tally, what, max_tokens, review):
    """Ask, check with ``review(data) -> problems``, and ask once more with the problems
    named when there are any. Returns (data, problems left, repaired?)."""
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    data, text = _ask(tenant_id, messages, tally, what, max_tokens)
    if data is None:
        return None, ["no usable reply"], False
    problems = review(data)
    if not problems:
        return data, [], False
    fix = ("Some parts break the rules. Fix them and reply with the full JSON again; drop a "
           "claim you cannot support with the data.\n- " + "\n- ".join(problems[:12]))
    again, _ = _ask(tenant_id, messages + [{"role": "assistant", "content": text},
                                           {"role": "user", "content": fix}],
                    tally, what + " (repair)", max_tokens)
    if again is None:
        return data, problems, False
    return again, review(again), True


# ---------------------------------------------------------------- pages
def _insights(raw, ground: Ground, rejected: list, page: str) -> list:
    out = []
    for it in (raw if isinstance(raw, list) else [])[:4]:
        if not isinstance(it, dict):
            continue
        item = _insight(it, ground, rejected, page)
        if item is not None:
            out.append(item)
    return out[:3]


def _insight(it: dict, ground: Ground, rejected: list, page: str) -> dict | None:
    """One insight that passes the checks, else None (a failed text lands in ``rejected``)."""
    label = str(it.get("label") or "").upper().strip()
    label = label if label in LABELS else "INTERPRETATION"
    text, problems = check(it.get("text"), ground, LIMITS["insight"])
    alt, alt_problems = check(it.get("alternative"), ground, LIMITS["insight"])
    if not text or problems:
        if text:
            rejected.append({"page": page, "field": "insight", "text": text,
                             "reason": "; ".join(problems)})
        return None
    conf = str(it.get("confidence") or lead_confidence(it.get("text")) or "").lower().strip()
    item = {"label": label, "text": text,
            "confidence": conf if conf in CONFIDENCE else "medium"}
    if label == "INFERENCE" and alt and not alt_problems:
        item["alternative"] = alt
    return item


def _page_problems(data, ground: Ground) -> list:
    problems = []
    for key in ("headline", "takeaway", "framing", "message"):
        text, p = check(data.get(key), ground, LIMITS[key])
        if key in ("headline", "takeaway") and not text:
            p = ["missing"]
        problems += [f"{key}: {x}" for x in p]
    for i, it in enumerate(data.get("insights") if isinstance(data.get("insights"), list)
                           else []):
        if isinstance(it, dict):
            _, p = check(it.get("text"), ground, LIMITS["insight"])
            problems += [f"insight {i + 1}: {x}" for x in p]
    return problems


def narrate_page(tenant_id, sec, context, mask, tally, deadline) -> dict:
    """Narration for one page: {status, headline?, takeaway?, insights, framing?, message?,
    rejected}. Fields that never pass the checks are left out (rule text stays)."""
    page = sec.get("ref") or sec.get("id")
    if time.monotonic() > deadline:
        return {"status": "skipped", "reason": "time budget", "rejected": []}
    ground = Ground()
    _context_ground(context, ground)
    card = page_card(sec, mask, ground)
    prompt = PAGE_ASK.format(context=json.dumps(context, ensure_ascii=False),
                             ref=page, title=sec.get("title"),
                             card=json.dumps(card, ensure_ascii=False, default=str))
    data, _, repaired = _converse(tenant_id, prompt, tally, f"page {page}", 4000,
                                  lambda d: _page_problems(d, ground))
    if data is None:
        return {"status": "fallback", "reason": "no usable reply", "rejected": []}
    rejected = []
    out = _page_texts(data, ground, rejected, page)
    out["insights"] = _insights(data.get("insights"), ground, rejected, page)
    out["rejected"] = rejected
    out["status"] = _page_status(out, rejected, repaired)
    out["_ground"] = ground
    return out


def _page_texts(data: dict, ground: Ground, rejected: list, page) -> dict:
    """{"rejected": [], <each text field that passes>}; a field that is only too long is
    cut, one that fails otherwise lands in ``rejected``."""
    out = {"rejected": []}
    for key in ("headline", "takeaway", "framing", "message"):
        text, p = check(data.get(key), ground, LIMITS[key])
        only_long = p and all(x.startswith("longer than") for x in p)
        if text and (not p or only_long):
            out[key] = _cut(text, LIMITS[key])
        elif text:
            rejected.append({"page": page, "field": key, "text": text, "reason": "; ".join(p)})
    return out


def _page_status(out: dict, rejected: list, repaired: bool) -> str:
    if "headline" in out and "takeaway" in out and not rejected:
        return "repaired" if repaired else "written"
    if any(k in out for k in ("headline", "takeaway")) or out["insights"]:
        return "partial"
    return "fallback"


def _apply_page(sec, n: dict):
    sec["narration"] = {k: v for k, v in n.items()
                        if k != "rejected" and not k.startswith("_")}
    if n.get("headline"):
        sec["computed_headline"] = sec.get("headline") or ""
        sec["headline"] = n["headline"]
    if n.get("takeaway"):
        sec["takeaway"] = n["takeaway"]
    for it in n.get("insights") or []:
        f = {"label": it["label"], "text": it["text"], "source": "narration", "by": "ai",
             "confidence": it["confidence"]}
        if it.get("alternative"):
            f["alternative"] = it["alternative"]
        sec["facts"].append(f)


def narratable(sec) -> bool:
    if sec.get("group") in SKIP_GROUPS or sec.get("id") == "synthesis":
        return False
    if sec.get("status") not in ("complete", "partial"):
        return False
    return bool(sec.get("facts") or any(t.get("rows") for t in sec.get("tables") or []))


# ---------------------------------------------------------------- synthesis
def _pages_card(sections, mask, ground: Ground) -> tuple[list, dict]:
    out, refs = [], {}
    for s in sections:
        if s.get("group") in SKIP_GROUPS or s.get("id") == "synthesis":
            continue
        facts = [mask.text(f["text"]) for f in s.get("facts") or []
                 if f.get("label") == "FACT"][:4]
        insights = [mask.text(f["text"]) for f in s.get("facts") or [] if f.get("by") == "ai"]
        entry = {"page": s.get("ref"), "title": s.get("title"), "status": s.get("status"),
                 "headline": mask.text(s.get("headline") or ""),
                 "takeaway": mask.text(s.get("takeaway") or ""),
                 "facts": facts, "insights": insights,
                 "missing": [mask.text(m.get("what")) for m in (s.get("missing") or [])[:3]]}
        for t in [entry["headline"], entry["takeaway"]] + facts + insights:
            ground.add_text(t)
        out.append(entry)
        refs[s.get("ref")] = s["id"]
    return out, refs


def _synthesis_problems(data, ground) -> list:
    problems = []
    _, p = check(data.get("story"), ground, LIMITS["story"])
    if not tidy(data.get("story")):
        p = ["missing"]
    problems += [f"story: {x}" for x in p]
    for key, fields in (("summary", ("text",)), ("drivers", ("text", "alternative")),
                        ("foresight", ("text",)), ("asks", ("text",)),
                        ("strategy", ("action", "why", "impact", "assumption")),
                        ("decisions", ("text", "unlocks"))):
        for i, it in enumerate(data.get(key) if isinstance(data.get(key), list) else []):
            if not isinstance(it, dict):
                continue
            for f in fields:
                _, p = check(it.get(f), ground, LIMITS["item"])
                problems += [f"{key} {i + 1} {f}: {x}" for x in p]
    if tidy(data.get("tension")):
        _, p = check(data.get("tension"), ground, LIMITS["item"])
        problems += [f"tension: {x}" for x in p]
    return problems


def _clean_items(raw, fields, ground, refs, rejected, key, limit) -> list:
    """``fields[0]`` is the item; an optional field that fails is dropped on its own."""
    out = []
    for it in (raw if isinstance(raw, list) else [])[:limit]:
        if not isinstance(it, dict):
            continue
        item = _clean_item(dict(it), fields, ground, refs, rejected, key)
        if item is not None:
            out.append(item)
    return out


def _clean_item(it: dict, fields, ground, refs, rejected, key) -> dict | None:
    """One item (``it`` is a copy, edited in place) or None when its main field fails."""
    main = fields[0]
    lead = lead_confidence(it.get(main))             # read before tidy strips it
    _inline_alternative(it, fields)
    item, bad = _item_fields(it, fields, ground)
    if bad or not item.get(main):
        if tidy(it.get(main)):
            rejected.append({"page": "SYNTHESIS", "field": key,
                             "text": tidy(it.get(main)), "reason": " | ".join(bad)})
        return None
    if "confidence" in it or lead:
        conf = str(it.get("confidence") or lead or "").lower().strip()
        item["confidence"] = conf if conf in CONFIDENCE else "medium"
    item["pages"] = _item_pages(it, main, refs)
    return item


def _inline_alternative(it: dict, fields):
    main = fields[0]
    if "alternative" in fields and not tidy(it.get("alternative")):
        m = _ALT_TAIL.search(tidy(it.get(main)))       # "... Alternative: Y" inline
        if m:
            it["alternative"] = m.group("alt")
            it[main] = tidy(it.get(main))[:m.start()].rstrip()


def _item_pages(it: dict, main: str, refs: dict) -> list:
    """The section ids of the page codes an item names, in its field or its text."""
    named = it.get("pages") or tail_pages(it.get(main))
    return list(dict.fromkeys(refs[p] for p in named
                              if isinstance(p, str) and p in refs))[:4]


def _item_fields(it: dict, fields, ground) -> tuple[dict, list]:
    """(the fields that pass, the main field's problems)."""
    item, bad = {}, []
    main = fields[0]
    for f in fields:
        text, p = check(it.get(f), ground, LIMITS["item"])
        if p and f == main:
            bad.append(f"{f}: {'; '.join(p)}")
        elif text and not p:
            item[f] = text
    return item, bad


def synthesize(tenant_id, sections, context, mask, tally) -> dict | None:
    ground = Ground()
    _context_ground(context, ground)
    pages, refs = _pages_card(sections, mask, ground)
    if not pages:
        return None
    prompt = SYNTHESIS_ASK.format(context=json.dumps(context, ensure_ascii=False),
                                  pages=json.dumps(pages, ensure_ascii=False, default=str))
    data, _left, repaired = _converse(tenant_id, prompt, tally, "synthesis", 6000,
                                      lambda d: _synthesis_problems(d, ground))
    if data is None:
        return None
    rejected = []
    story, p = check(data.get("story"), ground, LIMITS["story"])
    if p and not all(x.startswith("longer than") for x in p):
        rejected.append({"page": "SYNTHESIS", "field": "story", "text": story,
                         "reason": "; ".join(p)})
        story = ""
    tension, tp = check(data.get("tension"), ground, LIMITS["item"])
    out = {
        "story": _cut(story, LIMITS["story"]) if story else "",
        "tension": tension if tension and not tp else "",
        "summary": _clean_items(data.get("summary"), ("text",), ground, refs, rejected,
                                "summary", 5),
        "drivers": _clean_items(data.get("drivers"), ("text", "alternative"), ground, refs,
                                rejected, "drivers", 4),
        "foresight": _clean_items(data.get("foresight"), ("text", "alternative"), ground,
                                  refs, rejected, "foresight", 2),
        "asks": _clean_items(data.get("asks"), ("text",), ground, refs, rejected, "asks", 4),
        "strategy": _clean_items(data.get("strategy"), ("action", "why", "impact",
                                                        "assumption"),
                                 ground, refs, rejected, "strategy", 4),
        "decisions": _clean_items(data.get("decisions"), ("text", "owner", "when", "unlocks"),
                                  ground, refs, rejected, "decisions", 3),
        "repaired": repaired,
        "rejected": rejected,
        "_ground": ground,
    }
    if not (out["story"] or out["summary"]):
        return None
    return out


def _apply_synthesis(sec, syn: dict, by_id: dict):
    """The synthesis page: the story as its headline, every item as a labelled claim."""
    sec["synthesis"] = {k: v for k, v in syn.items() if not k.startswith("_")
                        and k != "rejected"}
    if syn.get("story"):
        sec["computed_headline"] = sec.get("headline") or ""
        sec["headline"] = syn["story"]
    sec["facts"] = _synthesis_readings(syn, by_id) + _synthesis_moves(syn, by_id)
    sec["missing"] = []
    sec["status"] = "complete"
    if syn.get("summary"):
        sec["takeaway"] = syn["summary"][0]["text"]


def _synthesis_readings(syn: dict, by_id: dict) -> list:
    """Summary, tension, drivers and foresight as labelled claims."""
    facts = []
    for it in syn.get("summary") or []:
        facts.append(_claim("INTERPRETATION", it["text"], _source(it, by_id), "high"))
    if syn.get("tension"):
        facts.append({"label": "INTERPRETATION", "text": "Tension: " + syn["tension"],
                      "by": "ai", "source": "narration", "confidence": "medium"})
    for it in syn.get("drivers") or []:
        facts.append(_inference(it, it["text"], by_id))
    for it in syn.get("foresight") or []:
        facts.append(_inference(it, "Next: " + it["text"], by_id))
    return facts


def _synthesis_moves(syn: dict, by_id: dict) -> list:
    """Asks, strategy and this week's decisions as recommendations."""
    facts = []
    for it in syn.get("asks") or []:
        facts.append(_claim("RECOMMENDATION", "Ask: " + it["text"], _source(it, by_id)))
    for it in syn.get("strategy") or []:
        facts.append(_claim("RECOMMENDATION", _strategy_text(it), _source(it, by_id)))
    for it in syn.get("decisions") or []:
        facts.append(_claim("RECOMMENDATION", _decision_text(it), _source(it, by_id)))
    return facts


def _source(it: dict, by_id: dict) -> str:
    """The page codes an item rests on, as its claim's source."""
    return ", ".join(by_id[i]["ref"] for i in it.get("pages") or []
                     if i in by_id) or "narration"


def _claim(label: str, text: str, pages: str, confidence: str = "medium") -> dict:
    return {"label": label, "text": text, "by": "ai", "source": "narration",
            "pages": pages, "confidence": confidence}


def _inference(it: dict, text: str, by_id: dict) -> dict:
    f = _claim("INFERENCE", text, _source(it, by_id), it.get("confidence", "medium"))
    if it.get("alternative"):
        f["alternative"] = it["alternative"]
    return f


def _strategy_text(it: dict) -> str:
    return it["action"] + "".join(f" {name}: {it[k]}" for k, name in (
        ("why", "Why"), ("impact", "Expected impact"), ("assumption", "Assumption"))
        if it.get(k))


def _decision_text(it: dict) -> str:
    return "Decision this week: " + it["text"] + "".join(
        f" {name}: {it[k]}." for k, name in (("owner", "Owner"), ("when", "By"),
                                             ("unlocks", "Unlocks")) if it.get(k))


# ---------------------------------------------------------------- review
# the field a review rewrite replaces, by the field the reviewer named
_REWRITES = {"headline": "headline", "story": "headline", "takeaway": "takeaway"}


def review(tenant_id, sections, syn_sec, context, mask, tally, grounds: dict) -> list:
    """One pass of a reviewing manager over the storyline; rewrites that pass the same
    checks are applied, every problem is kept for the open items."""
    lines, by_ref = _storyline(sections, syn_sec, mask)
    if len(lines) < 2:
        return []
    prompt = REVIEW_ASK.format(context=json.dumps(context, ensure_ascii=False),
                               storyline=json.dumps(lines, ensure_ascii=False))
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    data, _ = _ask(tenant_id, messages, tally, "review", 5000)
    out = []
    for p in (data or {}).get("problems") or []:
        if not isinstance(p, dict):
            continue
        item = _review_item(p, syn_sec, by_ref, grounds)
        if item["problem"]:
            out.append(item)
    return out[:6]


def _storyline(sections, syn_sec, mask: _Mask) -> tuple[list, dict]:
    """(what the reviewer reads, the narrated pages by page code)."""
    lines, by_ref = [], {}
    for s in sections:
        if s.get("group") in SKIP_GROUPS or (s.get("narration") or {}).get("status") \
                not in ("written", "repaired", "partial"):
            continue
        by_ref[s["ref"]] = s
        lines.append({"page": s["ref"], "title": s["title"],
                      "headline": mask.text(s.get("headline")),
                      "takeaway": mask.text(s.get("takeaway") or ""),
                      "insights": [mask.text(f["text"]) for f in s["facts"]
                                   if f.get("by") == "ai"]})
    if syn_sec is not None and syn_sec.get("synthesis"):
        lines.append({"page": "SYNTHESIS", "story": syn_sec.get("headline"),
                      "points": [f["text"] for f in syn_sec.get("facts") or []]})
    return lines, by_ref


def _review_item(p: dict, syn_sec, by_ref: dict, grounds: dict) -> dict:
    """One problem the reviewer named; its rewrite is applied when it passes the checks."""
    page, fld = str(p.get("page") or "").strip(), str(p.get("field") or "").lower().strip()
    item = {"page": page, "field": fld, "problem": tidy(p.get("problem"))[:300],
            "applied": False}
    repl = tidy(p.get("replacement"))
    target = syn_sec if page.upper() == "SYNTHESIS" else by_ref.get(page)
    key = _REWRITES.get(fld)
    if repl and target is not None and key:
        _rewrite(item, repl, target, key, syn_sec, grounds.get(target["id"]))
    return item


def _rewrite(item: dict, repl: str, target: dict, key: str, syn_sec, ground):
    is_story = target is syn_sec and key == "headline"
    if ground is not None and target is not syn_sec:
        _ground_own_insights(ground, target)
    limit = LIMITS["story" if is_story else key]
    text, probs = check(repl, ground, limit) if ground else (repl, ["no data"])
    if probs:
        item["rejected_replacement"] = text
        item["reason"] = "; ".join(probs)
        return
    target[key] = text
    if is_story:                                       # the deck reads the story
        syn_sec["synthesis"]["story"] = text
    item["applied"] = True
    item["replacement"] = text


def _ground_own_insights(ground: Ground, target: dict):
    for f in target.get("facts") or []:       # its own approved insights
        if f.get("by") == "ai":
            ground.add_text(f["text"])


# ---------------------------------------------------------------- the step
@dataclass
class _Job:
    """What every step of one narration run shares."""
    tenant_id: str
    context: dict
    mask: _Mask
    tally: Tally
    deadline: float
    report: Callable | None = None

    def progress(self, pct: int, message: str):
        if self.report:
            self.report(pct, "narrating", message)


def _skip_reason(tenant_id) -> str | None:
    if enabled(tenant_id):
        return None
    return "switched off" if _switched_off() else "no model configured for the tenant"


def _narrate_pages(job: _Job, pages: list) -> dict:
    """Every page in parallel -> {section id: narration}."""
    results = {}
    workers = _int_env("MBR_NARRATION_WORKERS", 4, 1, 8)
    job.progress(86, f"Writing the story: 0 of {len(pages)} pages")
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mbr-narrate")
    try:
        futures = {pool.submit(contextvars.copy_context().run, narrate_page, job.tenant_id, s,
                               job.context, job.mask, job.tally, job.deadline): s
                   for s in pages}
        for done, fut in enumerate(as_completed(futures), start=1):
            sec = futures[fut]
            results[sec["id"]] = _page_result(fut, sec, job.tally)
            job.progress(86 + int(8 * done / max(1, len(pages))),
                         f"Writing the story: {done} of {len(pages)} pages")
    except PipelineCancelled:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return results


def _page_result(fut, sec: dict, tally: Tally) -> dict:
    try:
        return fut.result()
    except PipelineCancelled:
        raise
    except Exception as exc:          # a narration bug never costs the page
        log.exception("mbr narration page %s failed", sec.get("id"))
        tally.fail(f"page {sec.get('ref')}", exc)
        return {"status": "fallback", "reason": "error", "rejected": []}


def _apply_pages(pages: list, results: dict, grounds: dict) -> list:
    """Writes each page's narration into it; returns the texts its checks rejected."""
    rejected = []
    for sec in pages:
        n = results.get(sec["id"]) or {"status": "fallback", "rejected": []}
        rejected += n.get("rejected") or []
        grounds[sec["id"]] = n.pop("_ground", None)
        _apply_page(sec, n)
    return rejected


def _synthesis_step(job: _Job, sections, syn_sec, grounds: dict, rejected: list):
    """The synthesis written into its page (None when there is none); its rejected
    texts join ``rejected``."""
    if syn_sec is None or time.monotonic() >= job.deadline:
        return None
    job.progress(95, "Writing the synthesis, asks and strategy")
    try:
        syn = synthesize(job.tenant_id, sections, job.context, job.mask, job.tally)
    except PipelineCancelled:
        raise
    except Exception as exc:
        log.exception("mbr synthesis failed")
        job.tally.fail("synthesis", exc)
        return None
    if syn:
        grounds[syn_sec["id"]] = syn.pop("_ground")
        rejected.extend(syn.get("rejected") or [])
        _apply_synthesis(syn_sec, syn, {s["id"]: s for s in sections})
    return syn


def _review_step(job: _Job, sections, pages, syn_sec, grounds: dict) -> list:
    if time.monotonic() >= job.deadline or not any(s.get("narration") for s in pages):
        return []
    job.progress(97, "Reviewing the storyline")
    try:
        return review(job.tenant_id, sections, syn_sec, job.context, job.mask, job.tally,
                      grounds)
    except PipelineCancelled:
        raise
    except Exception as exc:
        log.exception("mbr narration review failed")
        job.tally.fail("review", exc)
        return []


def _overall_status(written: int, total: int, syn) -> str:
    if written == total and syn:
        return "written"
    if written or syn:
        return "partial"
    return "failed"


def narrate(run, model: dict, report=None) -> dict:
    """Write the story into ``model`` (in place) and return the narration summary."""
    tenant_id = model.get("tenant_id") or getattr(run, "tenant_id", "")
    started = time.monotonic()
    why = _skip_reason(tenant_id)
    if why:
        return {"status": "skipped", "reason": why}
    sections = model.get("sections") or []
    pages = [s for s in sections if narratable(s)]
    mask = _Mask(run)
    context = _context(model)
    tally = Tally()
    budget = _int_env("MBR_NARRATION_BUDGET_SEC", 900, 60, 3600)
    job = _Job(tenant_id, context, mask, tally, started + budget, report)
    grounds = {}
    results = _narrate_pages(job, pages)
    rejected = _apply_pages(pages, results, grounds)

    syn_sec = next((s for s in sections if s.get("id") == "synthesis"), None)
    syn = _synthesis_step(job, sections, syn_sec, grounds, rejected)
    problems = _review_step(job, sections, pages, syn_sec if syn else None, grounds)

    statuses = {s["id"]: (results.get(s["id"]) or {}).get("status", "fallback") for s in pages}
    written = sum(1 for v in statuses.values() if v in ("written", "repaired", "partial"))
    return {
        "status": _overall_status(written, len(pages), syn),
        "model": tally.model,
        "pages": statuses,
        "pages_written": written,
        "pages_total": len(pages),
        "synthesis": bool(syn),
        "review": problems,
        "rejected": rejected[:60],
        "calls": tally.calls,
        "failed_calls": tally.failed,
        "errors": tally.errors,
        "prompt_tokens": tally.prompt_tokens,
        "completion_tokens": tally.completion_tokens,
        "cost": round(tally.cost, 6),
        "seconds": round(time.monotonic() - started, 1),
        "pii_values_masked": len(mask.values),
    }
