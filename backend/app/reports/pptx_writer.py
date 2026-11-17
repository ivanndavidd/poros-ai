"""MBR review deck (.pptx) in the house design of the reference deck.

The look is the reference package's design system (ACS Business Review Sep 2026 v4,
build/lib.js), ported from pptxgenjs to python-pptx so the backend needs no Node:

* three masters: COVER (bg_cover.png), DARK (bg_dark.png) for the key slides (executive
  summary, synthesis, the month ahead, decisions) and LIGHT for the rest;
* every slide: spaced kicker top left ("C  |  WHY DID IT CHANGE: MARKETPLACES"), the
  "<BRAND> × Acme  |  <MONTH> REVIEW" strip top right, a 26 pt headline, a source line,
  "Strictly confidential" and the page number at the foot;
* building blocks of lib.js: KPI card, RAG pill, panel with an icon heading, takeaway band
  with a lightbulb and a blue action button, navy header tables with ▲▼ coloured changes;
* charts are NATIVE (editable), styled like the reference: grey for before / previous,
  blue for Acme / current, violet for the second measure, data labels on short series.

House rules kept and checked by ``qc()``: no text under 13 pt, no dash characters in
visible text, no picture other than the two backgrounds and the icons, speaker notes on
every slide (framing, message, evidence, caveat, workbook trace, transition).

The deck never computes a business figure of its own: it lays out the cells, facts and
headlines the engine produced, so a number on a slide is the number in the evidence file.
The colour of a signal (red, amber, green) is a presentation rule stated in the notes.
"""
from __future__ import annotations

import calendar
import io
import os
import re
from collections import namedtuple
from copy import deepcopy

from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION, XL_MARKER_STYLE, XL_TICK_LABEL_POSITION
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE, MSO_SHAPE_TYPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

from . import labels as L
from .markdown import delivery_status
from .mbr_sections import GROUPS, share_text
from .xlsx_writer import section_sheet_names

ASSETS = os.path.join(os.path.dirname(__file__), "assets")
FONT = "Calibri"
MIN_PT = 13


def _rgb(h):
    return RGBColor.from_string(h)


# lib.js palette
C = {
    "navy": "0B1B3F", "navy2": "13285A", "navy3": "1B3570", "ink": "0F172A", "slate": "475569",
    "muted": "64748B", "line": "E2E8F0", "line2": "CBD5E1", "bg": "F5F7FB", "bg2": "EEF2F8",
    "white": "FFFFFF", "acme": "2563EB", "pre": "94A3B8", "target": "7C3AED", "expected": "0D9488",
    "good": "16A34A", "bad": "DC2626", "watch": "D97706", "goodBg": "E8F7EE", "badBg": "FDECEC",
    "watchBg": "FEF3E2", "infoBg": "EAF1FE", "cyan": "22D3EE", "violetL": "A78BFA",
    "greenL": "4ADE80", "redL": "F87171", "blueL": "93C5FD",
}
RAG = {"RED": ("bad", "badBg"), "AMBER": ("watch", "watchBg"), "GREEN": ("good", "goodBg"),
       "INFO": ("muted", "bg2")}
STATUS_PILL = {"complete": "GREEN", "partial": "AMBER", "placeholder": "RED", "failed": "RED"}
STATUS_WORD = {"complete": "COMPLETE", "partial": "PARTIAL", "placeholder": "DATA PENDING",
               "failed": "QUERY FAILED"}
DARK_GROUPS = ("E", "F")
SECTION_ICON = {
    "scorecard": "Gauge", "monthly_trend": "ChartColumn", "daily": "CalendarDays",
    "after_acme": "Flag", "marketplace": "Store", "traffic": "MousePointerClick",
    "price_vs_competitor": "Scale", "products": "Trophy", "product_mix": "Layers",
    "product_funnel": "Search", "margin": "Coins", "creators": "Users", "live_video": "Video",
    "ads": "Megaphone", "mp_economics": "Wallet", "fulfilment": "Truck",
    "cancellation": "CircleX", "payment": "ShoppingCart", "geography": "MapPin",
    "customers": "UserPlus", "customer_service": "Package", "inventory": "Boxes",
    "synthesis": "Sparkles", "month_ahead": "Rocket", "reconciliation": "ShieldCheck",
    "status_map": "Tag",
}
TAKEAWAY_Y, TAKEAWAY_H = 6.12, 0.82
# one colour per platform wherever a chart names it (the analyst: TikTok black, Tokopedia green)
PLATFORM_COLOUR = {"tiktok": "000000", "tokopedia": "03AC0E", "shopee": "EE4D2D",
                   "lazada": "0F146D", "blibli": "0095DA"}


def _platform_colour(name, dark=False):
    n = str(name or "").lower()
    for k, v in PLATFORM_COLOUR.items():
        if k in n:
            return "E5E7EB" if dark and v == "000000" else v
    return None
BODY_Y, BODY_H = 1.72, 4.25
MAX_ROWS = 12
FOOT_Y = 6.92                       # the source line; a slide without a takeaway band
CHARS_PER_INCH = 12.0               # Calibri 13 pt bold, a little under its average width
LINE_H, CELL_PAD = 0.235, 0.09      # one wrapped line of 13 pt text; cell margins
PARA_LINE_H, PARA_GAP, PARA_FIT = 0.235, 0.1, 0.97   # 14 pt panel text, measured on renders
STATUS_TEXT = {"PASS": "complete", "DELIVER_WITH_WARNINGS": "delivered with open items",
               "QUALITY_FAIL_DELIVERED": "delivered, quality checks failed"}
_DASHES = re.compile(r"\s{1,10}[-‒–—―]\s{1,10}|[‒–—―]")
_PCT_COL = re.compile(r"%|\brate\b|\bshare\b", re.I)
_CHG_COL = re.compile(r"\b(mom|yoy)\b|\bvs\b.*%|change", re.I)
# a comparison between two sources is not good or bad news: its arrows stay grey
_CHECK_COL = re.compile(r"difference|\bvs\b", re.I)
_RATE_COL = re.compile(r"conversion|\bcvr\b", re.I)
_HTTP_NOISE = re.compile(r"\b\d{3} [A-Z]+ https?://\S+:?\s*")
_MONEY_WORD = re.compile(r"\b(gmv|nmv|revenue|value|price|cost|idr|settled|fees?|margin|"
                         r"spend|commission|subsidy|voucher|paid out)\b", re.I)
_COUNT_WORD = re.compile(r"\b(orders?|units?|buyers?|views?|clicks?|sessions?|videos?|chats?|"
                         r"customers?|impressions?|qty|cohort|lines?|products?|items?|pieces?)\b",
                         re.I)
_BG_NAME, _ICON_NAME = "Poros bg", "Poros icon"
_SEP = "  |  "                      # between the parts of a kicker, a strip or a delta line
_LN_TAGS = ("a:lnL", "a:lnR", "a:lnT", "a:lnB")
_LN_BOTTOM = "a:lnB"
_SOLID_FILL, _SRGB, _SP_PR = "a:solidFill", "a:srgbClr", "c:spPr"
_ORDER_HISTORY = "order history"


# ---------------------------------------------------------------- formatting
def clean(text) -> str:
    """Visible text: no dash characters (house rule)."""
    return _DASHES.sub(", ", str(text if text is not None else "")).strip()


def _cut(text, n) -> str:
    text = clean(text)
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "..."


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def fmt_idr(v) -> str:
    """lib.js fmtM / fmtBn: IDR 439M, IDR 14.3M, IDR 1.00bn."""
    x = _num(v)
    if x is None:
        return "n/a"
    a, sign = abs(x), ("minus " if x < 0 else "")
    if a >= 1e9:
        return f"{sign}IDR {a / 1e9:.2f}bn"
    if a >= 1e6:
        m = a / 1e6
        return f"{sign}IDR {m:,.0f}M" if m >= 100 else f"{sign}IDR {m:,.1f}M"
    if a >= 1e3:
        return f"{sign}IDR {a / 1e3:,.1f}k"
    return f"{sign}IDR {a:,.0f}"


def fmt_n(v) -> str:
    x = _num(v)
    if x is None:
        return "n/a"
    return f"{x:,.0f}" if abs(x) >= 100 or x.is_integer() else f"{x:,.1f}"


def arrow(v, d=0) -> str:
    """lib.js arrow(): ▲27% / ▼31%."""
    x = _num(v)
    if x is None:
        return "n/a"
    if round(abs(x), d) == 0:
        return f"{0:.{d}f}%"
    return ("▲" if x >= 0 else "▼") + f"{abs(x):.{d}f}%"


def _month_long(model) -> str:
    y, m = str(model.get("period") or "0000-01").split("-")
    return f"{calendar.month_name[int(m)]} {y}"


def _brand_display(brand) -> str:
    b = str(brand or "")
    return b.title() if b.isupper() else b


# ---------------------------------------------------------------- low level drawing
def _run(p, text, size, color, bold=False, spacing=None):
    r = p.add_run()
    r.text = _DASHES.sub(", ", str(text if text is not None else ""))   # keep edge spaces
    r.font.size = Pt(max(size, MIN_PT))
    r.font.bold = bold
    r.font.name = FONT
    r.font.color.rgb = _rgb(color)
    if spacing:
        r.font._element.set("spc", str(int(spacing * 100)))
    return r


def _text(s, x, y, w, h, parts, size=14, color=None, bold=False, align=PP_ALIGN.LEFT,
          anchor=MSO_ANCHOR.TOP, spacing=None, gap=4):
    """``parts`` = str | list of paragraphs; a paragraph = str or list of runs, a run =
    (text, size, color, bold)."""
    color = color or C["ink"]
    tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    paras = [parts] if isinstance(parts, str) else list(parts)
    for i, para in enumerate(paras):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.space_after = Pt(gap)
        _para_runs(p, para, (size, color, bold), spacing)
    return tb


def _para_runs(p, para, style, spacing):
    """The runs of one paragraph; ``style`` = the paragraph's (size, color, bold), which a
    run tuple overrides from its second item on."""
    runs = [para] if isinstance(para, (str, tuple)) else para
    for r in runs:
        if isinstance(r, tuple):
            _run(p, *_run_style(r, style), spacing)
        else:
            _run(p, r, *style, spacing)


def _run_style(r, style) -> tuple:
    """(text, size, color, bold) of a run tuple, the paragraph's own where it stops short."""
    full = (r[0],) + tuple(style)
    return (tuple(r) + full[len(r):])[:4]


def _rrect(s, x, y, w, h, fill=None, line=None, radius=0.08, width=0.75):
    shp = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    shp.adjustments[0] = min(0.5, radius / max(min(w, h), 0.01))
    if fill:
        shp.fill.solid()
        shp.fill.fore_color.rgb = _rgb(fill)
    else:
        shp.fill.background()
    if line:
        shp.line.color.rgb = _rgb(line)
        shp.line.width = Pt(width)
    else:
        shp.line.fill.background()
    shp.shadow.inherit = False
    return shp


def _icon(s, name, color, x, y, size=0.3):
    path = os.path.join(ASSETS, f"ic_{name}_{color}.png")
    if not os.path.exists(path):
        return None
    pic = s.shapes.add_picture(path, Inches(x), Inches(y), Inches(size), Inches(size))
    pic.name = _ICON_NAME
    return pic


def _background(s, kind):
    pic = s.shapes.add_picture(os.path.join(ASSETS, f"bg_{kind}.png"), 0, 0,
                               Inches(13.333), Inches(7.5))
    pic.name = _BG_NAME
    tree = s.shapes._spTree
    tree.remove(pic._element)
    tree.insert(2, pic._element)


# ---------------------------------------------------------------- the deck
def _title_size(title) -> int:
    """26 pt headline, smaller as it grows longer."""
    if len(title) <= 110:
        return 26
    return 22 if len(title) <= 150 else 18


