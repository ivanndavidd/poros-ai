"""Design-doc phase 3: an answer states what it DELIVERED, and a gap is named.

Before this, every non-blocked turn was audited "ok": an answer missing the requested
client breakdown, an answer over zero rows, and an answer whose every SQL attempt failed
all looked identical to a complete one. The exit criteria pinned here:

  * a request for month × client × brand cannot come back "complete" with month × brand
    only — the missing dimension is NAMED;
  * zero valid SQL evidence can never produce a complete answer (error, or partial when
    documents kept the prose honest);
  * an empty governed result is an ANSWER (`no_data`), never grounds for a claim.

The request contract is deliberately conservative: only STRUCTURAL cues ("per client",
"top 10 seller", "bulanan", "chart") create requirements, and only when the schema can
ground them — a bare value mention is a filter, and over-claiming would demote correct
answers until people learn to ignore the label.
"""
import sys
from types import SimpleNamespace
from pathlib import Path

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import answer_contract, request_contract, store        # noqa: E402
from app.agent.verifier import verify                           # noqa: E402
from app.main import app                                        # noqa: E402

client = TestClient(app)
AK = {"X-Admin-Key": "admin-dev-key"}

SCHEMA = [{"id": "ds1", "name": "Acme", "type": "duckdb", "mdl": {
    # cost_per_unit puts "per" among the schema parts — the live Acme condition under
    # which the two-word reading "client per" once grounded (and could never be
    # satisfied by any result column)
    "tables": {"mp_orders": ["month_id", "gmv", "brand", "cost_per_unit"],
               "clients": ["client_name", "region"]},
    "allowed_tables": ["mp_orders", "clients"],
    "reference_values": {"brand": ["Philipsome", "Samsung"]},
}}]


# ---- request contract: what the question requires ----
def test_grouping_cues_claim_grounded_dimensions():
    req = request_contract.build("berapa gmv bulanan per client per brand", SCHEMA)
    assert req["dimensions"] == ["client", "brand"]
    assert req["time_bucket"] == "month"
    # a bucket word after the dimension stays a bucket, never half of the dimension
    assert request_contract.build("gmv per client bulanan", SCHEMA)["dimensions"] == ["client"]


def test_a_two_word_dimension_grounds_and_is_satisfiable():
    schema = [{"id": "d", "name": "S", "type": "duckdb", "mdl": {
        "tables": {"orders": ["product_category", "gmv", "cost_per_unit"]}}}]
    req = request_contract.build("gmv per product category", schema)
    assert req["dimensions"] == ["product category"]
    ev = {"sql": ok_set(["product_category", "gmv"], [["A", 1.0]])}
    assert answer_contract.missing_requirements(req, ev, "…") == []


def test_scattered_parts_do_not_ground_a_two_word_dimension():
    """"product" and "category" both exist, but no single column carries the pair — a
    requirement no one result column could satisfy must not be created."""
    schema = [{"id": "d", "name": "S", "type": "duckdb", "mdl": {
        "tables": {"orders": ["product_id", "gmv"], "cats": ["category_name"]}}}]
    req = request_contract.build("gmv per product category", schema)
    assert req["dimensions"] == ["product"]


def test_a_bare_mention_is_a_filter_not_a_requirement():
    """"gmv brand Philips" wants ONE number — demanding a brand column would demote a
    correct answer to partial."""
    assert request_contract.build("berapa gmv brand Philipsome bulan Juli 2026", SCHEMA) is None


def test_an_ungrounded_dimension_is_ignored():
    """"per kategori" over data with no category column is not a checkable requirement."""
    assert request_contract.build("gmv per kategori", SCHEMA) is None


def test_top_n_claims_the_count_and_its_dimension():
    req = request_contract.build("top 10 client dengan gmv tertinggi", SCHEMA)
    assert req["outputs"]["top_n"] == 10 and req["dimensions"] == ["client"]
    again = request_contract.build("tampilkan 5 brand terbesar", SCHEMA)
    assert again["outputs"]["top_n"] == 5 and again["dimensions"] == ["brand"]


