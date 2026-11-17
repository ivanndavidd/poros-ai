"""MBR review workbook (.xlsx) from the report model.

Sheets: README (scope, status, how to read, sheet index), Storyline (every section with
its headline and status, linked), one sheet per section (facts, tables, what is missing),
Open Items, QC & Reconciliation (cross-sheet checks as LIVE formulas), Queries (the
governed SQL behind every table).

Derived columns the model marked (MoM %, share %, ratios) are written as live Excel
formulas WITH their cached value, so the numbers show without recalculation and still
update when an analyst edits an input cell. Charts are native Excel charts over the table
ranges, never images. Nothing here computes a new figure: every number is already in the
model, which is what the Markdown evidence and the deck read too.
"""
from __future__ import annotations

import calendar
import io
import re

import xlsxwriter
from xlsxwriter.utility import xl_rowcol_to_cell

from . import labels as L
from . import markdown
from .mbr_sections import GROUPS

# Reference workbook palette and formats (ACS review build, xl_helpers.Fmt)
NAVY, INK, SLATE, LINE, BG = "#0B1B3F", "#0F172A", "#475569", "#D9E1EC", "#F5F7FB"
Acme, PRE, TARGET = "#2563EB", "#94A3B8", "#7C3AED"
GOOD, BAD, WATCH = "#16A34A", "#DC2626", "#D97706"
FONT = "Arial"
NUM_FMT = "#,##0"                       # whole numbers with a thousands comma
STATUS_PILL = {"complete": "good", "partial": "watch", "placeholder": "nodata", "failed": "bad"}
STATUS_WORD = {"complete": "COMPLETE", "partial": "PARTIAL", "placeholder": "DATA PENDING",
               "failed": "FAILED"}
_BAD_SHEET = re.compile(r"[\[\]:*?/\\]")
_CHANGE_COL = re.compile(r"\b(mom|yoy)\b|\bvs\b.*%|change", re.I)
_COUNT_COL = re.compile(r"order|unit|buyer|view|click|session|video|chat|customer|impression|"
                        r"qty|cohort|lines|rows|days|sku", re.I)


_TRAILING = {"and", "or", "of", "the", "by", "for", "to", "a", "with", "in", "on"}


def _clean_sheet(text: str) -> str:
    """Excel caps a sheet name at 31 characters; cut at a word, never mid-word ("C5 Units,
    price and listings be"), and drop a dangling "and"/"for"/comma at the cut."""
    name = " ".join(_BAD_SHEET.sub(" ", text).split())
    if len(name) > 31:
        words = name[:32].split(" ")[:-1] if name[31:32] != " " else name[:31].split(" ")
        while len(words) > 1 and (words[-1].lower().strip(",;") in _TRAILING or not words[-1]):
            words.pop()
        name = " ".join(words).rstrip(",;- ") or name[:31]
    return name or "Sheet"


def _unique(base: str, taken: set) -> str:
    name, n = base, 2
    while name.lower() in taken:
        suffix = f" ({n})"
        name = base[:31 - len(suffix)] + suffix
        n += 1
    taken.add(name.lower())
    return name


def section_sheet_names(sections: list) -> dict:
    """section id -> workbook sheet name. Shared with the deck, whose speaker notes trace
    every slide to the sheet holding its numbers — one function so they cannot drift."""
    taken = {"readme", "storyline"}
    return {s["id"]: _unique(_clean_sheet(f"{s.get('ref', s['group'])} {s['title']}"), taken)
            for s in sections}


