"""What the question REQUIRES the answer to deliver — extracted deterministically.

Design doc phase 3 (§5.1). The failure this closes: "GMV bulanan per client per brand"
answered with month × brand only, and nothing anywhere recorded that the client
breakdown was asked for and never delivered. The verifier could check figures and
citations, but not "did the result contain what the question demanded" — it had no
statement of the demand. This module is that statement.

Deliberately the DETERMINISTIC SUBSET of the doc's RequestContract, and deliberately
conservative:

  * a dimension is required only when the question says so STRUCTURALLY — "per client",
    "by brand", "top 10 seller", "breakdown per gudang". A bare mention is a FILTER, not
    a grouping ("gmv brand Philips" wants one number, not a brand column), and claiming
    it would demote correct answers to partial, which teaches people to ignore the label.
  * every extracted dimension must be GROUNDED in the visible schema (a curated
    reference_values dimension or a column-name part). "per kategori" over data with no
    category column is not a checkable requirement — the answer path already says what
    the data cannot do.
  * the doc's LLM-proposed-contract fallback is NOT built: a guessed requirement is the
    same bug as a guessed business, and the exit criteria only need the explicit cues.

Filters, time-range coverage, currency and freshness stay out until the owner decisions
in doc §19 (items 3-4) exist; ``unresolved_slots`` therefore has nothing to hold yet.
"""
import re

from . import nl2sql

# words that name a TIME bucket after "per/tiap" — a bucket, never a dimension
_BUCKETS = [
    (re.compile(r"\b(bulanan|per\s+bulan|tiap\s+bulan|setiap\s+bulan|monthly|per\s+month|"
                r"by\s+month|month\s+over\s+month)\b", re.I), "month"),
    (re.compile(r"\b(harian|per\s+hari|tiap\s+hari|setiap\s+hari|daily|per\s+day|by\s+day)\b",
                re.I), "day"),
    (re.compile(r"\b(mingguan|per\s+minggu|tiap\s+minggu|weekly|per\s+week|by\s+week)\b",
                re.I), "week"),
    (re.compile(r"\b(tahunan|per\s+tahun|tiap\s+tahun|yearly|annually|per\s+year|by\s+year)\b",
                re.I), "year"),
    # a trend/time-series ask needs SOME time axis; which one is the model's choice
    (re.compile(r"\b(tren|trend|time\s*series|pergerakan|perkembangan)\b", re.I), "period"),
]
_BUCKET_WORDS = {"bulan", "bulanan", "month", "monthly", "hari", "harian", "day", "daily",
                 "minggu", "mingguan", "week", "weekly", "tahun", "tahunan", "year",
                 "yearly", "waktu", "time", "periode", "period"}

# words that may FOLLOW a captured dimension without belonging to it: the next cue in a
# chain ("per client per brand"), a connector, or a preposition. Live Acme regression:
# "per" is also a column-name part (cost_per_unit), so the two-word reading "client per"
# GROUNDED — and no result column can ever satisfy that dimension.
_CONNECTOR_WORDS = {"per", "by", "tiap", "setiap", "masing", "berdasarkan", "breakdown",
                    "group", "grouped", "dikelompokkan", "each", "every", "dan", "and",
                    "atau", "or", "yang", "dengan", "untuk", "pada", "dari", "ke", "di",
                    "of", "in", "on", "for", "with", "the"}

# "per client", "by brand", "berdasarkan gudang", "breakdown per seller",
# "masing-masing region" — the phrase that makes a word a REQUIRED grouping. One word
# captured; the word AFTER it is read separately in build() ("per product category") so
# a chain like "per client per brand" is never half-swallowed by a greedy capture.
# re.A keeps \w and \b ASCII — the same set the spelled-out [A-Za-z0-9_] classes
# matched; without it a unicode word character would silently widen these captures.
# "masing masing" is accepted without the hyphen: chat users rarely type it (live Acme
# question "dari masing masing brand" extracted NO brand dimension), and "each" /
# "every" / "for each client" are the English shapes of the same cue.
_GROUP_CUE = re.compile(
    r"\b(?:per|by|tiap|setiap|masing[\s-]masing|berdasarkan|breakdown(?:\s+(?:per|by))?|"
    r"group(?:ed)?\s+by|dikelompokkan\s+(?:per|berdasarkan)|(?:for\s+)?each|every)\s+"
    r"([a-z]\w*)", re.I | re.A)
_NEXT_WORD = re.compile(r"\s+([a-z]\w*)", re.I | re.A)

# "top 10 client", "10 seller terbesar" — the N and, when grounded, the dimension too
_TOP_N = re.compile(r"\btop[\s-]*(\d{1,3})(?:\s+([a-z]\w*))?", re.I | re.A)
_N_BIGGEST = re.compile(r"\b(\d{1,3})\s+([a-z]\w*)\s+"
                        r"(?:terbesar|teratas|tertinggi|terbanyak|terendah|terkecil)\b",
                        re.I | re.A)

# "graph" is how the live Acme users ask for one ("generate graph and table") — it was
# missing, so that request carried no chart requirement and a text-only answer read as
# complete.
_CHART = re.compile(r"\b(chart|charts|grafik|graf|graph|graphs|dashboard|diagram|plot|"
                    r"visualisasi|visualization|visualisation|kurva)\b", re.I)

_PART = re.compile(r"[a-z0-9]+")

