"""Verifier (Feature 2.2): checks the synthesized answer for citations, PII safety,
and grounding before it leaves the platform.

v2 additions:
- figures_traceable: every substantial figure in the answer must trace back to the
  SQL rows / pre-computed derived metrics / docs / memory / question (tolerant of
  rounding, thousand-separator styles, and Indonesian/English scale words). Catches
  the LLM inventing or mis-computing numbers even when a SQL result exists —
  previously ANY figure passed as soon as one query succeeded.
- compute_confidence: deterministic High/Medium/Low from signals that already flow
  through the pipeline (verifier checks, SQL rows, doc rerank score, unmatched figures).

Chat overhaul item 4: an answer with no evidence is only a problem when it states
business figures. A discussion with none (a greeting, general advice, an explanation)
gets ``basis: general_knowledge`` and the confidence says so instead of "low".

Chat overhaul item 6: the verifier only reports. Nothing rewrites the answer; the figures
it could not match to the evidence leave as ``unverified_figures`` (the clients show a
small note) and the confidence drops with them.
"""
import re

from . import analytics
from .. import answer_contract

# ---- numeric grounding helpers ----
_FENCE_RE = re.compile(r"```.*?```", re.S)                       # chart/artifact/code blocks
_DATE_RE = re.compile(r"\b\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?\b|\b\d{1,2}:\d{2}(?::\d{2})?\b")
_CITE_RE = re.compile(r"\[[^\]]{0,120}\]")                        # [file — Slide 3] etc.
_TOKEN_RE = re.compile(r"\d[\d.,]*")
# scale word directly after a figure ("227 juta", "9,2 miliar", "1.2 million", "5 rb")
_SCALES = {"ribu": 1e3, "rb": 1e3, "k": 1e3, "thousand": 1e3,
           "juta": 1e6, "jt": 1e6, "m": 1e6, "million": 1e6, "mio": 1e6,
           "miliar": 1e9, "milyar": 1e9, "b": 1e9, "billion": 1e9,
           "triliun": 1e12, "t": 1e12, "trillion": 1e12}
_SCALE_RE = re.compile(r"^\s*(" + "|".join(sorted(_SCALES, key=len, reverse=True)) + r")\b", re.I)

_SMALL = 100          # figures below this are exempt (ranks, counts, percentages, scaled units)
_PCT_RE = re.compile(r"^\s*(?:%|persen\b|percent\b)", re.I)
_CUR_RE = re.compile(r"(?:\brp\.?|\bidr|\busd|\$)\s*$", re.I)
_YEAR_MIN, _YEAR_MAX = 1900, 2100


def _parse_token(tok):
    """Candidate float values for a numeric token — handles US (1,234.56),
    Indonesian/EU (1.234,56) and ambiguous (1.234 / 1,234) styles by returning
    EVERY plausible reading."""
    t = tok.strip(".,")
    if not t:
        return []
    cands = set()
    if re.fullmatch(r"\d+", t):
        cands.add(float(t))
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", t):              # US thousands
        cands.add(float(t.replace(",", "")))
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", t):              # ID/EU thousands
        cands.add(float(t.replace(".", "").replace(",", ".")))
    if re.fullmatch(r"\d+,\d+", t):                               # decimal comma
        cands.add(float(t.replace(",", ".")))
    if re.fullmatch(r"\d+\.\d+", t):                              # decimal dot
        cands.add(float(t))
    return list(cands)


def _walk_numbers(obj, out):
    """Collect every numeric leaf of a nested dict/list (derived metrics blocks)."""
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, str):
        for tok in _TOKEN_RE.findall(obj):
            out.update(_parse_token(tok))
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk_numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk_numbers(v, out)


def _sql_sets(evidence):
    results = [r for r in (evidence.get("sql_results") or []) if r and r.get("ok")]
    prim = evidence.get("sql")
    if prim and prim.get("ok") and not any(r is prim for r in results):
        results.append(prim)
    return results


