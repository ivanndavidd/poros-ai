"""The agentic chat engine (config/chat engine=agentic): the model reads the question, then
the Knowledge Base through tools, instead of deterministic layers choosing the business,
values and tables from the raw message first.

Real store + a real DuckDB; only the model is scripted. Each test pins what moved to the
model (choosing the business, finding how a value is stored) or what must NOT move
(grants, govern(), PII, the semantic contract).

Run:  cd backend && python -m pytest tests/test_kb_agent.py -q -n0
"""
import json
import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store                                           # noqa: E402
from app import main                                            # noqa: E402,F401 (store.init)
from app.agent import kb_agent, orchestrator                    # noqa: E402
from app.connectors.factory import make_connector               # noqa: E402
from app.llm.gatekeeper import GateDecision                     # noqa: E402
from app.llm.gateway import ToolResult                          # noqa: E402
from app.schema_scan import scan_connector                      # noqa: E402

OFFLINE, MARKETPLACE, PRODUCTS = "offline_sales", "mp_orders", "mp_products"
EMAIL = "rina@example.com"


@pytest.fixture
def world(tmp_path):
    path = str(tmp_path / "kb.duckdb")
    con = duckdb.connect(path)
    con.execute(f"CREATE TABLE {OFFLINE}(month_id INTEGER, gmv DOUBLE, buyer_email VARCHAR)")
    con.execute(f"INSERT INTO {OFFLINE} VALUES (202607, 100.0, '{EMAIL}')")
    con.execute(f"CREATE TABLE {MARKETPLACE}(month_id INTEGER, gmv DOUBLE, brand VARCHAR)")
    con.execute(f"INSERT INTO {MARKETPLACE} VALUES (202607, 250.0, 'Philipsome'), "
                "(202607, 40.0, 'Philipsome'), (202607, 75.0, 'Oral-B')")
    con.execute(f"CREATE TABLE {PRODUCTS}(product_id INTEGER, brand VARCHAR)")
    con.execute(f"INSERT INTO {PRODUCTS} VALUES (1, 'Philipsome'), (2, 'Oral-B')")
    con.close()
    tid = store.create_tenant("KB-agentic")
    store.create_role(tid, "analyst", {"full_access": True, "can_see_pii": True})
    mdl = scan_connector(make_connector("duckdb", {"path": path}))
    mdl["pii_columns"] = ["buyer_email"]
    did = store.create_datasource(tid, "duckdb", "Acme L2", {"path": path}, mdl)
    one_p = store.create_business(tid, "1P", "Offline sales from our own warehouses")
    three_p = store.create_business(tid, "3P", "Marketplace orders of brand clients")
    store.set_business_knowledge(one_p, did, {"allowed_tables": [OFFLINE]})
    store.set_business_knowledge(three_p, did, {
        "allowed_tables": [MARKETPLACE, PRODUCTS],
        "descriptions": {MARKETPLACE: "Marketplace order lines per brand"},
        "business_concepts": [{"name": "GMV", "table": MARKETPLACE, "metric": "gmv",
                               "aggregation": "SUM"}]})
    store.set_role_businesses(tid, "analyst", [one_p, three_p])
    store.set_tenant_config(tid, "business", {"required": True})
    store.set_tenant_config(tid, "chat", {"engine": "agentic"})
    yield {"tid": tid, "did": did, "1P": one_p, "3P": three_p}
    store.delete_tenant(tid)


class Script:
    """A scripted model: each turn returns the next list of tool calls (or a final text),
    and every message it was sent is kept so a test can read the prompts."""

    def __init__(self, *turns):
        self.turns, self.sent, self.tools = list(turns), [], []

    def __call__(self, tenant_id, purpose, messages, tools, **kw):
        self.sent.append(json.loads(json.dumps(messages, default=str)))
        self.tools.append([t["function"]["name"] for t in tools])
        step = self.turns.pop(0) if self.turns else "done"
        if isinstance(step, str):
            return ToolResult(text=step, tool_calls=[], model="m", provider="p")
        calls = [{"id": f"c{i}", "name": name, "arguments": args}
                 for i, (name, args) in enumerate(step)]
        return ToolResult(text="", tool_calls=calls, model="m", provider="p")

    def replies(self):
        """Every tool reply the model got, in order."""
        last = self.sent[-1] if self.sent else []
        return [m["content"] for m in last if m.get("role") == "tool"]

    def everything(self):
        return json.dumps(self.sent, ensure_ascii=False)


