"""Analytics Layer (Level-2 foundation): deterministic derived metrics, multi-result
evidence, numeric verification, and the confidence score.

Run:  cd backend && python -m pytest tests/test_analytics_layer.py -q

Fully offline — pure functions + monkeypatched connectors/LLM. These verify the
COMPUTATION and WIRING (derived block attached, all result sets kept, figures traced,
confidence aggregated), not model quality.
"""
import os, sys
from decimal import Decimal
from types import SimpleNamespace

import pytest  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

from app.agent import analytics                                    # noqa: E402
from app.agent.verifier import verify, compute_confidence           # noqa: E402
from app.agent import orchestrator as orch                          # noqa: E402
from app.skills import registry                                     # noqa: E402
from app.skills.registry import _evidence_text                      # noqa: E402


# ---------- analytics.summarize ----------
def test_summarize_totals_top_share():
    cols = ["marketplace", "gmv"]
    rows = [["Shopee", 600], ["Blibli", 300], ["TikTok", 100]]
    d = analytics.summarize(cols, rows)
    assert d["row_count"] == 3
    m = d["measures"][0]
    assert m["column"] == "gmv" and m["total"] == 1000 and m["min"] == 100 and m["max"] == 600
    assert d["top"]["marketplace"] == "Shopee" and d["top"]["share_of_total_pct"] == 60
    assert d["bottom"]["marketplace"] == "TikTok"


def test_summarize_trend_over_periods():
    cols = ["bulan", "omzet"]
    rows = [["2026-05", 100], ["2026-06", 150], ["2026-07", 120]]
    d = analytics.summarize(cols, rows)
    t = d["trend"]
    assert t["first"] == 100 and t["last"] == 120 and t["delta"] == 20
    assert t["pct_change"] == 20 and t["direction"] == "up"
    # period-over-period detail computed deterministically
    julys = [b for b in t["by_period"] if b["period"] == "2026-07"][0]
    assert julys["delta_vs_prev"] == -30 and julys["pct_vs_prev"] == -20


def test_summarize_handles_decimal_and_none_and_nonnumeric():
    cols = ["item", "qty"]
    rows = [["a", Decimal("2.5")], ["b", None], ["c", Decimal("1.5")]]
    d = analytics.summarize(cols, rows)
    assert d["measures"][0]["total"] == 4
    # no numeric columns at all -> nothing to derive
    assert analytics.summarize(["a", "b"], [["x", "y"]]) is None
    assert analytics.summarize([], []) is None


def test_summarize_outliers_iqr():
    cols = ["toko", "sales"]
    rows = [["t%d" % i, 100 + i] for i in range(10)] + [["outlet-x", 9999]]
    d = analytics.summarize(cols, rows)
    assert any(o.get("toko") == "outlet-x" for o in d["outliers"])


def test_compare_results_shared_dim_and_measure():
    a = (["marketplace", "gmv"], [["Shopee", 500], ["Blibli", 300]])
    b = (["marketplace", "gmv"], [["Shopee", 400], ["Blibli", 350], ["TikTok", 50]])
    c = analytics.compare_results(a[0], a[1], b[0], b[1])
    assert c["measure"] == "gmv"
    assert c["result_1"]["total"] == 800 and c["result_2"]["total"] == 800
    assert c["delta"] == 0
    shopee = [k for k in c["by_key"] if k["key"] == "Shopee"][0]
    assert shopee["delta"] == -100 and shopee["pct_change"] == -20
    tiktok = [k for k in c["by_key"] if k["key"] == "TikTok"][0]
    assert tiktok["result_1"] is None and tiktok["result_2"] == 50


def test_compare_results_none_without_numbers():
    assert analytics.compare_results(["a"], [["x"]], ["b"], [["y"]]) is None


# ---------- verifier: figures_traceable ----------
_GATE = SimpleNamespace(pii_request=False, complexity="simple")


def _sql_ev(rows, cols=("marketplace", "gmv"), derived=None):
    ev = {"ok": True, "columns": list(cols), "rows": rows, "safe_sql": "SELECT 1", "engine": "t"}
    if derived:
        ev["derived"] = derived
    return ev