def _ground_numbers(question, evidence):
    """Every number the answer may legitimately contain: SQL cells, derived metrics
    (recomputed as a fallback when a set has none), the 2-set comparison, doc/memory
    text, and the question itself."""
    vals = set()
    headline = []          # measure totals — their pairwise sum/diff are legitimate derived figures
    for s in _sql_sets(evidence):
        for row in s.get("rows") or []:
            _walk_numbers(list(row), vals)
        derived = s.get("derived")
        if not derived:
            try:
                derived = analytics.summarize(s.get("columns"), s.get("rows"))
            except Exception:
                derived = None
        if derived:
            _walk_numbers(derived, vals)
            for m in (derived.get("measures") or []):
                t = m.get("total")
                if isinstance(t, (int, float)) and not isinstance(t, bool):
                    headline.append(float(t))
    # An analyst legitimately states "GMV − NMV = ..." or "A + B = ..." — a figure derived
    # PURELY from two validated SQL totals is still grounded in the data (not invented). Add
    # the pairwise sums and absolute differences of the headline totals to the ground set.
    headline = headline[:24]
    for i in range(len(headline)):
        for j in range(i + 1, len(headline)):
            vals.add(abs(headline[i] - headline[j]))
            vals.add(headline[i] + headline[j])
    sets = _sql_sets(evidence)
    if len(sets) == 2:                                            # mirror the synthesis comparison
        try:
            _walk_numbers(analytics.compare_results(sets[0]["columns"], sets[0]["rows"],
                                                    sets[1]["columns"], sets[1]["rows"]), vals)
        except Exception:
            pass
    for d in (evidence.get("docs") or {}).get("results") or []:
        _walk_numbers(d.get("text") or "", vals)
    web = evidence.get("web") or {}
    if web.get("ok"):
        _walk_numbers(web.get("text") or "", vals)
    for m in (evidence.get("memory") or {}).get("results") or []:
        _walk_numbers(m.get("text") or "", vals)
    _walk_numbers(question or "", vals)
    return vals


def _answer_figures(answer):
    """(token, value) figures to check: fenced blocks, dates/times and citation labels
    stripped first; scale words applied ('227 juta' -> 227e6)."""
    text = _FENCE_RE.sub(" ", answer or "")
    text = _CITE_RE.sub(" ", text)
    text = _DATE_RE.sub(" ", text)
    out = []
    for m in _TOKEN_RE.finditer(text):
        cands = _parse_token(m.group())
        if not cands:
            continue
        tok = m.group().rstrip(".,")         # "987.654." ends a sentence, not the figure
        scale = _SCALE_RE.match(text[m.end():])
        if scale:
            # Keep BOTH the scaled and the raw reading. Abbreviations are ambiguous —
            # Indonesian finance writes "9,2 M" for MILIAR while _SCALES maps "m" to 1e6 —
            # so a correct answer could otherwise fail on the wrong scale guess. The raw
            # reading of a scaled figure is small ("9,2" < 100) and passes the small-figure
            # exemption; unabbreviated large figures are still checked against the data.
            factor = _SCALES[scale.group(1).lower()]
            out.append((tok + " " + scale.group(1), [c * factor for c in cands] + cands))
        else:
            out.append((tok, cands))
    return out


def _stated_figures(question, answer):
    """Figures an answer with NO evidence states about the business: a percentage or a
    money figure (currency or scale word) always, any other number from 100 up. Years,
    period keys, dates and numbers the question carries are not; nor are small plain
    numbers (steps, counts, days), which are ordinary language."""
    text = _DATE_RE.sub(" ", _CITE_RE.sub(" ", _FENCE_RE.sub(" ", answer or "")))
    asked = set()
    _walk_numbers(question or "", asked)
    out = []
    for m in _TOKEN_RE.finditer(text):
        cands = _parse_token(m.group())
        if not cands or any(abs(c - q) <= 0.5 for c in cands for q in asked):
            continue
        after = text[m.end():]
        # every reading must be exempt: "1.500" is 1500 to an Indonesian reader, even
        # though the English reading 1.5 would pass as a small number
        if (_PCT_RE.match(after) or _SCALE_RE.match(after) or _CUR_RE.search(text[:m.start()])
                or not all(_traceable([c], asked) for c in cands)):
            out.append(m.group().strip(".,"))
    return out


def _traceable(cands, ground):
    """A figure is traceable when ANY reading matches ANY ground number within
    max(0.5, 5%) — tolerant of rounding ('1.500' for 1547) — or is exempt (small
    figures < 100 such as ranks/percentages/scaled units, calendar years, and
    YYYYMM period keys like 202606 which are time coordinates, not metrics)."""
    for c in cands:
        if abs(c) < _SMALL:
            return True
        if float(c).is_integer():
            ci = int(c)
            if _YEAR_MIN <= ci <= _YEAR_MAX or analytics._is_period(ci):
                return True
        for g in ground:
            # Currency is commonly rendered as ``-Rp3.000``. The token scanner sees
            # ``3.000`` because the sign is separated from the number by ``Rp``;
            # magnitude matching therefore remains necessary for a grounded loss.
            if abs(abs(c) - abs(g)) <= max(0.5, 0.05 * abs(g)):
                return True
    return False