class _Book:
    def __init__(self):
        self.buf = io.BytesIO()
        self.wb = xlsxwriter.Workbook(self.buf, {"in_memory": True, "strings_to_numbers": False,
                                                 "strings_to_formulas": False,
                                                 "strings_to_urls": False})
        self.wb.set_calc_mode("auto")
        base = {"font_name": FONT, "font_size": 10, "font_color": INK, "valign": "vcenter"}

        def f(**kw):
            d = dict(base)
            d.update(kw)
            return self.wb.add_format(d)

        cell = {"border": 1, "border_color": LINE}
        self.f = {
            "kicker": f(font_size=9, bold=True, font_color=Acme),
            "title": f(font_size=16, bold=True, font_color=NAVY),
            "subtitle": f(font_size=11, italic=True, font_color=SLATE),
            "section": f(font_size=12, bold=True, font_color=NAVY, bottom=2, bottom_color=Acme),
            "head": f(bold=True, font_color="#FFFFFF", bg_color=NAVY, text_wrap=True,
                      align="center", **cell),
            "text": f(**cell),
            "textw": f(text_wrap=True, valign="top", **cell),
            "textwc": f(text_wrap=True, **cell),
            "bold": f(bold=True, **cell),
            "boldw": f(bold=True, text_wrap=True, valign="top", **cell),
            "plain": f(),
            "plainw": f(text_wrap=True, valign="top"),
            "note": f(font_size=9, italic=True, font_color=SLATE),
            "notew": f(font_size=9, italic=True, font_color=SLATE, text_wrap=True, valign="top"),
            "num": f(num_format=NUM_FMT, **cell),
            "dec": f(num_format="#,##0.00", **cell),
            "pct": f(num_format='0.0"%"', **cell),
            "numb": f(num_format=NUM_FMT, bold=True, **cell),
            "link": f(num_format=NUM_FMT, font_color="#008000", **cell),     # cross-sheet link
            "url": f(font_color="#0563C1", underline=1),
            "urlc": f(font_color="#0563C1", underline=1, **cell),
            "callout": f(text_wrap=True, valign="top", bg_color="#EAF1FE", border=1,
                         border_color="#BFD3FB"),
            "warn": f(text_wrap=True, valign="top", bg_color="#FEF3E2", border=1,
                      border_color="#F5C27A"),
            "code": f(font_name="Consolas", font_size=9, text_wrap=True, valign="top", **cell),
            "up": f(font_color=GOOD),
            "down": f(font_color=BAD),
        }
        for key, (ink, bg) in {"good": (GOOD, "#E8F7EE"), "bad": (BAD, "#FDECEC"),
                               "watch": (WATCH, "#FEF3E2"), "nodata": (SLATE, BG)}.items():
            self.f[key] = f(bold=True, font_color=ink, bg_color=bg, align="center", **cell)
        self.names = set()
        self.where = {}                  # table id -> location for QC formulas
        self.takeover = None

    def sheet_name(self, text: str) -> str:
        return _unique(_clean_sheet(text), self.names)


def _title(book, ws, kicker, title, sub=None):
    """Reference title block: kicker, title, subtitle; gridlines hidden; data from row 5."""
    ws.write(0, 0, kicker.upper(), book.f["kicker"])
    ws.write(1, 0, title, book.f["title"])
    if sub:
        ws.write(2, 0, sub, book.f["subtitle"])
    ws.set_row(1, 24)
    ws.hide_gridlines(2)


def _pill(book, status):
    return book.f[STATUS_PILL.get(status, "nodata")]


def _is_pct(col: str) -> bool:
    return "%" in col


def _money(t, j) -> bool:
    col = str(t["columns"][j])
    if _is_pct(col) or _COUNT_COL.search(col):
        return False
    vals = [abs(r[j]) for r in t.get("rows") or [] if j < len(r)
            and isinstance(r[j], (int, float)) and not isinstance(r[j], bool)]
    return bool(vals) and max(vals) >= 1e6


def _fmt(book, col: str, value):
    if _is_pct(col):
        return book.f["pct"]
    if isinstance(value, float) and not value.is_integer() and abs(value) < 100:
        return book.f["dec"]
    return book.f["num"]


def _formula(spec, row_cells, col_range_abs):
    """Excel formula text for one derived cell."""
    kind = spec["kind"]
    a = row_cells[spec["a"]]
    if kind == "change":
        b = row_cells[spec["b"]]
        return f'=IF(N({b})=0,"",(N({a})-N({b}))/ABS({b})*100)'
    if kind == "share":
        rng = col_range_abs[spec["a"]]
        return f'=IF(SUM({rng})=0,"",N({a})/SUM({rng})*100)'
    if kind == "ratio":
        b = row_cells[spec["b"]]
        scale = spec.get("scale", 1)
        tail = f"*{scale}" if scale != 1 else ""
        return f'=IF(N({b})=0,"",N({a})/{b}{tail})'
    if kind == "diff":
        b = row_cells[spec["b"]]
        return f"=N({a})-N({b})"
    return None


def _month_key(v):
    """YYYYMM for a category that names a month ('Sep 2026', '202609', '2026-09-03')."""
    text = str(v)
    m = re.fullmatch(r"([A-Z][a-z]{2}) (\d{4})", text)
    if m and m.group(1) in calendar.month_abbr:
        return int(m.group(2)) * 100 + list(calendar.month_abbr).index(m.group(1))
    m = re.fullmatch(r"(\d{4})-?(\d{2})(?:-\d{2})?", text)
    return int(m.group(1)) * 100 + int(m.group(2)) if m else None


def _takeover_points(book, rows, x):
    """Per-point fills for a month axis: grey before the Acme takeover month, blue from it;
    None when there is no takeover date or no category reads as a month."""
    keys = [_month_key(r[x]) for r in rows]
    if not (book.takeover and any(keys)):
        return None
    return [{"fill": {"color": PRE if (k and k < book.takeover) else Acme}} for k in keys]


