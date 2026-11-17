"""A period label must describe the rows it groups — deterministic SQL check.

Live case (Acme, 2026-10-07, trace 93764877): "Evaluasi Oktober menggunakan perbandingan
month-to-date versus month-to-date September" ran

    CASE WHEN order_date BETWEEN '2026-10-01' AND '2026-10-07' THEN 'Oct MTD 1-7'
         ELSE 'Sep MTD 1-7' END AS period ...
    WHERE order_date BETWEEN '2026-09-01' AND '2026-10-07'

An ELSE branch takes every row the WHERE lets through outside the WHEN ranges, so
'Sep MTD 1-7' summed ALL of September and the answer reported -82.7% at High confidence.
The verifier could not see it: the figure WAS in the result — the label was wrong. Same
family on 2026-09-29 (traces 1e92d130 / 787fd239): a bucket labelled 'Rata-rata 3 bulan
terakhir (Juni-Agustus 2026)' held SUM(nmv) over three months, and the answer said -55%
where the truth was +34.8%.

``violation()`` runs inside execute_governed BEFORE govern and the (billed) dry-run, so a
rejected query costs nothing and returns to the model as an ERROR with the fix spelled
out — the same self-correction path a DRY_RUN_ERROR takes. It is deliberately QUIET: it
only speaks when it can PROVE a mismatch — a label it can read (ISO dates, month names,
YYYYMM, years, quarters), a CASE whose every WHEN is a range on ONE date column, and a
WHERE it fully understands. Anything else is skipped; an unparseable query is never
blocked here (governance still sees it).

Rules, in the order they are reported:
  1. range   — the dates a bucket actually covers (WHEN ∩ WHERE minus earlier WHENs; the
               ELSE bucket = WHERE minus every WHEN) must lie inside the dates its label
               names. Ranges are clamped at today: data cannot be newer than today.
  2. mtd     — buckets labelled MTD / month-to-date must start on day 1 and end on the
               same day number of their months.
  3. average — a label saying average/rata-rata over plain SUM/COUNT totals (no AVG and
               no division anywhere in the statement) is a total wearing an average's name.
  4. today   — a month-to-date QUESTION counts through YESTERDAY unless it says "today"
               or names an end date: an aggregate whose day-precision date filter reaches
               today compares a day that is still filling up.

Labels come from two SQL shapes: a projected CASE whose THEN/ELSE values are string
literals (the label is the literal), and conditional aggregation —
``SUM(CASE WHEN d BETWEEN … THEN gmv END) AS gmv_oktober_mtd`` (the label is the alias).
"""
import calendar
import datetime as _dt
import re

import sqlglot
from sqlglot import exp

CODE = "PERIOD_LABEL_MISMATCH"

_NEG, _POS = -(10 ** 9), 10 ** 9          # open interval ends, as day ordinals
_ALL = [(_NEG, _POS)]
_UNRELATED = "unrelated"                  # a predicate that never mentions the column
_LABEL_CAP = 60                           # the whole error must fit the 500-char tool feedback

_WRAPPERS = tuple(t for t in (getattr(exp, n, None) for n in (
    "Cast", "TryCast", "Paren", "Date", "Datetime", "Timestamp", "TsOrDsToDate",
    "TimeStrToTime", "DateStrToDate", "StrToDate")) if t)
_COMPARE = {exp.EQ: "=", exp.GTE: ">=", exp.GT: ">", exp.LT: "<", exp.LTE: "<="}
_FLIP = {"=": "=", ">=": "<=", ">": "<", "<": ">", "<=": ">="}
_AGGS = (exp.Sum, exp.Count, exp.Avg)
_DIVISIONS = tuple(t for t in (getattr(exp, n, None) for n in ("Div", "SafeDivide", "IntDiv")) if t)