def _check_citations(answer, evidence, has_docs, has_web, checks, issues):
    # 2) citations: if doc evidence was used, the answer should cite at least one source
    # (either machine format file#chunkN or the human label "file — Slide/Page N")
    if has_docs:
        cited = bool(re.search(r"\[[^\]]+#chunk\d+\]", answer)) or \
                bool(re.search(r"\[[^\]]+[—-]\s*(Slide|Page|bagian)\s*\d+", answer)) or \
                any(d["filename"] in answer for d in evidence["docs"]["results"])
        checks["citations_present"] = cited
        if not cited:
            issues.append("documents were retrieved but the answer does not cite a source")
    else:
        checks["citations_present"] = True

    if has_web:
        web_cited = any(
            f"[{source.get('label')}]" in (answer or "")
            or str(source.get("url") or "") in (answer or "")
            for source in (evidence.get("web") or {}).get("sources") or []
        )
        checks["web_citations_present"] = web_cited
        if not web_cited:
            issues.append("web research was used but the answer does not cite a web source")
    else:
        checks["web_citations_present"] = True


def _check_figures(question, answer, evidence, has_sql, checks, issues, stated=()) -> list:
    """The figures that trace to nothing this turn gathered (every one, in order)."""
    # 3) Legacy check name retained for API compatibility. A number is valid when it
    # traces to governed SQL OR an explicitly retrieved document/web source. External
    # benchmarks must not be rejected merely because they correctly came from the web.
    checks["numbers_from_sql"] = True

    # 3b) figures_traceable: every substantial figure must trace back to the data,
    # deterministic derived metrics, documents, web evidence, memory, or question.
    checks["figures_traceable"] = True
    bad = []
    if (answer or "").strip():
        ground = _ground_numbers(question, evidence)
        bad = [tok for tok, cands in _answer_figures(answer) if not _traceable(cands, ground)]
        bad += list(stated)
        if bad:
            checks["figures_traceable"] = False
            uniq = list(dict.fromkeys(bad))
            issues.append("figures not present in the governed/internal/external evidence: "
                          + ", ".join(uniq[:6]))
    if not has_sql and bad:
        checks["numbers_from_sql"] = False
        issues.append("answer contains figures that were not produced by a validated source")
    return list(dict.fromkeys(bad))


def _check_requirements(request, evidence, answer, checks, issues):
    # 5) requirements (phase 3): what the question EXPLICITLY asked for — "per client",
    # a monthly bucket, top-10, a chart — must be in the delivered evidence. Same
    # computation classify() uses for the partial status, so check and status agree.
    checks["requirements_met"] = True
    if request:
        gaps = answer_contract.missing_requirements(request, evidence, answer)
        if gaps:
            checks["requirements_met"] = False
            issues.append("the answer does not deliver everything the question asked "
                          "for: " + ", ".join(gaps))


def _check_grounding(question, answer, evidence, coverage, has_any, checks, issues) -> list:
    """Sets ``grounded``; returns the business figures a no-evidence answer states."""
    attempted = bool(evidence.get("sql") or coverage.get("required") or coverage.get("missing"))
    stated = [] if has_any else _stated_figures(question, answer)
    checks["grounded"] = has_any or (not attempted and not stated)
    if not checks["grounded"]:
        issues.append("answer states business figures with no evidence behind them: "
                      + ", ".join(list(dict.fromkeys(stated))[:6]) if stated else
                      "answer has no grounded evidence (no SQL rows, docs, or memory)")
    return stated


def _check_coverage(coverage, checks, issues):
    missing_coverage = coverage.get("missing") or []
    checks["source_coverage"] = not missing_coverage
    if missing_coverage:
        issues.append(
            "decision evidence coverage is incomplete: " + ", ".join(missing_coverage[:8])
        )


def _proposed_figures(question, answer, evidence, has_any) -> list:
    """A brainstorm's figures that trace to nothing this turn gathered: the discount to
    test, the voucher value. Proposals, named for the reader, never a failed check."""
    ground = _ground_numbers(question, evidence)
    out = [tok for tok, cands in _answer_figures(answer or "") if not _traceable(cands, ground)]
    if not has_any:
        out += _stated_figures(question, answer)
    return list(dict.fromkeys(out))


