"""A term that only RESEMBLES a known value is an open reading, never used silently.

The reported failure: "What is GMV for Philips" was answered from data that says
**Philipsome**. Phases 1-3 stopped the cross-slice half of that; this is the other half,
inside one slice. Until chat overhaul item 5 the turn ended in a question; now the
candidates go to the model as an open reading (app/assumptions.py), the answer says which
one it took, and each other option is a one-click correction.

What is worth pinning down is mostly where the check STAYS QUIET, because a reading that
fires on ordinary questions is noise:

  * an exact value under ONE meaning is never open, however many tables carry it — that
    is a curation job (business_concepts naming the canonical table);
  * an exact value under SEVERAL meanings is open, because "Samsung the brand" and
    "Samsung the seller" are different questions — unless the question already says
    which ("gmv brand Samsung");
  * words the SCHEMA owns (tables, columns, metrics, concepts) are not entities;
  * a term nothing resembles is not open either — "no rows" is an honest answer;
  * a correction settles it: the picked value is exact, a meaning is spelled out, and
    "use exactly what I wrote" keeps the person's own word.
"""
import sys
from pathlib import Path

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import entity_confirm, store                              # noqa: E402
from app.main import app                                           # noqa: E402

client = TestClient(app)
AK = {"X-Admin-Key": "admin-dev-key"}

MDL = {
    "tables": {"mp_orders": ["month_id", "gmv", "brand", "warehouse"]},
    "allowed_tables": ["mp_orders"],
    "metrics": [{"name": "GMV", "definition": "SUM(gmv)"}],
    "business_concepts": [{"name": "GMV", "synonyms": ["gross merchandise value"],
                           "table": "mp_orders"}],
    "reference_values": {
        "brand": ["Philipsome", "Philco", "Samsung", "Sharp"],
        "warehouse": ["Marunda", "Cakung"],
    },
}
SOURCES = [{"id": "ds1", "name": "Acme L3", "type": "duckdb", "mdl": MDL}]
QUESTION = "berapa gmv brand Philips bulan Juli 2026?"

# same catalog, but "Samsung" is both a brand and a seller
TWO_MEANINGS = [{"id": "ds1", "name": "Acme L3", "type": "duckdb", "mdl": {
    **MDL, "tables": {"mp_orders": ["month_id", "gmv", "brand", "seller"]},
    "reference_values": {"brand": ["Philipsome", "Samsung"],
                         "seller": ["Samsung", "Erafone"]}}}]
# same meaning, two datasources: one dimension either side, so nothing to ask
TWO_SOURCES = [{"id": "ds1", "name": "Acme L3", "type": "duckdb", "mdl": MDL},
               {"id": "ds2", "name": "Acme L2", "type": "duckdb", "mdl": {
                   "tables": {"offline_sales": ["month_id", "gmv", "brand"]},
                   "reference_values": {"brand": ["Samsung", "Sharp"]}}}]
# live case: auto-enriched return_reason values carry the word "order" — in a slice where
# every identifier speaks "order", that word is schema language, never an entity
ORDER_SOURCES = [{"id": "ds1", "name": "Acme L3", "type": "duckdb", "mdl": {
    "tables": {"acme_offline_sales_order_odoo_ai_curated": [
        "order_id", "client_name", "order_status", "return_reason", "total_price_invoice"]},
    "reference_values": {
        "return_reason": ["Salah Order", "Toko Salah Order", "Sales salah order",
                          "Cancel order"],
        "order_status": ["sale", "cancel"]}}}]


def _one(message, sources):
    """The single open reading of a question, or None."""
    found = entity_confirm.readings(message, sources)
    assert len(found) <= 1, found
    return found[0] if found else None


