"""Read a KPI phrase with several curated meanings as one of them, and say so.

Design doc phase 1 made the collision typed: a strict tenant's "berapa GMV?" where GMV is
curated as two different things stopped being settled by datasource order. ``/v1/query``
(raw SQL, nobody to read it) still refuses with SEMANTIC_AMBIGUOUS. A chat turn used to end
as a question carrying the meanings (phase 2, with a server-side token for the pick); since
chat overhaul item 5 the meanings are an OPEN READING instead (app/assumptions.py): the
model takes the meaning the question and the conversation most likely mean, says so in the
answer's first sentence, and each other meaning is a one-click correction.

Still governed: when the meanings live in different tables, their contracts are ENFORCED
together (``enforceable``), so whichever one a query takes it must compute it exactly as
curated; a query cannot invent a third GMV from those tables. An AUTHORITY_CONFLICT (broken
replica curation) is not a reading and still refuses (409): no reading fixes it.
"""
import re

from . import assumptions, confirm


def _rewrite(question: str, term: str, name: str) -> str:
    """The question with the chosen concept named outright. Only offered when option
    names are pairwise distinct: two options both named "GMV" cannot be told apart by a
    name, so those carry the meaning's description instead (``_clarified``)."""
    pattern = re.compile(
        r"(?<![0-9A-Za-z_])" + r"[\s_-]+".join(map(re.escape, term.split())) +
        r"(?![0-9A-Za-z_])", re.IGNORECASE)
    return pattern.sub(name, question or "", count=1)


def _clarified(message: str, name: str, descr: str, english: bool) -> str:
    body = (message or "").rstrip()
    meaning = f"{name}: {descr}" if descr else name
    return (f"{body} (I mean: {meaning})" if english
            else f"{body} (yang saya maksud: {meaning})")


def _definition(contract: dict) -> str:
    return (str(contract.get("formula") or "").strip()
            or f"{contract.get('aggregation') or 'SUM'}({contract.get('metric') or ''})")


def _distinct_tables(group: dict) -> bool:
    pairs = [(m.get("datasource_id"), str(m.get("table") or "").lower())
             for m in group["meanings"]]
    return all(table for _ds, table in pairs) and len(set(pairs)) == len(pairs)


def enforceable(analysis) -> list:
    """The contracts of every open meaning whose tables tell them apart.

    ``semantic_contract.sql_violation`` enforces the contracts whose table a query reads,
    so with one table per meaning a query on any of them must use that meaning's exact
    definition. Two meanings on the SAME table would both bind one query, which can
    satisfy only one: those stay prompt context only."""
    out = []
    for group in analysis.ambiguities if analysis else []:
        if _distinct_tables(group):
            out.extend(group["contracts"])
    return out


def _reading(group: dict, message: str, english: bool) -> dict:
    term, options = group["term"], group["options"]
    names = [o["name"] for o in options]
    distinct = len({n.lower() for n in names}) == len(names)
    shown, lines = [], []
    for position, (option, meaning, contract) in enumerate(
            zip(options, group["meanings"], group["contracts"]), 1):
        descr = (option.get("descr") or "").strip()
        rename = distinct and option["name"].lower() != term.lower()
        shown.append({"id": meaning["concept_id"] or f"opt{position}",
                      "name": option["name"], "descr": descr,
                      "question": (_rewrite(message, term, option["name"]) if rename
                                   else _clarified(message, option["name"], descr, english))})
        lines.append(f"({position}) {option['name']}: {_definition(contract)} from "
                     f"{contract.get('table')} in datasource "
                     f"'{contract.get('datasource_name') or ''}' "
                     f"(id {contract.get('datasource_id')})"
                     + (f"; meaning: {descr}" if descr else ""))
    checked = (" (a query on those tables is checked against it)"
               if _distinct_tables(group) else "")
    return {"kind": assumptions.METRIC, "term": term, "options": shown, "chosen": None,
            "_match": list(group["contracts"]),
            "_context": (f"'{term}' has {len(options)} curated meanings: "
                         + "; ".join(lines)
                         + f". Use exactly the definition of the meaning you take{checked}.")}


def readings(analysis, message: str) -> list:
    """One open reading per ambiguous KPI phrase of this turn."""
    english = confirm.looks_english(message)
    return [_reading(group, message, english)
            for group in (analysis.ambiguities if analysis else [])
            [:assumptions.MAX_READINGS]]