_MONTHS = {
    "januari": 1, "january": 1, "jan": 1, "februari": 2, "pebruari": 2, "february": 2,
    "feb": 2, "maret": 3, "march": 3, "mar": 3, "april": 4, "apr": 4, "mei": 5, "may": 5,
    "juni": 6, "june": 6, "jun": 6, "juli": 7, "july": 7, "jul": 7, "agustus": 8,
    "august": 8, "aug": 8, "agu": 8, "agt": 8, "ags": 8, "september": 9, "sept": 9,
    "sep": 9, "oktober": 10, "october": 10, "oct": 10, "okt": 10, "november": 11,
    "nov": 11, "nop": 11, "desember": 12, "december": 12, "dec": 12, "des": 12,
}
_ISO_DAY = re.compile(r"(?<!\d)(\d{4})[-_/.](\d{1,2})[-_/.](\d{1,2})(?!\d)")
# the short end of an ISO range: '2026-07-01_to_17', '2026-07-06_to_07-12'
_PARTIAL_END = re.compile(r"\s*(?:_?to_?|[-–—])\s*(?:(\d{1,2})[-_/.])?(\d{1,2})(?![-_/.]?\d)")
# 'orders_2026_plus', 'since 2025' / 'before_2026' — the label is open on that side
_OPEN_END = re.compile(r"(?<![a-z])(?:plus|onwards?|since|after|sejak|setelah|later)(?![a-z])|\+")
_OPEN_START = re.compile(r"(?<![a-z])(?:before|pre|prior|sebelum|until|upto)(?![a-z])")
_YYYYMM = re.compile(r"(?<!\d)((?:19|20)\d{2})(0[1-9]|1[0-2])(?!\d)")
_ISO_MONTH = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_/](0?[1-9]|1[0-2])(?!\d)")
_QUARTER = re.compile(r"(?<![a-z\d])q([1-4])(?![a-z\d])")
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_DAY_RANGE = re.compile(r"(?<!\d)(\d{1,2})\s*[-–—]\s*(\d{1,2})(?!\d)")
_WORD = re.compile(r"[a-z]+")
_MONTH_JOIN = re.compile(r"^\s*(?:[-–—]|to|s\.?d\.?|sampai|hingga|until)\s*$")
_ISO_LITERAL = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")
_YYYYMM_LITERAL = re.compile(r"((?:19|20)\d{2})(0[1-9]|1[0-2])")

_MTD_LABEL = re.compile(r"(?<![a-z])mtd(?![a-z])|month[\s_-]*to[\s_-]*date", re.I)
_AVG_LABEL = re.compile(r"rata[\s_-]*rata|rata2|rerata|average|(?<![a-z])(?:avg|mean)(?![a-z])",
                        re.I)
_MTD_QUESTION = re.compile(r"(?<![a-z])mtd(?![a-z])|month[\s-]*to[\s-]*date|bulan\s+berjalan",
                           re.I)
# the asker set the end themselves: "sampai hari ini", "today", "s.d. tanggal 8", "1-8 Okt"
_ENDS_TODAY = re.compile(r"hari\s+ini|today|sekarang|saat\s+ini|(?<![a-z])now(?![a-z])", re.I)
_ENDS_ON_A_DAY = re.compile(r"tanggal\s*\d|(?<!\d)\d{1,2}\s*[-–]\s*\d{1,2}(?!\d)", re.I)

_FIX = {
    "range": ("Give each period its own WHEN with explicit start and end dates (no ELSE "
              "label) and limit WHERE to exactly those windows."),
    "mtd": "MTD windows must cover the same day numbers in each month (e.g. Oct 1-7 vs Sep 1-7).",
    "average": ("Compute the average in SQL (e.g. SUM(x) / number of months, or AVG over a "
                "per-month subquery) or label it as a total."),
}


# ---- interval sets: sorted, merged lists of inclusive (lo, hi) day ordinals ----
def _norm(ivs):
    out = []
    for lo, hi in sorted(i for i in ivs if i[0] <= i[1]):
        if out and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def _and(a, b):
    return _norm([(max(l1, l2), min(h1, h2)) for l1, h1 in a for l2, h2 in b])


def _cut(interval, hole):
    """``interval`` minus ``hole``: zero, one or two pieces."""
    (lo1, hi1), (lo2, hi2) = interval, hole
    if hi2 < lo1 or lo2 > hi1:
        return [interval]
    return [piece for piece in ((lo1, lo2 - 1), (hi2 + 1, hi1)) if piece[0] <= piece[1]]


def _minus(a, b):
    out = list(a)
    for hole in b:
        out = [piece for interval in out for piece in _cut(interval, hole)]
    return _norm(out)


def _day(ordinal) -> str:
    if ordinal <= _NEG:
        return "(no start)"
    if ordinal >= _POS:
        return "(no end)"
    return _dt.date.fromordinal(ordinal).isoformat()


def _fmt(ivs) -> str:
    return ", ".join(_day(lo) if lo == hi else f"{_day(lo)}..{_day(hi)}" for lo, hi in ivs)


def _month_span(year, month):
    if not (1 <= year <= 9999 and 1 <= month <= 12):
        return None
    last = calendar.monthrange(year, month)[1]
    return (_dt.date(year, month, 1).toordinal(), _dt.date(year, month, last).toordinal())