def test_figures_traceable_passes_for_data_numbers():
    ev = {"sql": _sql_ev([["Shopee", 227093794.93], ["Blibli", 1547]])}
    vr = verify("q", "Total **227.093.794,93** dari Shopee dan **1.547** dari Blibli.", ev, _GATE)
    assert vr["checks"]["figures_traceable"] is True and vr["ok"]


def test_figures_traceable_tolerates_rounding_and_scale_words():
    ev = {"sql": _sql_ev([["Shopee", 227093794.93], ["Blibli", 1547]])}
    # "227 juta" (scale word) and "1.500" (rounded, within 5%) must both pass
    vr = verify("q", "Sekitar **227 juta** dari Shopee; Blibli kurang lebih 1.500.", ev, _GATE)
    assert vr["checks"]["figures_traceable"] is True


def test_figures_scale_abbreviation_ambiguity_not_flagged():
    # Indonesian finance writes "9,2 M" for MILIAR (1e9) but _SCALES reads "m" as juta
    # (1e6). The raw reading (9.2 < 100) must exempt it — a correct answer may not fail
    # on a wrong scale guess. Regression for the scaled-only candidate bug.
    ev = {"sql": _sql_ev([["Shopee", 9_200_000_000]])}
    vr = verify("q", "Revenue bulan ini **Rp9,2 M** dari Shopee.", ev, _GATE)
    assert vr["checks"]["figures_traceable"] is True


def test_figures_traceable_flags_invented_number():
    ev = {"sql": _sql_ev([["Shopee", 500], ["Blibli", 300]])}
    vr = verify("q", "Total penjualan mencapai **987.654** unit.", ev, _GATE)
    assert vr["checks"]["figures_traceable"] is False
    assert any("987.654" in i for i in vr["issues"])


def test_figures_small_years_percent_exempt():
    ev = {"sql": _sql_ev([["Shopee", 500]])}
    # 12 (%), 5 (rank), 2026 (year) are exempt even though not in the data
    vr = verify("q", "Naik 12% dibanding 2026, peringkat 5.", ev, _GATE)
    assert vr["checks"]["figures_traceable"] is True


def test_figures_inside_chart_blocks_ignored():
    ev = {"sql": _sql_ev([["Shopee", 500]])}
    ans = 'Ringkasan **500**.\n```chart\n{"labels":["x"],"data":[123456789]}\n```'
    vr = verify("q", ans, ev, _GATE)
    assert vr["checks"]["figures_traceable"] is True


def test_figures_grounded_by_derived_metrics():
    # a delta that exists only in the derived block (not a raw cell) still traces
    ev = {"sql": _sql_ev([["2026-06", 150], ["2026-07", 120]], cols=("bulan", "omzet"))}
    vr = verify("q", "Omzet turun **30** poin (-20%) dibanding bulan lalu.", ev, _GATE)
    assert vr["checks"]["figures_traceable"] is True


def test_figures_grounded_across_multiple_result_sets():
    ev = {"sql": _sql_ev([["Shopee", 500]]),
          "sql_results": [_sql_ev([["Blibli", 43210]]), _sql_ev([["Shopee", 500]])]}
    vr = verify("q", "Bulan lalu **43.210**, bulan ini **500**.", ev, _GATE)
    assert vr["checks"]["figures_traceable"] is True


def test_figures_yyyymm_period_key_exempt():
    # 202606 is a warehouse month key (a period coordinate the model echoes from its WHERE
    # filter), NOT a metric — it must not be flagged just because it is not a result cell.
    ev = {"sql": _sql_ev([["Shopee", 500]])}
    vr = verify("q", "Untuk periode **202606**, total Shopee **500**.", ev, _GATE)
    assert vr["checks"]["figures_traceable"] is True
    # a 6-digit number that is NOT a valid YYYYMM (month 13) is still checked
    vr2 = verify("q", "Angka aneh **201613** muncul.", ev, _GATE)
    assert vr2["checks"]["figures_traceable"] is False