def _style_series(series, kind, combo, colour):
    """A line with circle markers for a plain line chart, a filled bar for everything else."""
    if kind == "line" and not combo:
        series["line"] = {"color": colour, "width": 2.25}
        series["marker"] = {"type": "circle", "size": 5,
                            "fill": {"color": colour}, "border": {"color": colour}}
    else:
        series["fill"] = {"color": colour}
        series["gap"] = 60


def _add_series(book, ch, sheet, t, header, first, last, ys, combo):
    spec = t["chart"]
    kind = spec.get("type", "column")
    rows = t.get("rows") or []
    x = spec.get("x", 0)
    palette = [Acme, TARGET] if kind != "bar" else [PRE, Acme]
    for i, y in enumerate(ys[:1] if combo else ys):
        series = {"name": [sheet, header, y], "categories": [sheet, first, x, last, x],
                  "values": [sheet, first, y, last, y]}
        colour = palette[i % 2]
        if len(ys) == 1 or combo:
            colour = Acme
            points = _takeover_points(book, rows, x)
            if points is not None:
                series["points"] = points
        _style_series(series, kind, combo, colour)
        ch.add_series(series)


def _combine_line(book, ch, sheet, x, y, header, first, last):
    """The second measure of a combo chart as a violet line over the first one's bars."""
    ln = book.wb.add_chart({"type": "line"})
    ln.add_series({"name": [sheet, header, y], "categories": [sheet, first, x, last, x],
                   "values": [sheet, first, y, last, y],
                   "line": {"color": TARGET, "width": 2.25},
                   "marker": {"type": "circle", "size": 5, "fill": {"color": TARGET},
                              "border": {"color": TARGET}}})
    ch.combine(ln)


def _style_chart(ch, spec, t, ys, kind, money):
    """Title, legend and axes in the reference style; the value axis in millions for IDR."""
    font = {"name": FONT, "size": 9, "color": SLATE}
    ch.set_title({"name": (spec.get("title") or t.get("title") or "") + (" (IDR)" if money else ""),
                  "name_font": {"name": FONT, "size": 11, "bold": True, "color": INK}})
    if len(ys) > 1:
        ch.set_legend({"position": "bottom", "font": font})
    else:
        ch.set_legend({"none": True})
    value_axis = {"num_format": '#,##0,,"M"' if money else NUM_FMT, "num_font": font,
                  "line": {"none": True},
                  "major_gridlines": {"visible": True, "line": {"color": "#E5E9F0"}}}
    cat_axis = {"num_font": font, "line": {"color": "#CBD5E1"}}
    if kind == "bar":
        ch.set_x_axis(value_axis)
        ch.set_y_axis(dict(cat_axis, reverse=True))
    else:
        ch.set_x_axis(cat_axis)
        ch.set_y_axis(value_axis)
    ch.set_chartarea({"border": {"none": True}})
    ch.set_size({"width": 760, "height": 340})


def _chart(book, ws, sheet, t, header, first, last, at_row, at_col):
    """Native chart in the reference style: grey before Acme, blue with Acme, violet for the
    second measure; IDR axis in millions; Arial 11 title; light gridlines."""
    spec = t["chart"]
    kind = spec.get("type", "column")
    ys = [y for y in spec.get("y") or [] if y < len(t["columns"])]
    money = any(_money(t, y) for y in ys)
    combo = kind == "line" and len(ys) >= 2
    ch = book.wb.add_chart({"type": "column" if combo or kind == "column" else kind})
    _add_series(book, ch, sheet, t, header, first, last, ys, combo)
    if combo:                                    # first measure bars, second a line
        _combine_line(book, ch, sheet, spec.get("x", 0), ys[1], header, first, last)
    _style_chart(ch, spec, t, ys, kind, money)
    ws.insert_chart(at_row, at_col, ch)


def _write_value(book, ws, r, c, value, colname):
    """One plain cell: yes/no, a number in its column's format, a blank or text (the row
    label bold)."""
    if isinstance(value, bool):
        ws.write(r, c, "yes" if value else "no", book.f["text"])
    elif isinstance(value, (int, float)):
        ws.write_number(r, c, value, _fmt(book, colname, value))
    elif value is None:
        ws.write_blank(r, c, None, book.f["text"])
    else:
        ws.write_string(r, c, str(value), book.f["bold" if c == 0 else "text"])


def _write_row(book, ws, r, values, cols, formulas, col_range_abs):
    """One table row; a derived column is a live formula with its cached value."""
    cells = [xl_rowcol_to_cell(r, c) for c in range(len(cols))]
    for c, value in enumerate(values):
        colname = cols[c] if c < len(cols) else ""
        spec = formulas.get(c)
        if not spec:
            _write_value(book, ws, r, c, value, colname)
            continue
        text = _formula(spec, cells, col_range_abs)
        cached = value if isinstance(value, (int, float)) else ""
        ws.write_formula(r, c, text, book.f["pct"] if _is_pct(colname)
                         else _fmt(book, colname, value), cached)