def _months_span(start_year, first, end_year, last):
    a, b = _month_span(start_year, first), _month_span(end_year, last)
    return (a[0], b[1]) if a and b else None


# ---- SQL predicates -> interval sets ----
def _unwrap(node):
    while isinstance(node, _WRAPPERS):
        node = node.this
    return node


def _column_name(node):
    node = _unwrap(node)
    return node.name.lower() if isinstance(node, exp.Column) else None


def _literal(node):
    """('day', ordinal) for '2026-10-01' (also inside CAST/DATE/TIMESTAMP),
    ('month', (first, last)) for a YYYYMM integer like 202608; None otherwise."""
    node = _unwrap(node)
    if not isinstance(node, exp.Literal):
        return None
    text = str(node.this)
    if not node.is_string:
        m = _YYYYMM_LITERAL.fullmatch(text)
        return ("month", _month_span(int(m[1]), int(m[2]))) if m else None
    m = _ISO_LITERAL.match(text)
    if not m:
        return None
    try:
        return ("day", _dt.date(int(m[1]), int(m[2]), int(m[3])).toordinal())
    except ValueError:
        return None


def _span(lit):
    """(first day, last day) a literal stands for."""
    kind, value = lit
    return (value, value) if kind == "day" else value


def _compare(op, lit):
    lo, hi = _span(lit)
    return {"=": [(lo, hi)], ">=": [(lo, _POS)], ">": [(hi + 1, _POS)],
            "<": [(_NEG, lo - 1)], "<=": [(_NEG, hi)]}[op]


def _mentions(node, col) -> bool:
    return any(c.name.lower() == col for c in node.find_all(exp.Column))


def _between(node, col):
    if _column_name(node.this) != col:
        return None
    low, high = _literal(node.args.get("low")), _literal(node.args.get("high"))
    return [(_span(low)[0], _span(high)[1])] if low and high else None


def _in_list(node, col):
    if _column_name(node.this) != col or node.args.get("query"):
        return None
    lits = [_literal(e) for e in node.expressions]
    return _norm([_span(lit) for lit in lits]) if lits and all(lits) else None


def _comparison(node, op, col):
    if _column_name(node.this) == col and _literal(node.expression):
        return _compare(op, _literal(node.expression))
    if _column_name(node.expression) == col and _literal(node.this):
        return _compare(_FLIP[op], _literal(node.this))
    return None


def _leaf(node, col):
    """A single comparison of ``col`` against date literals, or None."""
    if isinstance(node, exp.Between):
        return _between(node, col)
    if isinstance(node, exp.In):
        return _in_list(node, col)
    op = _COMPARE.get(type(node))
    return _comparison(node, op, col) if op else None


def _as_set(value):
    return _ALL if value == _UNRELATED else value


def _pred(node, col):
    """Interval set ``node`` allows for ``col``; _UNRELATED when it never mentions
    ``col``; None when it mentions ``col`` in a way this module does not understand."""
    if node is None or not _mentions(node, col):
        return _UNRELATED
    if isinstance(node, exp.Paren):
        return _pred(node.this, col)
    if not isinstance(node, (exp.And, exp.Or)):
        return _leaf(node, col)
    left, right = _pred(node.this, col), _pred(node.expression, col)
    if left is None or right is None:
        return None
    if isinstance(node, exp.And):
        return _and(_as_set(left), _as_set(right))
    # an OR mixing this column with another one does not bound this column at all
    return None if _UNRELATED in (left, right) else _norm(left + right)


def _branches(case):
    return case.args.get("ifs") or []


# ---- the scope a SELECT reads from ----
def _ctes(root):
    return {cte.alias.lower(): cte.this for cte in root.find_all(exp.CTE)
            if isinstance(cte.this, exp.Select) and cte.alias}


def _source(select, ctes):
    """The inner SELECT a single-source FROM reads, ``"table"`` for a base table, or None
    (joins, unions, functions — not followed)."""
    if select.args.get("joins"):
        return None
    from_ = select.args.get("from") or select.args.get("from_")
    src = getattr(from_, "this", None)
    if isinstance(src, exp.Subquery):
        return src.this if isinstance(src.this, exp.Select) else None
    if isinstance(src, exp.Table):
        return ctes.get(src.name.lower(), "table")
    return None