def test_figures_delta_between_two_totals_traces():
    # GMV and NMV totals in one set; the model states their difference (a legitimate derived
    # figure from two validated totals) — it must trace even though the delta is no raw cell.
    ev = {"sql": _sql_ev([["Juni", 1218140247, 1095457665]],
                         cols=("bulan", "gmv", "nmv"))}
    vr = verify("q", "GMV **Rp1.218.140.247**, NMV **Rp1.095.457.665**; selisih **Rp122,68 juta**.",
                ev, _GATE)
    assert vr["checks"]["figures_traceable"] is True
    # the SUM of the two totals is also legitimate
    vr2 = verify("q", "Gabungan GMV+NMV kira-kira **Rp2,31 miliar**.", ev, _GATE)
    assert vr2["checks"]["figures_traceable"] is True
    # but an unrelated invented figure between the two still fails
    vr3 = verify("q", "Nilai misterius **777.777.777**.", ev, _GATE)
    assert vr3["checks"]["figures_traceable"] is False


def test_numbers_without_sql_still_fail_as_before():
    vr = verify("q", "Totalnya 123456.", {"docs": {"results": [
        {"filename": "sop.md", "text": "kebijakan refund", "citation": "sop.md#chunk0"}]}}, _GATE)
    assert vr["checks"]["numbers_from_sql"] is False


def test_verifier_tolerates_explicitly_missing_sql_evidence():
    vr = verify(
        "q", "Belum ada bukti SQL yang cukup.",
        {"sql": None, "coverage": {"missing": ["governed SQL evidence"]}},
        _GATE,
    )
    assert isinstance(vr, dict)
    assert vr["checks"]["grounded"] is False


# ---------- confidence ----------
def test_confidence_high_with_clean_sql():
    vr = {"ok": True, "issues": [], "checks": {"grounded": True, "citations_present": True,
                                               "numbers_from_sql": True, "figures_traceable": True,
                                               "pii_screened": True}}
    ev = {"sql": _sql_ev([["Shopee", 500]])}
    c = compute_confidence(vr, ev, _GATE)
    assert c["level"] == "high" and c["score"] >= 80 and c["signals"]["sql_rows"] == 1


def test_confidence_not_high_when_internal_sql_is_empty():
    vr = {"ok": True, "issues": [], "checks": {
        "grounded": True,
        "citations_present": True,
        "numbers_from_sql": True,
        "figures_traceable": True,
        "pii_screened": True,
    }}
    ev = {"sql": _sql_ev([])}
    c = compute_confidence(
        vr, ev, SimpleNamespace(pii_request=False, complexity="complex"),
    )
    assert c["level"] == "low"
    assert c["signals"]["sql_evidence_empty"] is True


def test_confidence_low_when_ungrounded():
    c = compute_confidence({"checks": {"grounded": False}}, {}, _GATE)
    assert c["level"] == "low" and c["signals"] == {"grounded": False}


def test_confidence_medium_docs_only_and_penalties():
    vr = {"ok": True, "checks": {"grounded": True, "citations_present": True,
                                 "numbers_from_sql": True, "pii_screened": True}}
    ev = {"docs": {"results": [{"filename": "a.md", "text": "x", "rerank_score": 1.2,
                                "citation": "a.md#chunk0"}]}}
    c = compute_confidence(vr, ev, _GATE)
    assert c["level"] == "high"
    assert c["signals"]["doc_rerank_top"] == pytest.approx(1.2)
    # weak retrieval -> lower
    ev["docs"]["results"][0]["rerank_score"] = -2.0
    c2 = compute_confidence(vr, ev, _GATE)
    assert c2["score"] < c["score"] and c2["level"] in ("medium", "low")


def test_confidence_drops_on_failed_checks_and_unmatched_figures():
    vr = {"ok": False, "unverified_figures": ["9.999", "8.888"],
          "checks": {"grounded": True, "citations_present": False,
                     "numbers_from_sql": True, "figures_traceable": False, "pii_screened": True}}
    ev = {"sql": _sql_ev([["Shopee", 500]])}
    c = compute_confidence(vr, ev, _GATE)
    assert c["level"] == "low"
    assert set(c["signals"]["checks_failed"]) == {"citations_present", "figures_traceable"}
    assert c["signals"]["unverified_figures"] == 2


