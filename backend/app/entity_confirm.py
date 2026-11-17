"""Read a name the data only resembles as one of the values it holds, and say so.

The reported failure: "What is GMV for Philips" came back answered from a 3P table whose
data says **Philipsome**. The question named an entity that does not exist in the slice, and
the closest thing to it won silently. The business layer stops the cross-slice half of that
(a 1P question can no longer reach a 3P table at all); this stops the other half — inside
one slice, a term that merely RESEMBLES what the data holds is never used silently.

Until chat overhaul item 5 the turn ended in a question ("which one did you mean?"). Now
the candidates are an OPEN READING (app/assumptions.py): the model takes the one the
question and the conversation most likely mean, says so in the answer's first sentence,
and every other reading is a one-click correction (its ``question``).

Detection is deterministic. The index is the curated ``reference_values`` of the
datasources this turn can see, and the test is string similarity. The rule is narrow on
purpose:

  * a term that matches a known value EXACTLY under one meaning is never open — that is
    the common case;
  * a term that matches nothing at all is not open either. Not every noun is an entity,
    and the answer path already reports "no rows" honestly;
  * only the in-between — no exact match but close ones, or an exact value under more
    than one dimension — is an open reading.

Off by default (``config/business`` -> ``confirm_entities``): it changes what every question
in a tenant may do, so it is the tenant's decision, like the business toggle beside it.
"""
import difflib
import re

from . import assumptions, confirm, store

CONFIG_KEY = "confirm_entities"

_MIN_LEN = 4          # below this, similarity is noise ("3P" resembles everything)
_MAX_NGRAM = 3        # "philips indonesia timur" is still one entity; longer is a sentence
_NEAR = 0.82          # SequenceMatcher ratio for a close-but-not-equal value
_MAX_OPTIONS = 5
_MAX_NEAR = 8         # a term this many values resemble is a generic word, not an entity
_MAX_VALUES = 4000    # index cap: a runaway reference_values list must not stall a turn
# a reference "value" is a NAME; longer is curation prose pasted into the wrong field.
# Live Acme case: routing instructions saved as reference_values ("if the user ask about
# the brand, you should refering to this column...") — a sentence can never be matched
# exactly by a candidate phrase, so indexing it manufactures confirmations that no pick
# can ever resolve ("zentra" asked forever). Real long names stay in: 5 words / 60 chars
# still admits "ZENTRA FOOD INTERNATIONAL INDONESIA TBK".
_MAX_VALUE_WORDS = 5
_MAX_VALUE_LEN = 60

_WORD = re.compile(r"[0-9A-Za-z]+(?:[-'][0-9A-Za-z]+)*")
# the "use exactly what I wrote" correction: `(persis "Philips")` / `(exactly "Philips")`
_EXACT = re.compile(r'\((?:persis|exactly)\s+"([^"\n]{1,80})"\)', re.I)

# Function words and calendar/analytics vocabulary. Everything the SCHEMA names (tables,
# columns, metrics, concepts, dimensions) is excluded separately and dynamically — that
# filter does the heavy lifting, this list only covers what no MDL would ever contain.
_STOP = {
    "yang", "dari", "untuk", "dengan", "pada", "adalah", "atau", "dan", "juga", "saja",
    "berapa", "bagaimana", "kenapa", "mengapa", "siapa", "kapan", "dimana", "mana",
    "persis", "exactly",
    "tolong", "coba", "buatkan", "tampilkan", "jelaskan", "bandingkan", "hitung",
    "bulan", "tahun", "minggu", "hari", "kemarin", "lalu", "ini", "itu", "sekarang",
    "total", "jumlah", "rata", "paling", "tertinggi", "terendah", "naik", "turun",
    "januari", "februari", "maret", "april", "juni", "juli", "agustus", "september",
    "oktober", "november", "desember",
    "what", "which", "when", "where", "why", "who", "how", "show", "list", "give",
    "please", "compare", "explain", "calculate", "between", "during", "about", "with",
    "from", "into", "over", "than", "that", "this", "these", "those", "there",
    "month", "year", "week", "yesterday", "today", "last", "next", "current",
    "january", "february", "march", "may", "june", "july", "august", "october",
    "november", "december",
    "sales", "revenue", "growth", "trend", "report", "dashboard", "average", "highest",
    "lowest", "top", "data", "table", "value", "values", "brand", "merek", "produk",
    "product", "region", "wilayah", "area", "kategori", "category",
}


