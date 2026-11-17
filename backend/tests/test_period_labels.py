"""Period labels must name exactly the dates their rows cover (app/period_labels.py).

The positive cases are the live Acme queries that produced wrong answers; the negative
cases are live Acme queries that used the same CASE/ELSE shape CORRECTLY — the check is
only worth having if it stays quiet on those. Fully offline: no model, no warehouse.
"""
import datetime as dt
from types import SimpleNamespace

import pytest

from app import period_labels
from app.agent import research_agent, sql_agent
from app.nl2sql import PERIOD_SQL_INSTRUCTIONS
from app.skills import registry
from app.skills.registry import Ctx

MTD_QUESTION = ("Evaluasi Oktober menggunakan perbandingan month-to-date versus "
                "month-to-date September")
FILTERS = ("flagging_client_join = 'include' "
           "AND LOWER(order_status) IN ('diproses/selesai', 'unpaid')")

# 2026-10-07, trace 93764877: the ELSE bucket summed ALL of September -> "-82.72%".
LIVE_ELSE_WHOLE_MONTH = (
    "SELECT CASE WHEN order_date BETWEEN CAST('2026-10-01' AS DATE) AND CAST('2026-10-07' AS DATE) "
    "THEN 'Oct MTD 1-7' ELSE 'Sep MTD 1-7' END AS period, SUM(gmv) AS gmv, SUM(nmv) AS nmv, "
    "COUNT(DISTINCT order_id) AS orders, SAFE_DIVIDE(SUM(nmv), COUNT(DISTINCT order_id)) AS aov "
    "FROM `acme_L3.acme_store_order_mp_ai_curated` "
    "WHERE order_date BETWEEN CAST('2026-09-01' AS DATE) AND CAST('2026-10-07' AS DATE) "
    f"AND {FILTERS} GROUP BY period ORDER BY period LIMIT 10000")

# 2026-09-29, trace 1e92d130: a three-month SUM labelled an average -> "-55%" (truth +34.8%).
LIVE_SUM_AS_AVERAGE = (
    "SELECT CASE WHEN order_date >= CAST('2026-09-01' AS DATE) AND order_date < CAST('2026-10-01' AS DATE) "
    "THEN 'Bulan ini (September 2026)' ELSE 'Rata-rata 3 bulan terakhir (Juni–Agustus 2026)' END "
    "AS periode, SUM(nmv) AS nmv FROM acme_store_order_mp_ai_curated "
    f"WHERE {FILTERS} AND order_date >= CAST('2026-06-01' AS DATE) "
    "AND order_date < CAST('2026-10-01' AS DATE) GROUP BY 1 LIMIT 10000")

# 2026-10-08, job_b18e19: correct windows, but asked ON Oct 8 — day 8 was still filling up.
LIVE_MTD_THROUGH_TODAY = (
    "SELECT CASE WHEN order_date BETWEEN CAST('2026-10-01' AS DATE) AND CAST('2026-10-08' AS DATE) "
    "THEN 'Oktober MTD 1-8' ELSE 'September MTD 1-8' END AS periode, SUM(gmv) AS total_gmv "
    f"FROM `acme_store_order_mp_ai_curated` WHERE {FILTERS} "
    "AND ((order_date BETWEEN CAST('2026-10-01' AS DATE) AND CAST('2026-10-08' AS DATE)) "
    "OR (order_date BETWEEN CAST('2026-09-01' AS DATE) AND CAST('2026-09-08' AS DATE))) "
    "GROUP BY periode ORDER BY periode LIMIT 10000")

CONDITIONAL_AGG_MTD = (
    "SELECT marketplace, "
    "SUM(CASE WHEN order_date BETWEEN CAST('2026-10-01' AS DATE) AND CAST('2026-10-08' AS DATE) "
    "THEN gmv ELSE 0 END) AS gmv_oktober_mtd, "
    "SUM(CASE WHEN order_date BETWEEN CAST('2026-09-01' AS DATE) AND CAST('2026-09-08' AS DATE) "
    "THEN gmv ELSE 0 END) AS gmv_september_mtd FROM t "
    "WHERE order_date BETWEEN CAST('2026-09-01' AS DATE) AND CAST('2026-10-08' AS DATE) GROUP BY 1")

FIXED_MTD = (
    "SELECT CASE WHEN order_date BETWEEN '2026-10-01' AND '2026-10-07' THEN 'Oct 1-7' "
    "WHEN order_date BETWEEN '2026-09-01' AND '2026-09-07' THEN 'Sep 1-7' END AS period, "
    "SUM(gmv) AS gmv FROM sales WHERE (order_date BETWEEN '2026-10-01' AND '2026-10-07') "
    "OR (order_date BETWEEN '2026-09-01' AND '2026-09-07') GROUP BY 1")