class _Deck:
    def __init__(self, model):
        self.model = model
        self.prs = Presentation()
        self.prs.slide_width = Emu(12192000)
        self.prs.slide_height = Emu(6858000)
        self.blank = self.prs.slide_layouts[6]
        self.pending = []
        self.header = (f"{str(model['brand']).upper()} × Acme  |  "
                       f"{_month_long(model).upper()} REVIEW")

    def raw(self, kind=None):
        s = self.prs.slides.add_slide(self.blank)
        s.background.fill.solid()
        s.background.fill.fore_color.rgb = _rgb(C["navy"] if kind else C["white"])
        if kind:
            _background(s, kind)
        s._dark = bool(kind)
        return s

    def slide(self, kicker, title, dark=False):
        """LIGHT / DARK master: kicker, header strip, title, footer."""
        s = self.raw("dark" if dark else None)
        _text(s, 0.45, 0.24, 7.6, 0.32, kicker.upper(), MIN_PT, C["cyan"] if dark else C["acme"],
              True, spacing=2)
        _text(s, 8.35, 0.24, 4.55, 0.32, self.header, MIN_PT, C["blueL"] if dark else C["muted"],
              True, align=PP_ALIGN.RIGHT, spacing=1)
        title = clean(title)
        if title.endswith(".") and not title.endswith(".."):
            title = title[:-1].rstrip()
        _text(s, 0.45, 0.6, 12.45, 0.95, _cut(title, 190), _title_size(title),
              C["white"] if dark else C["ink"], True)
        foot = C["blueL"] if dark else C["muted"]
        _text(s, 9.3, 7.08, 2.95, 0.3, "Strictly confidential", MIN_PT, foot, align=PP_ALIGN.RIGHT)
        _text(s, 12.3, 7.08, 0.6, 0.3, str(len(self.prs.slides)), MIN_PT, foot,
              align=PP_ALIGN.RIGHT)
        return s

    def notes(self, s, title, framing, message, evidence, caveat, trace):
        self.pending.append((s, {"title": clean(title), "framing": framing, "message": message,
                                 "evidence": evidence, "caveat": caveat, "trace": trace}))

    def finish(self):
        for i, (s, n) in enumerate(self.pending):
            nxt = self.pending[i + 1][1]["title"] if i + 1 < len(self.pending) else ""
            ev = "; ".join(clean(e) for e in (n["evidence"] or [])[:6]) or "none on this slide"
            s.notes_slide.notes_text_frame.text = "\n".join([
                f"Framing: {clean(n['framing'])}",
                f"Message: {clean(n['message'])}",
                f"Evidence: {ev}",
                f"Caveat: {clean(n['caveat']) or 'none'}",
                f"Trace: {clean(n['trace'])}",
                f"Transition: {('next, ' + _cut(nxt, 120)) if nxt else 'close and take questions'}",
            ])


# ---------------------------------------------------------------- lib.js building blocks
def _source(s, text):
    _text(s, 0.45, 6.99, 8.75, 0.44, _cut(text, 230), MIN_PT,
          C["blueL"] if s._dark else C["muted"])


def _takeaway(s, text, action=None, y=TAKEAWAY_Y, h=TAKEAWAY_H):
    dark = s._dark
    _rrect(s, 0.45, y, 12.45, h, C["navy2"] if dark else C["infoBg"],
           C["navy3"] if dark else "D6E4FD")
    _icon(s, "Lightbulb", "c" if dark else "b", 0.62, y + (h - 0.34) / 2, 0.34)
    _text(s, 1.08, y + 0.04, 7.75 if action else 11.6, h - 0.08, _cut(text, 240 if action else 330),
          14, C["white"] if dark else C["ink"], anchor=MSO_ANCHOR.MIDDLE)
    if action:
        _rrect(s, 9.0, y + 0.1, 3.78, h - 0.2, C["acme"], radius=0.06)
        _text(s, 9.1, y + 0.1, 3.6, h - 0.2, [[("▶ ", 14, C["white"], True),
                                                 (_cut(action, 60), 14, C["white"], True)]],
              anchor=MSO_ANCHOR.MIDDLE)


def _pill(s, x, y, w, word, rag):
    ink, bg = RAG.get(rag, RAG["INFO"])
    shp = _rrect(s, x, y, w, 0.32, C[bg], radius=0.16)
    tf = shp.text_frame
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    _run(p, word, MIN_PT, C[ink], True)


def _kpi(s, x, y, w, h, label, value, delta=None, delta_color=None, sub=None, value_color=None,
         value_size=26, pill=None):
    dark = s._dark
    _rrect(s, x, y, w, h, C["navy2"] if dark else C["white"], C["navy3"] if dark else C["line"],
           width=1)
    lw = w - 0.3 - (0.95 if pill else 0)
    _text(s, x + 0.15, y + 0.1, lw, 0.3, _cut(label, 40).upper(), MIN_PT,
          C["blueL"] if dark else C["muted"], True, spacing=1)
    if pill:
        _pill(s, x + w - 1.0, y + 0.1, 0.85, pill, pill)
    _text(s, x + 0.15, y + 0.38, w - 0.3, 0.55, value, value_size,
          value_color or (C["white"] if dark else C["ink"]), True, anchor=MSO_ANCHOR.MIDDLE)
    if delta:
        _text(s, x + 0.15, y + h - 0.4 - (0.3 if sub else 0), w - 0.3, 0.32, delta, MIN_PT,
              delta_color or C["muted"], True)
    if sub:
        _text(s, x + 0.15, y + h - 0.38, w - 0.3, 0.3, _cut(sub, 60), MIN_PT,
              C["blueL"] if dark else C["muted"])


def _panel(s, x, y, w, h, heading, facts, icon=None, fill=None, line=None, head_color=None):
    """A panel with an icon heading over its facts, each a (lead or None, text) pair.
    Returns how many facts it placed."""
    dark = s._dark
    _rrect(s, x, y, w, h, fill or (C["navy2"] if dark else C["bg"]),
           line or (C["navy3"] if dark else C["line"]))
    yy = y + 0.14
    if heading:
        _panel_heading(s, x, yy, w, heading, icon,
                       head_color or (C["white"] if dark else C["ink"]))
        yy += 0.44
    width = (w - 0.36) * PARA_FIT                   # 14 pt runs wider than the 13 pt metric
    paras = _panel_paras(facts, width, y + h - yy - 0.14, dark)
    if paras:
        _text(s, x + 0.18, yy, w - 0.36, y + h - yy - 0.1, paras, 14, gap=7)
    return len(paras)


def _panel_heading(s, x, y, w, heading, icon, color):
    if icon:
        _icon(s, icon, "c" if s._dark else "b", x + 0.16, y + 0.01, 0.3)
    _text(s, x + (0.52 if icon else 0.18), y, w - (0.7 if icon else 0.36), 0.34,
          _cut(heading, 60), 15, color, True, anchor=MSO_ANCHOR.MIDDLE)


def _panel_paras(facts, width, room, dark) -> list:
    """The facts that fit in ``room`` inches, as paragraphs of runs."""
    paras = []
    used = 0.0
    for lead, text in facts:
        body = _cut(text, 230)
        lines = _lines(f"{lead} {body}" if lead else body, width)
        need = lines * PARA_LINE_H
        if used + need > room:
            left = int((room - used) / PARA_LINE_H)
            if paras or left < 2:
                continue                            # a shorter one may still fit; all are in the notes
            body = _fit_text(body, width, left)     # the first item always shows, cut to fit
            need = left * PARA_LINE_H
        paras.append(_panel_runs(lead, body, dark))
        used += need + PARA_GAP
    return paras


def _panel_runs(lead, body, dark) -> list:
    runs = []
    if lead:
        runs.append((lead + " ", 14, C["cyan"] if dark else C["acme"], True))
    runs.append((body, 14, "E2E8F0" if dark else C["slate"], False))
    return runs


def _cell_border(cell, color):
    tc_pr = cell._tc.get_or_add_tcPr()
    for tag in _LN_TAGS:
        old = tc_pr.find(qn(tag))
        if old is not None:
            tc_pr.remove(old)
    for i, tag in enumerate(_LN_TAGS):
        ln = etree.SubElement(tc_pr, qn(tag))
        if tag == _LN_BOTTOM:
            ln.set("w", str(int(Pt(0.75))))
            fill = etree.SubElement(ln, qn(_SOLID_FILL))
            clr = etree.SubElement(fill, qn(_SRGB))
            clr.set("val", color)
        else:
            ln.set("w", "0")
            etree.SubElement(ln, qn("a:noFill"))
        tc_pr.remove(ln)
        tc_pr.insert(i, ln)


def _money_col(t, j) -> bool:
    col = str(t["columns"][j])
    if _PCT_COL.search(col) or _CHG_COL.search(col):
        return False
    vals = [abs(_num(r[j])) for r in t["rows"] if j < len(r) and _num(r[j]) is not None]
    if not vals or max(vals) < 1e6:
        return False
    if _MONEY_WORD.search(col):
        return True
    return not _COUNT_WORD.search(col)


def _cell(v, col, money) -> tuple:
    """(text, colour) for a table cell in the reference style."""
    x = _num(v)
    if v is None:
        return "n/a", C["muted"]
    if x is None:
        return _cut(v, 110), None
    if _CHG_COL.search(col) or (_CHECK_COL.search(col) and _PCT_COL.search(col)):
        if round(abs(x)) == 0 or _CHECK_COL.search(col):
            return arrow(x), C["muted"]
        return arrow(x), C["good"] if x >= 0 else C["bad"]
    if _PCT_COL.search(col):
        # conversion rates live under 1%: one decimal would print most of them as 0.0%
        return (f"{x:,.2f}%" if _RATE_COL.search(col) else f"{x:,.1f}%"), None
    if money:
        return f"{x / 1e6:,.1f}", None
    return fmt_n(x), None


def _deck_view(t, max_cols):
    """The columns a slide shows (``deck_columns`` drops identifiers such as SKU ids,
    which stay in the workbook), as (indexes, names)."""
    idx = [j for j in (t.get("deck_columns") or range(len(t["columns"])))
           if j < len(t["columns"])][:max_cols]
    return idx, [t["columns"][j] for j in idx]


def _lines(text, width_in) -> int:
    """Wrapped lines a 13 pt cell needs at this width (word wrap, conservative)."""
    per_line = max(int((width_in - 0.1) * CHARS_PER_INCH), 4)
    lines, used = 1, 0
    for word in str(text).split():
        n = len(word)
        while n > per_line:                       # a word longer than the cell breaks
            lines += 1 if used else 0
            n -= per_line
            used = 0
        if used and used + 1 + n > per_line:
            lines += 1
            used = n
        else:
            used += (1 if used else 0) + n
    return lines


def _fit_text(text, width_in, max_lines=2) -> str:
    """Cut a cell to the lines it may take, on a word boundary."""
    text = str(text)
    if _lines(text, width_in) <= max_lines:
        return text
    words = text.split()
    while len(words) > 1 and _lines(" ".join(words) + "...", width_in) > max_lines:
        words.pop()
    cut = " ".join(words)
    per_line = max(int((width_in - 0.1) * CHARS_PER_INCH), 4)
    if _lines(cut + "...", width_in) > max_lines:
        cut = cut[: per_line * max_lines - 3]
    return cut.rstrip(" ,") + "..."


def _table(s, t, x, y, w, max_rows=MAX_ROWS, max_cols=10, h=None, max_lines=2):
    """A navy header table that FITS: rows are added only while their wrapped height stays
    inside ``h`` (default: down to the takeaway band) and text cells wrap to two lines at
    most. Returns True when rows or shown columns were left for the workbook."""
    h = (TAKEAWAY_Y - 0.1 - y) if h is None else h
    idx, cols = _deck_view(t, max_cols)
    view = {"columns": cols, "rows": [[r[j] if j < len(r) else None for j in idx]
                                      for r in t["rows"]]}
    money = [_money_col(view, j) for j in range(len(cols))]
    heads = [f"{c} (IDR M)" if money[j] else c for j, c in enumerate(cols)]
    texts = [[_cell(v, cols[j], money[j]) for j, v in enumerate(r)]
             for r in view["rows"][:max_rows]]
    col_in = _col_inches(heads, texts, w)
    head_cells = [_fit_text(_cut(c, 30), col_in[j]) for j, c in enumerate(heads)]
    head_h = _row_height(head_cells, col_in, max_lines)
    fit, used = _fit_rows(texts, col_in, h, head_h, max_lines)
    n_rows = len(fit) + 1
    gf = s.shapes.add_table(n_rows, len(cols), Inches(x), Inches(y), Inches(w), Inches(used))
    tbl = gf.table
    _size_table(tbl, col_in, head_h, fit)
    numeric = [j > 0 and any(_num(r[j]) is not None for r in view["rows"])
               for j in range(len(cols))]
    _head_row(tbl, head_cells, numeric)
    _body_rows(tbl, fit, numeric, s._dark)
    _cell_margins(tbl, n_rows, len(cols))
    shown = len(t.get("deck_columns") or t["columns"])
    return len(fit) < len(t["rows"]) or len(idx) < shown