def _scope_range(select, col, ctes, depth=0):
    """Dates of ``col`` the rows reaching ``select`` can carry, or None when unknown."""
    where = select.args.get("where")
    own = _pred(where.this, col) if where else _UNRELATED
    if own is None:
        return None
    own = _as_set(own)
    src = _source(select, ctes)
    if src == "table":
        return own
    inner = _scope_range(src, col, ctes, depth + 1) \
        if isinstance(src, exp.Select) and depth < 3 else None
    if inner is not None:
        return _and(own, inner)
    return own if own != _ALL else None


# ---- labels -> the dates they name ----
def _blank(text, regex, make, spans):
    """Collect ``make(match)`` for every match and blank the matched text out."""
    for m in regex.finditer(text):
        span = make(m)
        if span:
            spans.append(span)
    return regex.sub(lambda m: " " * len(m[0]), text)


def _date(year, month, day):
    try:
        return _dt.date(int(year), int(month), int(day))
    except ValueError:
        return None


def _range_end(start, tail):
    """End date of '…_to_17' (same month) / '…_to_07-12' (that month, maybe next year)."""
    month = int(tail[1]) if tail[1] else start.month
    end = _date(start.year + (month < start.month), month, tail[2])
    return end if end and end >= start else None


def _iso_days(text, spans):
    """ISO days, each optionally followed by the short end of a range, blanked out."""
    out = text
    for m in _ISO_DAY.finditer(text):
        start = _date(m[1], m[2], m[3])
        if not start:
            continue
        tail = _PARTIAL_END.match(text, m.end())
        end = _range_end(start, tail) if tail else None
        spans.append((start.toordinal(), (end or start).toordinal()))
        stop = tail.end() if end else m.end()
        out = out[:m.start()] + " " * (stop - m.start()) + out[stop:]
    return out


def _month_key(spans):
    """A lone '2026-07-01' is how month buckets are commonly keyed: read it as July."""
    if len(spans) == 1 and spans[0][0] == spans[0][1]:
        day = _dt.date.fromordinal(spans[0][0])
        if day.day == 1:
            return [_month_span(day.year, day.month)]
    return spans


def _year_for(month, years, actual):
    """The label's own year, else the year of the covered rows this month falls in."""
    if years:
        return years[0]
    edges = [edge for edge in (actual[0][0], actual[-1][1]) if _NEG < edge < _POS]
    candidates = [_dt.date.fromordinal(edge).year for edge in edges]
    for year in candidates:
        span = _month_span(year, month)
        if span and _and([span], actual):
            return year
    return candidates[0]


def _month_range(months, years, actual):
    """'Juni-Agustus 2026' / 'Des - Jan' as one span (a range may cross a year end)."""
    first, last = months[0][2], months[1][2]
    if years and last < first:
        return _months_span(years[0] - 1, first, years[0], last)
    start = _year_for(first, years, actual)
    return _months_span(start, first, start + (last < first), last)


def _month_days(month, day_range, years, actual):
    """'Sep MTD 1-7' / '1–17 Agustus' as those days; the whole month when they don't fit."""
    span = _month_span(_year_for(month, years, actual), month)
    d1, d2 = day_range
    if span and 1 <= d1 <= d2 <= span[1] - span[0] + 1:
        return (span[0] + d1 - 1, span[0] + d2 - 1)
    return span


def _named_month_spans(text, years, actual):
    months = [(m.start(), m.end(), _MONTHS[m[0]]) for m in _WORD.finditer(text)
              if m[0] in _MONTHS]
    days = [(int(m[1]), int(m[2])) for m in _DAY_RANGE.finditer(text)]
    if len(months) == 2 and _MONTH_JOIN.match(text[months[0][1]:months[1][0]]):
        return [_month_range(months, years, actual)]
    if len(months) == 1 and len(days) == 1:
        return [_month_days(months[0][2], days[0], years, actual)]
    return [_month_span(_year_for(month, years, actual), month) for _s, _e, month in months]


def _claimed(label, actual):
    """Interval the label names (hull of every date token in it), or None when the label
    names no date at all ('other_days', 'Bulan lalu')."""
    days = []
    text = _iso_days(label.lower(), days)
    spans = _month_key(days)
    text = _blank(text, _YYYYMM, lambda m: _month_span(int(m[1]), int(m[2])), spans)
    text = _blank(text, _ISO_MONTH, lambda m: _month_span(int(m[1]), int(m[2])), spans)
    years = [int(m[1]) for m in _YEAR.finditer(text)]
    text = _YEAR.sub(lambda m: " " * len(m[0]), text).replace("_", " ")
    named = _named_month_spans(text, years, actual)
    quarters = [int(m[1]) for m in _QUARTER.finditer(text)]
    base_year = years[0] if years else _year_for(1, [], actual)
    spans += named
    spans += [_months_span(base_year, 3 * q - 2, base_year, 3 * q) for q in quarters]
    if not named and not quarters:
        spans += [_months_span(year, 1, year, 12) for year in years]
    spans = [span for span in spans if span]
    if not spans:
        return None
    lo = _NEG if _OPEN_START.search(text) else min(s[0] for s in spans)
    hi = _POS if _OPEN_END.search(text) else max(s[1] for s in spans)
    return [(lo, hi)]