WAREHOUSE_QUESTION = "ekspansi gudang"


def _ctx(question, **extra):
    return Ctx(tenant_id="t", role="Admin", role_policy={"full_access": True},
               question=question, answer_purpose="answer_complex", **extra)


def _check(sql, question="", today=dt.date(2026, 10, 7)):
    return period_labels.violation(sql, "bigquery", question, today)


# ---- the live wrong answers are refused, with the fix spelled out ----
def test_else_bucket_wider_than_its_label_is_refused():
    msg = _check(LIVE_ELSE_WHOLE_MONTH, MTD_QUESTION)
    assert "'Sep MTD 1-7' covers 2026-09-01..2026-09-30" in msg
    assert "names 2026-09-01..2026-09-07" in msg
    assert "ELSE" in msg and "explicit start and end dates" in msg
    assert len(msg) < 500          # sql_agent feeds back at most 500 characters


def test_sum_labelled_as_average_is_refused():
    msg = _check(LIVE_SUM_AS_AVERAGE, "rata2 NMV 3 bulan terakhir", dt.date(2026, 9, 29))
    assert "labelled an average" in msg and "only sums" in msg
    assert "SUM(x) / number of months" in msg


def test_month_to_date_counts_through_yesterday():
    msg = _check(LIVE_MTD_THROUGH_TODAY, MTD_QUESTION, dt.date(2026, 10, 8))
    assert "through yesterday (2026-10-07)" in msg
    # the same SQL a day later is a complete-days comparison
    assert _check(LIVE_MTD_THROUGH_TODAY, MTD_QUESTION, dt.date(2026, 10, 9)) is None
    # and conditional aggregation (labels are the aliases) is held to the same rule
    assert "through yesterday" in _check(CONDITIONAL_AGG_MTD, MTD_QUESTION, dt.date(2026, 10, 8))


@pytest.mark.parametrize("question", [
    "GMV month-to-date sampai hari ini vs September",
    "MTD sampai tanggal 8 dibanding September",
    "bandingkan MTD 1-8 Oktober vs 1-8 September",
])
def test_an_end_the_asker_stated_wins_over_yesterday(question):
    assert _check(LIVE_MTD_THROUGH_TODAY, question, dt.date(2026, 10, 8)) is None


def test_today_rule_needs_a_month_to_date_question():
    assert _check(LIVE_MTD_THROUGH_TODAY, "gmv oktober vs september", dt.date(2026, 10, 8)) is None


def test_mtd_buckets_must_cover_the_same_day_numbers():
    sql = ("SELECT SUM(CASE WHEN d BETWEEN '2026-10-01' AND '2026-10-07' THEN gmv END) AS gmv_oct_mtd, "
           "SUM(CASE WHEN d BETWEEN '2026-09-01' AND '2026-09-30' THEN gmv END) AS gmv_sep_mtd "
           "FROM t WHERE d BETWEEN '2026-09-01' AND '2026-10-07'")
    msg = _check(sql, today=dt.date(2026, 10, 8))
    assert "MTD buckets differ" in msg and "same day numbers" in msg


def test_alias_label_wider_than_its_window_is_refused():
    sql = ("SELECT SUM(CASE WHEN d >= '2026-09-01' THEN gmv END) AS gmv_sep, "
           "SUM(CASE WHEN d >= '2026-10-01' THEN gmv END) AS gmv_oct "
           "FROM t WHERE d BETWEEN '2026-09-01' AND '2026-10-07'")
    assert "'gmv_sep' covers 2026-09-01..2026-10-07" in _check(sql, today=dt.date(2026, 10, 8))


def test_else_without_any_date_filter_sweeps_all_history():
    sql = ("SELECT CASE WHEN order_date >= '2026-10-01' THEN 'Oktober' ELSE 'September' END AS p, "
           "SUM(gmv) FROM t GROUP BY 1")
    assert "'September' covers (no start)..2026-09-30" in _check(sql, today=dt.date(2026, 10, 8))