@pytest.fixture
def run(monkeypatch):
    audits = []
    real_audit = store.log_audit

    def audit(*a, **k):
        audits.append((a, k))
        return real_audit(*a, **k)

    def go(world, message, script, intent="data_query", **kw):
        gate = GateDecision(True, intent, "complex", engine="llm:gate")
        monkeypatch.setattr(orchestrator.llm, "screen", lambda *a, **k: gate)
        monkeypatch.setattr(orchestrator.llm, "pick_answer_purpose", lambda *a, **k: "answer_complex")
        monkeypatch.setattr(orchestrator.llm, "complete_tools", script)
        monkeypatch.setattr(orchestrator.sql_agent, "usable", lambda *a, **k: True)
        monkeypatch.setattr(orchestrator.mbr_engine, "has_brand_orders", lambda *a, **k: False)
        monkeypatch.setattr(orchestrator.web_research, "enabled", lambda *a, **k: False)
        monkeypatch.setattr(store, "log_audit", audit)
        real_skill = orchestrator.run_skill

        def skill(name, ctx, **k):
            if name == "generate_report":
                return {"text": "written answer", "streamed": False}
            return real_skill(name, ctx, **k)

        monkeypatch.setattr(orchestrator, "run_skill", skill)
        principal = {"tenant_id": world["tid"], "role": kw.pop("role", "analyst")}
        return orchestrator.run_agent(principal, message, **kw)

    go.audits = audits
    return go


# ---- the model chooses: business, value, table ----

def test_the_model_opens_the_business_finds_the_stored_value_and_queries(world, run):
    script = Script(
        [("open_business", {"business": "3P"})],
        [("lookup_values", {"table": MARKETPLACE, "column": "brand", "term": "philips"})],
        [("run_sql", {"sql": f"SELECT SUM(gmv) AS gmv FROM {MARKETPLACE} "
                             "WHERE brand = 'Philipsome'"})],
        "GMV Philipsome is 290 (brand stored as Philipsome).")
    out = run(world, "berapa gmv philips bulan juli 2026", script)
    assert out["engine"] == "agentic"
    assert out["business"]["name"] == "3P"
    assert out["plan"][:1] == ["agent_loop(agentic)"]
    assert {"open_business:3P", "lookup_values", "run_sql"} <= set(out["plan"])
    lookup = json.loads(script.replies()[1])
    assert lookup["source"] == "data" and lookup["values"][0]["value"] == "Philipsome"
    assert out["sql"]["rows"] == [[290.0]]
    # the open business's table index reached the model; the other business's never did
    after_open = script.sent[1][0]["content"]
    assert MARKETPLACE in after_open and "Marketplace order lines per brand" in after_open
    assert OFFLINE not in script.everything()
    # every governed query is in the audit trail, the server-written lookup included
    sqls = [k["detail"]["safe_sql"] for a, k in run.audits if a[2] == "query"]
    assert len(sqls) == 2 and "LIKE" in sqls[0].upper()


def test_no_business_is_open_before_the_model_picks_one(world, run):
    script = Script("Halo! Ada yang bisa saya bantu?")
    out = run(world, "halo", script, intent="smalltalk")
    assert out["answer"] == "Halo! Ada yang bisa saya bantu?"
    assert out["business"] is None
    system = script.sent[0][0]["content"]
    assert "1P: Offline sales from our own warehouses" in system
    assert "3P: Marketplace orders of brand clients" in system
    assert "No business is open yet" in system
    assert "run_sql" not in script.tools[0] and "open_business" in script.tools[0]


