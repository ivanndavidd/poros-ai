"""Level 4 (Advisor): prescriptive intent, ranked-recommendation contract, deterministic
forecast, and cross-datasource queries in the chat agent (each governed per source).

Run:  cd backend && python -m pytest tests/test_advisor.py -q
Fully offline — llm / sql_agent are monkeypatched.
"""
import json
import os, sys
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

from app.llm import gatekeeper                        # noqa: E402
from app.agent import analytics, orchestrator, sql_agent  # noqa: E402
from app.agent.verifier import verify                 # noqa: E402
from app.skills import registry                       # noqa: E402
from app.skills.registry import Ctx                   # noqa: E402

_GATE = SimpleNamespace(pii_request=False, complexity="simple")


def _sql_ev(rows, cols=("marketplace", "gmv"), derived=None):
    ev = {"ok": True, "columns": list(cols), "rows": rows, "safe_sql": "SELECT 1", "engine": "t"}
    if derived:
        ev["derived"] = derived
    return ev


def _ctx(q="bagaimana cara meningkatkan GMV marketplace?", **kw):
    return Ctx(tenant_id="t_x", role="Admin", role_policy={}, question=q,
               answer_purpose="answer_simple", **kw)


def _llm_result(text):
    return SimpleNamespace(text=text, model="fake", provider="fake",
                           prompt_tokens=1, completion_tokens=1, cost=0.0)


# ---- gatekeeper: prescriptive intent ----
def test_heuristic_flags_improvement_questions_as_prescriptive():
    for q in ("bagaimana cara meningkatkan GMV marketplace?", "how can we improve retention?",
              "apa yang harus dilakukan supaya penjualan naik?", "rekomendasi untuk menaikkan omzet"):
        g = gatekeeper._heuristic(q)
        assert g.intent == "prescriptive" and g.complexity == "complex", q
    assert gatekeeper._heuristic("berapa total gmv shopee").intent == "data_query"
    # WHY beats IMPROVE when both appear (root cause first)
    assert gatekeeper._heuristic("kenapa GMV turun dan bagaimana meningkatkannya?").intent == "diagnostic"


def test_screen_keeps_heuristic_prescriptive_when_model_says_data_query(monkeypatch):
    verdict = json.dumps({"allow": True, "intent": "data_query", "complexity": "complex",
                          "injection": False, "pii_request": False, "reasons": []})
    monkeypatch.setattr(gatekeeper.gateway, "complete", lambda *a, **k: _llm_result(verdict))
    assert gatekeeper.screen("t_x", "bagaimana meningkatkan penjualan?").intent == "prescriptive"


# ---- analytics: deterministic forecast ----
def test_forecast_linear_series():
    fc = analytics.forecast([10, 20, 30, 40])
    assert fc["method"] == "linear" and fc["slope_per_period"] == 10
    assert fc["next"] == [50, 60, 70]


def test_forecast_needs_min_points_and_attaches_to_trend():
    assert analytics.forecast([10, 20, 30]) is None            # < 4 points -> no projection
    rows = [[f"2026-0{m}", 100 * m] for m in range(1, 6)]      # 5 clean monthly periods
    d = analytics.summarize(["bulan", "gmv"], rows)
    assert d["trend"]["forecast"]["next"] == [600, 700, 800]   # traceable via derived
    rows3 = rows[:3]
    d3 = analytics.summarize(["bulan", "gmv"], rows3)
    assert "forecast" not in (d3.get("trend") or {})


# ---- advisor contract injection ----
def _capture_sys(monkeypatch):
    captured = {}

    def fake_complete(tid, purpose, messages, **kw):
        captured["sys"] = messages[0]["content"]
        return _llm_result("jawaban")

    monkeypatch.setattr(registry.llm, "complete", fake_complete)
    return captured