def test_chart_and_trend_cues():
    req = request_contract.build("buatkan line chart tren gmv", SCHEMA)
    assert req["outputs"]["chart"] is True and req["time_bucket"] == "period"


def test_live_acme_wording_grounds_brand_client_month_and_graph():
    """The verbatim first prompt of the 2026-09-24 report: "masing masing" without the
    hyphen and "graph" instead of chart carried NO requirement, so a one-brand,
    text-only answer read as complete."""
    req = request_contract.build(
        "Berapa total GMV per bulan dari masing masing brand. generate graph and table to "
        "show GMV per client per month dari January sampai bulan ini", SCHEMA)
    assert req["dimensions"] == ["brand", "client"]
    assert req["time_bucket"] == "month"
    assert req["outputs"]["chart"] is True
    # its follow-up once made self-contained — "for each" is a grouping cue too
    each = request_contract.build("show total gmv for each client per month", SCHEMA)
    assert each["dimensions"] == ["client"] and each["time_bucket"] == "month"


MEASURE_GAP = "measure:value"


def test_how_much_requires_a_monetary_value_when_the_data_can_carry_one():
    """Live feedback: "How much inventory Acme has?" answered as 27.345.120 UNITS with high
    confidence. 'how much' asks for a VALUE; when the visible schema holds money at all,
    a unit count is a NAMED gap, not a complete answer."""
    req = request_contract.build("How much inventory Acme has? As of today?", SCHEMA)
    assert req["measure"] == "value" and req["dimensions"] == []
    units = {"sql": ok_set(["total_units"], [[27345120]])}
    out = answer_contract.classify(req, units, "Acme has **27.345.120 units** of inventory.",
                                   needs_sql=True)
    assert out["status"] == "partial" and out["missing_requirements"] == [MEASURE_GAP]
    # the honesty sentence names a currency WITHOUT a figure — still the gap
    honest = answer_contract.classify(
        req, units, "The data holds no value column to express it in Rupiah; the closest "
        "fact is 27.345.120 units.", needs_sql=True)
    assert honest["missing_requirements"] == [MEASURE_GAP]
    # delivered as money, either way: a monetary column, or a currency-denominated figure
    money_col = {"sql": ok_set(["inventory_value"], [[1200000.0]])}
    assert answer_contract.classify(req, money_col, "…", needs_sql=True)["status"] == "complete"
    money_text = answer_contract.classify(
        req, units, "Nilai inventory **Rp1.200.000** (±1,2 juta).", needs_sql=True)
    assert money_text["status"] == "complete"
    # the currency-figure shapes the prose comes in (and the honesty sentence, which is not one)
    for prose in ("Rp 1.200.000", "Rp. 1.200.000", "IDR 1.2 bn", "sekitar 12 rupiah"):
        assert answer_contract._delivers_money([], prose), prose
    assert not answer_contract._delivers_money([], "no value column to express it in Rupiah")


def test_how_much_stays_quiet_for_rates_counts_and_moneyless_schemas():
    assert request_contract.build("how much is the cancel rate this month", SCHEMA) is None
    assert request_contract.build("how many orders last month", SCHEMA) is None
    qty_only = [{"id": "d", "name": "IMS", "type": "duckdb", "mdl": {
        "tables": {"stock": ["sku", "quantity", "warehouse"]}}}]
    assert request_contract.build("how much inventory do we have", qty_only) is None


def test_an_ordinary_question_states_no_requirements():
    assert request_contract.build("berapa total gmv bulan juli 2026", SCHEMA) is None


# ---- classification: what the turn delivered ----
def ok_set(columns, rows):
    return {"ok": True, "columns": columns, "rows": rows}


REQ = {"dimensions": ["client", "brand"], "time_bucket": "month",
       "outputs": {"chart": False, "top_n": None}}


def test_month_client_brand_cannot_complete_with_month_brand_only():
    evidence = {"sql": ok_set(["month_id", "brand", "gmv"], [[202607, "Samsung", 5.0]])}
    out = answer_contract.classify(REQ, evidence, "GMV per brand ...", needs_sql=True)
    assert out["status"] == "partial"
    assert out["missing_requirements"] == ["dimension:client"]
    # ...and delivering it (any set, any column carrying the part) completes the turn
    evidence["sql_results"] = [ok_set(["month_id", "client_name", "brand", "gmv"],
                                      [[202607, "Acme", "Samsung", 5.0]])]
    assert answer_contract.classify(REQ, evidence, "…", needs_sql=True)["status"] == "complete"