def _col_inches(heads, texts, w) -> list:
    """Column widths in inches, shared out by the longest text of each column."""
    widths = []
    for j, c in enumerate(heads):
        longest = max([len(r[j][0]) for r in texts] + [len(str(c)) * 0.75, 6])
        widths.append(min(longest, 34) + 2)
    return [w * wd / sum(widths) for wd in widths]


def _row_height(cells, col_in, max_lines) -> float:
    n = max((min(_lines(text, col_in[j]), max_lines) for j, text in enumerate(cells)),
            default=1)
    return n * LINE_H + CELL_PAD


def _fit_rows(texts, col_in, h, head_h, max_lines) -> tuple:
    """The rows that fit under the header within ``h`` inches (the first always shows), as
    (cells, colours, height), and the height of the table with them."""
    fit, used = [], head_h
    for r in texts:
        cells = [_fit_text(text, col_in[j], max_lines) for j, (text, _c) in enumerate(r)]
        rh = _row_height(cells, col_in, max_lines)
        if used + rh > h and fit:
            break
        fit.append((cells, [c for _t, c in r], rh))
        used += rh
    return fit, used


def _size_table(tbl, col_in, head_h, fit):
    sid = tbl._tbl.tblPr.find(qn("a:tableStyleId"))
    if sid is not None:
        sid.text = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"      # no style, no grid
    for j, width in enumerate(col_in):
        tbl.columns[j].width = Inches(width)
    tbl.rows[0].height = Inches(head_h)
    for i, (_cells, _colours, rh) in enumerate(fit, 1):
        tbl.rows[i].height = Inches(rh)


def _head_row(tbl, head_cells, numeric):
    for j, text in enumerate(head_cells):
        cell = tbl.cell(0, j)
        cell.fill.solid()
        cell.fill.fore_color.rgb = _rgb(C["navy"])
        p = cell.text_frame.paragraphs[0]
        p.alignment = PP_ALIGN.RIGHT if numeric[j] else PP_ALIGN.LEFT
        _run(p, text, MIN_PT, C["white"], True)


def _body_rows(tbl, fit, numeric, dark):
    fill = C["navy2"] if dark else C["white"]
    ink = "E2E8F0" if dark else C["ink"]
    border = C["navy3"] if dark else C["line"]
    for i, (cells, colours, _rh) in enumerate(fit, 1):
        for j, is_number in enumerate(numeric):
            cell = tbl.cell(i, j)
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(fill)
            p = cell.text_frame.paragraphs[0]
            p.alignment = PP_ALIGN.RIGHT if is_number else PP_ALIGN.LEFT
            _run(p, cells[j], MIN_PT, colours[j] or ink, j == 0)
            _cell_border(cell, border)


def _cell_margins(tbl, n_rows, n_cols):
    for i in range(n_rows):
        for j in range(n_cols):
            tf = tbl.cell(i, j).text_frame
            tf.word_wrap = True
            tf.margin_top = tf.margin_bottom = Inches(0.03)
            tf.margin_left = tf.margin_right = Inches(0.05)
            tbl.cell(i, j).vertical_anchor = MSO_ANCHOR.MIDDLE


# ---------------------------------------------------------------- charts
def _short_cat(v) -> str:
    text = str(v)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text[:10])
    if m:
        return f"{m.group(3)} {calendar.month_abbr[int(m.group(2))]}"
    m = re.fullmatch(r"([A-Z][a-z]{2}) (\d{4})", text)
    if m:
        return f"{m.group(1)} {m.group(2)[2:]}"
    m = re.fullmatch(r"(\d{4})(\d{2})", text)
    if m:
        return f"{calendar.month_abbr[int(m.group(2))]} {m.group(1)[2:]}"
    return _cut(text, 22)


def _cat_mid(v):
    text = str(v)
    m = re.fullmatch(r"(\d{4})-(\d{2})-\d{2}", text[:10])
    if m:
        return int(m.group(1)) * 100 + int(m.group(2)), text[:10]
    m = re.fullmatch(r"([A-Z][a-z]{2}) (\d{4})", text)
    if m:
        return int(m.group(2)) * 100 + list(calendar.month_abbr).index(m.group(1)), None
    return None, None