def is_enabled(tenant_id: str) -> bool:
    """Whether this tenant wants close-but-not-exact terms read as open readings."""
    section = store.get_tenant_config(tenant_id, "business") or {}
    return bool(section.get(CONFIG_KEY))


def _learn(vocabulary: set, identifier) -> None:
    """An identifier and the words inside it are BOTH schema language.

    Identifiers are fused (``order_id``, ``acme_offline_sales_order_odoo_ai_curated``)
    while questions come in words ("order"), so membership on the fused form alone
    misses exactly the words the schema is made of.
    """
    text = str(identifier).lower()
    vocabulary.add(text)
    vocabulary.update(_WORD.findall(text))


def _learn_vocabulary(technical: set, mdl: dict) -> None:
    """Everything this MDL is named by. Tables, columns and dimension keys contribute
    their PARTS too (live case: "client_name unik dengan order terbanyak" got interrogated
    because "order" sat inside freshly sampled return_reason values like "Cancel order" —
    in a slice where every identifier speaks "order"). Metric/concept names stay whole:
    they are display phrases, not fused identifiers, and their loose words ("net", "rate")
    could shadow real entity names."""
    for table, columns in (mdl.get("tables") or {}).items():
        _learn(technical, table)
        for column in (columns or []):
            _learn(technical, column)
    for metric in (mdl.get("metrics") or []):
        technical.add(str(metric.get("name") or "").lower())
    for concept in (mdl.get("business_concepts") or []):
        technical.add(str(concept.get("name") or "").lower())
        technical.update(str(s).lower() for s in (concept.get("synonyms") or []))
    for dimension in (mdl.get("reference_values") or {}):
        _learn(technical, dimension)


def _indexable(text: str) -> bool:
    """A reference "value" is a NAME; prose pasted into the field is never indexed."""
    return (_MIN_LEN <= len(text) <= _MAX_VALUE_LEN
            and len(_WORD.findall(text)) <= _MAX_VALUE_WORDS)


def _index_values(values: dict, mdl: dict, label: str) -> None:
    for dimension, listed in (mdl.get("reference_values") or {}).items():
        for value in (listed or []):
            if len(values) >= _MAX_VALUES:
                return
            text = str(value).strip()
            if _indexable(text):
                values.setdefault(text.lower(), []).append((text, str(dimension), label))


def _index(datasources) -> tuple[dict, set]:
    """``({lowercased value: [(value, dimension, datasource)]}, schema vocabulary)``.

    The second half is everything the MODEL is named by — tables, columns, metrics,
    concepts and their synonyms, dimension names. A question using those words is speaking
    the schema's language, not naming a row, so it is never an ambiguous entity.
    """
    values, technical = {}, set()
    for ds in datasources or []:
        mdl = ds.get("mdl") or {}
        label = ds.get("name") or ds.get("id") or ""
        _learn_vocabulary(technical, mdl)
        _index_values(values, mdl, label)
    technical.discard("")
    return values, technical


def _resembles(term: str, value: str) -> bool:
    """Close, but not the same. Containment catches the reported case (`philips` inside
    `philipsome`); the ratio catches typos and spelling variants."""
    if term == value:
        return False
    short, long_ = sorted((term, value), key=len)
    if len(short) >= _MIN_LEN and short in long_:
        return True
    return difflib.SequenceMatcher(None, term, value).ratio() >= _NEAR