def _colour_changes(book, ws, cols, col_range_abs):
    for c, name in enumerate(cols):                  # changes read green up, red down
        if _CHANGE_COL.search(str(name)):
            rng = col_range_abs[c].replace("$", "")
            ws.conditional_format(rng, {"type": "cell", "criteria": ">", "value": 0,
                                        "format": book.f["up"]})
            ws.conditional_format(rng, {"type": "cell", "criteria": "<", "value": 0,
                                        "format": book.f["down"]})


def _write_table(book, ws, sheet, row, t) -> int:
    cols = t.get("columns") or []
    ws.write(row, 0, t.get("title") or "Table", book.f["section"])
    ws.write(row + 1, 0, f"Source: {L.source_label(t.get('source'))}", book.f["note"])
    row += 2
    rows = t.get("rows") or []
    if not rows:
        ws.write(row, 0, "No rows.", book.f["note"])
        return row + 2
    header, first, last = row, row + 1, row + len(rows)
    for c, name in enumerate(cols):
        ws.write(header, c, name, book.f["head"])
    ws.set_row(header, 30)
    formulas = {f["col"]: f for f in t.get("formulas") or []}
    col_range_abs = {c: xl_rowcol_to_cell(first, c, True, True) + ":"
                        + xl_rowcol_to_cell(last, c, True, True) for c in range(len(cols))}
    labels = {}
    for i, values in enumerate(rows):
        r = first + i
        if values:
            labels[str(values[0])] = r
        _write_row(book, ws, r, values, cols, formulas, col_range_abs)
    _colour_changes(book, ws, cols, col_range_abs)
    book.where[t["id"]] = {"sheet": sheet, "first": first, "last": last, "cols": cols,
                           "labels": labels}
    if t.get("chart") and len(rows) >= 2:
        _chart(book, ws, sheet, t, header, first, last, header, len(cols) + 1)
        last = max(last, header + 18)
    return last + 3


def _write_facts(book, ws, row, facts) -> int:
    """The "What the data says" block: one line per fact, AI claims on a callout fill."""
    ws.write(row, 0, "What the data says", book.f["section"])
    row += 1
    for f in facts:
        ws.write(row, 0, L.claim_label(f), book.f["boldw"])
        ws.merge_range(row, 1, row, 9,
                       f"{L.claim_text(f)} (source: {L.source_label(f['source'])})",
                       book.f["callout"] if f["label"] != "FACT" else book.f["textw"])
        ws.set_row(row, 30)
        row += 1
    return row + 1


def _write_drafts(book, ws, row, m) -> int:
    """The drafted readings of one open item, each only when it was drafted."""
    for word, k in (("If good", "if_good"), ("If bad", "if_bad"),
                    ("If neutral", "if_neutral")):
        if m.get(k):
            ws.merge_range(row, 0, row, 1, f"{word} (drafted, conditional)",
                           book.f["bold"])
            ws.merge_range(row, 2, row, 7, m[k], book.f["textw"])
            ws.set_row(row, 30)
            row += 1
    return row


def _write_missing(book, ws, row, missing) -> int:
    """The "Not in this review yet" block: what is missing, why it matters, what is needed
    and what shows instead, each followed by its drafted readings."""
    ws.write(row, 0, "Not in this review yet", book.f["section"])
    row += 1
    ws.merge_range(row, 0, row, 1, "What is missing", book.f["head"])
    for c, h in enumerate(("Why it matters", "What is needed", "Shown instead"), start=1):
        ws.merge_range(row, c * 2, row, c * 2 + 1, h, book.f["head"])
    row += 1
    for m in missing:
        ws.merge_range(row, 0, row, 1, m["what"], book.f["boldw"])
        for c, k in enumerate(("why", "needs", "fallback"), start=1):
            ws.merge_range(row, c * 2, row, c * 2 + 1, m.get(k) or "", book.f["warn"])
        ws.set_row(row, 48)
        row = _write_drafts(book, ws, row + 1, m)
    return row


def _section_kicker(s, names) -> str:
    g = s["group"]
    if g in ("Summary", "Appendix"):
        return f"{s.get('ref', g)}  |  {s['title']}"
    return f"{g}  |  {names.get(g, g)}: {s['title']}"