def _tick_skip(ax, n):
    if n > 14:
        skip = etree.SubElement(ax._element, qn("c:tickLblSkip"))
        skip.set("val", str(max(2, n // 10)))


def _combo_line(chart, series_index):
    """Move one bar series into a line plot on the same axes (reference: GMV bars, NMV
    line). python-pptx has no combo API; the series XML is moved under c:lineChart."""
    plot_area = chart._chartSpace.find(qn("c:chart")).find(qn("c:plotArea"))
    bar = plot_area.find(qn("c:barChart"))
    sers = bar.findall(qn("c:ser"))
    ser = sers[series_index]
    bar.remove(ser)
    for tag in ("c:invertIfNegative", "c:dLbls"):
        el = ser.find(qn(tag))
        if el is not None:
            ser.remove(el)
    line = etree.Element(qn("c:lineChart"))
    etree.SubElement(line, qn("c:grouping")).set("val", "standard")
    etree.SubElement(line, qn("c:varyColors")).set("val", "0")
    line.append(ser)
    marker = etree.SubElement(line, qn("c:marker"))
    marker.set("val", "1")
    for ax in bar.findall(qn("c:axId")):
        line.append(deepcopy(ax))
    bar.addnext(line)
    return ser


def _style_line_ser(ser, color):
    sp_pr = ser.find(qn(_SP_PR))
    if sp_pr is None:
        sp_pr = etree.SubElement(ser, qn(_SP_PR))
        ser.remove(sp_pr)
        ser.insert(3, sp_pr)
    for child in list(sp_pr):
        sp_pr.remove(child)
    ln = etree.SubElement(sp_pr, qn("a:ln"))
    ln.set("w", str(int(Pt(2.25))))
    fill = etree.SubElement(ln, qn(_SOLID_FILL))
    etree.SubElement(fill, qn(_SRGB)).set("val", color)
    marker = etree.Element(qn("c:marker"))
    etree.SubElement(marker, qn("c:symbol")).set("val", "circle")
    etree.SubElement(marker, qn("c:size")).set("val", "5")
    mp = etree.SubElement(marker, qn(_SP_PR))
    mf = etree.SubElement(mp, qn(_SOLID_FILL))
    etree.SubElement(mf, qn(_SRGB)).set("val", color)
    sp_pr.addnext(marker)
    smooth = etree.SubElement(ser, qn("c:smooth"))
    smooth.set("val", "0")


_CHART_CAP = {"line": 24, "column": 31, "bar": 12}


def _chart(s, t, x, y, w, h, takeover_mid=None, prev_label=None):
    spec = t["chart"]
    kind = spec.get("type", "column")
    dark = s._dark
    cols = t["columns"]
    x_col = spec.get("x", 0)
    rows = _chart_rows(t, kind)
    ys = _chart_ys(spec, cols, kind, prev_label)
    money = any(_money_col(t, j) for j in ys)
    platforms = _chart_platforms(rows, x_col, dark)
    colours, painted = _series_colours(cols, ys, kind, platforms, dark)
    data = _chart_data(rows, x_col, cols, ys, painted, 1e6 if money else 1)
    ctype = XL_CHART_TYPE.BAR_CLUSTERED if kind == "bar" else XL_CHART_TYPE.COLUMN_CLUSTERED
    gf = s.shapes.add_chart(ctype, Inches(x), Inches(y), Inches(w), Inches(h), data)
    ch = gf.chart
    _chart_frame(ch, _cut(spec.get("title") or t["title"], 80) + (", IDR M" if money else ""),
                 len(ys) > 1, dark)
    _chart_axes(ch, rows, ys, money or kind != "line", dark)
    plot = ch.plots[0]
    plot.gap_width = 60
    if len(ys) > 1:
        plot.overlap = -10
    if len(rows) <= 16:
        _data_labels(plot, [v for sr in data._series for v in sr.values], dark)
    series = list(plot.series)
    if len(ys) == 1:
        _paint_single(series[0], [r[x_col] for r in rows], platforms, takeover_mid)
    else:
        _paint_series(series, colours, painted, platforms, dark)
        if kind == "line":                          # reference: first measure bars, second line
            ser = _combo_line(ch, 1)
            _style_line_ser(ser, C["target"])
    return len(rows) < len(t["rows"])


def _chart_rows(t, kind) -> list:
    rows = [r for r in t["rows"] if r]
    cap = _CHART_CAP.get(kind, 24)
    rows = rows[-cap:] if kind == "line" else rows[:cap]
    if kind == "bar":
        rows = list(reversed(rows))
    return rows


def _chart_ys(spec, cols, kind, prev_label) -> list:
    ys = [j for j in (spec.get("y") or [1]) if j < len(cols)]
    if kind == "bar" and len(ys) == 2 and prev_label and cols[ys[1]] == prev_label:
        ys = [ys[1], ys[0]]                        # previous period first: grey, then blue
    return ys


def _chart_platforms(rows, x_col, dark):
    """Each category's platform colour, or None when no category names a platform."""
    platforms = [_platform_colour(r[x_col], dark) for r in rows]
    return platforms if any(platforms) else None


def _series_colours(cols, ys, kind, platforms, dark) -> tuple:
    """Each series' colour, and whether its bars wear their platform's colour instead."""
    palette = [C["pre"], C["acme"]] if kind == "bar" else \
        [C["acme"], C["target"], C["expected"], C["watch"]]
    colours, painted = [], []
    for i, j in enumerate(ys):
        own = _platform_colour(cols[j], dark)
        colour = _series_colour(cols[j], own, palette[i % len(palette)], len(ys))
        colours.append(colour)
        painted.append(bool(platforms) and colour != C["pre"] and not own and kind != "line")
    return colours, painted


def _series_colour(col, own, palette_colour, n_series):
    """Grey for a 'before' measure, else the platform's own colour, else the palette."""
    if str(col).lower().startswith("before"):
        return C["pre"]
    return own or (palette_colour if n_series > 1 else C["acme"])


def _chart_data(rows, x_col, cols, ys, painted, scale):
    data = CategoryChartData()
    data.categories = [_short_cat(r[x_col]) for r in rows]
    for j, paint in zip(ys, painted):
        name = clean(cols[j]) + (", platform colours" if paint and len(ys) > 1 else "")
        data.add_series(name, [(_num(r[j]) or 0) / scale for r in rows])
    return data


def _chart_frame(ch, title, legend, dark):
    """Chart font, title and (with more than one series) the legend on top."""
    ch.font.size = Pt(MIN_PT)
    ch.font.name = FONT
    ch.font.color.rgb = _rgb("CBD5E1" if dark else C["slate"])
    ch.has_title = True
    ch.chart_title.text_frame.text = ""
    _run(ch.chart_title.text_frame.paragraphs[0], title, 14, C["white"] if dark else C["ink"])
    ch.has_legend = legend
    if ch.has_legend:
        ch.legend.position = XL_LEGEND_POSITION.TOP
        ch.legend.include_in_layout = False
        ch.legend.font.size = Pt(MIN_PT)


def _chart_axes(ch, rows, ys, whole, dark):
    va, ca = ch.value_axis, ch.category_axis
    va.has_major_gridlines = True
    va.major_gridlines.format.line.color.rgb = _rgb("24386B" if dark else "E5E9F0")
    va.major_gridlines.format.line.width = Pt(0.75)
    va.format.line.fill.background()
    va.tick_labels.font.size = Pt(MIN_PT)
    va.tick_labels.number_format = "#,##0" if whole else "General"
    va.tick_labels.number_format_is_linked = False
    ca.tick_labels.font.size = Pt(MIN_PT)
    ca.format.line.color.rgb = _rgb("3B4F80" if dark else C["line2"])
    ca.has_major_gridlines = False
    _tick_skip(ca, len(rows))
    if any((_num(r[j]) or 0) < 0 for r in rows for j in ys):
        ca.tick_label_position = XL_TICK_LABEL_POSITION.LOW   # labels below negative bars


def _label_format(values) -> str:
    """Whole numbers from 100 up or when every value is whole, two decimals when a value
    sits under 0.1, else one."""
    top = max((abs(v) for v in values), default=0)
    whole = all(float(v).is_integer() for v in values)
    if top >= 100 or whole:
        return "#,##0"
    return "#,##0.00" if any(0 < abs(v) < 0.1 for v in values) else "#,##0.0"


def _data_labels(plot, values, dark):
    plot.has_data_labels = True
    dl = plot.data_labels
    dl.font.size = Pt(MIN_PT)
    dl.font.color.rgb = _rgb(C["white"] if dark else C["ink"])
    dl.number_format = _label_format(values)
    dl.number_format_is_linked = False
    dl.position = XL_LABEL_POSITION.OUTSIDE_END


def _fill_point(sr, i, colour):
    pt = sr.points[i]
    pt.format.fill.solid()
    pt.format.fill.fore_color.rgb = _rgb(colour)


def _paint_single(sr, cats, platforms, takeover_mid):
    """One series: blue, platform colours where a category names one, grey before Acme."""
    sr.format.fill.solid()
    sr.format.fill.fore_color.rgb = _rgb(C["acme"])
    for i, pc in enumerate(platforms or []):
        if pc:
            _fill_point(sr, i, pc)
    if takeover_mid:                                # grey before Acme, blue with Acme
        for i, cat in enumerate(cats):
            mid, _ = _cat_mid(cat)
            if mid and mid < takeover_mid:
                _fill_point(sr, i, C["pre"])
    sr.invert_if_negative = False                   # a negative bar keeps its colour


def _paint_series(series, colours, painted, platforms, dark):
    for i, sr in enumerate(series):
        sr.format.fill.solid()
        sr.format.fill.fore_color.rgb = _rgb(_series_fill(colours[i], painted[i], dark))
        if painted[i]:
            for k, pc in enumerate(platforms):   # this period's bars wear their platform
                if pc:
                    _fill_point(sr, k, pc)


def _series_fill(colour, painted, dark):
    """A series painted point by point in platform colours keeps a neutral fill."""
    if not painted:
        return colour
    return "E5E7EB" if dark else C["ink"]


# ---------------------------------------------------------------- data helpers
def _score(model) -> dict:
    for sec in model.get("sections") or []:
        if sec["id"] == "scorecard" and sec.get("tables"):
            return {str(r[0]).split(" (")[0].lower(): r for r in sec["tables"][0]["rows"]}
    return {}


def _sec(model, sid):
    return next((x for x in model.get("sections") or [] if x["id"] == sid), None)


def _band(value, green, amber) -> str:
    """GREEN at ``green`` or more, AMBER from ``amber``, RED below."""
    if value >= green:
        return "GREEN"
    return "AMBER" if value >= amber else "RED"


def _rag_change(mom, lower_better=False, pp=False):
    if mom is None:
        return "INFO"
    v = -mom if lower_better else mom
    green, red = (1, -1) if pp else (2, -5)
    if v >= green:
        return "GREEN"
    return "RED" if v <= red else "AMBER"


def _delta_part(x, word, pp) -> str:
    if pp:
        return ("▲" if x >= 0 else "▼") + f"{abs(x):.1f}pp {word}"
    return arrow(x) + f" {word}"


def _delta_line(mom, yoy, pp=False):
    parts = []
    for v, word in ((mom, "MoM"), (yoy, "YoY")):
        x = _num(v)
        if x is not None:
            parts.append(_delta_part(x, word, pp))
    return _SEP.join(parts)


def _target_tile(model, nmv) -> list:
    """NMV against the review month's target, when one was typed or configured: GREEN at
    100% or more, AMBER from 90%, RED below."""
    target = _num(((model.get("inputs") or {}).get("target") or {}).get("nmv"))
    if not target or nmv is None:
        return []
    reached = nmv / target * 100
    return [("NMV vs target", share_text(reached), f"of the {fmt_idr(target)} target",
             _band(reached, 100, 90), "")]


_MEASURES = (("gmv", "GMV", fmt_idr), ("nmv", "NMV", fmt_idr), ("orders", "Orders", fmt_n),
             ("buyers", "Buyers", fmt_n), ("units", "Units", fmt_n), ("aov", "AOV", fmt_idr))


def signals(model) -> list:
    """Scorecard tiles: (label, value, delta, rag, sub). RAG is a presentation rule: a
    measure where higher is better is GREEN at +2% MoM or more, RED at -5% or worse,
    AMBER between; for cancellation the same in percentage points, reversed."""
    sc = _score(model)
    out = _measure_tiles(model, sc) + _cancel_tile(sc) + _ready_tile(model) + \
        _cod_tile(model) + _fee_tile(model)
    return out[:12]


def _unit_of(name) -> str:
    """'GMV (IDR)' -> 'IDR': the unit a scorecard row names in brackets."""
    return name.split("(")[-1].rstrip(")") if "(" in name else ""


def _measure_tiles(model, sc) -> list:
    out = []
    for key, label, fmt in _MEASURES:
        r = sc.get(key)
        if not r:
            continue
        mom, yoy = _num(r[3]), _num(r[5])
        out.append((label, fmt(r[1]), _delta_line(mom, yoy), _rag_change(mom), _unit_of(r[0])))
        if key == "nmv":
            out.extend(_target_tile(model, _num(r[1])))
    return out


def _cancel_tile(sc) -> list:
    canc = sc.get("cancellation rate %")
    if not canc:
        return []
    cur, prev, ly = _num(canc[1]), _num(canc[2]), _num(canc[4])
    d = None if cur is None or prev is None else cur - prev
    dy = None if cur is None or ly is None else cur - ly
    return [("Cancellation", f"{cur:.1f}%" if cur is not None else "n/a",
             _delta_line(d, dy, pp=True), _rag_change(d, lower_better=True, pp=True),
             "share of all orders")]


def _ready_tile(model) -> list:
    ful = _sec(model, "fulfilment")
    if not ful or not ful.get("tables"):
        return []
    rows = ful["tables"][0]["rows"]
    total = sum(_num(r[1]) or 0 for r in rows)
    fast = sum(_num(r[1]) or 0 for r in rows[:2])
    if not total:
        return []
    share = fast / total * 100
    return [("Ready to ship in 1 day", share_text(share), "of orders", _band(share, 95, 80), "")]


def _cod_tile(model) -> list:
    pay = _sec(model, "payment")
    if not pay or not pay.get("tables"):
        return []
    cod = [r for r in pay["tables"][0]["rows"] if L.is_cod(r[0])]
    if not cod:
        return []
    return [("Cash on delivery", f"{sum(_num(r[3]) or 0 for r in cod):.1f}%",
             f"of NMV, {_num(cod[0][4]) or 0:.1f}% cancelled", "INFO", "")]


def _fee_tile(model) -> list:
    eco = _sec(model, "mp_economics")
    if not eco or not eco.get("headline"):
        return []
    m = re.search(r"took ([\d.]+)%", eco["headline"])
    if not m:
        return []
    return [("Fees", f"{m.group(1)}%", "of settled GMV, marketplace and affiliate", "INFO", "")]


# ---------------------------------------------------------------- slides
def _cover(deck):
    m = deck.model
    s = deck.raw("cover")
    sc = _score(m)
    _text(s, 0.7, 1.2, 11.5, 0.35, f"MONTHLY BUSINESS REVIEW  |  {_month_long(m).upper()}",
          14, C["cyan"], True, spacing=3)
    _text(s, 0.7, 1.6, 8.6, 0.95, f"{_brand_display(m['brand'])} Official Store × Acme", 44,
          C["white"], True)
    score = _sec(m, "scorecard") or {}
    story = ((_sec(m, "synthesis") or {}).get("synthesis") or {}).get("story")
    lead = story or score.get("computed_headline") or score.get("headline") or \
        f"{m['period_label']} review and the month ahead"
    _text(s, 0.7, 2.62, 7.6, 1.0, _cut(lead, 170), 20, "DCE6F7")
    cards = _cover_cards(m, sc)
    for i, (value, cap) in enumerate(cards[:3]):
        x = 0.7 + i * 2.65
        _rrect(s, x, 3.95, 2.45, 1.35, "132A5E", "3B5BA5")
        _text(s, x + 0.15, 4.1, 2.2, 0.6, value, 26, [C["white"], C["cyan"], C["violetL"]][i], True)
        _text(s, x + 0.15, 4.7, 2.2, 0.58, _cut(cap, 60), MIN_PT, "CBD5E1")
    _text(s, 0.7, 6.35, 11.5, 0.35, _cover_status(m), MIN_PT, "A9B8D6")
    _text(s, 0.7, 6.72, 11.5, 0.35, "Strictly confidential. Not to be distributed to any third "
          "party other than intended purposes.", MIN_PT, "8EA0C4")
    deck.notes(s, "Cover", "Open the monthly review for the brand and Acme.", lead, [f"{v} {c}" for v, c in cards],
               "Figures are read from the live warehouse at the time stated on the cover.",
               "workbook sheet Storyline")


def _cover_cards(m, sc) -> list:
    """(value, caption) of the cover cards: the month's GMV, NMV a day, then the target
    reached, next month's target or the orders."""
    cards = []
    if sc.get("gmv"):
        cards.append((fmt_idr(sc["gmv"][1]), f"{m['period_label']} GMV (NMV "
                      f"{fmt_idr(sc['nmv'][1]) if sc.get('nmv') else 'n/a'})"))
    daily = _sec(m, "daily")
    if daily and daily.get("tables") and daily["tables"][0]["rows"]:
        rows = daily["tables"][0]["rows"]
        avg = sum(_num(r[2]) or 0 for r in rows) / len(rows)
        cards.append((fmt_idr(avg), f"NMV a day over {len(rows)} days"))
    return cards + _cover_target_card(m, sc)


def _cover_target_card(m, sc) -> list:
    target = (m.get("inputs") or {}).get("next_month_target")
    reached = _target_tile(m, _num(sc["nmv"][1]) if sc.get("nmv") else None)
    if reached:
        return [(reached[0][1], f"of the {fmt_idr((m.get('inputs') or {})['target']['nmv'])} "
                 f"{m['period_label']} NMV target")]
    if target and target.get("nmv"):
        return [(fmt_idr(target["nmv"]), "next month NMV target")]
    if sc.get("orders"):
        return [(fmt_n(sc["orders"][1]), f"processed orders, {arrow(sc['orders'][3])} MoM")]
    return []


def _cover_status(m) -> str:
    """The cover's foot line: marketplaces, when the data was pulled, delivery status."""
    mps = []
    mk = _sec(m, "marketplace")
    if mk and mk.get("tables"):
        mps = [str(r[0]) for r in mk["tables"][0]["rows"] if _num(r[1])]
    latest = L.day_label(m.get("data_as_of")) if m.get("data_as_of") else "unknown"
    return (f"{' and '.join(mps) or 'All marketplaces'}  |  Data pulled "
            f"{L.stamp_label(m.get('data_pulled_at') or m.get('generated_at'))}, latest order "
            f"{latest}  |  {STATUS_TEXT.get(delivery_status(m), delivery_status(m)).capitalize()}")


def _pages_of(model, item) -> str:
    """'A2, C1': the pages an AI claim rests on, by their codes."""
    refs = [(_sec(model, i) or {}).get("ref") for i in item.get("pages") or []]
    return ", ".join(r for r in refs if r)


def _exec_summary(deck, sections, sheets):
    m = deck.model
    score = _sec(m, "scorecard") or {}
    syn = (_sec(m, "synthesis") or {}).get("synthesis") or {}
    title = syn.get("story") or score.get("headline") or \
        f"{m['brand']} {m['period_label']} in short"
    s = deck.slide("Executive summary", title, dark=True)
    sig = signals(m)
    for i, (label, value, delta, rag, _sub) in enumerate(sig[:3]):
        col = {"RED": C["redL"], "GREEN": C["greenL"]}.get(rag, C["blueL"])
        _kpi(s, 0.45, 1.72 + i * 1.42, 3.75, 1.3, f"{m['period_label']} {label}", value,
             delta, col)
    if syn.get("summary"):           # the synthesis: what the month means, page by page
        points = [("Pages " + (_pages_of(m, it) or "of this review"), it["text"], "Sparkles")
                  for it in syn["summary"][:4]]
    else:
        points = [(f"{x['ref']}  {x['title']}", x["headline"],
                   SECTION_ICON.get(x["id"], "ChartColumn"))
                  for x in sections if x.get("headline") and x["id"] != "scorecard"][:5]
    gap = 0.06
    box = min(1.04, (TAKEAWAY_Y - 0.1 - BODY_Y - gap * (len(points) - 1)) / max(len(points), 1))
    lines = 3 if box >= 1.0 else 2
    y = 1.72
    for lead, text, icon in points:
        _rrect(s, 4.45, y, 8.45, box, C["navy2"], C["navy3"])
        _icon(s, icon, "c", 4.6, y + (box - 0.34) / 2, 0.34)
        _text(s, 5.1, y + 0.05, 7.65, 0.28, lead.upper(), MIN_PT, C["cyan"], True, spacing=1)
        _text(s, 5.1, y + 0.3, 7.65, box - 0.32, _fit_text(clean(text), 7.2, lines), 14,
              C["white"])
        y += box + gap
    stats = m.get("stats") or {}
    _takeaway(s, ("Tension: " + syn["tension"]) if syn.get("tension") else
              f"{stats.get('complete', 0)} of {stats.get('sections', 0)} pages complete, "
              f"{stats.get('partial', 0)} partial and {stats.get('placeholder', 0)} waiting "
              f"for data; the review is {STATUS_TEXT.get(delivery_status(m), 'delivered')}.",
              "See the decisions")
    _source(s, "Source: orders, products, settlements and customer service in the Acme "
               "warehouse; every figure is traced in EVIDENCE-AND-REASONING.md.")
    deck.notes(s, "Executive summary", "Give the room the answer before the detail.", title,
               [p[1] for p in points],
               ("The story and its points were written by AI from the pages of this review; "
                "every number in them was checked against those pages."
                if syn else "Headlines follow fixed rules from the data; no written "
                "synthesis in this run."),
               f"workbook sheet {sheets.get('scorecard', 'Storyline')} and Storyline")


def _scorecard(deck, sec, sheet):
    m = deck.model
    sig = signals(m)
    reds = [x for x in sig if x[3] == "RED"]
    greens = [x for x in sig if x[3] == "GREEN"]
    title = (f"{len(reds)} red and {len(greens)} green signals in {m['period_label']}"
             + (f"; red: {_listed([x[0] for x in reds])}" if reds else ""))
    s = deck.slide(f"Performance scorecard  |  {_month_long(m)}", title)
    for i, (label, value, delta, rag, _sub) in enumerate(sig):
        r, c = divmod(i, 4)
        ink = {"RED": C["bad"], "GREEN": C["good"], "AMBER": C["watch"]}.get(rag, C["ink"])
        x, y = 0.45 + c * 3.16, 1.72 + r * 1.45
        _kpi(s, x, y, 2.98, 1.33, label, value, None, None, None, ink, 24, pill=rag)
        if delta:
            _text(s, x + 0.15, y + 0.95, 2.7, 0.3, [_delta_runs(label, delta)], MIN_PT)
    _takeaway(s, (f"Red: {', '.join(x[0] for x in reds)}. " if reds else "No red signal. ")
              + "Colours follow a fixed rule (see notes), not a judgement.",
              f"Workbook: {sheet}")
    _source(s, f"Source: {L.source_line(sec.get('queries') or [])}; MoM against "
               f"{(sec.get('tables') or [{}])[0].get('columns', ['', '', '?'])[2]}, YoY against "
               "the same month last year.")
    deck.notes(s, title, "Read the month on one page.", title,
               [f"{x[0]} {x[1]} ({x[2]})" for x in sig],
               "RAG rule: GREEN at +2% MoM or better, RED at -5% or worse, AMBER between; "
               "cancellation in percentage points, lower is better; INFO tiles have no rule.",
               f"workbook sheet {sheet}")


def _listed(names) -> str:
    """'A, B and C'."""
    if len(names) > 1:
        return ", ".join(names[:-1]) + f" and {names[-1]}"
    return "".join(names)


def _delta_runs(label, delta) -> list:
    """A tile's delta line with each change coloured: up is good, except for cancellation."""
    up, down = (C["bad"], C["good"]) if label == "Cancellation" else (C["good"], C["bad"])
    runs = []
    for part in delta.split(_SEP):
        if runs:
            runs.append((_SEP, MIN_PT, C["muted"], False))
        runs.append((part, MIN_PT, _change_colour(part, up, down), True))
    return runs


def _change_colour(part, up, down):
    if part.startswith("▲"):
        return up
    return down if part.startswith("▼") else C["muted"]


def _kicker(sec, names):
    g = sec["group"]
    if g in ("Summary", "Appendix"):
        return f"{sec['ref']}  |  {sec['title']}"
    group = names.get(g, g)
    if sec["title"].lower().startswith(str(group).lower()):   # "Synthesis: Synthesis, asks"
        return f"{g}  |  {sec['title']}"
    return f"{g}  |  {group}: {sec['title']}"


def _ai(f) -> bool:
    return f.get("by") == "ai"


def _lead_facts(sec):
    """What the page means (the AI insights) first, then the figures it rests on."""
    out = []
    for f in sorted(sec.get("facts") or [], key=lambda f: not _ai(f)):
        lead = None if f["label"] == "FACT" else f["label"].capitalize() + ":"
        out.append((lead, f["text"]))
    return out


def _cap(text) -> str:
    text = str(text or "")
    return text[:1].upper() + text[1:]


def _uncap(text) -> str:
    """'Product mix' -> 'product mix', but 'NMV bridge' and 'LIVE and video' keep case."""
    text = str(text or "")
    return text if text[1:2].isupper() else text[:1].lower() + text[1:]


def _takeaway_text(sec):
    if sec.get("takeaway"):
        return sec["takeaway"]
    interp = next((f["text"] for f in sec.get("facts") or [] if f["label"] != "FACT"), None)
    if interp:
        return interp
    if sec.get("missing"):
        mm = sec["missing"][0]
        return (f"Not in this review yet: {_cap(mm['what'])}. What is needed: "
                f"{_HTTP_NOISE.sub('', mm['needs'])}.")
    facts = sec.get("facts") or []
    return facts[-1]["text"] if facts else sec["title"]


_GHOST_QUADS = ("Star", "Hidden gem", "Rough diamond", "Underperformer")


_GhostPalette = namedtuple("_GhostPalette", "ink bar edge cur")   # text, bars, outlines, this month


def _ghost(s, kind, x, y, w, h):
    """The visual the page will hold, drawn as a grey outline with XX values: the reader
    sees what is coming instead of an empty box. Shapes only, never a figure."""
    dark = s._dark
    bar, edge = (C["navy3"], "3B5BA5") if dark else ("E2E8F0", C["line2"])
    pal = _GhostPalette(C["blueL"] if dark else C["muted"], bar, edge,
                        "1E3A8A" if dark else "DBEAFE")
    if kind in ("column", "waterfall"):
        _ghost_bars(s, kind, (x, y, w, h), pal)
    elif kind == "line":
        _ghost_line(s, (x, y, w, h), pal)
    elif kind == "funnel":
        _ghost_funnel(s, (x, y, w, h), pal)
    elif kind == "matrix":
        _ghost_matrix(s, (x, y, w, h), pal)
    else:                                             # table
        _ghost_table(s, (x, y, w, h), pal)


def _ghost_xx(s, cx, cy, cw, ink):
    """An XX value centred on ``cx``."""
    _text(s, cx - cw / 2, cy, cw, 0.3, "XX", MIN_PT, ink, True,
          align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)


def _ghost_step(kind, i, n, base, floor, bh) -> tuple:
    """(top, floor) of one ghost bar: a waterfall's middle steps stand on the one before."""
    if kind == "waterfall" and 0 < i < n - 1:
        return base - floor - bh, floor + bh
    return base - bh, (bh if kind == "waterfall" else 0)


def _ghost_bars(s, kind, box, pal):
    x, y, w, h = box
    n = 6
    heights = (0.48, 0.62, 0.55, 0.78, 0.66, 0.9) if kind == "column" else \
        (0.8, 0.25, 0.18, 0.12, 0.2, 0.95)
    gap = w / n * 0.35
    bw = (w - gap * (n + 1)) / n
    base = y + h - 0.1
    floor = 0.0
    for i, hh in enumerate(heights):
        bh = hh * (h - 0.55)
        bx = x + gap + i * (bw + gap)
        top, floor = _ghost_step(kind, i, n, base, floor, bh)
        _rrect(s, bx, top, bw, bh, pal.cur if i == n - 1 else pal.bar, pal.edge, radius=0.03)
        _ghost_xx(s, bx + bw / 2, top - 0.32, bw + gap, pal.ink)
    ln = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x), Inches(base),
                                Inches(x + w), Inches(base))
    ln.line.color.rgb = _rgb(pal.edge)


def _ghost_line(s, box, pal):
    x, y, w, h = box
    pts = (0.35, 0.5, 0.42, 0.6, 0.55, 0.72, 0.8)
    step = (w - 0.4) / (len(pts) - 1)
    coords = [(x + 0.2 + i * step, y + h - 0.2 - v * (h - 0.6)) for i, v in enumerate(pts)]
    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        ln = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1),
                                    Inches(x2), Inches(y2))
        ln.line.color.rgb = _rgb(pal.edge)
        ln.line.width = Pt(2.25)
    for i, (px, py) in enumerate(coords):
        dot = s.shapes.add_shape(MSO_SHAPE.OVAL, Inches(px - 0.07), Inches(py - 0.07),
                                 Inches(0.14), Inches(0.14))
        dot.fill.solid()
        dot.fill.fore_color.rgb = _rgb(pal.cur if i == len(coords) - 1 else pal.bar)
        dot.line.color.rgb = _rgb(pal.edge)
        dot.shadow.inherit = False
        if i in (0, len(coords) - 1):
            _ghost_xx(s, px, py - 0.4, 0.6, pal.ink)