# ---- when a reading is open ----
def test_a_term_that_only_resembles_the_data_is_an_open_reading():
    ask = _one(QUESTION, SOURCES)
    assert ask["kind"] == "entity" and ask["term"] == "Philips"   # as they typed it
    # only Philipsome (plus the person's own word): "Philco" shares a prefix but is not
    # close enough, and offering every vaguely similar value is how a reading turns into
    # noise
    assert [o["name"] for o in ask["options"]] == ["Philipsome", "Philips"]
    assert ask["chosen"] is None and "question" not in ask      # nothing is asked
    assert ask["_context"] == ("'Philips' matches no value in the data exactly; the "
                               "closest: 'Philipsome' (brand, Acme L3). The person may "
                               "also mean exactly 'Philips'.")


def test_keeping_the_persons_own_word_is_a_correction_that_settles_it():
    ask = _one(QUESTION, SOURCES)
    keep = next(o for o in ask["options"] if o.get("use_exact"))
    assert keep["name"] == "Philips"
    assert keep["question"] == QUESTION + ' (persis "Philips")'
    assert _one(keep["question"], SOURCES) is None                # no longer open
    english = _one("what is the GMV for Philips?", SOURCES)
    assert next(o for o in english["options"] if o.get("use_exact"))["question"] == (
        'what is the GMV for Philips? (exactly "Philips")')


def test_each_option_carries_the_question_it_would_answer():
    """Built server-side so every client does the same thing with a pick."""
    ask = _one(QUESTION, SOURCES)
    chosen = next(o for o in ask["options"] if o["name"] == "Philipsome")
    assert chosen["question"] == "berapa gmv brand Philipsome bulan Juli 2026?"


def test_the_option_says_where_the_value_lives():
    ask = _one(QUESTION, SOURCES)
    assert ask["options"][0]["descr"] == "brand · Acme L3"


def test_a_typo_is_caught_by_similarity_not_only_containment():
    ask = _one("berapa gmv Samsun bulan Juli?", SOURCES)
    assert [o["name"] for o in ask["options"] if not o.get("use_exact")] == ["Samsung"]


def test_the_option_wording_follows_the_language_of_the_question():
    english = _one("what is the GMV for Philips?", SOURCES)
    assert english["options"][-1]["descr"] == "use exactly what I wrote"
    assert _one(QUESTION, SOURCES)["options"][-1]["descr"] == "pakai persis yang saya tulis"


# ---- when it stays quiet ----
def test_an_exact_value_is_never_open():
    assert _one("berapa gmv brand Philipsome bulan Juli?", SOURCES) is None


def test_an_exact_value_inside_a_longer_phrase_is_still_exact():
    """"brand philipsome" resembles nothing on its own; the part that matters already
    matched, so there is nothing to confirm."""
    assert _one("gmv untuk brand Philipsome di Marunda", SOURCES) is None


def test_schema_words_are_not_entities():
    """A question naming a table, column, metric or concept speaks the schema's language."""
    for term in ("mp_orders", "warehouse", "gross merchandise value"):
        assert _one(f"berapa total {term} bulan Juli?", SOURCES) is None, term


def test_a_word_the_identifiers_are_made_of_is_not_an_entity():
    """Live case (2026-09-01): right after auto-enrich filled return_reason with values
    like "Cancel order", the ordinary word "order" in an analytics sentence got the
    which-value interrogation — in a slice where every table and column speaks "order".
    Identifier PARTS are schema language, same as the fused identifiers."""
    q = ("tampilkan 30 client_name unik dengan order terbanyak dari "
         "acme_offline_sales_order_odoo_ai_curated")
    assert _one(q, ORDER_SOURCES) is None
    assert _one("berapa order cancel bulan ini?", ORDER_SOURCES) is None


def test_identifier_parts_do_not_blanket_silence_real_entities():
    """The parts filter must only cover the schema's own words: a term that is part of
    no identifier ("Philips") keeps asking exactly as before."""
    ask = _one(QUESTION, SOURCES)
    assert ask is not None and ask["term"] == "Philips"


def test_a_term_nothing_resembles_is_not_open():
    assert _one("berapa gmv brand Panasonic bulan Juli?", SOURCES) is None