def _section_sheet(book, s, sheet, names) -> str:
    book.names.add(sheet.lower())
    ws = book.wb.add_worksheet(sheet)
    ws.set_column(0, 0, 30)
    ws.set_column(1, 12, 15)
    g = s["group"]
    _title(book, ws, _section_kicker(s, names), s.get("headline") or s["title"],
           f"Section {s.get('ref', g)}, {s['title']}. Sources: "
           f"{L.source_line(s.get('queries') or ['settings'])}.")
    ws.write(3, 0, "Status", book.f["bold"])
    ws.write(3, 1, STATUS_WORD.get(s["status"], s["status"].upper()), _pill(book, s["status"]))
    ws.write_url(3, 3, "internal:'Storyline'!A1", book.f["url"], string="Back to Storyline")
    row = 5
    if s.get("facts"):
        row = _write_facts(book, ws, row, s["facts"])
    for t in s.get("tables") or []:
        row = _write_table(book, ws, sheet, row, t)
    if s.get("missing"):
        _write_missing(book, ws, row, s["missing"])
    ws.freeze_panes(4, 0)
    return sheet


def _readme(book, ws, model, index):
    ws.set_column(0, 0, 30)
    ws.set_column(1, 1, 130)
    biz = (model.get("business") or {}).get("name") or "none"
    pulled = model.get("data_pulled_at") or model.get("generated_at")
    _title(book, ws, f"{model['brand']} x Acme  |  Monthly business review {model['period_label']}",
           f"Review of {model['period_label']} for {model['brand']}",
           f"Prepared {str(model['generated_at'])[:10]} by Poros {model['engine_version']}. "
           f"Companion to the Business Review deck. Status {markdown.delivery_status(model)}; "
           f"business slice {biz}; data pulled {pulled} from the live warehouse.")
    ws.write(4, 0, "How to use", book.f["section"])
    howto = [
        "0. Start with Storyline: every section in deck order with its status and headline; "
        "each name links to its sheet.",
        "1. Each section sheet shows what the data says, the tables behind the slide and what "
        "is not in this review yet (why it matters and what is needed).",
        "2. Every figure is a cell of a governed query (see Queries) or arithmetic over those "
        "cells. Percent columns marked in the model are live formulas.",
        "3. Open Items lists every gap; QC & Reconciliation checks that the sheets agree with "
        "each other as live formulas.",
        "4. The warehouse is live: recent order statuses keep moving (shipped orders complete, "
        "cancel or return), so a later run can differ slightly for the latest months.",
        "5. Colour code: black = value or formula; green = link to another sheet; navy header = "
        "table; status cells green COMPLETE, amber PARTIAL, grey DATA PENDING, red FAILED.",
    ]
    for i, h in enumerate(howto):
        ws.write(5 + i, 0, h, book.f["plain"])
    r = 5 + len(howto) + 1
    ws.write(r, 0, "Key definitions", book.f["section"])
    groups = model.get("status_groups") or {}
    defs = [
        ("GMV", "item value after seller discount, every order status (cancelled and unpaid "
         "included); net shipping is not in the order data"),
        ("NMV", "item value after seller discount with the platform discount added back, over "
         "orders counted as sold"),
        ("Counted as sold", L.status_list(groups.get("processed"))),
        ("Cancelled", L.status_list(groups.get("cancelled"))),
        ("Seller cancellation", "cancelled by the seller or fulfilment, or for a stock or "
         "fulfilment reason; a buyer who never paid is not one"),
        ("Percent changes", "written from X to Y; MoM = against the previous month, YoY = "
         "against the same month last year"),
        ("History", "the full order history; Acme curated orders are checked against it in the "
         "appendix"),
        ("Data pulled", f"{L.stamp_label(pulled)} from the live warehouse"),
    ]
    for i, (k, v) in enumerate(defs):
        ws.write(r + 1 + i, 0, k, book.f["bold"])
        ws.write(r + 1 + i, 1, v, book.f["text"])
    r = r + 1 + len(defs) + 1
    ws.write(r, 0, "Sheet index", book.f["section"])
    ws.write_row(r + 1, 0, ["Sheet", "What it contains"], book.f["head"])
    for i, (name, note) in enumerate(index):
        ws.write_url(r + 2 + i, 0, f"internal:'{name}'!A1", book.f["urlc"], string=name)
        ws.write(r + 2 + i, 1, note, book.f["text"])