def _ghost_funnel(s, box, pal):
    x, y, w, h = box
    steps = ("Impressions", "Clicks", "Add to cart", "Orders")
    sh = (h - 0.15 * (len(steps) - 1)) / len(steps)
    for i, name in enumerate(steps):
        sw = w * (1 - i * 0.2)
        sx = x + (w - sw) / 2
        sy = y + i * (sh + 0.15)
        _rrect(s, sx, sy, sw, sh, pal.cur if i == len(steps) - 1 else pal.bar, pal.edge)
        _text(s, sx + 0.2, sy, sw - 0.4, sh, [[(f"{name}  ", 14, C["white"] if s._dark
                                                 else C["ink"], True), ("XX", 14, pal.ink, True)]],
              14, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)


def _ghost_matrix(s, box, pal):
    x, y, w, h = box
    cw, ch = (w - 0.2) / 2, (h - 0.2) / 2
    for i, name in enumerate(_GHOST_QUADS):
        r, c = divmod(i, 2)
        qx, qy = x + c * (cw + 0.2), y + r * (ch + 0.2)
        _rrect(s, qx, qy, cw, ch, pal.cur if i == 0 else pal.bar, pal.edge)
        _text(s, qx + 0.2, qy + 0.15, cw - 0.4, ch - 0.3,
              [[(name, 15, C["white"] if s._dark else C["ink"], True)],
               [("XX products, XX% of NMV", 14, pal.ink, False)]], 14)


