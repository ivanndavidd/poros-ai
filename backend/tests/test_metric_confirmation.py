"""A strict tenant's ambiguous KPI phrase is an OPEN READING on chat (chat overhaul item 5).

Phase 1 made the collision typed; for ``/v1/query`` (raw SQL, nobody to read an
assumption) a 409 is the right shape and stays. On ``/v1/chat`` the turn used to end as a
question with a server-side token for the pick (phase 2). Now the meanings go to the model:
it takes the one the question and the conversation most likely mean, the answer says which,
and each other meaning is a one-click correction.

What these tests pin:

  * one reading per ambiguous phrase, its options naming meanings (description), never
    tables; the model's context carries each meaning's definition, table and datasource;
  * a correction is text: the concept named outright when names differ, the meaning
    spelled out when two share a name ("GMV" and "GMV");
  * meanings in different tables are ENFORCED together, so the one a query takes is
    computed exactly as curated; meanings on one table cannot both bind one query and
    stay prompt context;
  * on chat the turn is answered (no ask, no token), routes without connection order, and
    ``chosen`` names the meaning the governed SQL satisfied.
"""
import sys
from pathlib import Path

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import assumptions, metric_confirm, semantic_resolver, store   # noqa: E402
from app.main import app                                                # noqa: E402

client = TestClient(app)
AK = {"X-Admin-Key": "admin-dev-key"}

TABLES = {"mp_orders": ["month_id", "gmv", "brand"],
          "offline_sales": ["month_id", "gmv", "store"]}
MSG = "berapa total gmv bulan juli 2026"


def concept(name, table="mp_orders", **extra):
    return {"name": name, "table": table, "metric": "gmv", "aggregation": "SUM", **extra}


def ds(ident, name, concepts, tables=TABLES):
    return {"id": ident, "name": name, "type": "duckdb", "enabled": 1,
            "mdl": {"tables": tables, "allowed_tables": list(tables),
                    "business_concepts": concepts}}


COLLIDING = [ds("ds_a", "Datamart", [concept("GMV", table="offline_sales",
                                             description="offline take")]),
             ds("ds_b", "Acme L3", [concept("GMV", description="marketplace take")])]


def analyze(message=MSG, sources=COLLIDING):
    analysis = semantic_resolver.analyze(sources, message)
    assert analysis.ambiguities, "fixture must be ambiguous"
    return analysis


# ---- the reading ----
def test_one_reading_per_phrase_naming_meanings_not_tables():
    [reading] = metric_confirm.readings(analyze(), MSG)
    assert reading["kind"] == "metric" and reading["term"].lower() == "gmv"
    assert reading["chosen"] is None and "token" not in reading
    assert [o["id"] for o in reading["options"]] == ["opt1", "opt2"]
    assert {o["descr"] for o in reading["options"]} == {"offline take", "marketplace take"}
    # the client never sees a table or a datasource
    shown = repr(assumptions.public([reading]))
    assert "offline_sales" not in shown and "Datamart" not in shown


def test_the_model_reads_each_meaning_with_its_definition_and_source():
    [reading] = metric_confirm.readings(analyze(), MSG)
    context = reading["_context"]
    assert context.startswith("'gmv' has 2 curated meanings:")
    assert "SUM(gmv) from offline_sales in datasource 'Datamart' (id ds_a)" in context
    assert "SUM(gmv) from mp_orders in datasource 'Acme L3' (id ds_b)" in context
    assert "checked against it" in context            # different tables: enforced


def test_two_meanings_with_one_name_are_corrected_by_their_description():
    [reading] = metric_confirm.readings(analyze(), MSG)
    by_descr = {o["descr"]: o["question"] for o in reading["options"]}
    assert by_descr["marketplace take"] == MSG + " (yang saya maksud: GMV: marketplace take)"
    [english] = metric_confirm.readings(analyze("what is the total gmv for july 2026"),
                                        "what is the total gmv for july 2026")
    assert english["options"][0]["question"].endswith(")")
    assert "(I mean: GMV: " in english["options"][0]["question"]


def test_distinct_names_are_corrected_by_naming_the_concept():
    one = [ds("ds_x", "Acme L3", [
        concept("Gross GMV", synonyms=["GMV"], description="before cancellation",
                concept_id="gmv_gross",
                source_binding={"role": "primary", "equivalence_contract_id": "eq_g"}),
        concept("Net GMV", table="offline_sales", synonyms=["GMV"],
                description="after cancellation", concept_id="gmv_net",
                source_binding={"role": "primary", "equivalence_contract_id": "eq_n"}),
    ])]
    [reading] = metric_confirm.readings(analyze("berapa gmv bulan juli", one),
                                        "berapa gmv bulan juli")
    assert [o["id"] for o in reading["options"]] == ["gmv_gross", "gmv_net"]
    assert reading["options"][0]["question"] == "berapa Gross GMV bulan juli"
    assert reading["options"][1]["question"] == "berapa Net GMV bulan juli"
    # the corrected question resolves on its own: nothing is open any more
    assert not semantic_resolver.analyze(one, "berapa Net GMV bulan juli").ambiguities