# the role each page plays in the story; the standard asks for 10 to 15 main slides with
# role, headline, takeaway and ask
STORY_ROLE = {
    "scorecard": "Set the scene: the month against target",
    "monthly_trend": "Where we are: the trend",
    "daily": "Where we are: the run rate",
    "after_acme": "What Acme changed",
    "marketplace": "Why: which marketplace moved",
    "traffic": "Why: traffic and conversion",
    "price_vs_competitor": "Why: price against Shopee",
    "products": "Why: which products moved",
    "nmv_bridge": "Why: units, price and listings",
    "price_pack": "Why: price and pack",
    "product_funnel": "Where the opportunity is",
    "creators": "Growth engine: creators",
    "live_video": "Growth engine: LIVE and video",
    "ads": "Growth engine: ads",
    "fulfilment": "What works: operations",
    "cancellation": "What leaks: cancellation",
    "customers": "Who buys: new and returning",
    "inventory": "Readiness: stock",
    "synthesis": "The story, asks and strategy",
    "month_so_far": "Next month so far",
    "month_ahead": "The plan for next month",
}
MAIN_STORY = ("scorecard", "monthly_trend", "after_acme", "marketplace", "products",
              "nmv_bridge", "product_funnel", "creators", "ads", "fulfilment", "cancellation",
              "customers", "synthesis", "month_ahead")


def takeaway_of(sec) -> str:
    """What the page leaves the reader with: its takeaway, else its first interpretation,
    else the first fact that is not the headline repeated."""
    if sec.get("takeaway"):
        return sec["takeaway"]
    facts = sec.get("facts") or []
    head = (sec.get("headline") or "").rstrip(".")
    return next((f["text"] for f in facts if f["label"] != "FACT"), None) or next(
        (f["text"] for f in facts if f["text"].rstrip(".") != head), "")


def ask_of(model, sec) -> str:
    """The ask a page leads to: the AI ask or decision resting on it, else what it needs."""
    syn = next((s.get("synthesis") for s in model["sections"] if s["id"] == "synthesis"),
               None) or {}
    for key in ("decisions", "asks"):
        for it in syn.get(key) or []:
            if sec["id"] in (it.get("pages") or []):
                return it["text"]
    mm = next(iter(sec.get("missing") or []), None)
    return f"Needs {mm['needs']}" if mm and mm.get("needs") else ""


def main_story(model) -> list:
    """10 to 15 pages in deck order: the main storyline first, then other complete pages."""
    usable = [s for s in model["sections"] if s.get("headline")
              and s["status"] in ("complete", "partial")]
    picked = [s for s in usable if s["id"] in MAIN_STORY]
    for s in usable:
        if len(picked) >= 10:
            break
        if s not in picked and s["group"] not in ("Appendix",):
            picked.append(s)
    order = {s["id"]: i for i, s in enumerate(model["sections"])}
    return sorted(picked, key=lambda s: order[s["id"]])[:15]


def _storyline(book, ws, model, sheets):
    names = dict(GROUPS)
    stats = model.get("stats") or {}
    _title(book, ws, "Storyline",
           f"The review in deck order: {stats.get('sections', 0)} sections, "
           f"{stats.get('complete', 0)} complete",
           "Headlines are computed from the data, so the workbook and the deck say the same "
           "thing. Click a section to open its sheet.")
    main = main_story(model)
    ws.write(4, 0, f"Main storyline: {len(main)} slides", book.f["section"])
    ws.write_row(5, 0, ["#", "Ref", "Section", "Role", "Headline", "Takeaway", "Ask"],
                 book.f["head"])
    for i, s in enumerate(main):
        r = 6 + i
        ws.write_number(r, 0, i + 1, book.f["num"])
        ws.write(r, 1, s.get("ref", s["group"]), book.f["bold"])
        ws.write_url(r, 2, f"internal:'{sheets[s['id']]}'!A1", book.f["urlc"],
                     string=s["title"])
        ws.write(r, 3, STORY_ROLE.get(s["id"], dict(GROUPS).get(s["group"], s["group"])),
                 book.f["textwc"])
        ws.write(r, 4, s.get("headline") or "", book.f["textwc"])
        ws.write(r, 5, takeaway_of(s), book.f["textwc"])
        ws.write(r, 6, ask_of(model, s), book.f["textwc"])
        ws.set_row(r, 60)
    top = 8 + len(main)
    ws.write(top - 1, 0, "Every page in deck order", book.f["section"])
    ws.write_row(top, 0, ["#", "Ref", "Section", "Part of the story", "Headline", "Status"],
                 book.f["head"])
    for i, s in enumerate(model["sections"]):
        r = top + 1 + i
        g = s["group"]
        ws.write_number(r, 0, i + 1, book.f["num"])
        ws.write(r, 1, s.get("ref", g), book.f["bold"])
        ws.write_url(r, 2, f"internal:'{sheets[s['id']]}'!A1", book.f["urlc"],
                     string=s["title"])
        ws.write(r, 3, names.get(g, g), book.f["textwc"])
        ws.write(r, 4, s.get("headline") or "", book.f["textwc"])
        ws.write(r, 5, STATUS_WORD.get(s["status"], s["status"].upper()), _pill(book, s["status"]))
        ws.set_row(r, 30)
    r = top + 2 + len(model["sections"])
    ws.write(r, 0, "Status of this package", book.f["section"])
    ws.merge_range(r + 1, 0, r + 3, 5,
                   f"Delivery status {markdown.delivery_status(model)}: "
                   f"{stats.get('complete', 0)} complete, {stats.get('partial', 0)} partial, "
                   f"{stats.get('placeholder', 0)} waiting for data, {stats.get('failed', 0)} "
                   f"failed. {stats.get('queries_ok', 0)} of {stats.get('queries', 0)} governed "
                   "queries returned data. " + _narration_note(model), book.f["callout"])
    ws.set_column(0, 0, 5)
    ws.set_column(1, 1, 12)
    ws.set_column(2, 2, 32)
    ws.set_column(3, 3, 30)
    ws.set_column(4, 4, 50)
    ws.set_column(5, 5, 60)
    ws.set_column(6, 6, 50)
    ws.freeze_panes(4, 0)
    return ws