def test_two_fitting_businesses_end_the_turn_with_the_business_menu(world, run):
    script = Script([("ask_user", {"question": "1P atau 3P?",
                                   "options": [{"label": "1P"}, {"label": "3P"}]})])
    out = run(world, "berapa gmv bulan juli 2026", script)
    assert out["status"] == "awaiting_confirmation"
    ask = out["confirmation"]
    assert ask["kind"] == "business" and [o["name"] for o in ask["options"]] == ["1P", "3P"]
    assert ask["pending_question"] == "berapa gmv bulan juli 2026"
    assert out["sql"] is None


def test_any_other_question_comes_back_as_a_clarify_menu(world, run):
    script = Script([("open_business", {"business": "3P"})],
                    [("ask_user", {"question": "Philipsome atau Philips Home?",
                                   "options": [{"label": "Philipsome",
                                                "question": "gmv Philipsome juli 2026"},
                                               {"label": "Philips Home"}]})])
    out = run(world, "gmv philips juli 2026", script)
    ask = out["confirmation"]
    assert ask["kind"] == "clarify" and out["business"]["name"] == "3P"
    assert ask["options"][0]["question"] == "gmv Philipsome juli 2026"
    assert ask["options"][1]["question"] == "gmv philips juli 2026 (Philips Home)"


# ---- not found is not the end: other columns, other businesses, a cheaper query ----

def test_a_name_missing_from_one_column_points_to_where_else_it_lives(world, run):
    store.set_business_knowledge(world["1P"], world["did"], {
        "allowed_tables": [OFFLINE], "reference_values": {"gmv": ["Lumora Official"]}})
    script = Script(
        [("open_business", {"business": "3P"})],
        [("lookup_values", {"table": MARKETPLACE, "column": "brand", "term": "lumora"})],
        "Lumora is not in the 3P brand column.")
    out = run(world, "berapa gmv lumora juli 2026", script)
    miss = json.loads(script.replies()[1])
    assert miss["curated_in_other_businesses"] == ["1P"]
    assert "before concluding" in miss["next"]
    assert "other columns that hold names" in script.sent[0][0]["content"]
    # it searched the data and came back empty: no data, never a complete discussion
    assert out["status"] == "no_data"
    assert out["confidence"]["level"] != "general_knowledge"


def test_a_pin_to_a_business_that_no_longer_exists_reads_as_no_pin(world, run):
    script = Script([("open_business", {"business": "3P"})], "done")
    out = run(world, "gmv juli", script, business="b_deleted01")
    assert out["business"]["name"] == "3P"
    gdv = store.create_business(world["tid"], "GDV", "Not granted")
    with pytest.raises(orchestrator.business_scope.BusinessError):
        run(world, "gmv juli", Script("done"), business=gdv)


def test_a_query_over_the_scan_limit_is_told_how_to_get_cheaper(monkeypatch):
    monkeypatch.setattr(orchestrator.sql_agent, "execute_governed", lambda *a, **k: {
        "ok": False, "code": "COST_LIMIT", "error": "query would scan ~3 GB"})
    reply = orchestrator.sql_agent.sql_reply(
        None, {}, "SELECT 1", orchestrator.sql_agent.Findings(),
        kb_agent.TextPiiRedactor(), "llm:m")
    assert reply.startswith("ERROR COST_LIMIT") and "still answerable" in reply
    assert "partitioned by that column" in reply and "product name" in reply


def test_the_scan_limit_error_says_whether_a_date_filter_can_help():
    from app.skills.registry import cost_advice
    flat = cost_advice("SELECT brand FROM orders WHERE d >= '2026-01-01'", "bigquery",
                       {"table_metadata": {"orders": {"partitioning": None}}})
    assert "orders is not partitioned: a date filter does not lower its scan" in flat
    parted = cost_advice("SELECT brand FROM orders", "bigquery", {"table_metadata": {
        "orders": {"partitioning": {"type": "DAY", "field": "order_date"}}}})
    assert "partitioned by order_date" in parted and "Read fewer columns" in parted