def test_zero_sql_evidence_is_never_complete():
    assert answer_contract.classify(None, {}, "jawaban", needs_sql=True)["status"] == "error"
    docs = {"docs": {"results": [{"text": "SOP"}]}}
    out = answer_contract.classify(None, docs, "jawaban", needs_sql=True)
    assert out["status"] == "partial"
    assert out["missing_requirements"] == ["governed_sql_evidence"]


def test_empty_rows_are_no_data_not_evidence():
    evidence = {"sql": ok_set(["month_id", "gmv"], [])}
    assert answer_contract.classify(None, evidence, "tidak ada data",
                                    needs_sql=True)["status"] == "no_data"


def test_top_n_shortfall_is_partial_unless_the_answer_documents_it():
    req = {"dimensions": [], "time_bucket": None, "outputs": {"chart": False, "top_n": 10}}
    evidence = {"sql": ok_set(["client_name", "gmv"], [[f"c{i}", 1.0] for i in range(7)])}
    silent = answer_contract.classify(req, evidence, "Top client: c0…c6.", needs_sql=True)
    assert silent["status"] == "partial" and silent["missing_requirements"] == ["top_10:7_rows"]
    honest = answer_contract.classify(req, evidence, "Hanya ada 7 client di data ini: …",
                                      needs_sql=True)
    assert honest["status"] == "complete"


def test_a_requested_chart_must_be_in_the_answer():
    req = {"dimensions": [], "time_bucket": None, "outputs": {"chart": True, "top_n": None}}
    evidence = {"sql": ok_set(["month_id", "gmv"], [[202607, 5.0]])}
    missing = answer_contract.classify(req, evidence, "GMV Juli 5.0.", needs_sql=True)
    assert missing["missing_requirements"] == ["chart"]
    drawn = answer_contract.classify(req, evidence, "GMV:\n```chart\n{}\n```",
                                     needs_sql=True)
    assert drawn["status"] == "complete"


def test_non_sql_turns_classify_on_retrieval():
    docs = {"docs": {"results": [{"text": "SOP"}]}}
    assert answer_contract.classify(None, docs, "jawab", needs_sql=False)["status"] == "complete"
    assert answer_contract.classify(None, {}, "tidak ketemu dokumennya",
                                    needs_sql=False)["status"] == "no_data"


# ---- the verifier check: same computation, visible as a finding ----
def test_verifier_names_the_missing_requirement():
    evidence = {"sql": ok_set(["month_id", "brand", "gmv"], [[202607, "Samsung", 5.0]])}
    vr = verify("gmv bulanan per client per brand", "GMV per brand: …", evidence,
                SimpleNamespace(pii_request=False), request=REQ)
    assert vr["checks"]["requirements_met"] is False
    assert any("dimension:client" in issue for issue in vr["issues"])
    without = verify("berapa gmv", "GMV: 5.0", evidence,
                     SimpleNamespace(pii_request=False))
    assert without["checks"]["requirements_met"] is True


# ---- /v1/chat end to end (no LLM: rule-based SQL over a real DuckDB) ----
@pytest.fixture
def world(tmp_path):
    import duckdb

    tid = store.create_tenant("STATUS-e2e")
    store.create_role(tid, "analyst", {"full_access": True, "can_see_pii": True})
    path = str(tmp_path / "acme.duckdb")
    con = duckdb.connect(path)
    con.execute("CREATE TABLE mp_orders(month_id INTEGER, gmv DOUBLE, brand VARCHAR)")
    con.execute("INSERT INTO mp_orders VALUES (202607, 250.0, 'Samsung'), (202608, 90.0, 'Sharp')")
    con.close()
    store.create_datasource(
        tid, "duckdb", "Acme", {"path": path},
        {"tables": {"mp_orders": ["month_id", "gmv", "brand"],
                    "clients": ["client_name", "region"]},
         "allowed_tables": ["mp_orders", "clients"]})
    key = client.post("/admin/apikeys", headers=AK,
                      json={"tenant_id": tid, "label": "pytest"}).json()["key"]
    token = pyjwt.encode({"sub": "u", "role": "analyst"}, "test-secret", algorithm="HS256")
    yield {"tid": tid,
           "headers": {"X-API-Key": key, "Authorization": f"Bearer {token}"}}
    store.delete_tenant(tid)