# ---- the same shapes used CORRECTLY on staging stay quiet ----
@pytest.mark.parametrize("sql,question,today", [
    # 2026-08-19 c4c2e3e6: full July vs Aug 1-17, honestly labelled
    ("SELECT brand, CASE WHEN order_date < CAST('2026-08-01' AS DATE) THEN 'Juli 1-31' "
     "ELSE 'Agustus 1-17' END AS periode, SUM(gmv) AS gmv FROM t "
     "WHERE order_date >= CAST('2026-07-01' AS DATE) AND order_date < CAST('2026-08-18' AS DATE) "
     "AND brand IN ('SMO', 'SMN') GROUP BY brand, periode",
     "bandingkan performa Agustus 2026 dengan Juli 2026", dt.date(2026, 8, 19)),
    # 2026-08-19 9c01906a: ISO-range labels, OR-ed windows
    ("SELECT CASE WHEN order_date BETWEEN CAST('2026-07-01' AS DATE) AND CAST('2026-07-17' AS DATE) "
     "THEN '2026-07-01–2026-07-17' ELSE '2026-08-01–2026-08-17' END AS periode, SUM(gmv) FROM t "
     "WHERE brand IN ('SMO') AND ((order_date BETWEEN CAST('2026-07-01' AS DATE) AND "
     "CAST('2026-07-17' AS DATE)) OR (order_date BETWEEN CAST('2026-08-01' AS DATE) AND "
     "CAST('2026-08-17' AS DATE))) GROUP BY 1",
     "bandingkan periode 1-17 Juli 2026 vs 1-17 Agustus 2026", dt.date(2026, 8, 19)),
    # 2026-08-07 ce10f3a8: YYYYMM month keys
    ("SELECT CASE WHEN month_id = 202608 THEN '202608_partial' ELSE '202601_202607' END AS scope, "
     "SUM(gmv) FROM t WHERE brand = 'PHILIPS' AND month_id BETWEEN 202601 AND 202608 GROUP BY 1",
     "sales philips ytd 2026", dt.date(2026, 8, 7)),
    # 2026-07-31 4bc82352: year labels over a YTD window
    ("SELECT province, CASE WHEN order_date < CAST('2026-01-01' AS DATE) THEN '2025' ELSE '2026' END "
     "AS period, SUM(gmv) FROM t WHERE order_date >= CAST('2025-01-01' AS DATE) "
     "AND order_date < CAST('2026-08-01' AS DATE) AND NOT province IS NULL GROUP BY 1, 2",
     WAREHOUSE_QUESTION, dt.date(2026, 7, 31)),
    # 2026-09-01 4fa48dae: window lives in a CTE, 'other_days' names no date
    ("WITH d AS (SELECT order_date, SUM(gmv) AS gmv FROM t WHERE order_date >= "
     "CAST('2026-07-01' AS DATE) AND order_date < CAST('2026-08-01' AS DATE) GROUP BY 1) "
     "SELECT CASE WHEN order_date = CAST('2026-07-07' AS DATE) THEN 'outlier_2026_07_07' "
     "ELSE 'other_days' END AS grp, SUM(gmv), AVG(gmv) FROM d GROUP BY 1",
     "review this month", dt.date(2026, 9, 1)),
    # 2026-08-19 tiktok + 2026-08-03 brodo: ISO ranges with a short end
    ("SELECT CASE WHEN video_creation_date BETWEEN CAST('2026-07-01' AS DATE) AND "
     "CAST('2026-07-17' AS DATE) THEN '2026-07-01_to_17' WHEN video_creation_date BETWEEN "
     "CAST('2026-08-01' AS DATE) AND CAST('2026-08-17' AS DATE) THEN '2026-08-01_to_17' END "
     "AS period, SUM(gmv) FROM t GROUP BY 1",
     "apakah ada perubahan signifikan pada traffic tiktok?", dt.date(2026, 8, 19)),
    ("SELECT CASE WHEN DATE(Order_Date) BETWEEN CAST('2026-07-06' AS DATE) AND "
     "CAST('2026-07-12' AS DATE) THEN '2026-07-06_to_07-12' WHEN DATE(Order_Date) BETWEEN "
     "CAST('2026-07-27' AS DATE) AND CAST('2026-08-02' AS DATE) THEN '2026-07-27_to_08-02' END "
     "AS week_bucket, COUNT(*) FROM t WHERE DATE(Order_Date) BETWEEN CAST('2026-07-06' AS DATE) "
     "AND CAST('2026-08-02' AS DATE) GROUP BY 1", "performance review of brodo",
     dt.date(2026, 8, 3)),
    # 2026-08-18: '2026-07-01' keys the July bucket (first ten days of each month)
    ("SELECT brand, CASE WHEN order_date BETWEEN CAST('2026-07-01' AS DATE) AND "
     "CAST('2026-07-10' AS DATE) THEN '2026-07-01' WHEN order_date BETWEEN "
     "CAST('2026-08-01' AS DATE) AND CAST('2026-08-10' AS DATE) THEN '2026-08-01' END AS period, "
     "SUM(gmv) FROM t WHERE order_date BETWEEN CAST('2026-07-01' AS DATE) AND "
     "CAST('2026-08-10' AS DATE) GROUP BY 1, 2",
     "brand apa yang mengalami sales drop month on month?", dt.date(2026, 8, 18)),
    # 2026-07-30: '2025_plus' is open-ended on purpose; no date filter at all on June/July
    ("SELECT province, COUNT(DISTINCT CASE WHEN month_id >= 202501 THEN month_id END) "
     "AS active_months_2025_plus, SUM(CASE WHEN month_id >= 202501 AND month_id < 202601 "
     "THEN gmv ELSE 0 END) AS gmv_2025 FROM t GROUP BY 1", WAREHOUSE_QUESTION, dt.date(2026, 7, 30)),
    ("SELECT marketplace, SUM(CASE WHEN order_date >= CAST('2026-06-01' AS DATE) AND order_date < "
     "CAST('2026-07-01' AS DATE) THEN gmv ELSE 0 END) AS june_gmv, SUM(CASE WHEN order_date >= "
     "CAST('2026-07-01' AS DATE) AND order_date < CAST('2026-08-01' AS DATE) THEN gmv ELSE 0 END) "
     "AS july_gmv FROM t GROUP BY 1", "GMV proportion between June and July 2026",
     dt.date(2026, 9, 1)),
    # 2026-08-19 H1 YoY: aliases name a year; a projection with several CASEs is a ratio
    ("SELECT brand, SUM(CASE WHEN order_date >= CAST('2025-01-01' AS DATE) AND order_date < "
     "CAST('2025-07-01' AS DATE) THEN gmv ELSE 0 END) AS h1_2025_gmv, SAFE_DIVIDE(SUM(CASE WHEN "
     "order_date >= CAST('2026-01-01' AS DATE) THEN gmv ELSE 0 END), SUM(CASE WHEN order_date < "
     "CAST('2026-01-01' AS DATE) THEN gmv ELSE 0 END)) AS yoy_growth FROM t WHERE order_date >= "
     "CAST('2025-01-01' AS DATE) AND order_date < CAST('2026-07-01' AS DATE) GROUP BY 1",
     "largest increase in H1 GMV 2026 vs YoY", dt.date(2026, 8, 19)),
    # 2026-07-31: '2025_JanJul' (the word is not a month name; the year still bounds it)
    ("SELECT province, period, SUM(gmv) FROM (SELECT province, CASE WHEN order_date >= "
     "CAST('2025-01-01' AS DATE) AND order_date < CAST('2025-08-01' AS DATE) THEN '2025_JanJul' "
     "WHEN order_date >= CAST('2026-01-01' AS DATE) AND order_date < CAST('2026-08-01' AS DATE) "
     "THEN '2026_JanJul' END AS period, SUM(gmv) AS gmv FROM t GROUP BY 1, 2) "
     "WHERE NOT period IS NULL GROUP BY 1, 2", WAREHOUSE_QUESTION, dt.date(2026, 7, 31)),
    # 2026-10-08 job_b18e19 asked a day later: equal windows, complete days
    (LIVE_MTD_THROUGH_TODAY, MTD_QUESTION, dt.date(2026, 10, 9)),
    # the corrected shape the instructions ask for
    (FIXED_MTD, MTD_QUESTION, dt.date(2026, 10, 8)),
    # non-date CASEs
    ("SELECT CASE WHEN paid_date IS NULL THEN 'unpaid' ELSE 'paid' END AS b, SUM(gmv) FROM t "
     "WHERE order_date >= CAST('2026-01-01' AS DATE) GROUP BY 1", "", dt.date(2026, 8, 7)),
    ("SELECT CASE WHEN total_orders >= 100 THEN '>=100' ELSE '<100' END AS b FROM t "
     "WHERE order_date >= CAST('2026-01-01' AS DATE)", "", dt.date(2026, 9, 24)),
    # a monthly key cannot express month-to-date, so the today rule does not apply
    ("SELECT SUM(gmv) FROM t WHERE month_id = 202610", MTD_QUESTION, dt.date(2026, 10, 8)),
])
def test_correct_live_shapes_stay_quiet(sql, question, today):
    assert _check(sql, question, today) is None