def _ghost_cell_fill(r, dark, bar):
    """Navy header row, then banded rows."""
    if r == 0:
        return C["navy3"] if dark else C["navy"]
    if r % 2:
        return bar
    return C["navy2"] if dark else C["white"]


def _ghost_table(s, box, pal):
    x, y, w, h = box
    cols, rows = 4, 5
    cw, rh = w / cols, min(0.5, (h - 0.4) / (rows + 1))
    for r in range(rows + 1):
        for c in range(cols):
            cx, cy = x + c * cw, y + r * rh
            _rrect(s, cx + 0.02, cy + 0.02, cw - 0.04, rh - 0.04,
                   _ghost_cell_fill(r, s._dark, pal.bar), pal.edge, radius=0.02)
            if r:
                _ghost_xx(s, cx + cw / 2, cy + (rh - 0.3) / 2, cw - 0.1, pal.ink)
    _text(s, x, y + (rows + 1) * rh + 0.1, w, 0.3, "Rows fill with the real products",
          MIN_PT, pal.ink)


def _drafts_of(sec):
    return next((mm for mm in sec.get("missing") or [] if mm.get("if_good")), None)


def _draft_facts(mm):
    return [("If good:", mm["if_good"]), ("If bad:", mm["if_bad"]),
            ("If neutral:", mm["if_neutral"])]


def _drafts_note(sec) -> str:
    mm = _drafts_of(sec)
    if not mm:
        return ""
    return (f"Drafted for when {mm['what']} arrives (conditional): if good, {mm['if_good']} "
            f"If bad, {mm['if_bad']} If neutral, {mm['if_neutral']}")


def _pending_body(s, sec):
    """A page whose data is not in yet. With drafts: the intended visual as a grey outline
    with XX values, and what the page will say if the data turns out good, bad or neutral
    (contract section 11). Without drafts: amber cards, one per missing item."""
    mm = _drafts_of(sec)
    if mm:
        _drafted_body(s, sec, mm)
        return
    items = sec.get("missing") or []
    warn = _warn_style(s._dark)
    n = max(1, min(len(items), 3))
    w = (12.45 - 0.2 * (n - 1)) / n
    for i, mm in enumerate(items[:3]):
        x = 0.45 + i * (w + 0.2)
        facts = _missing_facts(mm)
        _panel(s, x, BODY_Y, w, BODY_H, _cut(_cap(mm["what"]), 70), facts, "TriangleAlert",
               **warn)
    if not items:
        _panel(s, 0.45, BODY_Y, 12.45, BODY_H, "No figure for this page", [], "TriangleAlert",
               **warn)


def _warn_style(dark) -> dict:
    """Amber panel colours of a page waiting for data."""
    if dark:
        return {"fill": None, "line": "B7791F", "head_color": None}
    return {"fill": C["watchBg"], "line": "F5C27A", "head_color": C["ink"]}


def _drafted_body(s, sec, mm):
    """The intended visual as a grey outline, beside what the page will say."""
    dark = s._dark
    _rrect(s, 0.45, BODY_Y, 7.85, BODY_H, C["navy2"] if dark else C["white"],
           C["navy3"] if dark else C["line2"])
    _text(s, 0.65, BODY_Y + 0.12, 7.45, 0.3, "Intended visual  |  data pending".upper(),
          MIN_PT, C["cyan"] if dark else C["watch"], True, spacing=1)
    _text(s, 0.65, BODY_Y + 0.42, 7.45, 0.34,
          _cut(_cap(mm.get("visual") or sec["title"]), 80), 15,
          C["white"] if dark else C["ink"], True)
    _ghost(s, mm.get("ghost") or "column", 0.85, BODY_Y + 1.15, 7.05, BODY_H - 1.35)
    _panel(s, 8.55, BODY_Y, 4.35, BODY_H, "When the data arrives", _draft_facts(mm),
           "TriangleAlert", **_warn_style(dark))


def _missing_facts(mm) -> list:
    facts = [("Why it matters:", mm["why"]),
             ("What is needed:", _HTTP_NOISE.sub("", str(mm["needs"])))]
    if mm.get("fallback"):
        facts.append(("Shown instead:", mm["fallback"]))
    if mm.get("updates"):
        facts.append(("When it arrives:", mm["updates"]))
    return facts


def _section_slides(deck, sec, sheet, names, takeover_mid):
    dark = sec["group"] in DARK_GROUPS
    kicker = _kicker(sec, names)
    title = sec.get("headline") or sec["title"]
    tables = [t for t in sec.get("tables") or [] if t.get("rows")]
    facts = _lead_facts(sec)
    sources = L.source_line(sec.get("queries") or ["settings"])
    notes = _section_notes(sec, sheet, names, title)
    prev_label = _prev_label(tables)

    pending = _drafts_of(sec)
    if not sec.get("headline") and not tables and not facts and pending:
        # a page waiting for data still says what it is waiting for, not just its topic
        title = _fit_text(f"{sec['title']}: waiting for {_uncap(pending['what'])}", 9.3, 1)
    s = deck.slide(kicker, title, dark)
    main = next((t for t in tables if t.get("chart")), tables[0] if tables else None)
    clipped = _section_body(s, sec, main, facts, takeover_mid, prev_label)
    _takeaway(s, _takeaway_text(sec), f"Workbook: {sheet}")
    src = f"Source: {sources}."
    if clipped:
        src = f"Top rows shown; full table in workbook sheet {sheet}. " + src
    _source(s, src)
    deck.notes(s, title, **notes)

    for t in tables:                        # further tables, each on its own page
        if t is not main:
            _table_slide(deck, sec, t, kicker, sheet, notes, takeover_mid, prev_label)


def _section_notes(sec, sheet, names, title) -> dict:
    """Speaker notes of a section page: framing, message, evidence, caveat and trace."""
    queries = ", ".join(sec.get("queries") or []) or "settings"
    trace = f"workbook sheet {sheet}; queries {queries}"
    told = sec.get("narration") or {}
    written = told.get("status") in ("written", "repaired", "partial")
    framing = (told.get("framing") if written else None) or \
        f"{sec['title']}, part of {names.get(sec['group'], sec['group']).lower()}."
    message = (told.get("message") if written else None) or title
    evidence = [f["text"] for f in sec.get("facts") or [] if not _ai(f)]
    if written:
        trace += "; headline, takeaway and insights written by AI from these figures"
    return {"framing": framing, "message": message, "evidence": evidence,
            "caveat": _section_caveat(sec), "trace": trace}


def _section_caveat(sec) -> str:
    """What is missing, the other readings of the AI's inferences, and the drafts."""
    caveat = "; ".join([mm["what"] for mm in (sec.get("missing") or [])[:3]]
                       + [f"another reading of \"{_cut(f['text'], 80)}\": {f['alternative']}"
                          for f in sec.get("facts") or [] if _ai(f) and f.get("alternative")][:2])
    if _drafts_note(sec):
        caveat = (caveat + ". " if caveat else "") + _drafts_note(sec)
    return caveat


def _prev_label(tables):
    """The previous month's column name ('Aug 2026'), from the last table that has one."""
    prev_label = None
    for t in tables:
        if len(t["columns"]) > 3 and re.fullmatch(r"[A-Z][a-z]{2} \d{4}", str(t["columns"][3])):
            prev_label = t["columns"][3]
    return prev_label


def _section_body(s, sec, main, facts, takeover_mid, prev_label) -> bool:
    """The body of a section page: its main chart or table with the facts beside it, or
    the facts alone, or the page waiting for data. True when rows went to the workbook."""
    if main is None:
        if facts:
            _facts_body(s, sec, facts)
        else:
            _pending_body(s, sec)
        return False
    if not main.get("chart") and len(_deck_view(main, 10)[1]) > 5:      # a wide table
        return _table(s, main, 0.45, BODY_Y, 12.45, max_rows=30)
    if main.get("chart"):
        clipped = _chart(s, main, 0.45, BODY_Y, 7.85, BODY_H, takeover_mid, prev_label)
    else:
        clipped = _table(s, main, 0.45, BODY_Y, 7.85, max_rows=30, max_cols=6)
    side = facts or [(None, mm["what"]) for mm in sec.get("missing") or []]
    _panel(s, 8.55, BODY_Y, 4.35, BODY_H, sec["title"], side, SECTION_ICON.get(sec["id"]))
    return clipped


def _facts_body(s, sec, facts):
    """Facts without a table, beside the first item still missing."""
    dark = s._dark
    _panel(s, 0.45, BODY_Y, 6.1 if sec.get("missing") else 12.45, BODY_H, sec["title"], facts,
           SECTION_ICON.get(sec["id"]))
    m0 = _drafts_of(sec) or next(iter(sec.get("missing") or []), None)
    if not m0:
        return
    body = [("What is needed:", _HTTP_NOISE.sub("", str(m0["needs"])))]
    body += _draft_facts(m0) if m0.get("if_good") else \
        [("Shown instead:", m0.get("fallback") or "nothing yet")]
    _panel(s, 6.75, BODY_Y, 6.15, BODY_H, _cut(_cap(m0["what"]), 60), body,
           "TriangleAlert", fill=None if dark else C["watchBg"],
           line=None if dark else "F5C27A")


def _table_slide(deck, sec, t, kicker, sheet, notes, takeover_mid, prev_label):
    """A further table of a section, on its own page."""
    s2 = deck.slide(kicker, t["title"], sec["group"] in DARK_GROUPS)
    if t.get("chart"):
        clip = _chart(s2, t, 0.45, BODY_Y, 12.45, BODY_H, takeover_mid, prev_label)
    else:
        clip = _table(s2, t, 0.45, BODY_Y, 12.45, max_rows=30)
    _takeaway(s2, t.get("takeaway") or
              _cut(f"{t['title']}: detail behind {_uncap(sec['title'])}.", 200),
              f"Workbook: {sheet}")
    _source(s2, (f"Top rows shown; full table in workbook sheet {sheet}. " if clip else "")
            + f"Source: {L.source_label(t['source'])}.")
    deck.notes(s2, t["title"], notes["framing"], t["title"], [], notes["caveat"], notes["trace"])


# where each group sits on the traffic x conversion matrix, and its signal colour
_QUAD_CELL = {"Hidden gem": (0, 0, "infoBg", "acme"), "Star": (0, 1, "goodBg", "good"),
              "Underperformer": (1, 0, "badBg", "bad"), "Rough diamond": (1, 1, "watchBg", "watch")}


def _quadrant_slides(deck, sec, sheet, names):
    """Products by traffic and conversion as the analyst's matrix: Star, Hidden gem, Rough
    diamond, Underperformer, then the names behind each group."""
    q = sec["quadrants"]
    th, groups = q["thresholds"], q["groups"]
    kicker = _kicker(sec, names)
    title = sec.get("headline") or sec["title"]
    trace = f"workbook sheet {sheet}; queries {', '.join(sec.get('queries') or [])}"
    s = deck.slide(kicker, title)
    _quadrant_matrix(s, th, groups)
    facts = _lead_facts(sec)
    _panel(s, 8.55, BODY_Y, 4.35, BODY_H, sec["title"], facts, SECTION_ICON.get(sec["id"]))
    _takeaway(s, _takeaway_text(sec), f"Workbook: {sheet}")
    _source(s, f"Source: {L.source_line(sec.get('queries') or [])}. Lines: traffic "
               f"{th['views_basis']}; conversion {th['cvr_basis']}.")
    told = sec.get("narration") or {}
    deck.notes(s, title, told.get("framing") or "Which products earn their traffic.",
               told.get("message") or title,
               [f["text"] for f in sec.get("facts") or [] if not _ai(f)],
               "Revenue is the product report's sales revenue; NMV per listing is not in it.",
               trace)
    _quadrant_names(deck, kicker, groups, sheet, trace)


