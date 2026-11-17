"""Classify what a finished turn actually DELIVERED — the doc §5.4 answer statuses.

Design doc phase 3. Before this, every non-blocked turn was audited ``status="ok"``:
an answer missing the requested client breakdown, an answer over zero rows, and an
answer whose every SQL attempt failed all counted the same as a complete one. The
statuses are:

  * ``complete``  — every explicit requirement (dimensions, time bucket, top-N, chart)
                    is satisfied by the delivered evidence;
  * ``partial``   — valid evidence exists, but named requirements are missing. Also the
                    honest label for a data question answered from documents because
                    every SQL attempt failed — prose is not governed evidence;
  * ``no_data``   — the governed query ran and returned no qualifying rows. An empty
                    result is an ANSWER, never grounds for a positive claim (the
                    verifier's figures_traceable already refuses invented numbers; this
                    makes the emptiness visible instead of implicit);
  * ``blocked``   — the gatekeeper or governance refused the turn;
  * ``error``     — the pipeline produced no valid analytical result at all.

``awaiting_confirmation`` (phase 2) is assigned where confirmations are built, not here.

Deliberately NOT built with this (argued deviations): the one bounded repair the doc
sketches for missing requirements would need re-entering the SQL agent loop — a text
rewrite cannot conjure a missing result column — so a gap is REPORTED honestly instead
of retried expensively; ``data_as_of`` needs the per-source freshness tokens that are a
§19 owner decision (item 4) and a phase-4 concern.
"""
import re

from . import request_contract

_VIZ_RE = re.compile(r"```(?:chart|artifact)\b.*?```", re.S | re.I)
# a FIGURE stated in a currency — "Rp2.785.176.381", "Rp. 1.200", "IDR 1.2 bn", "12 rupiah".
# A currency word without a figure attached ("no value column to express it in Rupiah") is
# the honesty sentence, not a delivered amount. The optional dot and the (bounded) gap are
# separate, non-overlapping steps on purpose: `\s*\.?\s*` lets two repetitions claim the same
# whitespace and backtracks polynomially (Sonar S5852 flagged it).
_MONEY_FIGURE = re.compile(r"(?<![a-z])(?:rp|idr)\.?\s{0,2}\d|\d\s*(?:rupiah|idr)(?![a-z])",
                           re.I)
# a column that carries the time axis, by name ("month_id", "tanggal", "periode", "week")
_TIME_COL = re.compile(r"(month|bulan|date|tanggal|periode|period|week|minggu|day|hari|"
                       r"year|tahun|time|waktu|tgl)", re.I)
_PART = re.compile(r"[a-z0-9]+")


def _ok_sets(evidence) -> list:
    """Every successful governed result set this turn produced (same rule as the
    verifier's grounding: the primary result plus each kept plan-step set)."""
    results = [r for r in (evidence.get("sql_results") or []) if r and r.get("ok")]
    primary = evidence.get("sql")
    if primary and primary.get("ok") and not any(r is primary for r in results):
        results.append(primary)
    return results


def _column_satisfies(dim: str, column: str) -> bool:
    """"client" is delivered by a column named client, client_name or nama_client, and a
    two-word "product category" by product_category(_name) — whole identifier parts
    only, so "sales" never claims "salesperson"."""
    col_parts = _PART.findall(str(column or "").lower())
    dim_parts = _PART.findall(str(dim or "").lower())
    return bool(dim_parts) and all(p in col_parts for p in dim_parts)


def _has_time_column(result) -> bool:
    return any(_TIME_COL.search(str(c) or "") for c in result.get("columns") or [])


def _missing_dimensions(request, sets) -> list:
    missing = []
    for dim in request.get("dimensions") or []:
        delivered = any(_column_satisfies(dim, c)
                        for s in sets for c in s.get("columns") or [])
        if not delivered:
            missing.append(f"dimension:{dim}")
    return missing