def _candidates(question: str) -> list[str]:
    """Question phrases that could name an entity, longest first.

    Longest first so "philips indonesia" is judged before "philips" — the more specific
    reading is the one the person wrote.
    """
    words = [w.lower() for w in _WORD.findall(question or "")]
    out = []
    for size in range(_MAX_NGRAM, 0, -1):
        for start in range(len(words) - size + 1):
            gram = " ".join(words[start:start + size])
            if len(gram) < _MIN_LEN or gram.isdigit():
                continue
            if size == 1 and gram in _STOP:
                continue
            out.append(gram)
    return list(dict.fromkeys(out))


def _overlaps(gram: str, resolved: list) -> bool:
    """True when this phrase covers, or is covered by, a phrase that IS a known value.

    "brand philips" resembles nothing on its own, but if "philips" is a real value then
    that part of the question is already resolved and there is nothing to confirm.
    """
    return any(hit in gram or gram in hit for hit in resolved)


def _dimensions(entries) -> list:
    """Distinct dimensions these matches sit under — the same column in two datasources
    is ONE meaning, so the datasource is not part of the key."""
    out = {}
    for _value, dimension, _source in entries:
        out.setdefault(str(dimension).lower(), dimension)
    return list(out.values())


def _names_dimension(question: str, dimensions) -> bool:
    """The question already says which one it means ("gmv BRAND Philipsome").

    Asking then is not governance, it is not reading the question.
    """
    words = {w.lower() for w in _WORD.findall(question or "")}
    for dimension in dimensions:
        parts = [p for p in _WORD.findall(str(dimension).lower()) if p]
        if parts and all(part in words for part in parts):
            return True
    return False


def _resolved_phrases(question: str, candidates, values: dict) -> list:
    """Phrases already accounted for: candidate n-grams that ARE values, plus any indexed
    value fully present in the question — a value can be longer than the candidate window
    ("ZENTRA FOOD INTERNATIONAL INDONESIA TBK" after a correction), and its fragments must
    not reopen the reading the correction just settled."""
    resolved = [gram for gram in candidates if gram in values]
    qnorm = " ".join(w.lower() for w in _WORD.findall(question or ""))
    resolved += [norm for norm in (" ".join(_WORD.findall(key)) for key in values)
                 if norm and norm in qnorm]
    return resolved


def _meaning_ambiguity(question: str, gram: str, entries):
    """An exact value under more than one dimension is two different questions — unless
    the sentence already says which one it means."""
    dimensions = _dimensions(entries)
    if 1 < len(dimensions) <= _MAX_NEAR and not _names_dimension(question, dimensions):
        return gram, entries, "meaning"
    return None


def _kept_exact(question: str) -> list:
    """Words the person asked to keep exactly as written (the "use exactly what I wrote"
    correction): they are no longer open."""
    return [" ".join(w.lower() for w in _WORD.findall(m.group(1)))
            for m in _EXACT.finditer(question or "")]


def _ambiguities(question: str, values: dict, technical: set) -> list:
    """``[(term, entries, kind)]`` for every open phrase, longest first, at most
    assumptions.MAX_READINGS. ``kind`` is "value" (nothing matches exactly, but something
    is close) or "meaning" (it matches exactly, and the same word names more than one
    dimension). A phrase inside one already reported is not reported again."""
    candidates = _candidates(question)
    resolved = _resolved_phrases(question, candidates, values) + _kept_exact(question)
    found = []
    for gram in candidates:
        if len(found) >= assumptions.MAX_READINGS:
            break
        hit = _gram_ambiguity(question, gram, values, technical, found, resolved)
        if hit:
            found.append(hit)
    return found


def _gram_ambiguity(question: str, gram: str, values: dict, technical: set, found: list,
                    resolved: list):
    """The open reading of one candidate phrase, or None."""
    if gram in technical or _overlaps(gram, [term for term, _e, _k in found]):
        return None
    if gram in values:        # one meaning, however many tables carry it
        return _meaning_ambiguity(question, gram, values[gram])
    if _overlaps(gram, resolved):
        return None
    near = [entry for key, entries in values.items() if _resembles(gram, key)
            for entry in entries]
    if near and len(near) <= _MAX_NEAR:
        return gram, near, "value"
    return None