def _quadrant_matrix(s, th, groups):
    """The 2x2 of traffic by conversion, each group with its share of revenue and views."""
    x0, y0, w, h = 0.95, BODY_Y, 7.35, BODY_H - 0.35
    cw, ch = (w - 0.15) / 2, (h - 0.15) / 2
    _text(s, 0.3, y0 + h / 2 - 0.15, 0.6, 0.3, "CVR", MIN_PT, C["muted"], True,
          align=PP_ALIGN.CENTER)
    _text(s, 0.3, y0 + 0.02, 0.6, 0.3, "high", MIN_PT, C["muted"], align=PP_ALIGN.CENTER)
    _text(s, 0.3, y0 + h - 0.32, 0.6, 0.3, "low", MIN_PT, C["muted"], align=PP_ALIGN.CENTER)
    _text(s, x0, y0 + h + 0.04, w, 0.3,
          f"Traffic: low  |  {fmt_n(th['views'])} page views  |  high      Conversion line "
          f"{th['cvr']:.2f}%", MIN_PT, C["muted"], True, align=PP_ALIGN.CENTER)
    for name, (r, c, bg, ink) in _QUAD_CELL.items():
        g = groups.get(name) or {}
        x, y = x0 + c * (cw + 0.15), y0 + r * (ch + 0.15)
        _rrect(s, x, y, cw, ch, C[bg], C["line2"])
        tops = [_fit_text(f"{i}. {t['name']}", cw - 0.4, 1)
                for i, t in enumerate((g.get("top") or [])[:2], 1)]
        paras = [[(name, 16, C[ink], True)],
                 [(f"{g.get('products', 0)} products, {share_text(g.get('revenue_share'), 0)} "
                   f"of revenue", 14, C["ink"], True)],
                 [(f"{share_text(g.get('views_share'), 0)} of page views", MIN_PT, C["slate"],
                   False)]]
        paras += [[(t, MIN_PT, C["slate"], False)] for t in tops]
        _text(s, x + 0.2, y + 0.12, cw - 0.4, ch - 0.2, paras, 14, gap=2)


def _quadrant_names(deck, kicker, groups, sheet, trace):
    """The page of the top products behind each group."""
    s2 = deck.slide(kicker, "The names behind each group: the top products by revenue")
    pw, ph = (12.45 - 0.2) / 2, (BODY_H - 0.15) / 2
    for name, (r, c, _bg, ink) in _QUAD_CELL.items():
        g = groups.get(name) or {}
        x, y = 0.45 + c * (pw + 0.2), BODY_Y + r * (ph + 0.15)
        _rrect(s2, x, y, pw, ph, C["white"], C["line"])
        _rrect(s2, x, y, 0.09, ph, C[ink], radius=0.02)
        _text(s2, x + 0.25, y + 0.1, pw - 0.4, ph - 0.15, _quadrant_lines(name, g, ink, pw), 14,
              gap=1)
    _takeaway(s2, "Feed the Hidden gems with traffic, fix the Rough diamonds before buying "
                  "them more traffic, and review the Underperformers for delisting.",
              f"Workbook: {sheet}")
    _source(s2, "Source: product report; revenue and conversion as the report states them.")
    deck.notes(s2, "The names behind each group", "The products behind the matrix.",
               "Name the products the plan acts on.",
               [f"{n}: " + ", ".join(t["name"] for t in (groups.get(n) or {}).get("top") or [])
                for n in _QUAD_CELL], "", trace)


def _quadrant_lines(name, g, ink, pw) -> list:
    """A group's name and count, then its top five products with revenue and conversion."""
    lines = [[(f"{name}  ", 15, C[ink], True),
              (f"{g.get('products', 0)} products", MIN_PT, C["muted"], True)]]
    for i, t in enumerate((g.get("top") or [])[:5], 1):
        tail = f"  {fmt_idr(t['revenue'])}, {t['cvr']:.2f}%"
        body = _fit_text(t["name"], pw - 0.4 - len(tail) / CHARS_PER_INCH, 1)
        lines.append([(f"{i}. {body}", MIN_PT, C["ink"], False),
                      (tail, MIN_PT, C["slate"], True)])
    if len(lines) == 1:
        lines.append([("no product sold in this group", MIN_PT, C["muted"], False)])
    return lines


def _synthesis_slides(deck, sec, sheet, names) -> int:
    """Section E when the narration wrote it: what drove the month, the asks of the brand
    and Acme's moves. Every item names the pages it rests on. Returns the slides made."""
    syn = sec.get("synthesis") or {}
    kicker = _kicker(sec, names)
    trace = (f"workbook sheet {sheet}; written by AI from the pages of this review, every "
             "number checked against them")
    made = _drivers_slide(deck, syn, kicker, sheet, trace)
    made += _asks_slide(deck, syn, kicker, sheet, trace)
    made += _strategy_slide(deck, syn, kicker, sheet, trace)
    return made


def _driver_width(cols, n, i) -> float:
    """A driver card's width: the last of an odd number spans both columns."""
    if cols != 2:
        return 12.45
    return 12.45 if n % 2 and i == n - 1 else (12.45 - 0.2) / cols


def _driver_card(s, m, i, it, box):
    x, y, w, h = box
    _rrect(s, x, y, w, h, C["navy2"], C["navy3"])
    _icon(s, "Zap", "c", x + 0.18, y + 0.15, 0.3)
    pages = _pages_of(m, it)
    head = f"Driver {i + 1}  |  {it.get('confidence', 'medium')} confidence" + \
        (f"  |  pages {pages}" if pages else "")
    _text(s, x + 0.6, y + 0.13, w - 0.8, 0.34, head.upper(), MIN_PT, C["cyan"], True,
          spacing=1, anchor=MSO_ANCHOR.MIDDLE)
    room = max(int((h - 0.62) / 0.25), 3)
    alt = it.get("alternative")
    paras = [[(_fit_text(clean(it["text"]), (w - 0.4) * 0.92,
                         room - (2 if alt else 0)), 14, C["white"], False)]]
    if alt:
        paras.append([("Other reading: ", MIN_PT, C["blueL"], True),
                      (_fit_text(clean(alt), (w - 0.4) * 0.92, 2), MIN_PT, "E2E8F0",
                       False)])
    _text(s, x + 0.2, y + 0.55, w - 0.4, h - 0.6, paras, 14, gap=4)


def _drivers_slide(deck, syn, kicker, sheet, trace) -> int:
    """What drove the month, one card a driver with its other reading."""
    drivers, foresight = (syn.get("drivers") or [])[:4], syn.get("foresight") or []
    if not drivers:
        return 0
    title = syn.get("tension") or drivers[0]["text"]
    s = deck.slide(kicker, title, dark=True)
    cols = 1 if len(drivers) == 1 else 2
    rows = (len(drivers) + cols - 1) // cols
    w = (12.45 - 0.2 * (cols - 1)) / cols
    h = (BODY_H - 0.15 * (rows - 1)) / rows
    for i, it in enumerate(drivers):
        r, c = divmod(i, cols)
        x, y = 0.45 + c * (w + 0.2), BODY_Y + r * (h + 0.15)
        w = _driver_width(cols, len(drivers), i)
        _driver_card(s, deck.model, i, it, (x, y, w, h))
    nxt = ("Next month: " + foresight[0]["text"]) if foresight else \
        ((syn.get("summary") or [{}])[0].get("text") or syn.get("story") or title)
    _takeaway(s, nxt, f"Workbook: {sheet}")
    _source(s, "Source: the pages of this review, named on each card. Drivers are "
               "inferences: the other reading is what would make each one wrong.")
    deck.notes(s, title, "Why the month moved the way it did.", title,
               [d["text"] for d in drivers] + [f["text"] for f in foresight],
               "; ".join(f"driver {i + 1}, other reading: {d['alternative']}"
                         for i, d in enumerate(drivers) if d.get("alternative"))[:600],
               trace)
    return 1


def _asks_slide(deck, syn, kicker, sheet, trace) -> int:
    """What Acme asks of the brand, one card an ask with the pages it rests on."""
    m = deck.model
    asks = (syn.get("asks") or [])[:4]
    if not asks:
        return 0
    brand = _brand_display(m["brand"])
    title = (f"What Acme asks of {brand}: {len(asks)} ask{'s' if len(asks) > 1 else ''}, "
             "each resting on a page of this review")
    s = deck.slide(kicker, title, dark=True)
    w = (12.45 - 0.2 * (len(asks) - 1)) / len(asks)
    for i, it in enumerate(asks):
        x = 0.45 + i * (w + 0.2)
        _rrect(s, x, BODY_Y, w, 3.55, C["navy2"], "3B5BA5")
        _icon(s, "Target", "c", x + 0.2, BODY_Y + 0.2, 0.38)
        _text(s, x + 0.7, BODY_Y + 0.2, w - 0.9, 0.4, f"Ask {i + 1}".upper(), MIN_PT,
              C["cyan"], True, spacing=1, anchor=MSO_ANCHOR.MIDDLE)
        _text(s, x + 0.2, BODY_Y + 0.8, w - 0.4, 2.05,
              _fit_text(clean(it["text"]), (w - 0.4) * 0.92, 8), 14, "E2E8F0")
        _rrect(s, x + 0.15, BODY_Y + 2.95, w - 0.3, 0.45, C["navy3"], radius=0.06)
        _text(s, x + 0.3, BODY_Y + 2.95, w - 0.6, 0.45,
              f"Rests on: pages {_pages_of(m, it) or 'of this review'}", MIN_PT,
              C["white"], True, anchor=MSO_ANCHOR.MIDDLE)
    _takeaway(s, "Each ask is a proposal for the meeting: it names the pages whose figures "
                 "make the case, so the brand can check them before agreeing.",
              f"Workbook: {sheet}")
    _source(s, "Source: the pages named on each card; written by AI, every number checked "
               "against those pages.")
    deck.notes(s, title, "Turn the findings into what we need from the brand.", title,
               [a["text"] for a in asks], "Asks are proposals, not commitments; confirm "
               "each with the account team before the meeting.", trace)
    return 1


def _strategy_slide(deck, syn, kicker, sheet, trace) -> int:
    """Acme's moves as a table: why, the impact expected and the assumption behind it."""
    m = deck.model
    strategy = (syn.get("strategy") or [])[:6]
    if not strategy:
        return 0
    t = {"title": "Acme moves", "source": "narration",
         "columns": ["Move", "Why", "Expected impact", "Assumption", "Pages"],
         "rows": [[it.get("action") or "", it.get("why") or "", it.get("impact") or "",
                   it.get("assumption") or "", _pages_of(m, it)] for it in strategy]}
    title = (f"{len(strategy)} move{'s' if len(strategy) > 1 else ''} for Acme next month, "
             "each with the impact we expect and the assumption behind it")
    s = deck.slide(kicker, title, dark=True)
    # four lines a cell leaves room for three moves; the prompt asks for cells of three
    clip = _table(s, t, 0.45, BODY_Y, 12.45, max_rows=6,
                  max_lines=4 if len(strategy) <= 3 else 3)
    _takeaway(s, "The moves are proposals for the owner to accept or change; the assumption "
                 "column says what would make each one wrong.", f"Workbook: {sheet}")
    _source(s, (f"Top rows shown; the full list is in workbook sheet {sheet}. " if clip
                else "") + "Source: the pages named in each row; written by AI.")
    deck.notes(s, title, "Close the story with what Acme will do.", title,
               [it.get("action") or "" for it in strategy],
               "Impacts are expectations from this review's figures, not forecasts.", trace)
    return 1


