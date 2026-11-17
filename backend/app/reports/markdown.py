"""EVIDENCE-AND-REASONING.md and OPEN-ITEMS-AND-REVIEW.md from an MBR report model.

Follows the delivery standard the reference package was built to (Production Foundation
v0.5): every claim is labelled FACT / INTERPRETATION / INFERENCE / RECOMMENDATION and
traced to its query; nothing missing is left as a bare gap — each open item says why it
matters, what it needs, the fallback used and what changes when it arrives.
Output language is English regardless of who asked (owner decision, 13 Oct 2026).
"""
from __future__ import annotations

import hashlib

from . import labels as L
from .mbr_sections import GROUPS

STATUS_PASS = "PASS"
STATUS_WARN = "DELIVER_WITH_WARNINGS"
STATUS_QFAIL = "QUALITY_FAIL_DELIVERED"


def delivery_status(model: dict) -> str:
    stats = model.get("stats") or {}
    if stats.get("failed"):
        return STATUS_QFAIL
    if stats.get("partial") or stats.get("placeholder"):
        return STATUS_WARN
    return STATUS_PASS


def _cell(value, column: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        if "%" in column or column.lower() in ("roas", "cvr", "csat"):
            return f"{value:,.1f}" if "%" in column or column.lower() == "roas" else f"{value:,.3f}"
        if float(value).is_integer() or abs(value) >= 100:
            return f"{value:,.0f}"
        return f"{value:,.2f}"
    text = str(value).replace("|", "/").replace("\n", " ")
    return text[:80]


def _table(t: dict) -> str:
    cols = t.get("columns") or []
    if not t.get("rows"):
        return f"_{t.get('title')}: no rows._\n"
    lines = [f"**{t.get('title')}** (source: {L.source_label(t.get('source'))}, "
             f"query `{t.get('source')}`)", "",
             "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for row in t["rows"]:
        lines.append("| " + " | ".join(_cell(v, cols[i] if i < len(cols) else "")
                                       for i, v in enumerate(row)) + " |")
    return "\n".join(lines) + "\n"


def _narration_line(n: dict) -> str:
    if n.get("status") in ("written", "partial"):
        return (f"Headlines, takeaways and the claims marked AI were written by "
                f"{n.get('model') or 'the tenant model'} from the figures of each page; every "
                "number they contain was checked against those figures, and a sentence that "
                "failed the check was dropped. AI claims carry a confidence and, for an "
                "inference, the strongest alternative reading.")
    return "No text in this file was written by a model; headlines follow fixed rules."


def evidence(model: dict) -> str:
    sections = model.get("sections") or []
    out = _evidence_header(model)
    out += _short_answer(sections)
    out += ["", "## 2. Evidence by section", ""]
    out += _evidence_by_section(sections)
    out += _query_evidence(model)
    return "\n".join(out).rstrip() + "\n"


def _evidence_header(model: dict) -> list:
    biz = (model.get("business") or {}).get("name") or "none"
    return [
        f"# Evidence and Reasoning: {model['brand']}, {model['period_label']} Monthly Business Review",
        "",
        f"Generated {L.stamp_label(model['generated_at'])} by Poros "
        f"({model['engine_version']}). Business: {biz}. Data pulled from the warehouse at "
        f"{L.stamp_label(model.get('data_pulled_at') or model['generated_at'])}; latest order "
        f"date {model.get('data_as_of') or 'unknown'}. Status: **{delivery_status(model)}**.",
        "",
        "The warehouse is live: order statuses keep moving after this pull (shipped orders "
        "complete, cancel or return), so a later run, or a package built from an older "
        "extract, can differ slightly for the latest months.",
        "",
        "Every figure below is a cell of a governed query result or arithmetic over those cells. "
        + _narration_line(model.get("narration") or {}),
        "Queries and their SQL are listed at the end.",
        "",
        "## 1. The answer in short",
        "",
    ]


def _short_answer(sections: list) -> list:
    heads = [s for s in sections if s.get("headline")]
    if not heads:
        return ["No headline could be computed; see OPEN-ITEMS-AND-REVIEW.md."]
    return [f"{i}. {s['headline']}. ({s.get('ref', s['group'])} {s['title']})"
            for i, s in enumerate(heads[:8], 1)]


def _evidence_by_section(sections: list) -> list:
    out = []
    names = dict(GROUPS)
    current = None
    for s in sections:
        if s["group"] != current:
            current = s["group"]
            label = names.get(current, current)
            out.append(f"### {current}. {label}" if current not in ("Summary", "Appendix")
                       else f"### {label}")
            out.append("")
        out += _section_evidence(s)
    return out


def _section_evidence(s: dict) -> list:
    out = [f"#### {s.get('ref', s['group'])}. {s['title']} [{s['status']}]", ""]
    for f in s.get("facts") or []:
        out.append(f"- **{L.claim_label(f).upper()}**: {L.claim_text(f)} (source: "
                   f"{L.source_label(f['source'])}, `{f['source']}`)")
    if s.get("facts"):
        out.append("")
    for t in s.get("tables") or []:
        out.append(_table(t))
    if s.get("missing"):
        out.append("Not shown in this section:")
        for m in s["missing"]:
            out.append(f"- {m['what']}: {m['why']}.")
        out.append("")
    return out


def _query_evidence(model: dict) -> list:
    stats = model.get("stats") or {}
    out = ["## 3. Queries", "",
           f"{stats.get('queries', 0)} queries, {stats.get('queries_ok', 0)} succeeded, "
           f"estimated scan {(stats.get('estimated_bytes') or 0) / 1024 ** 3:,.2f} GiB.", ""]
    for q in model.get("queries") or []:
        out += _query_lines(q)
    return out


def _query_lines(q: dict) -> list:
    state = "ok" if q["ok"] else f"failed ({q.get('code')})"
    out = [f"##### `{q['key']}` on `{q['table']}` ({q.get('datasource') or 'n/a'}): "
           f"{state}, {len(q.get('rows') or [])} rows"]
    if q.get("sql"):
        out += ["", "```sql", q["sql"], "```"]
    if not q["ok"]:
        out.append(f"Error: {q.get('error')}")
    out.append("")
    return out


def open_items(model: dict, checksums: dict | None = None) -> str:
    sections = model.get("sections") or []
    stats = model.get("stats") or {}
    status = delivery_status(model)
    out = [
        f"# Open Items and Review: {model['brand']}, {model['period_label']} Monthly Business Review",
        "",
        f"## Status: {status}",
        "",
        f"Sections: {stats.get('sections', 0)} total, {stats.get('complete', 0)} complete, "
        f"{stats.get('partial', 0)} partial, {stats.get('placeholder', 0)} placeholder, "
        f"{stats.get('failed', 0)} failed. Queries: {stats.get('queries_ok', 0)} of "
        f"{stats.get('queries', 0)} succeeded.",
        "",
        "## 1. Decisions only the owner can make",
        "",
    ]
    out += _owner_decisions(model)
    out += ["", "## 2. Missing data and open items", ""]
    out += _missing_items(sections)
    out += _narration_review(model.get("narration") or {})
    out += _deterministic_checks(model, stats)
    if checksums:
        out += ["", "## 4. Checksums", ""]
        for name, digest in sorted(checksums.items()):
            out.append(f"- `{name}`: sha256 {digest}")
    return "\n".join(out).rstrip() + "\n"


def _owner_decisions(model: dict) -> list:
    out = []
    inputs = model.get("inputs") or {}
    guessed = [r for r in model.get("status_values") or [] if not r.get("configured")]
    n = 0
    if guessed:
        n += 1
        out.append(f"{n}. Confirm the order status groups. {len(guessed)} value(s) were grouped "
                   "by keyword (INFERENCE): " + "; ".join(
                       f"'{r['value']}' ({L.status_label(r['value'])}) as "
                       f"{L.GROUP_LABELS.get(r['group'], r['group']).lower()}"
                       for r in guessed[:12]) + ".")
    if not inputs.get("brand_configured"):
        n += 1
        out.append(f"{n}. Add {model['brand']} to the MBR settings (takeover date, campaign "
                   "calendar, focus products, warehouse client codes). Without them the related "
                   "pages are placeholders. Targets can also be typed when the report is "
                   "requested.")
    n += 1
    out.append(f"{n}. Confirm the GMV definition. This report counts GMV as item value after "
               "seller discount over every order status (NMV less the platform discount); the "
               "reference definition also adds net shipping, which the order data does not hold.")
    return out


def _missing_items(sections: list) -> list:
    out = []
    k = 0
    for s in sections:
        for m in s.get("missing") or []:
            k += 1
            out += _open_item(k, s, m)
    if not k:
        out += ["None.", ""]
    return out


def _open_item(k: int, s: dict, m: dict) -> list:
    out = [f"### 2.{k} {m['what']} ({s.get('ref', s['group'])} {s['title']})", "",
           f"- Why it matters: {m['why']}.", f"- What is needed: {m['needs']}."]
    if m.get("visual"):
        out.append(f"- Intended visual: {m['visual']}.")
    if m.get("fallback"):
        out.append(f"- Fallback used now: {m['fallback']}.")
    if m.get("updates"):
        out.append(f"- When it arrives: {m['updates']}.")
    if m.get("if_good"):
        out.append("- Drafted in advance, conditional until the data arrives:")
        out.append(f"  - If good: {m['if_good']}")
        out.append(f"  - If bad: {m['if_bad']}")
        out.append(f"  - If neutral: {m['if_neutral']}")
    if m.get("detail"):
        out.append(f"- Technical detail (data team): {m['detail']}")
    out.append("")
    return out


def _deterministic_checks(model: dict, stats: dict) -> list:
    out = ["## 3. Deterministic checks", ""]
    failed = [q for q in model.get("queries") or [] if not q["ok"]]
    out.append(f"- Every query passed govern() before running: yes (failed queries never ran "
               f"or were refused: {len(failed)}).")
    out.append(f"- Estimated bytes scanned: {stats.get('estimated_bytes') or 0:,}.")
    out.append(f"- Data pulled at: {model.get('data_pulled_at') or model['generated_at']} "
               "(live warehouse; statuses of recent orders can still change).")
    out.append(f"- Data as of (latest order date for the brand): {model.get('data_as_of') or 'unknown'}.")
    for q in failed:
        out.append(f"- `{q['key']}` on `{q['table']}`: {q.get('code')}: {q.get('error')}")
    return out


def _narration_review(n: dict) -> list:
    """What the narration wrote, what its checks threw out and what the review found."""
    out = ["## 2b. Narration (AI) review", ""]
    if not n or n.get("status") in (None, "skipped"):
        out += [f"Narration did not run: {n.get('reason') or 'not available'}. Headlines "
                "follow fixed rules.", ""]
        return out
    out.append(_narration_status(n))
    if n.get("pii_values_masked"):
        out.append(f"- {n['pii_values_masked']} personal data value(s) were masked before "
                   "anything was sent to the model.")
    out += _review_lines(n.get("review") or [])
    out += _rejected_lines(n.get("rejected") or [])
    for e in n.get("errors") or []:
        out.append(f"- Error: {e}")
    out.append("")
    return out


def _narration_status(n: dict) -> str:
    return (f"- Status: {n.get('status')}; model {n.get('model') or 'n/a'}; "
            f"{n.get('pages_written', 0)} of {n.get('pages_total', 0)} pages written; "
            f"synthesis {'written' if n.get('synthesis') else 'not written'}; "
            f"{n.get('calls', 0)} model calls, {n.get('failed_calls', 0)} failed; "
            f"cost USD {n.get('cost') or 0:,.4f}; {n.get('seconds', 0)} s.")


def _review_lines(problems: list) -> list:
    out = []
    for p in problems:
        state = "applied" if p.get("applied") else "for the owner"
        line = f"- Review ({p.get('page')}, {p.get('field')}, {state}): {p.get('problem')}"
        if p.get("replacement") and p.get("applied"):
            line += f" Now reads: {p['replacement']}"
        out.append(line)
    return out


def _rejected_lines(rej: list) -> list:
    if not rej:
        return []
    out = [f"- {len(rej)} sentence(s) were dropped because a figure or wording failed "
           "the checks:"]
    for r in rej[:20]:
        out.append(f"  - {r.get('page')} {r.get('field')}: \"{r.get('text')}\" "
                   f"({r.get('reason')})")
    return out


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