def _narration_note(model) -> str:
    n = model.get("narration") or {}
    if n.get("status") in ("written", "partial"):
        return (f"Headlines, takeaways and the claims marked AI were written by "
                f"{n.get('model') or 'the tenant model'} from each page's figures "
                f"({n.get('pages_written', 0)} of {n.get('pages_total', 0)} pages"
                f"{', plus the synthesis' if n.get('synthesis') else ''}); every number in them "
                "was checked against those figures. What the checks removed is in Open Items.")
    return ("Headlines follow fixed rules from the data; no text was written by a model "
            f"({n.get('reason') or n.get('status') or 'narration did not run'}).")


def _open_items(book, model, sheets):
    ws = book.wb.add_worksheet(book.sheet_name("Open Items"))
    for c, w in enumerate((6, 30, 40, 50, 50, 35, 35, 45, 45, 45, 60)):
        ws.set_column(c, c, w)
    items = [(s, m) for s in model["sections"] for m in s.get("missing") or []]
    _title(book, ws, "Open items", f"{len(items)} item(s) not in this review yet",
           "Each line says why it matters, what is needed, what the review shows instead and "
           "what the page will say if the data turns out good, bad or neutral (drafted in "
           "advance, conditional until the data arrives).")
    ws.write_row(4, 0, ["#", "Section", "What is missing", "Why it matters", "What is needed",
                        "Shown instead", "When it arrives", "If good", "If bad", "If neutral",
                        "Technical detail (data team)"],
                 book.f["head"])
    r = 5
    for n, (s, m) in enumerate(items, start=1):
        ws.write_number(r, 0, n, book.f["num"])
        ws.write_url(r, 1, f"internal:'{sheets[s['id']]}'!A1", book.f["urlc"],
                     string=f"{s.get('ref', '')} {s['title']}")
        for c, k in enumerate(("what", "why", "needs", "fallback", "updates", "if_good",
                               "if_bad", "if_neutral", "detail"), start=2):
            ws.write(r, c, m.get(k) or "", book.f["boldw" if c == 2 else "textw"])
        ws.set_row(r, 60)
        r += 1
    if not items:
        ws.write(r, 0, "None.", book.f["note"])
    ws.freeze_panes(5, 0)


def _cell(book, table_id, label, col):
    loc = book.where.get(table_id)
    if not loc or label not in loc["labels"]:
        return None
    return f"'{loc['sheet']}'!{xl_rowcol_to_cell(loc['labels'][label], col, True, True)}"


def _col_sum(book, table_id, col):
    loc = book.where.get(table_id)
    if not loc:
        return None
    a = xl_rowcol_to_cell(loc["first"], col, True, True)
    b = xl_rowcol_to_cell(loc["last"], col, True, True)
    return f"SUM('{loc['sheet']}'!{a}:{b})"