def _week_decisions(deck):
    """The meeting closes on what is decided this week: each decision with one owner, a
    date and what it unlocks (written by AI in the synthesis, numbers checked)."""
    m = deck.model
    syn = (_sec(m, "synthesis") or {}).get("synthesis") or {}
    items = (syn.get("decisions") or [])[:3]
    if not items:
        return
    title = (f"{len(items)} decision{'s' if len(items) > 1 else ''} to take this week, each "
             "with one owner")
    s = deck.slide("Decisions this week", title, dark=True)
    w = (12.45 - 0.2 * (len(items) - 1)) / len(items)
    for i, it in enumerate(items):
        x = 0.45 + i * (w + 0.2)
        _rrect(s, x, BODY_Y, w, 3.95, C["navy2"], "3B5BA5")
        _icon(s, "Flag", "c", x + 0.2, BODY_Y + 0.2, 0.38)
        _text(s, x + 0.7, BODY_Y + 0.2, w - 0.9, 0.4, f"Decision {i + 1}".upper(), MIN_PT,
              C["cyan"], True, spacing=1, anchor=MSO_ANCHOR.MIDDLE)
        _text(s, x + 0.2, BODY_Y + 0.75, w - 0.4, 1.6,
              _fit_text(clean(it["text"]), (w - 0.4) * 0.92, 5), 15, C["white"], True)
        lines = [[("Owner: ", MIN_PT, C["cyan"], True),
                  (clean(it.get("owner") or "to agree in the meeting"), MIN_PT, "E2E8F0",
                   False)],
                 [("By: ", MIN_PT, C["cyan"], True),
                  (clean(it.get("when") or "this week"), MIN_PT, "E2E8F0", False)]]
        if it.get("unlocks"):
            lines.append([("Unlocks: ", MIN_PT, C["cyan"], True),
                          (_fit_text(clean(it["unlocks"]), (w - 0.6) * 0.92, 2), MIN_PT,
                           "E2E8F0", False)])
        _text(s, x + 0.2, BODY_Y + 2.35, w - 0.4, 1.15, lines, MIN_PT, gap=2)
        _rrect(s, x + 0.15, BODY_Y + 3.4, w - 0.3, 0.42, C["navy3"], radius=0.06)
        _text(s, x + 0.3, BODY_Y + 3.4, w - 0.6, 0.42,
              f"Rests on: pages {_pages_of(m, it) or 'of this review'}", MIN_PT, C["white"],
              True, anchor=MSO_ANCHOR.MIDDLE)
    _takeaway(s, "The meeting ends on these decisions; the next review opens by checking "
                 "what each one changed.", "Workbook: Storyline")
    _source(s, "Source: the pages named on each card; written by AI, every number checked "
               "against those pages.")
    deck.notes(s, title, "Close the meeting on decisions, not on findings.", title,
               [d["text"] for d in items], "Decisions are proposals until the owner agrees "
               "in the meeting.", "workbook sheet Storyline")


def _decisions(deck, sections):
    _week_decisions(deck)
    m = deck.model
    guessed = [r for r in m.get("status_values") or [] if not r.get("configured")]
    cards = []
    if guessed:
        cards.append(("Tag", "Confirm the status groups",
                      f"{len(guessed)} order status value(s) were grouped by keyword: "
                      + "; ".join(f"{L.status_label(r['value'])} as "
                                  f"{L.GROUP_LABELS.get(r['group'], r['group']).lower()}"
                                  for r in guessed[:5])
                      + ". NMV, orders and cancellation depend on it.",
                      "brand owner, then the MBR settings"))
    if not (m.get("inputs") or {}).get("brand_configured"):
        cards.append(("Target", f"Complete the {_brand_display(m['brand'])} settings",
                      "Acme takeover date, campaign calendar, focus products and warehouse "
                      "client codes. Without them the matching pages stay pending.",
                      "account team, then the MBR settings"))
    cards.append(("Scale", "Confirm the GMV definition",
                  "GMV here is item value after seller discount over every order status; the "
                  "reference also adds net shipping, which the order data does not hold. NMV "
                  "counts orders counted as sold.",
                  "brand owner and Acme finance"))
    title = f"{len(cards)} decisions unlock the pages still pending in this review"
    s = deck.slide("Decisions needed", title, dark=True)
    w = (12.45 - 0.2 * (len(cards) - 1)) / len(cards)
    for i, (icon, head, body, needs) in enumerate(cards):
        x = 0.45 + i * (w + 0.2)
        _rrect(s, x, BODY_Y, w, 3.55, C["navy2"], "3B5BA5")
        _icon(s, icon, "c", x + 0.2, BODY_Y + 0.2, 0.38)
        _text(s, x + 0.7, BODY_Y + 0.2, w - 0.9, 0.4, f"{i + 1}  {head}".upper(), MIN_PT,
              C["cyan"], True, spacing=1, anchor=MSO_ANCHOR.MIDDLE)
        _text(s, x + 0.2, BODY_Y + 0.8, w - 0.4, 2.0, _cut(body, 260), 14, "E2E8F0")
        _rrect(s, x + 0.15, BODY_Y + 2.95, w - 0.3, 0.45, C["navy3"], radius=0.06)
        _text(s, x + 0.3, BODY_Y + 2.95, w - 0.6, 0.45, f"Where: {needs}", MIN_PT, C["white"],
              True, anchor=MSO_ANCHOR.MIDDLE)
    gaps = [(x["ref"], mm["what"]) for x in sections for mm in x.get("missing") or []]
    _text(s, 0.45, 5.4, 12.45, 0.6,
          [[("Data still missing: ", MIN_PT, "FBBF24", True),
            (_cut("; ".join(f"{r} {w_}" for r, w_ in gaps[:6]) +
                  (f"; and {len(gaps) - 6} more" if len(gaps) > 6 else ""), 300), MIN_PT,
             "E2E8F0", False)]], MIN_PT)
    _takeaway(s, "Each decision turns pending pages into figures on the next run; nothing "
                 "else in the pipeline changes.", "Decide in the MBR settings")
    _source(s, "Source: OPEN-ITEMS-AND-REVIEW.md")
    deck.notes(s, title, "Close on what only the owner can decide.", title,
               [c[1] for c in cards], f"{len(gaps)} data item(s) still missing.",
               "workbook sheet Open Items")


def _definitions(deck):
    m = deck.model
    groups = m.get("status_groups") or {}
    rows = [
        ["GMV", "Item value after seller discount, every order status (cancelled and unpaid "
         "included); net shipping is not in the order data", _ORDER_HISTORY],
        ["NMV", "Item value after seller discount with the platform discount added back, "
         "orders counted as sold", _ORDER_HISTORY],
        ["Counted as sold", L.status_list(groups.get("processed")), "order status groups"],
        ["Cancelled", L.status_list(groups.get("cancelled")), "order status groups"],
        ["AOV", "NMV per order counted as sold", _ORDER_HISTORY],
        ["Seller cancellation", "Cancelled by the seller or fulfilment, or for a stock or "
         "fulfilment reason; a buyer who never paid is not one", "cancelled orders"],
        ["MoM / YoY", "Change against the previous month / the same month last year",
         "all pages"],
        ["Data pulled", f"{L.stamp_label(m.get('data_pulled_at') or m.get('generated_at'))} "
         "from the live warehouse; recent statuses can still move", "run log"],
        ["History", "The full order history; Acme curated orders are checked against it in "
         "the appendix", "owner rule"],
    ]
    t = {"title": "Definitions", "columns": ["Term", "Definition used in this review",
                                             "Source"], "rows": rows, "source": "definitions"}
    title = "Definitions used throughout this review: sales, orders and the source rule"
    s = deck.slide("Appendix  |  Definitions", title)
    _table(s, t, 0.45, BODY_Y, 12.45, max_rows=20, h=FOOT_Y - 0.08 - BODY_Y, max_lines=3)
    _source(s, "Source: report definitions and the order status groups of this run.")
    deck.notes(s, title, "Reference page.", title, [r[0] for r in rows], "",
               "workbook sheet README")


def _data_quality(deck):
    m = deck.model
    stats = m.get("stats") or {}
    rows = []
    for q in m.get("queries") or []:
        label = L.source_label(q["key"])
        if q["ok"]:
            n = len(q.get("rows") or [])
            rows.append([label, f"{n} row{'' if n == 1 else 's'} returned", "PASS"])
        else:
            why = {"TABLE_NOT_IN_SCOPE": "not available to this business",
                   "COLUMN_MISSING": "a field this page reads is missing",
                   "SCHEMA_DRIFT": "the data changed shape; rescan needed",
                   "COST_LIMIT": "too large to read for one report",
                   "REPORT_BYTES_LIMIT": "report reading budget used up"}.get(
                q.get("gap_code") or q.get("code"), "the request failed")
            rows.append([label, why, "FLAG"])
    rows.sort(key=lambda r: r[2] == "PASS")
    t = {"title": "Data requests", "columns": ["Data", "Result", "Check"], "rows": rows,
         "source": "queries"}
    title = (f"{stats.get('queries_ok', 0)} of {stats.get('queries', 0)} data requests "
             f"returned data; every one passed the access and safety check before it ran")
    s = deck.slide("Appendix  |  Data quality and method", title)
    clip = _table(s, t, 0.45, BODY_Y, 12.45, max_rows=40, h=FOOT_Y - 0.08 - BODY_Y)
    _source(s, ("Flagged first; the full list is in workbook sheet Queries. " if clip else "")
            + f"Source: EVIDENCE-AND-REASONING.md section 3; about "
            f"{(stats.get('estimated_bytes') or 0) / 1024 ** 3:,.1f} GB read.")
    deck.notes(s, title, "Answer how the numbers were produced if asked.", title,
               [r[0] for r in rows[:6]], "Flagged requests are listed in the open items.",
               "workbook sheet Queries; engine " + str(m.get("engine_version")))


def _page(deck, sec, sheet, names, takeover_mid):
    """The slides of one section: its own design for the scorecard, a written synthesis
    and the product quadrants, else the standard section page."""
    if sec["id"] == "scorecard":
        _scorecard(deck, sec, sheet)
        return
    if sec["id"] == "synthesis" and sec.get("synthesis") and \
            _synthesis_slides(deck, sec, sheet, names):
        return
    if sec["id"] == "product_funnel" and sec.get("quadrants"):
        _quadrant_slides(deck, sec, sheet, names)
        return
    _section_slides(deck, sec, sheet, names, takeover_mid)


def _closes_story(g, sections) -> bool:
    """The decisions page follows group F, or E when the review has no F."""
    return g == "F" or (g == "E" and not any(x["group"] == "F" for x in sections))


def build(model: dict) -> bytes:
    deck = _Deck(model)
    sections = model.get("sections") or []
    sheets = section_sheet_names(sections)
    names = dict(GROUPS)
    takeover = (model.get("inputs") or {}).get("takeover_date")
    takeover_mid = int(str(takeover)[:4] + str(takeover)[5:7]) if takeover else None
    _cover(deck)
    _exec_summary(deck, sections, sheets)
    for g, _label in GROUPS:
        part = [x for x in sections if x["group"] == g]
        for sec in part:
            _page(deck, sec, sheets[sec["id"]], names, takeover_mid)
        if _closes_story(g, sections):
            _decisions(deck, sections)
    if not any(x["group"] in ("E", "F") for x in sections):
        _decisions(deck, sections)
    _definitions(deck)
    _data_quality(deck)
    deck.finish()
    buf = io.BytesIO()
    deck.prs.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- QC
def _runs(shape):
    if shape.has_text_frame:
        for p in shape.text_frame.paragraphs:
            yield from p.runs
    if getattr(shape, "has_table", False) and shape.has_table:
        for row in shape.table.rows:
            for cell in row.cells:
                for p in cell.text_frame.paragraphs:
                    yield from p.runs


def qc(data: bytes) -> list[str]:
    """House-rule breaches in a finished deck: dashes, text under 13 pt, pictures other
    than the backgrounds and icons (charts must be native), slides without notes."""
    prs = Presentation(io.BytesIO(data))
    issues = []
    for i, slide in enumerate(prs.slides, 1):
        for shape in slide.shapes:
            issues += _shape_issues(i, shape)
        if not slide.has_notes_slide or not slide.notes_slide.notes_text_frame.text.strip():
            issues.append(f"slide {i}: no speaker notes")
    return issues


def _shape_issues(i, shape) -> list:
    issues = []
    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE and shape.name not in (_BG_NAME, _ICON_NAME):
        issues.append(f"slide {i}: picture found, charts must be native")
    for r in _runs(shape):
        issues += _run_issues(i, r)
    if getattr(shape, "has_chart", False) and shape.has_chart:
        f = shape.chart.font.size
        if f is None or f.pt < MIN_PT:
            issues.append(f"slide {i}: chart text under {MIN_PT} pt")
    return issues


def _run_issues(i, r) -> list:
    issues = []
    if r.text and _DASHES.search(r.text):
        issues.append(f"slide {i}: dash in '{r.text[:40]}'")
    if r.text.strip() and (r.font.size is None or r.font.size.pt < MIN_PT):
        issues.append(f"slide {i}: text under {MIN_PT} pt '{r.text[:40]}'")
    return issues