# ---------- wiring: derived + multi-result evidence ----------
def test_execute_governed_attaches_derived(monkeypatch):
    class FakeConn:
        dialect = "duckdb"
        max_bytes_billed = None
        def dry_run(self, sql): return None
        def run(self, sql): return ["marketplace", "gmv"], [["Shopee", 600], ["Blibli", 400]]
    monkeypatch.setattr(registry, "make_connector", lambda t, c: FakeConn())
    monkeypatch.setattr(registry, "govern", lambda sql, d, m, p, **kw: sql)
    ctx = registry.Ctx(tenant_id="t", role="Admin", role_policy={})
    out = registry.execute_governed(ctx, {"type": "duckdb", "conn": {}, "mdl": {}}, "SELECT 1", "test")
    assert out["ok"] and out["derived"]["measures"][0]["total"] == 1000
    assert out["derived"]["top"]["marketplace"] == "Shopee"


def test_derived_computed_on_masked_rows(monkeypatch):
    """PII cells must be masked BEFORE analytics: the top-item label in the derived
    block (which enters the LLM prompt) must be the redaction token, never a name."""
    class FakeConn:
        dialect = "duckdb"
        max_bytes_billed = None
        def dry_run(self, sql): return None
        def run(self, sql): return ["customer_name", "total"], [["Budi", 900], ["Ani", 100]]
    monkeypatch.setattr(registry, "make_connector", lambda t, c: FakeConn())
    monkeypatch.setattr(registry, "govern", lambda sql, d, m, p, **kw: sql)
    ctx = registry.Ctx(tenant_id="t", role="Admin", role_policy={})
    mdl = {"pii_columns": ["customer_name"]}
    out = registry.execute_governed(ctx, {"type": "duckdb", "conn": {}, "mdl": mdl}, "SELECT 1", "test")
    assert out["ok"]
    top = out["derived"].get("top") or {}
    assert "Budi" not in str(out["derived"])
    assert top.get("customer_name") == "[PII:customer_name]"


def test_evidence_text_renders_all_result_sets_and_comparison():
    r1 = _sql_ev([["Shopee", 500], ["Blibli", 300]])
    r2 = _sql_ev([["Shopee", 400], ["Blibli", 350]])
    txt = _evidence_text({"sql": r2, "sql_results": [r1, r2]}, {})
    assert "SQL RESULT 1/2" in txt and "SQL RESULT 2/2" in txt
    assert "COMPARISON result_1 vs result_2" in txt
    # single result set keeps the old header (no regression)
    txt1 = _evidence_text({"sql": r1}, {})
    assert "SQL RESULT (validated" in txt1 and "1/1" not in txt1


def test_sql_view_exposes_extra_results_and_derived():
    r1 = _sql_ev([["Shopee", 500]], derived={"row_count": 1})
    r2 = _sql_ev([["Blibli", 300]], derived={"row_count": 1})
    r1["generated_sql"] = r2["generated_sql"] = "SELECT 1"
    view = orch._sql_view(r2, [r1, r2])
    assert view["ok"] and view["derived"] == {"row_count": 1}
    assert len(view["results"]) == 1 and view["results"][0]["row_count"] == 1
    # single result -> no extra key (old shape preserved)
    v1 = orch._sql_view(r1, [r1])
    assert "results" not in v1


def test_evidence_summary_bundles_all_sources():
    ev = {"sql": _sql_ev([["Shopee", 500]]),
          "docs": {"results": [{"filename": "a.md", "text": "x", "citation": "a.md#chunk0",
                                "citation_label": "a.md — bagian 1", "rerank_score": 0.7}]},
          "memory": {"results": [{"text": "user suka ringkasan singkat"}]}}
    s = orch._evidence_summary(ev)
    types = [e["type"] for e in s]
    assert types == ["sql", "document", "memory"]
    assert s[0]["row_count"] == 1
    assert s[1]["label"] == "a.md — bagian 1"
    assert s[1]["score"] == pytest.approx(0.7)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