def _pattern(term: str):
    """The term as it sits in the question: matching is lowercased and space-
    normalized, what the person typed is neither."""
    return re.compile(r"\b" + r"\W+".join(re.escape(w) for w in term.split()) + r"\b",
                      re.I)


def _rewrite(question: str, term: str, value: str) -> str:
    """The question as it reads with that value: what a correction resends. Built
    server-side so every client (console, SDK, anything) does the same thing."""
    return _pattern(term).sub(value, question, count=1)


def _as_written(question: str, term: str) -> str:
    """`Philips`, not `philips`. Quoting someone's own word back at them in the wrong
    case reads as if the system misheard it."""
    hit = _pattern(term).search(question or "")
    return hit.group(0) if hit else term


def _clarified(message: str, value: str, dimension: str, english: bool) -> str:
    """The same question with the meaning spelled out.

    Appended rather than substituted: the value was already right, and the dimension is
    a column name, so naming it steers catalog pruning and the SQL the model writes
    without mangling the sentence the person wrote.
    """
    body = (message or "").rstrip()
    return (f"{body} (I mean: {value} as {dimension})" if english
            else f"{body} (yang saya maksud: {value} sebagai {dimension})")


def _kept(message: str, shown: str, english: bool) -> str:
    body = (message or "").rstrip()
    return f'{body} ({"exactly" if english else "persis"} "{shown}")'


def _meaning_reading(term: str, entries: list, message: str) -> dict:
    """One option per MEANING, not per row of the index."""
    english = confirm.looks_english(message)
    shown = _as_written(message, term)
    slots = {}
    for value, dimension, source in entries:
        slot = slots.setdefault(str(dimension).lower(),
                                {"dimension": dimension, "value": value, "sources": []})
        if source and source not in slot["sources"]:
            slot["sources"].append(source)
    slots = list(slots.values())[:_MAX_OPTIONS]
    options = [{"id": slot["dimension"], "name": slot["dimension"],
                "descr": " · ".join(slot["sources"]),
                "question": _clarified(message, slot["value"], slot["dimension"], english)}
               for slot in slots]
    where = "; ".join(f"{slot['dimension']} ({', '.join(slot['sources'])})"
                      if slot["sources"] else slot["dimension"] for slot in slots)
    return {"kind": assumptions.ENTITY, "term": shown, "options": options, "chosen": None,
            "_reading": "meaning",
            "_match": [(slot["dimension"], slot["value"]) for slot in slots],
            "_context": f"'{shown}' is a value of more than one column: {where}."}


def _value_reading(term: str, near: list, message: str) -> dict:
    english = confirm.looks_english(message)
    shown = _as_written(message, term)
    options, seen = [], set()
    for value, dimension, source in near:
        if value.lower() in seen:
            continue
        seen.add(value.lower())
        where = " · ".join(part for part in (dimension, source) if part)
        options.append({"id": value, "name": value, "descr": where,
                        "question": _rewrite(message, term, value)})
        if len(options) >= _MAX_OPTIONS:
            break
    closest = "; ".join(f"'{o['name']}' ({o['descr'].replace(' · ', ', ')})"
                        if o["descr"] else f"'{o['name']}'" for o in options)
    match = [o["name"] for o in options] + [shown]
    # the person may have meant their own word: that correction keeps it exactly
    options.append({"id": shown, "name": shown,
                    "descr": ("use exactly what I wrote" if english
                              else "pakai persis yang saya tulis"),
                    "question": _kept(message, shown, english), "use_exact": True})
    return {"kind": assumptions.ENTITY, "term": shown, "options": options, "chosen": None,
            "_reading": "value", "_match": match,
            "_context": (f"'{shown}' matches no value in the data exactly; the closest: "
                         f"{closest}. The person may also mean exactly '{shown}'.")}


def readings(message: str, datasources) -> list:
    """This turn's open readings of names in the question; [] when nothing is open."""
    values, technical = _index(datasources)
    if not values:
        return []
    return [_meaning_reading(term, entries, message) if kind == "meaning"
            else _value_reading(term, entries, message)
            for term, entries, kind in _ambiguities(message, values, technical)]


