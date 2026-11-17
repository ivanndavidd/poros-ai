"""Catalog pruning (latency/cost): a large catalog is cut to question-relevant tables
before entering an LLM prompt — deterministic vocabulary scoring, names-only index for
the rest, describe_table tool as the recovery path. Governance is untouched (govern()
always validates against the FULL mdl).

Run:  cd backend && python -m pytest tests/test_catalog_prune.py -q
Fully offline.
"""
import json
import os, sys
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

from app import nl2sql  # noqa: E402
from app.agent import sql_agent  # noqa: E402
from app.skills.registry import Ctx  # noqa: E402


def _mdl(n_misc=10):
    tables = {
        "acme_store_order_mp": ["marketplace", "gmv", "nmv", "month_id", "brand"],
        "acme_store_traffic": ["client", "sessions", "month_id"],
        "ims_outbound": ["Quantity", "warehouse_name", "sales_channel"],
    }
    for i in range(n_misc):
        tables[f"misc_table_{i}"] = [f"col_a_{i}", f"col_b_{i}"]
    return {
        "tables": tables,
        "allowed_tables": list(tables),
        "descriptions": {"acme_store_order_mp": "Pesanan marketplace harian; berisi GMV dan NMV",
                         "acme_store_order_mp.gmv": "Gross merchandise value",
                         "ims_outbound": "Pengiriman keluar dari gudang"},
        "grain": {"acme_store_order_mp": "1 baris = 1 order item",
                  "ims_outbound": "1 baris = 1 delivery line"},
        "reference_values": {"marketplace": ["Shopee", "Tokopedia", "TikTok"],
                             "ims_outbound.sales_channel": ["B2B", "B2C"]},
        "metrics": [{"name": "GMV", "definition": "SUM(gmv) FROM acme_store_order_mp"},
                    {"name": "Outbound qty", "definition": "SUM(Quantity) FROM ims_outbound"},
                    {"name": "Rasio umum", "definition": "pembilang / penyebut"}],
        "examples": [{"q": "gmv per marketplace",
                      "sql": "SELECT marketplace, SUM(gmv) FROM acme_store_order_mp GROUP BY 1"},
                     {"q": "outbound bulan ini",
                      "sql": "SELECT SUM(Quantity) FROM ims_outbound"}],
        "pii_columns": [],
    }


# ---- pruned_view selection ----
def test_prune_keeps_question_relevant_tables_only():
    view, dropped = nl2sql.pruned_view(_mdl(), "berapa total GMV Shopee bulan Juni 2026?")
    assert "acme_store_order_mp" in view["tables"]           # gmv column + Shopee ref value
    assert all(t.startswith("misc") or t in ("ims_outbound", "acme_store_traffic")
               for t in dropped)
    assert "ims_outbound" in dropped                        # zero vocabulary overlap


def test_reference_value_match_pulls_the_right_table():
    # 'Tokopedia' appears ONLY in reference_values.marketplace — table must still be found
    view, _ = nl2sql.pruned_view(_mdl(), "penjualan Tokopedia dong")
    assert "acme_store_order_mp" in view["tables"]


def test_no_signal_or_small_catalog_returns_full():
    full = _mdl()
    view, dropped = nl2sql.pruned_view(full, "halo, apa kabar?")     # stopwords/no overlap
    assert view is full and dropped == []
    small = _mdl(n_misc=0)                                           # 3 tables <= keep_n
    view, dropped = nl2sql.pruned_view(small, "berapa GMV Shopee?")
    assert view is small and dropped == []


def test_env_knobs(monkeypatch):
    monkeypatch.setenv("CATALOG_PRUNE", "0")
    view, dropped = nl2sql.pruned_view(_mdl(), "berapa GMV Shopee?")
    assert dropped == []
    monkeypatch.setenv("CATALOG_PRUNE", "1")
    monkeypatch.setenv("CATALOG_KEEP_TABLES", "1")
    view, dropped = nl2sql.pruned_view(_mdl(), "berapa GMV Shopee?")
    assert list(view["tables"]) == ["acme_store_order_mp"] and len(dropped) == 12


def test_numeric_tokens_are_not_signal():
    # '15' from 'naik 15%' must not match anything (values are not vocabulary)
    mdl = _mdl()
    mdl["reference_values"]["misc_table_0.col_a_0"] = [15, 30]
    mdl["tables"]["misc_table_0"] = ["col_a_0"]
    view, dropped = nl2sql.pruned_view(mdl, "kalau GMV naik 15% jadi berapa?")
    assert "misc_table_0" in dropped


def test_allowed_tables_respected():
    mdl = _mdl()
    mdl["allowed_tables"] = ["acme_store_order_mp", "acme_store_traffic", "ims_outbound"]
    view, dropped = nl2sql.pruned_view(mdl, "berapa GMV Shopee?")
    # governed-out misc tables appear in NEITHER the view nor the names index
    assert view is mdl and dropped == []                    # 3 visible <= keep_n -> full


# ---- curated layers follow the kept tables ----
def test_view_filters_metrics_examples_grain_refs():
    view, _ = nl2sql.pruned_view(_mdl(), "berapa total GMV Shopee?")
    names = [m["name"] for m in view["metrics"]]
    assert "GMV" in names and "Rasio umum" in names         # generic metric kept
    assert "Outbound qty" not in names                      # mentions a dropped table
    assert all("ims_outbound" not in e["sql"] for e in view["examples"])
    assert "ims_outbound" not in view["grain"]
    assert "marketplace" in view["reference_values"]
    assert "ims_outbound.sales_channel" not in view["reference_values"]