def test_unreadable_sql_is_never_blocked_here(monkeypatch):
    assert _check("SELEC broken ((") is None

    def boom(*_a, **_k):
        raise RuntimeError("checker bug")

    monkeypatch.setattr(period_labels, "_buckets", boom)
    assert _check(LIVE_ELSE_WHOLE_MONTH, MTD_QUESTION) is None


# ---- wiring: refused before govern and the billed dry-run ----
class _Conn:
    dialect = "bigquery"
    max_bytes_billed = None

    def __init__(self):
        self.dry_runs, self.runs = [], []

    def dry_run(self, sql):
        self.dry_runs.append(sql)

    def run(self, sql):
        self.runs.append(sql)
        return ["period", "gmv"], [["Oct 1-7", 100], ["Sep 1-7", 90]]


def _ds():
    return {"id": "ds_sales", "name": "Sales", "type": "duckdb", "conn": {},
            "mdl": {"tables": {"sales": ["order_date", "gmv"]}, "allowed_tables": ["sales"],
                    "pii_columns": []}}


@pytest.fixture
def oct_8(monkeypatch):
    monkeypatch.setattr(period_labels, "_today", lambda: dt.date(2026, 10, 8))


@pytest.mark.usefixtures("oct_8")
def test_execute_governed_refuses_before_govern_and_dry_run(monkeypatch):
    conn, governed = _Conn(), []
    monkeypatch.setattr(registry, "make_connector", lambda t, c: conn)
    monkeypatch.setattr(registry, "govern", lambda sql, *a, **k: governed.append(sql) or sql)
    out = registry.execute_governed(_ctx(MTD_QUESTION), _ds(), LIVE_ELSE_WHOLE_MONTH, "llm:fake")
    assert out["ok"] is False and out["code"] == period_labels.CODE
    assert "Sep MTD 1-7" in out["error"]
    assert governed == [] and conn.dry_runs == [] and conn.runs == []