def _chat(world, message):
    r = client.post("/v1/chat", headers=world["headers"], json={"message": message})
    assert r.status_code == 200, r.text
    return r.json()


def _answer_audit(tid):
    return next(r for r in store.list_audit(tid) if r["event"] == "answer")


def test_chat_reports_complete_and_partial_with_the_gap_named(world):
    done = _chat(world, "berapa total gmv bulan juli 2026")
    assert done["status"] == "complete" and done["missing_requirements"] == []
    assert _answer_audit(world["tid"])["status"] == "complete"

    # client is grounded by the clients table, but the executed SQL reads mp_orders —
    # the requested breakdown is not in the delivered result, and the turn says so
    gap = _chat(world, "berapa gmv bulanan per client per brand")
    assert gap["status"] == "partial"
    assert gap["missing_requirements"] == ["dimension:client"]
    audit = _answer_audit(world["tid"])
    assert audit["status"] == "partial"
    assert audit["detail"]["missing_requirements"] == ["dimension:client"]
    assert audit["detail"]["request_contract"]["dimensions"] == ["client", "brand"]


def test_chat_reports_no_data_over_an_empty_table(world, tmp_path):
    import duckdb

    empty = str(tmp_path / "empty.duckdb")
    con = duckdb.connect(empty)
    con.execute("CREATE TABLE mp_orders(month_id INTEGER, gmv DOUBLE)")
    con.close()
    tid = store.create_tenant("STATUS-empty")
    store.create_role(tid, "analyst", {"full_access": True})
    store.create_datasource(tid, "duckdb", "Acme", {"path": empty},
                            {"tables": {"mp_orders": ["month_id", "gmv"]},
                             "allowed_tables": ["mp_orders"]})
    key = client.post("/admin/apikeys", headers=AK,
                      json={"tenant_id": tid, "label": "pytest"}).json()["key"]
    token = pyjwt.encode({"sub": "u", "role": "analyst"}, "test-secret", algorithm="HS256")
    out = client.post("/v1/chat", headers={"X-API-Key": key,
                                           "Authorization": f"Bearer {token}"},
                      json={"message": "berapa total gmv bulan juli"}).json()
    assert out["status"] == "no_data"
    assert _answer_audit(tid)["status"] == "no_data"
    store.delete_tenant(tid)


def test_chat_reports_error_when_every_sql_attempt_failed(world, tmp_path):
    import duckdb

    broken = str(tmp_path / "ghost.duckdb")
    duckdb.connect(broken).close()
    tid = store.create_tenant("STATUS-error")
    store.create_role(tid, "analyst", {"full_access": True})
    # the allowlisted table does not exist in the database: every attempt fails
    store.create_datasource(tid, "duckdb", "Acme", {"path": broken},
                            {"tables": {"ghost": ["month_id", "gmv"]},
                             "allowed_tables": ["ghost"]})
    key = client.post("/admin/apikeys", headers=AK,
                      json={"tenant_id": tid, "label": "pytest"}).json()["key"]
    token = pyjwt.encode({"sub": "u", "role": "analyst"}, "test-secret", algorithm="HS256")
    out = client.post("/v1/chat", headers={"X-API-Key": key,
                                           "Authorization": f"Bearer {token}"},
                      json={"message": "berapa total gmv bulan juli"}).json()
    assert out["status"] == "error"
    assert _answer_audit(tid)["status"] == "error"
    store.delete_tenant(tid)


def test_smalltalk_is_trivially_complete(world):
    out = _chat(world, "halo")
    assert out["status"] == "complete" and out["blocked"] is False