# Identifier parts that carry MONEY — what grounds a "how much" (value) requirement.
# Deliberately the unmistakably monetary vocabulary: "sales" or "total" can be counts.
_MONEY_PARTS = frozenset({"value", "amount", "nilai", "harga", "price", "gmv", "nmv",
                          "revenue", "pendapatan", "omzet", "omset", "cost", "biaya", "idr",
                          "rupiah", "rp", "hpp", "profit", "margin", "laba", "valuasi",
                          "nominal"})
# A "how much" that is plainly NOT asking for money ("how much is the cancel rate", "how
# much stock in units") — claiming a currency there would demote correct answers.
_NON_MONEY_ASK = re.compile(
    r"(?<![a-z])(?:rate|ratio|percent(?:age)?|persen(?:tase)?|share|pangsa|proporsi|count|"
    r"jumlah|units?|pcs|qty|quantity|kuantitas|volume|weight|berat|days?|hari|hours?|jam|"
    r"score|skor)(?![a-z])|%", re.I)


def _money_grounded(datasources) -> bool:
    """Does the visible schema hold a monetary measure at all — a money column, or a
    curated metric/concept whose NAME says money? Without one, "how much" is not a
    checkable requirement: the answer path already says what the data cannot express."""
    return any(_mdl_has_money(ds.get("mdl") or {}) for ds in datasources or [])


def _says_money(name) -> bool:
    return bool(_MONEY_PARTS & set(_PART.findall(str(name or "").lower())))


def _mdl_has_money(mdl: dict) -> bool:
    for columns in (mdl.get("tables") or {}).values():
        if any(_says_money(column) for column in columns or []):
            return True
    entries = list(mdl.get("metrics") or []) + list(mdl.get("business_concepts") or [])
    return any(_says_money(e.get("name") if isinstance(e, dict) else e) for e in entries)


def _required_measure(text: str, datasources) -> str | None:
    """"value" when the wording asks HOW MUCH (a monetary amount — live Acme feedback:
    "how much inventory" answered as 27.345.120 UNITS with high confidence) and the data
    can carry one; None for count asks, rate/percent asks, and money-less schemas."""
    if nl2sql.measure_intent(text) != "value" or _NON_MONEY_ASK.search(text):
        return None
    return "value" if _money_grounded(datasources) else None


def _schema_parts(datasources) -> list:
    """Per-identifier part sets — what grounds a dimension.

    Column names split into parts so "per client" is grounded by a ``client_name``
    column; curated reference_values keys are dimensions by definition. Kept PER
    IDENTIFIER (not one flat pool) so a two-word candidate only grounds when a single
    identifier carries BOTH parts — "product category" needs a product_category column,
    not a product_id here and a category_name there (which no one result column could
    ever satisfy).
    """
    idents = []
    for ds in datasources or []:
        mdl = ds.get("mdl") or {}
        for _table, columns in (mdl.get("tables") or {}).items():
            for column in columns or []:
                idents.append(frozenset(_PART.findall(str(column).lower())))
        for dimension in (mdl.get("reference_values") or {}):
            idents.append(frozenset(_PART.findall(str(dimension).lower())))
    return [ident for ident in idents if ident]


def _grounded(word: str, idents: list) -> str | None:
    """The normalized dimension when the schema knows this word, else None."""
    w = (word or "").strip().lower()
    if not w or w in _BUCKET_WORDS:
        return None
    for candidate in (w, w.rstrip("s")):        # clients -> client
        cparts = _PART.findall(candidate)
        if cparts and any(all(p in ident for p in cparts) for ident in idents):
            return candidate
    return None


def _cued_dimensions(text: str, idents: list) -> list:
    """Grounded dimensions sitting behind structural grouping cues."""
    dimensions = []
    for hit in _GROUP_CUE.finditer(text):
        first = hit.group(1)
        following = _NEXT_WORD.match(text, hit.end())
        second = following.group(1).lower() if following else ""
        # the two-word reading first ("per product category") — but never across a
        # connector, the next cue, or a bucket word ("per client per brand" must not
        # read "client per"), then the single word
        candidates = [first]
        if second and second not in _CONNECTOR_WORDS and second not in _BUCKET_WORDS:
            candidates.insert(0, f"{first} {second}")
        for candidate in candidates:
            dim = _grounded(candidate, idents)
            if dim:
                if dim not in dimensions:
                    dimensions.append(dim)
                break
    return dimensions


def _top_n_ask(text: str, idents: list):
    """``(top_n, grounded dimension | None)`` from "top 10 client" / "10 seller terbesar"."""
    for pattern in (_TOP_N, _N_BIGGEST):
        hit = pattern.search(text)
        if hit:
            return int(hit.group(1)), _grounded(hit.group(2) or "", idents)
    return None, None


def build(message: str, datasources) -> dict | None:
    """The explicit output requirements of this question, or None when it states none.

    None is the common case and means phase 3 changes nothing about that turn — the
    same opt-in-by-evidence posture as every layer before it.
    """
    text = message or ""
    idents = _schema_parts(datasources)

    dimensions = _cued_dimensions(text, idents)
    top_n, top_dim = _top_n_ask(text, idents)
    if top_dim and top_dim not in dimensions:
        dimensions.append(top_dim)

    time_bucket = next((bucket for pattern, bucket in _BUCKETS if pattern.search(text)),
                       None)
    wants_chart = bool(_CHART.search(text))
    measure = _required_measure(text, datasources)

    if not (dimensions or time_bucket or top_n or wants_chart or measure):
        return None
    return {"dimensions": dimensions, "time_bucket": time_bucket,
            "outputs": {"chart": wants_chart, "top_n": top_n},
            # "value" = the answer must deliver a monetary amount, not a unit count
            "measure": measure}