def _missing_top_n(request, sets, answer: str) -> list:
    top_n = (request.get("outputs") or {}).get("top_n")
    if not top_n:
        return []
    best = max(len(s.get("rows") or []) for s in sets)
    # fewer rows than asked is fine WHEN the answer says so ("hanya ada 7 client") —
    # the doc's "documented data shortage". The count itself is the document.
    if best < top_n and str(best) not in (answer or ""):
        return [f"top_{top_n}:{best}_rows"]
    return []


def _delivers_money(sets, answer: str) -> bool:
    """A monetary column in a delivered set, or a currency-denominated figure in the
    prose. Live gap this names: "how much inventory" answered as a unit count."""
    for s in sets:
        for column in s.get("columns") or []:
            if request_contract._MONEY_PARTS & set(_PART.findall(str(column or "").lower())):
                return True
    return bool(_MONEY_FIGURE.search(answer or ""))


def missing_requirements(request, evidence, answer: str) -> list:
    """Named gaps between what the question required and what the turn delivered.

    Shared by the verifier (as the ``requirements_met`` check) and by classify(), so the
    check that fails and the status that reports it can never disagree.
    """
    if not request:
        return []
    sets = _ok_sets(evidence)
    if not sets:
        return []          # no evidence at all is classified wholesale, not per field
    missing = _missing_dimensions(request, sets)
    bucket = request.get("time_bucket")
    if bucket and not any(_has_time_column(s) for s in sets):
        missing.append(f"time_bucket:{bucket}")
    missing += _missing_top_n(request, sets, answer)
    if (request.get("outputs") or {}).get("chart") and not _VIZ_RE.search(answer or ""):
        missing.append("chart")
    if request.get("measure") == "value" and not _delivers_money(sets, answer):
        missing.append("measure:value")
    return missing


def _empty(result_set) -> bool:
    """No qualifying rows: none at all, or the one row an aggregate returns over nothing
    (every cell 0 or NULL — staging "jub sido": a COUNT over a brand filter that matched
    no row answered "0 orders" as complete)."""
    rows = result_set.get("rows") or []
    if not rows:
        return True
    if len(rows) != 1:
        return False
    cells = list(rows[0].values()) if isinstance(rows[0], dict) else list(rows[0] or [])
    return bool(cells) and all(
        cell is None or (isinstance(cell, (int, float)) and not isinstance(cell, bool)
                         and cell == 0)
        for cell in cells)


def _classify_sql_turn(request, sets, grounded_other: bool, evidence, answer: str) -> dict:
    if not sets:
        # zero valid SQL evidence can never be complete (exit criterion). Docs/web
        # kept the answer honest prose — partial; nothing at all — error.
        if grounded_other:
            return {"status": "partial",
                    "missing_requirements": ["governed_sql_evidence"]}
        return {"status": "error", "missing_requirements": []}
    if all(_empty(s) for s in sets):
        return {"status": "no_data", "missing_requirements": []}
    missing = missing_requirements(request, evidence, answer)
    if missing:
        return {"status": "partial", "missing_requirements": missing}
    return {"status": "complete", "missing_requirements": []}


def classify(request, evidence, answer: str, needs_sql: bool, discussion: bool = False) -> dict:
    """``{"status", "missing_requirements"}`` for a finished (non-confirmation) turn.

    ``discussion``: the answer needed no data (no query was run, no business figure was
    stated, nothing the question asked for is missing) — that is a complete answer."""
    if discussion:
        return {"status": "complete", "missing_requirements": []}
    sets = _ok_sets(evidence)
    grounded_other = bool((evidence.get("docs") or {}).get("results")
                          or (evidence.get("memory") or {}).get("results")
                          or (evidence.get("web") or {}).get("ok"))
    if needs_sql:
        return _classify_sql_turn(request, sets, grounded_other, evidence, answer)
    # non-SQL turns (doc/memory questions): grounded is complete, empty-handed is the
    # retrieval flavor of no_data — "nothing qualifying found" is an answer, not a failure
    if grounded_other:
        return {"status": "complete", "missing_requirements": []}
    return {"status": ("no_data" if (answer or "").strip() else "error"),
            "missing_requirements": []}
