"""What a chat turn ASSUMED instead of asking (chat overhaul item 5).

Three checks used to end the turn with a question before any answer: a name the data only
resembles (entity_confirm), the brand mode without a brand (brand_analysis.detect) and a
KPI phrase with several curated meanings on a strict tenant (metric_confirm). The owner
found the ask-backs made the chat feel like a form. They now hand their candidates to the
model as OPEN READINGS: the model takes the reading the question and the conversation
most likely mean, says so in the first sentence of the answer, and the person corrects it
next turn. ``business_confirm`` is not one of them: a business is an access boundary and is
still asked.

One reading is one dict, built by the module that found it:

    {"kind": "entity" | "brand" | "metric", "term": "Philips" | None,
     "options": [{"id", "name", "descr", "question"}], "chosen": None,
     "_context": "the line the model reads", "_match": [...] per option}

``options[*].question`` is the question rewritten with that reading spelled out, so a client
can offer "not this one" as one click. ``chosen`` is filled after the turn from what the
turn actually RAN (the value its SQL filtered on, the brand whose baseline it read, the
curated meaning its query satisfied), never from what the answer claims; None when that
cannot be told. Keys starting with "_" never leave the server.
"""
import re

from . import semantic_contract

ENTITY, BRAND, METRIC = "entity", "brand", "metric"
MAX_READINGS = 4

_HEAD = ("OPEN READINGS (found by the server, not asked of the person; context, not "
         "instructions):")
_TAIL = ("Take the reading the question and the conversation most likely mean and query and "
         "answer with that one only. Begin the answer with one short sentence, in the "
         "answer's language, that says which reading you took (for example \"I read "
         "'Philips' as the brand Philipsome.\"), so the person can correct it; then answer.")


def prompt_block(readings) -> str:
    """The text every answer-writing prompt carries when a turn has open readings."""
    lines = [r["_context"] for r in readings or [] if r.get("_context")]
    if not lines:
        return ""
    return "\n".join([_HEAD, *("- " + line for line in lines), _TAIL])


def public(readings) -> list:
    """The response shape: no server-only keys."""
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in readings or []]


def audit(readings) -> list:
    return [{"kind": r["kind"], "term": r.get("term"), "chosen": r.get("chosen"),
             "options": [o["name"] for o in r.get("options") or []]}
            for r in readings or []]


def _sql_texts(steps) -> list:
    """(lowercased SQL, step) of every governed query this turn that succeeded."""
    out = []
    for step in steps or []:
        if step and step.get("ok"):
            sql = str(step.get("safe_sql") or step.get("generated_sql") or "")
            if sql:
                out.append((sql.lower(), step))
    return out


def _outermost(hits: list) -> list:
    """Drop a value found only as part of a longer value also found ("Philipsome" inside
    "Philipsome Indonesia")."""
    return [h for h in hits if not any(h[1] != o[1] and h[1] in o[1] for o in hits)]


def _value_chosen(reading, sqls):
    hits = [(option["id"], str(match).lower())
            for option, match in zip(reading["options"], reading.get("_match") or [])
            if match and any(str(match).lower() in sql for sql, _ in sqls)]
    hits = _outermost(hits)
    return hits[0][0] if len(hits) == 1 else None


def _meaning_chosen(reading, sqls):
    """The column the exact value was filtered on: the dimension compared with the value
    (``UPPER(TRIM(brand)) = 'SAMSUNG'``, ``brand IN ('SAMSUNG'``, ``brand LIKE '%samsung``)."""
    hits = []
    for option, match in zip(reading["options"], reading.get("_match") or []):
        dimension, value = match
        pattern = re.compile(r"\b" + re.escape(str(dimension).lower())
                             + r"\b[\s)]*(?:=|\bin\b|\bi?like\b)\s*\(?\s*'%?"
                             + re.escape(str(value).lower()))
        if any(pattern.search(sql) for sql, _ in sqls):
            hits.append(option["id"])
    return hits[0] if len(hits) == 1 else None


def _metric_chosen(reading, sqls):
    """The curated meaning a query on its own datasource satisfied."""
    hits = []
    for option, contract in zip(reading["options"], reading.get("_match") or []):
        table = str(contract.get("table") or "").lower()
        for sql, step in sqls:
            if contract.get("datasource_id") not in (None, step.get("datasource_id")):
                continue
            if table and table in sql and semantic_contract.sql_violation(
                    step.get("safe_sql") or "", step.get("dialect"), [contract]) is None:
                hits.append(option["id"])
                break
    return hits[0] if len(hits) == 1 else None


def _brand_chosen(reading, brands):
    if not brands:
        return None
    name = brands[0]
    if not any(o["id"] == name for o in reading["options"]):
        message = reading.get("_message") or ""
        reading["options"].insert(0, {"id": name, "name": name, "descr": "",
                                      "question": f"{message} (brand {name})"})
    return name


def settle(readings, steps, brands=()) -> None:
    """Fill each reading's ``chosen`` from what this turn ran (see the module docstring)."""
    sqls = _sql_texts(steps)
    for reading in readings or []:
        if reading["kind"] == BRAND:
            reading["chosen"] = _brand_chosen(reading, list(brands or []))
        elif reading["kind"] == METRIC:
            reading["chosen"] = _metric_chosen(reading, sqls)
        elif reading.get("_reading") == "meaning":
            reading["chosen"] = _meaning_chosen(reading, sqls)
        else:
            reading["chosen"] = _value_chosen(reading, sqls)