def test_advisor_block_injected_for_prescriptive_and_diagnostic(monkeypatch):
    ev = {"sql": {"ok": True, "columns": ["a"], "rows": [[1]], "safe_sql": "S"}}
    cap = _capture_sys(monkeypatch)
    registry._synthesize(_ctx(advise=True), ev)
    assert "ADVISOR MODE" in cap["sys"] and "RANKED" in cap["sys"]
    registry._synthesize(_ctx(q="kenapa turun?", diagnostic=True), ev)
    assert "ADVISOR MODE" in cap["sys"]                        # advice follows root cause
    registry._synthesize(_ctx(q="berapa total gmv"), ev)
    assert "ADVISOR MODE" not in cap["sys"]                    # plain lookups keep the old contract


def test_sql_agent_prompt_gets_advisor_guidance_from_the_flag(monkeypatch):
    seen = {}

    def fake_tools(tid, purpose, messages, tools, **kw):
        seen["sys"] = messages[0]["content"]
        return SimpleNamespace(text="ok", model="fake", provider="fake",
                               prompt_tokens=1, completion_tokens=1, cost=0.0, tool_calls=[])

    monkeypatch.setattr(sql_agent.llm, "complete_tools", fake_tools)
    sql_agent.run_sql_agent(_ctx(advise=True), {"mdl": {"tables": {}}})
    assert "ADVISOR MODE" in seen["sys"]
    sql_agent.run_sql_agent(_ctx(), {"mdl": {"tables": {}}})
    assert "ADVISOR MODE" not in seen["sys"]


# ---- cross-datasource queries in the chat agent ----
_DS_A = {"id": "ds_sales", "name": "Sales", "type": "bigquery",
         "mdl": {"tables": {"orders": ["gmv", "marketplace"]}}}
_DS_B = {"id": "ds_inv", "name": "Inventory", "type": "bigquery",
         "mdl": {"tables": {"stock": ["qty", "warehouse"]}}}


def test_agent_queries_another_datasource_governed_against_that_source(monkeypatch):
    """What the bounded planner did (a step per datasource) the chat agent now does in
    one loop: run_sql takes a datasource id, and each query is executed and PII-masked
    with THAT source's MDL."""
    seen, used = {}, []
    calls = iter([
        [{"id": "1", "name": "run_sql", "arguments": {"sql": "SELECT gmv FROM orders"}}],
        [{"id": "2", "name": "run_sql",
          "arguments": {"sql": "SELECT qty FROM stock", "datasource": "ds_inv"}}],
        [{"id": "3", "name": "run_sql",
          "arguments": {"sql": "SELECT 1", "datasource": "ds_hallucinated"}}],
        [],
    ])

    def fake_tools(tid, purpose, messages, tools, **kw):
        seen.setdefault("sys", messages[0]["content"])
        seen.setdefault("tools", tools)
        seen["last"] = messages[-1]["content"]
        tool_calls = next(calls)
        return SimpleNamespace(text="" if tool_calls else "jawaban", model="fake",
                               provider="fake", prompt_tokens=1, completion_tokens=1,
                               cost=0.0, tool_calls=tool_calls)

    def fake_exec(ctx, ds, sql, engine):
        used.append(ds["id"])
        return {"ok": True, "columns": ["x"], "rows": [[1]], "safe_sql": sql,
                "generated_sql": sql, "engine": engine, "datasource_id": ds["id"]}

    monkeypatch.setattr(sql_agent.llm, "complete_tools", fake_tools)
    monkeypatch.setattr(sql_agent, "execute_governed", fake_exec)
    out = sql_agent.run_sql_agent(_ctx(advise=True), _DS_A, others=[_DS_B])
    assert used == ["ds_sales", "ds_inv"]                      # the bad id never ran
    assert "Unknown datasource" in seen["last"]
    assert "ds_inv (Inventory): tables stock" in seen["sys"]
    run_sql = next(t for t in seen["tools"] if t["function"]["name"] == "run_sql")
    assert "datasource" in run_sql["function"]["parameters"]["properties"]
    assert len(out["results"]) == 2 and out["answer"] == "jawaban"