@pytest.mark.usefixtures("oct_8")
def test_sql_agent_gets_the_fix_and_retries(monkeypatch):
    conn, sent = _Conn(), []
    monkeypatch.setattr(registry, "make_connector", lambda t, c: conn)
    monkeypatch.setattr(registry, "govern", lambda sql, *a, **k: sql)

    def call(i, sql):
        return SimpleNamespace(text="", tool_calls=[{"id": f"c{i}", "name": "run_sql",
                                                     "arguments": {"sql": sql}}],
                               model="m", provider="fake", prompt_tokens=1,
                               completion_tokens=1, cost=0.0)

    script = [call(1, LIVE_ELSE_WHOLE_MONTH), call(2, FIXED_MTD),
              SimpleNamespace(text="Oct 1-7 vs Sep 1-7", tool_calls=[], model="m",
                              provider="fake", prompt_tokens=1, completion_tokens=1, cost=0.0)]

    def fake_complete_tools(_tenant, _purpose, messages, _tools=None, **_kw):
        sent.extend(str(m.get("content") or "") for m in messages)
        return script.pop(0)

    monkeypatch.setattr(sql_agent.llm, "complete_tools", fake_complete_tools)
    out = sql_agent.run_sql_agent(_ctx(MTD_QUESTION), _ds(), max_turns=3)
    assert [q["code"] for q in out["queries"] if not q["ok"]] == [period_labels.CODE]
    assert any(f"ERROR {period_labels.CODE}" in s for s in sent)
    assert conn.runs == [FIXED_MTD]                 # only the corrected SQL ever ran
    assert out["sql"]["generated_sql"] == FIXED_MTD


# ---- the rules reach every SQL-writing prompt ----
def test_rules_are_in_the_tool_loop_prompt():
    assert PERIOD_SQL_INSTRUCTIONS in sql_agent._system_prompt(_ds()["mdl"], question="gmv")


def test_rules_and_the_date_are_in_the_deep_research_prompt():
    text = research_agent._system_prompt(_ctx(MTD_QUESTION, deep_analysis=True), [_ds()],
                                         research_agent.ResearchBudget(), False, False)
    assert text.startswith("CURRENT DATE (server):")
    assert PERIOD_SQL_INSTRUCTIONS in text


def test_rules_are_in_the_single_shot_prompt(monkeypatch):
    seen = []

    def fake_complete(_tenant, _purpose, messages, **_kw):
        seen.append(messages[0]["content"])
        return SimpleNamespace(text="SELECT 1", model="m", provider="fake",
                               prompt_tokens=1, completion_tokens=1, cost=0.0)

    monkeypatch.setattr(registry.llm, "complete", fake_complete)
    registry._llm_sql(_ctx("gmv"), _ds()["mdl"])
    assert PERIOD_SQL_INSTRUCTIONS in seen[0]