def test_a_name_too_big_to_scan_in_its_table_is_looked_up_in_a_smaller_one(
        world, run, monkeypatch):
    ds = store.get_datasource(world["did"])
    meta = dict(ds["mdl"].get("table_metadata") or {})
    meta[MARKETPLACE] = {**(meta.get(MARKETPLACE) or {}), "estimated_bytes": 12_500_000_000,
                         "partitioning": None}
    meta[PRODUCTS] = {**(meta.get(PRODUCTS) or {}), "estimated_bytes": 20_000_000}
    store.update_mdl(world["did"], {**ds["mdl"], "table_metadata": meta})
    real = kb_agent.execute_governed

    def governed(ctx, target, sql, engine, **kw):
        if MARKETPLACE in sql:     # the order table is over the scan limit for this column
            return {"ok": False, "code": "COST_LIMIT", "error": "would scan ~2.3 GB",
                    "safe_sql": sql}
        return real(ctx, target, sql, engine, **kw)

    monkeypatch.setattr(kb_agent, "execute_governed", governed)
    script = Script(
        [("open_business", {"business": "3P"})],
        [("lookup_values", {"table": MARKETPLACE, "column": "brand", "term": "philips"})],
        "done")
    run(world, "gmv philips juli 2026", script)
    found = json.loads(script.replies()[1])
    assert found["table"] == PRODUCTS and found["values"][0]["value"] == "Philipsome"
    assert "too large to scan" in found["note"]
    assert "12.5 GB, not partitioned" in script.sent[1][0]["content"]


def test_a_name_never_found_leaves_the_answer_partial(world, run):
    script = Script(
        [("open_business", {"business": "3P"})],
        [("lookup_values", {"table": MARKETPLACE, "column": "brand", "term": "milkita"})],
        [("run_sql", {"sql": f"SELECT brand, SUM(gmv) AS gmv FROM {MARKETPLACE} "
                             "GROUP BY brand"})],
        "Milkita is not in the result; these are the other brands.")
    out = run(world, "analyze gmv milkita", script)
    assert out["status"] == "partial"
    assert out["missing_requirements"] == ["value:milkita"]


def test_a_business_name_or_a_respelled_name_is_not_an_unfound_value():
    class Session:
        choices = [{"name": "1P"}, {"name": "Acme Enabler"}]
        terms = {"1p": False, "jub sido": False, "milkita": False}
    evidence = {"sql_results": [{"ok": True, "rows": [[855908]],
                                 "safe_sql": "SELECT COUNT(*) FROM t WHERE owner = 'JUB-SIDO'"}]}
    assert orchestrator._unfound_terms(Session(), evidence) == ["milkita"]


def test_an_engine_error_keeps_the_column_it_names_and_redacts_values():
    redactor = kb_agent.TextPiiRedactor()
    text = redactor.redact_error("400 POST ...: Unrecognized name: order_id at [1:36]")
    assert "`order_id`" in text and "PII" not in text
    leaked = redactor.redact_error("invalid input 'budi@acme.co.id'; Nama Lengkap: Budi Santoso")
    assert "budi@acme" not in leaked and "Budi Santoso" not in leaked


def test_a_count_over_a_filter_that_matched_nothing_is_no_data(world, run):
    script = Script(
        [("open_business", {"business": "3P"})],
        [("run_sql", {"sql": f"SELECT COUNT(*) AS orders FROM {MARKETPLACE} "
                             "WHERE brand = 'Jub Sido'"})],
        "Total order Jub Sido adalah 0.")
    out = run(world, "berapa total order jub sido", script)
    assert out["sql"]["rows"] == [[0]]
    assert out["status"] == "no_data"


# ---- what does not move: grants, govern(), PII, semantic contracts ----

def test_a_business_outside_the_grants_cannot_be_opened(world, run):
    gdv = store.create_business(world["tid"], "GDV", "Internal")
    store.set_business_knowledge(gdv, world["did"], {"allowed_tables": [OFFLINE]})
    script = Script([("open_business", {"business": "GDV"})],
                    [("run_sql", {"sql": f"SELECT SUM(gmv) FROM {OFFLINE}"})], "done")
    out = run(world, "gmv gdv", script)
    replies = script.replies()
    assert "No business by that name" in replies[0] and "GDV" not in replies[0].split(":")[-1]
    assert "Open a business first" in replies[1]
    assert out["business"] is None and out["sql"] is None