def test_yyyymm_ints_are_periods_and_ids_never_measures():
    # month_id (YYYYMM int) = PERIOD -> trend + forecast work on it and it is never
    # totaled/scaled; numeric *_id columns are dimensions, not measures
    rows = [[202603, 100.0], [202604, 200.0], [202605, 300.0], [202606, 400.0]]
    d = analytics.summarize(["month_id", "gmv"], rows)
    assert [m["column"] for m in d["measures"]] == ["gmv"]      # month_id NOT a measure
    assert d["trend"]["period_column"] == "month_id"
    assert d["trend"]["forecast"]["next"] == [500, 600, 700]
    sc = analytics.scenario(d, 15.0)
    assert [m["column"] for m in sc["measures"]] == ["gmv"]     # scenario skips month_id


# ---- what-if scenario (deterministic, traceable) ----
def test_parse_what_if_variants():
    up = analytics.parse_what_if("kalau harga naik 10% berapa GMV?")
    assert up and up["signed_pct"] == pytest.approx(10.0)
    down = analytics.parse_what_if("jika pesanan turun 5,5% bulan depan?")
    assert down and down["signed_pct"] == pytest.approx(-5.5)
    en = analytics.parse_what_if("what if orders drop 20%?")
    assert en and en["signed_pct"] == pytest.approx(-20.0)
    assert analytics.parse_what_if("berapa GMV bulan ini") is None       # no conditional cue
    assert analytics.parse_what_if("kenapa GMV naik dibanding Mei?") is None  # cue-less %-free


def test_scenario_scales_measure_totals():
    derived = {"measures": [{"column": "gmv", "total": 1000}, {"column": "qty", "total": 50}]}
    sc = analytics.scenario(derived, -10.0, "turun 10%")
    assert sc["measures"][0]["scenario_total"] == 900
    assert sc["measures"][0]["delta"] == -100
    assert "simulasi" in sc["note"]
    assert analytics.scenario({"measures": []}, 10.0) is None


def test_scenario_figures_are_traceable_by_verifier():
    derived = {"measures": [{"column": "gmv", "total": 1000000}],
               "scenario": {"assumption_pct": 10, "measures": [
                   {"column": "gmv", "current_total": 1000000, "scenario_total": 1100000,
                    "delta": 100000}]}}
    ev = {"sql": _sql_ev([["x", 1000000]], cols=("k", "gmv"), derived=derived)}
    vr = verify("kalau naik 10%?", "Jika naik 10%, GMV menjadi sekitar **1.100.000**.", ev, _GATE)
    assert vr["checks"]["figures_traceable"] is True


def test_docs_irrelevant_gate():
    # reranker says every hit is irrelevant -> drop (data question must not be forced to cite)
    bad = {"results": [{"citation": "a#chunk0", "rerank_score": -3.2},
                       {"citation": "b#chunk1", "rerank_score": -1.1}]}
    good = {"results": [{"citation": "a#chunk0", "rerank_score": -3.2},
                        {"citation": "b#chunk1", "rerank_score": 0.7}]}
    nosignal = {"results": [{"citation": "a#chunk0"}]}          # offline embedder: keep
    assert orchestrator._docs_irrelevant(bad) is True
    assert orchestrator._docs_irrelevant(good) is False
    assert orchestrator._docs_irrelevant(nosignal) is False
    assert orchestrator._docs_irrelevant({"results": []}) is False


def test_evidence_masks_pii_per_result_source():
    # result from source B carries B's pii_columns — masked even though the caller's mdl
    # (source A) doesn't flag that column
    rb = {"ok": True, "columns": ["customer_name", "qty"], "rows": [["Budi", 5]],
          "safe_sql": "S2", "pii_columns": ["customer_name"]}
    ra = {"ok": True, "columns": ["gmv"], "rows": [[100]], "safe_sql": "S1", "pii_columns": []}
    text = registry._evidence_text({"sql": rb, "sql_results": [ra, rb]}, mdl={"pii_columns": []})
    assert "Budi" not in text and "[PII:customer_name]" in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