def test_meanings_in_different_tables_are_enforced_together():
    analysis = analyze()
    contracts = metric_confirm.enforceable(analysis)
    assert {(c["datasource_id"], c["table"]) for c in contracts} == {
        ("ds_a", "offline_sales"), ("ds_b", "mp_orders")}


def test_meanings_on_one_table_stay_prompt_context_only():
    """Both would bind every query of that table, and one query satisfies only one."""
    one_table = [ds("ds_x", "Acme L3", [
        concept("Gross GMV", synonyms=["GMV"], description="gross"),
        concept("Average GMV", synonyms=["GMV"], description="per order",
                aggregation="AVG"),
    ])]
    analysis = analyze(MSG, one_table)
    assert metric_confirm.enforceable(analysis) == []
    [reading] = metric_confirm.readings(analysis, MSG)
    assert "checked against it" not in reading["_context"]


def test_chosen_is_the_meaning_the_query_satisfied():
    [reading] = metric_confirm.readings(analyze(), MSG)
    step = {"ok": True, "safe_sql": "SELECT SUM(gmv) FROM mp_orders WHERE month_id = 202607",
            "dialect": "duckdb", "datasource_id": "ds_b"}
    assumptions.settle([reading], [step])
    marketplace = next(o for o in reading["options"] if o["descr"] == "marketplace take")
    assert reading["chosen"] == marketplace["id"]
    # a query that computes neither curated meaning chooses nothing
    [other] = metric_confirm.readings(analyze(), MSG)
    assumptions.settle([other], [{**step, "safe_sql": "SELECT MAX(gmv) FROM brands_x"}])
    assert other["chosen"] is None


# ---- /v1/chat end to end ----
@pytest.fixture
def world(tmp_path):
    import duckdb

    tid = store.create_tenant("METRIC-reading-e2e")
    store.create_role(tid, "analyst", {"full_access": True, "can_see_pii": True})
    for ident, name, table, descr in (
            ("A", "Datamart", "offline_sales", "offline take"),
            ("B", "Acme L3", "mp_orders", "marketplace take")):
        path = str(tmp_path / f"{ident}.duckdb")
        con = duckdb.connect(path)
        con.execute(f"CREATE TABLE {table}(month_id INTEGER, gmv DOUBLE)")
        con.execute(f"INSERT INTO {table} VALUES (202607, 125.0)")
        con.close()
        store.create_datasource(
            tid, "duckdb", name, {"path": path},
            {"tables": {table: ["month_id", "gmv"]}, "allowed_tables": [table],
             "business_concepts": [concept("GMV", table=table, description=descr)]})
    store.set_tenant_config(tid, "semantic", {"strict_metrics": True})
    key = client.post("/admin/apikeys", headers=AK,
                      json={"tenant_id": tid, "label": "pytest"}).json()["key"]
    token = pyjwt.encode({"sub": "u", "role": "analyst"}, "test-secret", algorithm="HS256")
    yield {"tid": tid,
           "headers": {"X-API-Key": key, "Authorization": f"Bearer {token}"}}
    store.delete_tenant(tid)


def _chat(world, message, **body):
    return client.post("/v1/chat", headers=world["headers"],
                       json={"message": message, **body})


def test_chat_answers_with_the_meaning_it_took_instead_of_asking(world):
    r = _chat(world, MSG)
    assert r.status_code == 200, r.text
    out = r.json()
    assert "ask_metric" not in out["plan"] and "confirmation" not in out
    assert out["status"] != "awaiting_confirmation"
    [reading] = out["assumptions"]
    assert reading["kind"] == "metric"
    # the governed query computed one curated meaning, and that one is reported chosen
    assert out["sql"]["ok"] is True
    chosen = next(o for o in reading["options"] if o["id"] == reading["chosen"])
    table = "mp_orders" if chosen["descr"] == "marketplace take" else "offline_sales"
    assert table in out["sql"]["safe_sql"]
    audit = [r for r in store.list_audit(world["tid"]) if r["event"] == "semantic_collision"]
    assert audit and audit[0]["status"] == "assumed"
    answer = next(a for a in store.list_audit(world["tid"]) if a["event"] == "answer")
    assert answer["detail"]["assumptions"][0]["chosen"] == reading["chosen"]


def test_an_open_phrase_routes_without_connection_order():
    from app.agent import orchestrator
    forward = orchestrator._semantic_route(analyze(MSG, COLLIDING))
    backward = orchestrator._semantic_route(analyze(MSG, list(reversed(COLLIDING))))
    assert forward == backward and forward in ("ds_a", "ds_b")