def _qc(book, model):
    ws = book.wb.add_worksheet(book.sheet_name("QC & Reconciliation"))
    ws.set_column(0, 0, 60)
    ws.set_column(1, 3, 18)
    ws.set_column(4, 4, 12)
    _title(book, ws, "QC and reconciliation", "Cross sheet checks as live formulas",
           "Values in green are links to the section sheets; a check passes when the two "
           "values agree within 0.1%.")
    ws.write_row(4, 0, ["Check", "Value A", "Value B", "Difference", "Result"], book.f["head"])
    nmv = _cell(book, "scorecard", "NMV", 1)
    checks = [
        ("Scorecard NMV equals the sum of daily NMV", nmv, _col_sum(book, "daily", 2)),
        ("Scorecard NMV equals the sum of NMV by marketplace", nmv, _col_sum(book, "marketplace", 1)),
        ("Scorecard NMV equals the sum of NMV by product type", nmv,
         _col_sum(book, "product_type", 1)),
        ("Scorecard NMV equals the review month in the 13 month trend", nmv,
         _cell(book, "trend", model["period_label"], 2)),
    ]
    r = 5
    for name, a, b in checks:
        ws.write(r, 0, name, book.f["text"])
        if not a or not b:
            for c in range(1, 4):
                ws.write_blank(r, c, None, book.f["text"])
            ws.write(r, 4, "N/A", book.f["nodata"])
        else:
            ws.write_formula(r, 1, f"={a}", book.f["link"])
            ws.write_formula(r, 2, f"={b}", book.f["link"])
            ws.write_formula(r, 3, f"={xl_rowcol_to_cell(r, 1)}-{xl_rowcol_to_cell(r, 2)}",
                             book.f["num"])
            ws.write_formula(r, 4, f'=IF(ABS({xl_rowcol_to_cell(r, 3)})<=MAX(1,ABS('
                                   f'{xl_rowcol_to_cell(r, 1)})*0.001),"PASS","CHECK")',
                             book.f["text"])
        r += 1
    rng = f"E6:E{r}"
    ws.conditional_format(rng, {"type": "cell", "criteria": "==", "value": '"PASS"',
                                "format": book.f["good"]})
    ws.conditional_format(rng, {"type": "cell", "criteria": "==", "value": '"CHECK"',
                                "format": book.f["bad"]})
    r += 1
    stats = model.get("stats") or {}
    ws.write(r, 0, "Run facts", book.f["section"])
    facts = [("Queries succeeded", f"{stats.get('queries_ok', 0)} of {stats.get('queries', 0)}"),
             ("Estimated bytes scanned", stats.get("estimated_bytes") or 0),
             ("Delivery status", markdown.delivery_status(model))]
    for i, (k, v) in enumerate(facts, start=r + 1):
        ws.write(i, 0, k, book.f["bold"])
        if isinstance(v, (int, float)):
            ws.write_number(i, 1, v, book.f["num"])
        else:
            ws.write(i, 1, v, book.f["text"])


def _queries(book, model):
    ws = book.wb.add_worksheet(book.sheet_name("Queries"))
    for c, w in enumerate((30, 22, 34, 22, 10, 18, 120)):
        ws.set_column(c, c, w)
    qs = model.get("queries") or []
    _title(book, ws, "Governed queries", f"{sum(1 for q in qs if q['ok'])} of {len(qs)} "
           "queries returned data",
           "Every query passed the governance check before it ran; the SQL is shown as run.")
    ws.write_row(4, 0, ["Data", "Key", "Table", "Result", "Rows", "Est. bytes", "SQL or error"],
                 book.f["head"])
    for r, q in enumerate(qs, start=5):
        ws.write(r, 0, L.source_label(q["key"]), book.f["bold"])
        ws.write(r, 1, q["key"], book.f["text"])
        ws.write(r, 2, q["table"], book.f["text"])
        ws.write(r, 3, "OK" if q["ok"] else (q.get("gap_code") or q.get("code") or "FAILED"),
                 book.f["good" if q["ok"] else "watch"])
        ws.write_number(r, 4, len(q.get("rows") or []), book.f["num"])
        ws.write_number(r, 5, q.get("estimated_bytes") or 0, book.f["num"])
        ws.write(r, 6, (q.get("sql") or "") if q["ok"] else f"{q.get('error')}\n{q.get('sql') or ''}",
                 book.f["code"])
    ws.freeze_panes(5, 1)


def build(model: dict) -> bytes:
    book = _Book()
    take = (model.get("inputs") or {}).get("takeover_date")
    book.takeover = int(str(take)[:4] + str(take)[5:7]) if take else None
    sections = model.get("sections") or []
    # created first so they lead the tab order; filled once every sheet name is known
    readme = book.wb.add_worksheet(book.sheet_name("README"))
    story = book.wb.add_worksheet(book.sheet_name("Storyline"))
    sheets = section_sheet_names(sections)
    names = dict(GROUPS)
    for s in sections:
        _section_sheet(book, s, sheets[s["id"]], names)
    _open_items(book, model, sheets)
    _qc(book, model)
    _queries(book, model)
    index = [("Storyline", "every section in deck order with status and headline")]
    index += [(sheets[s["id"]], s.get("headline") or s["status"]) for s in sections]
    index += [("Open Items", "every gap with why it matters and what is needed"),
              ("QC & Reconciliation", "cross sheet checks as live formulas"),
              ("Queries", "the governed SQL behind every table")]
    _readme(book, readme, model, index)
    _storyline(book, story, model, sheets)
    readme.activate()
    book.wb.set_properties({"title": f"{model['brand']} x Acme Monthly Business Review "
                                     f"{model['period_label']}", "author": "Poros"})
    book.wb.close()
    return book.buf.getvalue()