# ---- period buckets in one SELECT ----
def _string(node):
    return str(node.this) if isinstance(node, exp.Literal) and node.is_string else None


def _when_column(case):
    """The ONE date column every WHEN of ``case`` ranges over, or None."""
    cols = set()
    for branch in _branches(case):
        found = {c.name.lower() for c in branch.this.find_all(exp.Column)}
        if len(found) != 1:
            return None
        cols |= found
    return next(iter(cols)) if len(cols) == 1 else None


def _label_buckets(case, scope, col):
    """[(label, covered intervals, is_else)] for a CASE that maps date ranges to string
    labels; [] when any branch is not a plain date range."""
    buckets, taken = [], []
    for branch in _branches(case):
        cond, label = _pred(branch.this, col), _string(branch.args.get("true"))
        if cond in (None, _UNRELATED) or label is None:
            return []
        buckets.append((label, _minus(_and(cond, scope), taken), False))
        taken = _norm(taken + cond)
    default = case.args.get("default")
    if default is None or isinstance(default, exp.Null):
        return buckets
    label = _string(default)
    return buckets + [(label, _minus(scope, taken), True)] if label is not None else []


def _zero_or_null(node) -> bool:
    if node is None or isinstance(node, exp.Null):
        return True
    return isinstance(node, exp.Literal) and str(node.this) in ("0", "0.0")


def _measure_case(projection):
    """The single CASE of ``SUM(CASE WHEN … THEN x [ELSE 0] END) AS alias``, or None."""
    cases = list(projection.find_all(exp.Case))
    if len(cases) != 1 or not projection.find(*_AGGS):
        return None
    ifs = _branches(cases[0])
    if len(ifs) != 1 or _string(ifs[0].args.get("true")) is not None:
        return None
    return cases[0] if _zero_or_null(cases[0].args.get("default")) else None


def _measure_bucket(projection, scope_of):
    """(alias, covered intervals) for conditional aggregation over a date range."""
    case = _measure_case(projection) if isinstance(projection, exp.Alias) else None
    col = _when_column(case) if case is not None else None
    if not col:
        return None
    cond, scope = _pred(_branches(case)[0].this, col), scope_of(col)
    if cond in (None, _UNRELATED) or scope is None:
        return None
    return projection.alias, _and(cond, scope)


def _case_buckets(case, scope_of):
    col = _when_column(case)
    scope = scope_of(col) if col else None
    return _label_buckets(case, scope, col) if scope is not None else []


def _buckets(select, ctes, today):
    cache = {}

    def scope_of(col):
        if col not in cache:
            cache[col] = _scope_range(select, col, ctes)
        return cache[col]

    out = []
    for projection in select.expressions:
        node = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(node, exp.Case):
            out += _case_buckets(node, scope_of)
            continue
        found = _measure_bucket(projection, scope_of)
        if found:
            out.append((found[0], found[1], False))
    clamp = [(_NEG, today)]
    return [(label, _and(ivs, clamp), is_else) for label, ivs, is_else in out]


# ---- the four rules ----
def _range_issue(buckets):
    for label, actual, is_else in buckets:
        claim = _claimed(label, actual) if actual else None
        if claim and _minus(actual, claim):
            how = " (an ELSE bucket takes every row the WHERE allows)" if is_else else ""
            return ("range", f"period label '{label[:_LABEL_CAP]}' covers {_fmt(actual)} "
                             f"but names {_fmt(claim)}{how}.")
    return None


def _mtd_window(label, actual):
    """(None, (last day, month length)) for day 1 through day N of one month, else
    (the problem, None)."""
    lo, hi = actual[0][0], actual[-1][1]
    start = _dt.date.fromordinal(lo) if lo > _NEG else None
    end = _dt.date.fromordinal(hi) if hi < _POS else None
    if len(actual) > 1 or not start or not end or start.day != 1 \
            or (end.year, end.month) != (start.year, start.month):
        return (f"MTD bucket '{label[:_LABEL_CAP]}' covers {_fmt(actual)}, not day 1 through "
                "a fixed day of one month."), None
    return None, (end.day, calendar.monthrange(end.year, end.month)[1])