def test_a_datasource_without_reference_values_is_skipped_entirely():
    bare = [{"id": "d", "name": "x", "type": "duckdb", "mdl": {"tables": {"t": ["a"]}}}]
    assert _one(QUESTION, bare) is None


def test_a_term_half_the_catalog_resembles_is_treated_as_a_generic_word():
    noisy = [{"id": "d", "name": "x", "type": "duckdb", "mdl": {"reference_values": {
        "sku": [f"SKU-PROD-{n:03d}" for n in range(20)]}}}]
    assert _one("berapa gmv sku-prod bulan Juli?", noisy) is None


def test_short_terms_are_never_matched():
    """"3P" resembles far too much to mean anything."""
    assert _one("berapa gmv 3P bulan Juli?", SOURCES) is None


def test_prose_pasted_into_reference_values_is_not_an_entity():
    """Live Acme case: routing INSTRUCTIONS were saved as reference_values (the editor is
    comma-separated, so the sentence even split in two). A sentence can never be matched
    exactly by a candidate phrase, so indexing it manufactures confirmations no pick can
    resolve — "zentra" asked forever, and every pick pasted the sentence into the question."""
    prose = [{"id": "d", "name": "Acme L3", "type": "duckdb", "mdl": {"reference_values": {
        "client_name": ["if the user ask about the brand",
                        "you should refering to this column. (example: zentra is the client)"],
        "product_name": ["if the user ask about product from the brand or client",
                         "you should refering to this column"],
    }}}]
    assert _one(
        "saya mau tau berapa banyak total quantity dari zentra", prose) is None
    # the rewritten monstrosity an old pick produced is quiet too, never re-asked
    assert _one(
        "saya mau tau berapa banyak total quantity dari you should refering to this "
        "column. (example: zentra is the client)", prose) is None


def test_a_real_long_name_is_still_open_and_its_correction_settles_it():
    """5 words / 60 chars is still a NAME: a near-miss is open, and the rewritten question
    a correction produces is settled even though the value is longer than the candidate
    window — its fragments must not reopen the reading just settled."""
    ds = [{"id": "d", "name": "Acme L3", "type": "duckdb", "mdl": {"reference_values": {
        "client_name": ["ZENTRA FOOD INTERNATIONAL INDONESIA TBK"]}}}]
    ask = _one("total quantity dari zentra food bulan Juli", ds)
    assert ask and ask["kind"] == "entity"
    picked = ask["options"][0]["question"]
    assert "ZENTRA FOOD INTERNATIONAL INDONESIA TBK" in picked
    assert _one(picked, ds) is None


# ---- the same value meaning two different things ----
def test_a_value_under_two_dimensions_is_open():
    ask = _one("berapa gmv Samsung bulan Juli 2026?", TWO_MEANINGS)
    assert ask["term"] == "Samsung"
    assert [o["name"] for o in ask["options"]] == ["brand", "seller"]
    assert ask["_context"] == ("'Samsung' is a value of more than one column: "
                               "brand (Acme L3); seller (Acme L3).")


def test_correcting_a_meaning_spells_it_out_instead_of_rewriting_the_question():
    """The value was already right — only which column it lives in was in doubt."""
    ask = _one("berapa gmv Samsung bulan Juli 2026?", TWO_MEANINGS)
    seller = next(o for o in ask["options"] if o["name"] == "seller")
    assert seller["question"] == ("berapa gmv Samsung bulan Juli 2026? "
                                  "(yang saya maksud: Samsung sebagai seller)")


def test_a_question_that_already_names_the_dimension_is_not_open():
    """"gmv brand Samsung" already said which one."""
    assert _one("berapa gmv brand Samsung bulan Juli?", TWO_MEANINGS) is None


def test_one_meaning_spread_over_two_datasources_is_not_ambiguous():
    """A brand living in both L2 and L3 is ONE meaning. Which table to read from is a
    curation decision (business_concepts), not a reading of the person's words."""
    assert _one("berapa gmv Samsung bulan Juli?", TWO_SOURCES) is None