def verify(question: str, answer: str, evidence: dict, gate, request=None,
           brainstorm: bool = False) -> dict:
    issues = []
    checks = {}

    # 1) grounding: evidence behind the answer, or a discussion that states no business
    #    figures. A governed query that ran and failed, or research whose required
    #    evidence is missing, is not a discussion: that answer was meant to come from data.
    has_sql = bool((evidence.get("sql") or {}).get("ok"))
    has_docs = bool(evidence.get("docs", {}).get("results"))
    has_mem = bool(evidence.get("memory", {}).get("results"))
    has_web = bool((evidence.get("web") or {}).get("ok"))
    has_any = has_sql or has_docs or has_mem or has_web
    coverage = evidence.get("coverage") or {}
    # a brainstorm stands on general knowledge: no evidence is not a grounding failure
    stated = _check_grounding(question, answer, evidence, coverage, has_any or brainstorm,
                              checks, issues)

    _check_citations(answer, evidence, has_docs, has_web, checks, issues)
    _check_coverage(coverage, checks, issues)

    proposed = []
    if brainstorm:
        proposed = _proposed_figures(question, answer, evidence, has_any)
        checks["numbers_from_sql"] = checks["figures_traceable"] = True
        unverified = []
    else:
        unverified = _check_figures(question, answer, evidence, has_sql, checks, issues, stated)

    # 4) PII safety: governance blocks PII at query time; flag if the gate saw a PII request
    checks["pii_screened"] = True
    if getattr(gate, "pii_request", False):
        issues.append("gatekeeper flagged a possible PII solicitation (blocked at governance layer)")

    _check_requirements(request, evidence, answer, checks, issues)

    out = {"ok": len(issues) == 0, "issues": issues, "checks": checks,
           "unverified_figures": unverified}
    if brainstorm:
        out["proposed_figures"] = proposed
        out["basis"] = "brainstorm_grounded" if has_any else "brainstorm"
    elif not has_any and checks["grounded"] and checks["requirements_met"]:
        out["basis"] = "general_knowledge"
    return out


def compute_confidence(vr: dict, evidence: dict, gate=None) -> dict:
    """Deterministic confidence for the final answer — an AGGREGATION of signals that
    already flow through the pipeline (no LLM call, no new retrieval):
    verifier checks, unmatched figures, SQL row support, doc rerank strength, complexity.
    Returns {level: high|medium|low, score: 0-100, signals: {...}}."""
    checks = (vr or {}).get("checks") or {}
    signals = {}
    if (vr or {}).get("basis") == "general_knowledge":
        # nothing to be confident ABOUT: no business figure was stated. Clients show
        # "general knowledge" instead of a confidence level.
        return {"level": "general_knowledge", "score": None,
                "signals": {"general_knowledge": True}}
    if (vr or {}).get("basis") == "brainstorm":
        # ideas from general knowledge: clients show "ideas, not from data"
        return {"level": "brainstorm", "score": None, "signals": {"brainstorm": True}}
    if not checks.get("grounded", True):
        return {"level": "low", "score": 10, "signals": {"grounded": False}}

    score = 100
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        score -= 25 * len(failed)
        signals["checks_failed"] = failed
    unverified = len(vr.get("unverified_figures") or [])
    if unverified:
        # the failed check already costs 25; each further unmatched figure 15 more, so one
        # stray number reads "medium" and three or more "low"
        score -= min(30, 15 * (unverified - 1))
        signals["unverified_figures"] = unverified

    sql_ok = bool((evidence.get("sql") or {}).get("ok"))
    doc_hits = (evidence.get("docs") or {}).get("results") or []
    if sql_ok:
        rows = len((evidence.get("sql") or {}).get("rows") or [])
        signals["sql_rows"] = rows
        if rows == 0:
            # A technically valid query with no rows is not internal evidence. Penalise
            # strongly enough that a complex recommendation cannot display High merely
            # because unrelated web evidence was available.
            score -= 45
            signals["sql_evidence_empty"] = True
    elif doc_hits:
        score -= 10                                               # docs-only: no validated numbers
        rs = [h.get("rerank_score") for h in doc_hits if h.get("rerank_score") is not None]
        if rs:
            top = max(rs)
            signals["doc_rerank_top"] = round(top, 3)
            if top < 0:
                score -= 15                                       # reranker judged the best hit weak
    else:
        score -= 25                                               # memory-only evidence

    if getattr(gate, "complexity", "") == "complex":
        score -= 5; signals["complexity"] = "complex"

    coverage = evidence.get("coverage") or {}
    missing_coverage = coverage.get("missing") or []
    if missing_coverage:
        score -= min(40, 8 * len(missing_coverage))
        signals["coverage_missing"] = missing_coverage
    elif coverage.get("required"):
        signals["coverage_complete"] = coverage.get("succeeded") or []

    if (vr or {}).get("basis") == "brainstorm_grounded":
        signals["brainstorm"] = True             # ideas checked against this turn's data
    score = max(0, min(100, score))
    level = "high" if score >= 80 else "medium" if score >= 55 else "low"
    return {"level": level, "score": score, "signals": signals}
