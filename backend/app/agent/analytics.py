"""Deterministic analytics over governed SQL results (the Analytics Layer).

Pure-stdlib computation over the (columns, rows) that execute_governed returns.
Why: all "analysis" used to be the LLM eyeballing raw row JSON — its arithmetic
(deltas, growth %, shares) was unverified and sometimes wrong. This module
PRE-COMPUTES those numbers so (a) the LLM narrates trustworthy values instead of
doing its own math, and (b) the verifier can trace answer figures back to them.

PII note: callers pass PII-MASKED rows (mask_pii_for_llm) — derived values are
then safe to embed into any LLM prompt. Best-effort by design: any failure means
"no derived block", never a broken query result.
"""
from datetime import date, datetime
from decimal import Decimal
import re

_MAX_ROWS = 10000          # safety cap: derived stats are computed on at most this many rows
_MAX_MEASURES = 3          # numeric columns summarized per result set
_PERIOD_RE = re.compile(r"^\d{4}([-/]\d{1,2}([-/]\d{1,2})?)?([T ]\d{2}:\d{2}(:\d{2})?)?$")


def _num(v):
    """Coerce a cell to float for math; None when it isn't numeric. bool excluded."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float, Decimal)):
        try:
            f = float(v)
            return f if f == f and abs(f) != float("inf") else None   # NaN/inf guard
        except Exception:
            return None
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            try:
                return float(s)
            except Exception:
                return None
    return None


def _is_period(v):
    """True when a cell looks like a time period: date/datetime, ISO-ish string, or a
    YYYYMM integer (warehouse month keys like 202606 — summing/scaling those is nonsense,
    but as a period they give trend + forecast)."""
    if isinstance(v, (date, datetime)):
        return True
    if isinstance(v, bool):
        return False
    if isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit()):
        try:
            n = int(v)
        except Exception:
            return False
        return 190001 <= n <= 210012 and 1 <= n % 100 <= 12
    return isinstance(v, str) and bool(_PERIOD_RE.match(v.strip()))


def _r(x):
    """Round for prompt/JSON friendliness. Large values (>=1000, e.g. rupiah totals) get NO
    decimal tail — 4 decimals on a billion is meaningless precision that only made the model
    echo ugly numbers like 2.785.176.381,1759; small values (percentages, ratios) keep up to
    4 decimals."""
    if x is None:
        return None
    x = float(x)
    if abs(x) >= 1000:
        return int(round(x))
    r = round(x, 4)
    return int(r) if r == int(r) else r


def _pct(new, old):
    """Percent change new vs old; None when old == 0 (undefined)."""
    if old is None or new is None or old == 0:
        return None
    return _r((new - old) / abs(old) * 100.0)


def _classify(columns, rows):
    """Split column indexes into (measures, period_idx, dims) by sampling values.
    Columns NAMED like identifiers (*_id, id) are never measures — totaling or scaling
    an id is nonsense even when its values are numeric."""
    measures, dims, period_idx = [], [], None
    for i in range(len(columns)):
        sample = [r[i] for r in rows[:200] if i < len(r) and r[i] is not None]
        if not sample:
            dims.append(i)
            continue
        name = str(columns[i]).lower()
        id_like = name == "id" or name.endswith("_id")
        n_num = sum(1 for v in sample if _num(v) is not None)
        n_per = sum(1 for v in sample if _is_period(v))
        if n_per >= 0.8 * len(sample) and period_idx is None:
            period_idx = i
        elif n_num >= 0.8 * len(sample) and not id_like:
            measures.append(i)
        else:
            dims.append(i)
    return measures[:_MAX_MEASURES], period_idx, dims


def _period_key(v):
    """Sortable string key for a period cell."""
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return str(v).strip()


def _growth_by_segment(columns, rows, period_idx, dim_idx, m0, max_segments=12):
    """Per-segment first->last growth over the period column (e.g. GMV per province per
    month). Surfaces WHERE the MOMENTUM is — the decision-relevant angle a raw ranking of
    current totals misses ('this region is #4 today but growing fastest'). Deterministic;
    every figure traces back to the summed cells. Returns None when there is no period×
    segment structure to compare."""
    agg = {}                                    # segment -> {period_key: summed measure}
    for r in rows:
        if dim_idx >= len(r) or period_idx >= len(r) or m0 >= len(r):
            continue
        v = _num(r[m0])
        if v is None:
            continue
        seg = str(r[dim_idx])
        pk = _period_key(r[period_idx])
        d = agg.setdefault(seg, {})
        d[pk] = d.get(pk, 0.0) + v
    all_periods = sorted({pk for d in agg.values() for pk in d})
    if len(all_periods) < 2 or len(agg) < 2:
        return None                             # need >=2 periods and >=2 segments to compare
    first_p, last_p = all_periods[0], all_periods[-1]
    segs = []
    for seg, d in agg.items():
        first, last = d.get(first_p), d.get(last_p)
        if first is None or last is None or not first:   # need both endpoints + a non-zero base for a %
            continue
        segs.append({"segment": seg, "first": _r(first), "last": _r(last),
                     "delta": _r(last - first), "pct_change": _pct(last, first)})
    segs = [s for s in segs if s["pct_change"] is not None]
    if len(segs) < 2:
        return None
    segs.sort(key=lambda s: s["pct_change"], reverse=True)
    # Headline picks (fastest growing/declining) consider only segments that are MATERIAL by
    # their ENDING size, so a segment that is tiny at BOTH ends can't be crowned on a
    # meaningless % — a "small now but surging" segment ENDS large and is kept, which is
    # exactly the signal we want. Floor is pegged to the MEDIAN last (robust to one dominant
    # segment, unlike a max-pegged floor that would mask smaller-but-surging segments).
    lasts = sorted(s["last"] for s in segs)
    med_last = lasts[len(lasts) // 2]
    material = [s for s in segs if s["last"] >= 0.01 * med_last]
    growers = [s for s in material if s["pct_change"] > 0]
    decliners = [s for s in material if s["pct_change"] < 0]
    # keep both extremes of the ranking even when the display list is capped
    shown = segs[:max_segments]
    if len(segs) > max_segments:
        shown = segs[:max_segments - 1] + [segs[-1]]
    out = {"period_column": str(columns[period_idx]), "dimension": str(columns[dim_idx]),
           "value_column": str(columns[m0]), "first_period": first_p, "last_period": last_p,
           "segments": shown}
    if growers:                                          # only label a grower when one exists
        out["fastest_growing"] = growers[0]
    if decliners:                                        # ...and a decliner only when one exists
        out["fastest_declining"] = decliners[-1]
    return out


def summarize(columns, rows):
    """Compact dict of PRE-COMPUTED metrics for one result set, or None when there is
    nothing numeric to summarize. Shape (all values deterministic):
      {row_count, measures: [{column,total,mean,min,max}], top/bottom (+share),
       trend: {period_column, first, last, delta, pct_change, direction, by_period[]},
       growth_by_segment: {dimension, fastest_growing, fastest_declining, segments[]},
       outliers: [...] }"""
    if not columns or not rows:
        return None
    rows = rows[:_MAX_ROWS]
    measures, period_idx, dims = _classify(columns, rows)
    if not measures:
        return None
    out = {"row_count": len(rows)}

    # per-measure totals
    stats = []
    for mi in measures:
        vals = [n for r in rows if mi < len(r) for n in [_num(r[mi])] if n is not None]
        if not vals:
            continue
        stats.append({"column": str(columns[mi]), "total": _r(sum(vals)), "mean": _r(sum(vals) / len(vals)),
                      "min": _r(min(vals)), "max": _r(max(vals))})
    if not stats:
        return None
    out["measures"] = stats
    m0 = measures[0]

    # top/bottom + share of total (needs a dimension to name the item)
    if dims and 2 <= len(rows) <= 1000:
        d0 = dims[0]
        keyed = [(r[d0] if d0 < len(r) else None, _num(r[m0]) if m0 < len(r) else None) for r in rows]
        keyed = [(k, v) for k, v in keyed if v is not None]
        if len(keyed) >= 2:
            keyed.sort(key=lambda kv: kv[1], reverse=True)
            total = sum(v for _, v in keyed)
            top_k, top_v = keyed[0]
            bot_k, bot_v = keyed[-1]
            out["top"] = {str(columns[d0]): str(top_k), str(columns[m0]): _r(top_v)}
            if total:
                out["top"]["share_of_total_pct"] = _r(top_v / total * 100.0)
            out["bottom"] = {str(columns[d0]): str(bot_k), str(columns[m0]): _r(bot_v)}

    # trend over the period column (values summed per period, periods sorted)
    if period_idx is not None:
        agg = {}
        for r in rows:
            if period_idx >= len(r) or m0 >= len(r):
                continue
            v = _num(r[m0])
            if v is None:
                continue
            k = _period_key(r[period_idx])
            agg[k] = agg.get(k, 0.0) + v
        if len(agg) >= 2:
            periods = sorted(agg)
            first, last = agg[periods[0]], agg[periods[-1]]
            delta = last - first
            by = []
            prev = None
            for p in periods[-12:]:                          # cap the detail at the last 12 periods
                item = {"period": p, "value": _r(agg[p])}
                if prev is not None:
                    item["delta_vs_prev"] = _r(agg[p] - prev)
                    item["pct_vs_prev"] = _pct(agg[p], prev)
                by.append(item)
                prev = agg[p]
            out["trend"] = {"period_column": str(columns[period_idx]), "value_column": str(columns[m0]),
                            "first_period": periods[0], "last_period": periods[-1],
                            "first": _r(first), "last": _r(last), "delta": _r(delta),
                            "pct_change": _pct(last, first),
                            "direction": "up" if delta > 0 else "down" if delta < 0 else "flat",
                            "by_period": by}
            fc = forecast([agg[p] for p in periods])
            if fc:
                out["trend"]["forecast"] = fc
        # per-segment growth: which dimension value gained/lost momentum over the periods
        # (needs period × dimension × measure — e.g. month_id × provinsi × gmv)
        if dims:
            gseg = _growth_by_segment(columns, rows, period_idx, dims[0], m0)
            if gseg:
                out["growth_by_segment"] = gseg

    # outliers on the first measure (IQR fences; only meaningful with enough points)
    vals = sorted(n for r in rows if m0 < len(r) for n in [_num(r[m0])] if n is not None)
    if len(vals) >= 8:
        q1 = vals[len(vals) // 4]
        q3 = vals[(3 * len(vals)) // 4]
        iqr = q3 - q1
        if iqr > 0:
            lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
            found = []
            label_idx = dims[0] if dims else period_idx
            for r in rows:
                v = _num(r[m0]) if m0 < len(r) else None
                if v is None or lo <= v <= hi:
                    continue
                item = {str(columns[m0]): _r(v)}
                if label_idx is not None and label_idx < len(r):
                    item[str(columns[label_idx])] = str(r[label_idx])
                found.append(item)
                if len(found) >= 5:
                    break
            if found:
                out["outliers"] = found
    return out


def forecast(series, horizon=3, min_points=4):
    """Deterministic linear projection of a per-period series (least squares over the last
    12 points). Returns {method, next: [values...], slope_per_period, note} or None when the
    series is too short. The projected values live in the derived block, so (a) the LLM can
    NARRATE a forecast without inventing numbers and (b) the verifier can trace them.
    Deliberately simple — a labelled straight-line projection, not a seasonal model."""
    pts = [_num(v) for v in (series or [])]
    pts = [p for p in pts if p is not None][-12:]
    n = len(pts)
    if n < min_points:
        return None
    xm = (n - 1) / 2.0
    ym = sum(pts) / n
    den = sum((i - xm) ** 2 for i in range(n))
    if den == 0:
        return None
    slope = sum((i - xm) * (y - ym) for i, y in enumerate(pts)) / den
    nxt = [_r(ym + slope * (n - 1 + h - xm)) for h in range(1, horizon + 1)]
    return {"method": "linear", "based_on_periods": n, "slope_per_period": _r(slope),
            "next": nxt,
            "note": "straight-line projection of the recent trend — an estimate, not a promise"}


# what-if: a conditional cue + a %-change phrase ("kalau harga naik 10%", "what if orders drop 20%")
_WHATIF_CUE = re.compile(
    r"\b(kalau|jika|seandainya|andai(?:kan)?|misal(?:nya|kan)?|what if|if|bagaimana (?:jika|kalau)|skenario|scenario)\b",
    re.I)
_WHATIF_PCT = re.compile(
    r"\b(naik|meningkat|bertambah|naikkan|increase[sd]?|rises?|risen|grows?|grown|up|"
    r"turun|menurun|berkurang|turunkan|decrease[sd]?|drops?|down)\b[^0-9%]{0,30}?(\d+(?:[.,]\d+)?)\s*%",
    re.I)
_WHATIF_UP = {"naik", "meningkat", "bertambah", "naikkan", "increase", "increased", "increases",
              "rise", "rises", "risen", "grow", "grows", "grown", "up"}


def parse_what_if(question):
    """{'pct', 'signed_pct', 'phrase'} when the question poses a hypothetical %-change
    (a conditional cue AND a direction+percent phrase), else None."""
    q = question or ""
    if not _WHATIF_CUE.search(q):
        return None
    m = _WHATIF_PCT.search(q)
    if not m:
        return None
    word = m.group(1).lower()
    try:
        pct = float(m.group(2).replace(",", "."))
    except Exception:
        return None
    if pct <= 0 or pct > 1000:
        return None
    sign = 1.0 if word in _WHATIF_UP else -1.0
    return {"pct": pct, "signed_pct": sign * pct, "phrase": m.group(0)}


def scenario(derived, signed_pct, phrase=""):
    """Deterministic what-if over the ALREADY-computed measure totals: scenario_total =
    current_total x (1 + pct/100) per measure. Lives inside the derived block, so the LLM
    can STATE simulated figures and the verifier can trace them (an LLM-computed scenario
    number would otherwise be flagged as untraceable). Naive linear scaling BY DESIGN —
    the note says so; it is a simulation aid, not a prediction model."""
    measures = (derived or {}).get("measures") or []
    factor = 1.0 + signed_pct / 100.0
    items = [{"column": m["column"], "current_total": m["total"],
              "scenario_total": _r(m["total"] * factor),
              "delta": _r(m["total"] * factor - m["total"])}
             for m in measures if m.get("total") is not None]
    if not items:
        return None
    return {"assumption_pct": _r(signed_pct), "assumption_phrase": phrase or ("%+g%%" % signed_pct),
            "measures": items,
            "note": ("simulasi aritmetika sederhana (total saat ini dikali faktor asumsi) — "
                     "sajikan JELAS sebagai simulasi/andai-andai, bukan prediksi")}


def compare_results(cols_a, rows_a, cols_b, rows_b, label_a="result_1", label_b="result_2"):
    """Deterministic comparison of TWO result sets (e.g. this month vs last month).
    Matches on the first shared dimension + shared numeric column when possible;
    always reports the totals delta. Returns None when either side has no numbers."""
    if not (cols_a and rows_a and cols_b and rows_b):
        return None
    ma, pa, da = _classify(cols_a, rows_a[:_MAX_ROWS])
    mb, pb, db = _classify(cols_b, rows_b[:_MAX_ROWS])
    if not ma or not mb:
        return None
    # prefer a measure column both sides share by NAME; else first measure of each
    names_a = {str(cols_a[i]).lower(): i for i in ma}
    shared_measure = next((str(cols_b[i]).lower() for i in mb if str(cols_b[i]).lower() in names_a), None)
    ia = names_a[shared_measure] if shared_measure else ma[0]
    ib = next(i for i in mb if str(cols_b[i]).lower() == shared_measure) if shared_measure else mb[0]

    def _tot(rows, mi):
        return sum(n for r in rows if mi < len(r) for n in [_num(r[mi])] if n is not None)

    ta, tb = _tot(rows_a[:_MAX_ROWS], ia), _tot(rows_b[:_MAX_ROWS], ib)
    out = {"measure": str(cols_a[ia]) if shared_measure else f"{cols_a[ia]} vs {cols_b[ib]}",
           label_a: {"total": _r(ta)}, label_b: {"total": _r(tb)},
           "delta": _r(tb - ta), "pct_change": _pct(tb, ta)}

    # per-key comparison over the first dimension both sides share by NAME
    dims_a = {str(cols_a[i]).lower(): i for i in da}
    shared_dim = next((str(cols_b[i]).lower() for i in db if str(cols_b[i]).lower() in dims_a), None)
    if shared_dim:
        ka = dims_a[shared_dim]
        kb = next(i for i in db if str(cols_b[i]).lower() == shared_dim)

        def _by_key(rows, ki, mi):
            agg = {}
            for r in rows:
                if ki >= len(r) or mi >= len(r):
                    continue
                v = _num(r[mi])
                if v is None:
                    continue
                agg[str(r[ki])] = agg.get(str(r[ki]), 0.0) + v
            return agg

        aa = _by_key(rows_a[:_MAX_ROWS], ka, ia)
        bb = _by_key(rows_b[:_MAX_ROWS], kb, ib)
        keys = set(aa) | set(bb)
        per = []
        for k in keys:
            va, vb = aa.get(k), bb.get(k)
            per.append({"key": k, label_a: _r(va), label_b: _r(vb),
                        "delta": _r((vb or 0.0) - (va or 0.0)),
                        "pct_change": _pct(vb, va) if va is not None and vb is not None else None})
        per.sort(key=lambda x: abs(x["delta"] or 0), reverse=True)
        out["dimension"] = shared_dim
        out["by_key"] = per[:20]                               # biggest movers first, capped
    return out