def test_a_value_with_one_meaning_is_never_open():
    assert _one("berapa gmv Erafone bulan Juli?", TWO_MEANINGS) is None


def test_the_meaning_correction_follows_the_language_of_the_question():
    ask = _one("what is the GMV for Samsung?", TWO_MEANINGS)
    assert next(o for o in ask["options"] if o["name"] == "seller")["question"].endswith(
        "(I mean: Samsung as seller)")


# ---- several open names in one question ----
def test_every_open_name_of_a_question_is_read_at_once():
    """An ask settled one name per turn; a reading has no reason to wait, so both names
    go to the model in the same turn."""
    found = entity_confirm.readings("berapa gmv Philips di Marundo bulan Juli?", SOURCES)
    assert [r["term"] for r in found] == ["Philips", "Marundo"]
    assert [o["name"] for o in found[1]["options"] if not o.get("use_exact")] == ["Marunda"]


def test_a_correction_settles_its_name_and_leaves_the_other_open():
    first = entity_confirm.readings("berapa gmv Philips di Marundo bulan Juli?", SOURCES)[0]
    picked = next(o for o in first["options"] if o["name"] == "Philipsome")["question"]
    assert [r["term"] for r in entity_confirm.readings(picked, SOURCES)] == ["Marundo"]


# ---- /v1/chat end to end ----
@pytest.fixture
def world(tmp_path):
    import duckdb
    from app.connectors.factory import make_connector
    from app.schema_scan import scan_connector

    path = str(tmp_path / "acme.duckdb")
    con = duckdb.connect(path)
    con.execute("CREATE TABLE mp_orders(month_id INTEGER, gmv DOUBLE, brand VARCHAR)")
    con.execute("INSERT INTO mp_orders VALUES (202607, 250.0, 'Philipsome')")
    con.close()

    tid = store.create_tenant("BIZ-entity")
    store.create_role(tid, "analyst", {"full_access": True, "can_see_pii": True})
    mdl = scan_connector(make_connector("duckdb", {"path": path}))
    mdl["reference_values"] = {"brand": ["Philipsome", "Philco", "Samsung"]}
    did = store.create_datasource(tid, "duckdb", "Acme L3", {"path": path}, mdl)
    store.set_tenant_config(tid, "business", {"confirm_entities": True})
    yield {"tid": tid, "did": did}
    store.delete_tenant(tid)


def _chat(tid, message, **body):
    key = client.post("/admin/apikeys", headers=AK,
                      json={"tenant_id": tid, "label": "pytest"}).json()["key"]
    token = pyjwt.encode({"sub": "u", "role": "analyst"}, "test-secret", algorithm="HS256")
    return client.post("/v1/chat", headers={"X-API-Key": key,
                                            "Authorization": f"Bearer {token}"},
                       json={"message": message, **body})


def test_chat_answers_and_says_which_reading_it_took(world):
    """No question before the answer: the turn runs, and the open reading travels to the
    client (and the audit) without its server-only keys."""
    out = _chat(world["tid"], QUESTION).json()
    assert "ask_entity" not in out["plan"] and "confirmation" not in out
    assert out["status"] != "awaiting_confirmation"
    reading = out["assumptions"][0]
    assert reading["kind"] == "entity" and reading["term"] == "Philips"
    assert "Philipsome" in [o["name"] for o in reading["options"]]
    assert not any(k.startswith("_") for k in reading)
    answer = next(a for a in store.list_audit(world["tid"]) if a["event"] == "answer")
    assert answer["detail"]["assumptions"][0]["term"] == "Philips"


def test_chat_is_unchanged_while_the_toggle_is_off(world):
    store.set_tenant_config(world["tid"], "business", {"confirm_entities": False})
    assert _chat(world["tid"], QUESTION).json()["assumptions"] == []