def _mtd_issue(buckets):
    mtd = [(label, actual) for label, actual, _else in buckets
           if actual and _MTD_LABEL.search(label)]
    if len(mtd) < 2:
        return None
    windows = []
    for label, actual in mtd:
        problem, window = _mtd_window(label, actual)
        if problem:
            return ("mtd", problem)
        windows.append((label, actual, window))
    # a shorter month may end at its own last day (Feb 28 matches Mar 30)
    for label_a, act_a, (day_a, len_a) in windows:
        later = [(label_b, act_b) for label_b, act_b, (day_b, _len) in windows
                 if day_b > day_a and day_a != len_a]
        if later:
            label_b, act_b = later[0]
            return ("mtd", f"MTD buckets differ: '{label_a[:_LABEL_CAP]}' = {_fmt(act_a)} "
                           f"but '{label_b[:_LABEL_CAP]}' = {_fmt(act_b)}.")
    return None


def _average_issue(root, buckets):
    if any(root.find_all(exp.Avg)) or any(root.find_all(*_DIVISIONS)):
        return None
    label = next((label for label, _actual, _else in buckets if _AVG_LABEL.search(label)), None)
    if label is None:
        return None
    return ("average", f"bucket '{label[:_LABEL_CAP]}' is labelled an average, but the query "
                       "only sums (no AVG, no division).")


def _day_precision(cond, col) -> bool:
    """True when ``col`` is compared to at least one full date ('2026-10-01'), not only
    to YYYYMM months — a monthly key cannot express month-to-date at all."""
    for node in cond.find_all(exp.Between, exp.EQ, exp.GTE, exp.GT, exp.LT, exp.LTE, exp.In):
        sides = [s for s in (node.this, node.args.get("low"), node.args.get("high"),
                             node.args.get("expression"), *(node.args.get("expressions") or []))
                 if s is not None]
        if col in {_column_name(s) for s in sides} \
                and any((_literal(s) or ("",))[0] == "day" for s in sides):
            return True
    return False


def _reaching_today(select, today):
    """The date column an aggregate SELECT's window reaches today on, or None."""
    where = select.args.get("where")
    conds = ([where.this] if where else []) + [
        branch.this for case in select.find_all(exp.Case) for branch in _branches(case)]
    for cond in conds:
        for col in sorted({c.name.lower() for c in cond.find_all(exp.Column)}):
            ivs = _pred(cond, col)
            if ivs in (None, _UNRELATED) or not _day_precision(cond, col):
                continue
            if any(lo <= today <= hi for lo, hi in ivs):
                return col
    return None


def _today_issue(root, question, today):
    question = question or ""
    if not _MTD_QUESTION.search(question) or _ENDS_TODAY.search(question) \
            or _ENDS_ON_A_DAY.search(question):
        return None
    col = next((found for select in root.find_all(exp.Select) if select.find(*_AGGS)
                for found in (_reaching_today(select, today),) if found), None)
    if col is None:
        return None
    yesterday = _dt.date.fromordinal(today - 1)
    return ("today", f"month-to-date counts through yesterday ({yesterday.isoformat()}): this "
                     f"window on {col} reaches today, a day still filling up. End every window "
                     f"at that day number (e.g. day {yesterday.day} of each month).")


def _today() -> _dt.date:
    """Server date — the same clock nl2sql.now_context() shows the model."""
    return _dt.date.today()


def _first_issue(root, question, day):
    ctes = _ctes(root)
    buckets = [bucket for select in root.find_all(exp.Select)
               for bucket in _buckets(select, ctes, day)]
    return (_range_issue(buckets) or _mtd_issue(buckets)
            or _average_issue(root, buckets) or _today_issue(root, question, day))


def violation(sql: str, dialect: str = None, question: str = "",
              today: _dt.date = None) -> str | None:
    """The fix instruction for the first period-label problem in ``sql``, or None."""
    try:
        root = sqlglot.parse_one(sql or "", read=dialect or None)
        found = _first_issue(root, question, (today or _today()).toordinal()) if root else None
    except Exception:
        return None                    # unreadable SQL or a checker bug never blocks a query
    if not found:
        return None
    rule, text = found
    return text if rule == "today" else f"{text} {_FIX[rule]}"