def test_catalog_render_has_names_only_index():
    txt = nl2sql.catalog_for_prompt(_mdl(), question="berapa total GMV Shopee?")
    assert "OTHER TABLES" in txt and "misc_table_0" in txt
    assert "col_a_0" not in txt                             # dropped table's columns gone
    full = nl2sql.catalog_for_prompt(_mdl())                # no question -> old behavior
    assert "OTHER TABLES" not in full and "col_a_0" in full


# ---- describe_table (recovery path) ----
def test_describe_tables_renders_full_context_for_named_table():
    txt = nl2sql.describe_tables(_mdl(), ["IMS_OUTBOUND"])  # case-insensitive
    assert "ims_outbound" in txt and "sales_channel" in txt
    assert "1 baris = 1 delivery line" in txt               # grain included
    assert "B2B" in txt                                     # ref values included
    assert "Outbound qty" in txt                            # table's curated METRIC recoverable
    bad = nl2sql.describe_tables(_mdl(), ["tidak_ada"])
    assert "Unknown table" in bad and "acme_store_order_mp" in bad


def test_describe_tables_coerces_string_argument():
    # models emit {"tables": "a, b"} (string, not array) — must not iterate characters
    txt = nl2sql.describe_tables(_mdl(), "ims_outbound, acme_store_traffic")
    assert "sales_channel" in txt and "sessions" in txt
    assert "Unknown table 'i'" not in txt


def test_garbage_keep_tables_env_degrades_not_crashes(monkeypatch):
    monkeypatch.setenv("CATALOG_KEEP_TABLES", "banyak")     # malformed -> default 8, no raise
    view, dropped = nl2sql.pruned_view(_mdl(), "berapa GMV Shopee?")
    assert "acme_store_order_mp" in view["tables"] and dropped


def test_whole_identifier_matching_for_metric_and_example_attribution():
    # 'sales' must NOT claim mentions of 'sales_target' (substring aliasing)
    mdl = {
        "tables": {"sales": ["amount", "region"], "sales_target": ["target_amount"],
                   **{f"m{i}": [f"c{i}"] for i in range(9)}},
        "allowed_tables": [],
        "descriptions": {}, "grain": {},
        "reference_values": {},
        "metrics": [{"name": "Target", "definition": "SUM(target_amount) FROM sales_target"}],
        "examples": [{"q": "target sales", "sql": "SELECT SUM(target_amount) FROM sales_target"}],
    }
    view = nl2sql._mdl_view(mdl, ["sales"])
    assert view["metrics"] == []                            # sales_target metric filtered out
    assert view["examples"] == []


def test_agent_loop_recovers_a_pruned_out_table_via_describe_table(monkeypatch):
    # the REAL recovery scenario: the question prunes ims_outbound OUT of the catalog,
    # the model asks for it by name, and gets its schema back mid-loop
    calls = {"n": 0}

    def fake_tools(tid, purpose, messages, tools, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            sysmsg = messages[0]["content"]
            assert "OTHER TABLES" in sysmsg and "ims_outbound" not in sysmsg.split("OTHER TABLES")[0]
            assert any(t["function"]["name"] == "describe_table" for t in tools)
            return SimpleNamespace(text="", model="fake", provider="fake", prompt_tokens=1,
                                   completion_tokens=1, cost=0.0,
                                   tool_calls=[{"id": "1", "name": "describe_table",
                                                "arguments": {"tables": ["ims_outbound"]}}])
        # the tool result must have reached the message list with the pruned table's schema
        assert any(m.get("role") == "tool" and "sales_channel" in m.get("content", "")
                   for m in messages)
        return SimpleNamespace(text="selesai", model="fake", provider="fake", prompt_tokens=1,
                               completion_tokens=1, cost=0.0, tool_calls=[])

    monkeypatch.setattr(sql_agent.llm, "complete_tools", fake_tools)
    ctx = Ctx(tenant_id="t", role="Admin", role_policy={}, question="berapa total GMV Shopee?")
    out = sql_agent.run_sql_agent(ctx, {"mdl": _mdl(), "type": "t", "conn": {}})
    assert out["answer"] == "selesai" and calls["n"] == 2


def test_single_shot_path_keeps_the_full_catalog(monkeypatch):
    # single-shot has no describe_table tool and no retry loop — it must NOT be pruned
    from app.skills import registry
    seen = {}

    def fake_complete(tid, purpose, messages, **kw):
        seen["prompt"] = messages[0]["content"]
        return SimpleNamespace(text="SELECT 1", model="fake", provider="fake",
                               prompt_tokens=1, completion_tokens=1, cost=0.0)

    monkeypatch.setattr(registry.llm, "complete", fake_complete)
    ctx = Ctx(tenant_id="t", role="Admin", role_policy={}, question="berapa total GMV Shopee?")
    registry._llm_sql(ctx, _mdl())
    assert "col_a_0" in seen["prompt"]                      # every table still detailed
    assert "OTHER TABLES" not in seen["prompt"]


def test_agent_system_prompt_is_pruned_for_big_catalogs(monkeypatch):
    seen = {}

    def fake_tools(tid, purpose, messages, tools, **kw):
        seen["sys"] = messages[0]["content"]
        return SimpleNamespace(text="ok", model="fake", provider="fake", prompt_tokens=1,
                               completion_tokens=1, cost=0.0, tool_calls=[])

    monkeypatch.setattr(sql_agent.llm, "complete_tools", fake_tools)
    ctx = Ctx(tenant_id="t", role="Admin", role_policy={}, question="berapa total GMV Shopee?")
    sql_agent.run_sql_agent(ctx, {"mdl": _mdl(), "type": "t", "conn": {}})
    assert "OTHER TABLES" in seen["sys"] and "acme_store_order_mp" in seen["sys"]
    assert "col_a_3" not in seen["sys"]                     # misc columns pruned from prompt


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
