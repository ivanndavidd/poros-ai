"""Deep Research: the explicit, slower mode (analysis_mode=deep).

The chat agent explores as far as a question needs; Deep Research is the broader
contract the caller asks for: several independent evidence angles, an adversarial critic,
validated missing-data claims and a decision memo (research_agent). It runs ONLY when
requested (overhaul item 3, 2026-10-30): the cue regexes that used to send
"analytical-sounding" questions there in auto mode are gone, with the planner playbooks
nothing reached any more. "deep" means broader evidence, never broader permissions: SQL
still goes through the normal governed executor.
"""
from __future__ import annotations


def normalize_mode(value: str | None) -> str:
    mode = str(value or "auto").strip().lower()
    return mode if mode in {"auto", "deep", "fast"} else "auto"


def should_use(requested: str = "auto") -> bool:
    """Deep Research only when the caller asked for it."""
    return normalize_mode(requested) == "deep"


DEEP_REPORT_INSTRUCTIONS = """
- DEEP ANALYSIS MODE: write a substantial analytical answer, not a short dashboard caption.
  Choose the structure demanded by the question instead of forcing a canned framework. Lead with
  the conclusion, state the data scope/periods, connect independent evidence sets, explain what
  drives the result and what could overturn it, and close with concrete implications or actions.
  Use a matrix, scenario comparison, decomposition, timeline, or ranked options only when that
  representation materially helps. Disclose subjective weights/assumptions; never present them as
  observed data. Surface trade-offs, anomalies, base effects, counter-evidence, uncertainty, and
  exact unresolved evidence. Do not merely rank current totals.
- Treat web research as external validation/benchmark only. Internal SQL remains authoritative
  for company performance. Cite every used web claim with its exact [Web: ...] label.
- Respect the adaptive critic's remaining gaps. If an attempted angle failed or returned no rows,
  state the limitation without weakening conclusions that are independently well-supported.
- Use the HYPOTHESIS LEDGER as the audit trail for analytical claims. Explain material supported,
  rejected, partially-supported, and inconclusive hypotheses in natural language and preserve
  their assumptions/counter-evidence. Never upgrade an inconclusive/testing hypothesis into fact.
""".strip()