def test_the_open_business_still_bounds_every_query(world, run):
    script = Script([("run_sql", {"sql": f"SELECT SUM(gmv) FROM {MARKETPLACE}"})], "done")
    out = run(world, "gmv marketplace", script, business="1P")
    assert out["business"]["name"] == "1P"
    assert script.replies()[0].startswith("ERROR ")     # govern() refused the 3P table
    assert out["sql"]["ok"] is False


def test_pii_values_never_reach_the_model(world, run):
    script = Script(
        [("lookup_values", {"table": OFFLINE, "column": "buyer_email", "term": "rina"})],
        [("run_sql", {"sql": f"SELECT buyer_email, gmv FROM {OFFLINE}"})], "done")
    out = run(world, "siapa pembeli terbesar", script, business="1P")
    assert "personal data" in script.replies()[0]
    assert out["sql"]["rows"][0][0] == EMAIL            # the role may SEE it in the answer
    assert EMAIL not in script.everything()             # the model never does


def test_the_semantic_contract_binds_kpi_queries_but_not_lookups(world, run):
    script = Script(
        [("lookup_values", {"table": MARKETPLACE, "column": "brand", "term": "oral"})],
        [("run_sql", {"sql": f"SELECT AVG(gmv) AS gmv FROM {MARKETPLACE} GROUP BY brand"})],
        "done")
    out = run(world, "berapa gmv per brand", script, business="3P")
    replies = script.replies()
    assert json.loads(replies[0])["values"][0]["value"] == "Oral-B"
    assert "SEMANTIC_CONTRACT_REQUIRED" in replies[1]
    assert any(c["name"] == "GMV" for c in out["semantic_contracts"])


def test_tools_never_show_a_table_the_role_may_not_read(world, run):
    store.create_role(world["tid"], "mp_only", {"authorized_tables": [MARKETPLACE]})
    store.set_role_businesses(world["tid"], "mp_only", [world["3P"]])
    store.set_tenant_config(world["tid"], "business", {"required": False})
    store.set_business_knowledge(world["3P"], world["did"], {"allowed_tables": [MARKETPLACE]})
    ds = store.get_datasource(world["did"])
    store.update_mdl(world["did"], {**ds["mdl"], "descriptions": {
        OFFLINE: "Secret offline ledger", MARKETPLACE: "Marketplace order lines"}})
    script = Script([("list_tables", {}), ("get_definitions", {}),
                     ("search_catalog", {"query": "ledger"})], "done")
    run(world, "tabel apa saja", script, role="mp_only")
    text = script.everything()
    assert MARKETPLACE in text
    assert OFFLINE not in text and "Secret offline ledger" not in text


# ---- the switch ----

def test_the_switch_defaults_to_classic(world):
    assert kb_agent.engine(world["tid"]) == "agentic"
    store.set_tenant_config(world["tid"], "chat", {"engine": "nonsense"})
    assert kb_agent.engine(world["tid"]) == "classic"
    other = store.create_tenant("KB-classic")
    try:
        assert kb_agent.engine(other) == "classic"
    finally:
        store.delete_tenant(other)


def test_the_lookup_is_built_by_the_server_with_quoted_identifiers():
    sql = kb_agent.lookup_sql("mp_orders", "brand", kb_agent._term("phi'%_lips"), "bigquery")
    assert "`brand`" in sql and "LIKE '%philips%'" in sql and "LIMIT 20" in sql
    assert kb_agent._term("x' OR 1=1 --") == "x or 1=1 --"
    assert "LIKE '%jub%sido%'" in kb_agent.lookup_sql("t", "c", "jub sido", "bigquery")
    windowed = kb_agent.lookup_sql("mp_orders", "brand", "philips", "bigquery",
                                   since=("order_date", "2025-10-01"))
    assert "`order_date` >= '2025-10-01'" in windowed
