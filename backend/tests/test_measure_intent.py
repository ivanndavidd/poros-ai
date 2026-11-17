"""Measure intent: 'how much' asks for VALUE (IDR), 'how many' asks for a unit COUNT.

Live case behind this (feedback fb_061885f7, tenant Acme): "How much inventory Acme has?"
was answered with 14.438 UNITS. Both measures usually live side by side in one table
(quantity + gmv/price), so the question's wording is the only signal for which to use.
Offline — regex + prompt assembly only, no LLM and no store."""
from app import nl2sql
from app.agent import sql_agent


# ---- cue detection ----

def test_english_how_much_asks_for_value():
    assert nl2sql.measure_intent("How much inventory Acme has? As of today?") == "value"
    assert nl2sql.measure_intent("how much did we sell in May") == "value"
    assert nl2sql.measure_intent("What is the value of our stock?") == "value"
    assert nl2sql.measure_intent("what's the total value of inventory") == "value"
    assert nl2sql.measure_intent("Total value held in Marunda") == "value"


def test_english_how_many_asks_for_count():
    assert nl2sql.measure_intent("How many units of inventory does Acme have?") == "count"
    assert nl2sql.measure_intent("number of orders last month") == "count"
    assert nl2sql.measure_intent("count of active SKUs") == "count"


def test_indonesian_value_wordings():
    """Indonesian has no 'how much' — the money words the user adds are the signal."""
    for q in ("Berapa nilai inventory Acme hari ini?",
              "berapa rupiah stok di gudang",
              "berapa total nilai persediaan",
              "berapa nominal stok tersedia",
              "berapa banyak nilai aset yang tersimpan",   # count wording, money question
              "total aset yang tersimpan",
              "total nilai stok gudang",
              "stok senilai berapa di Marunda",
              "tampilkan stok dalam rupiah",
              "berapa omzet bulan Juli",
              "berapa pendapatan bulan ini"):
        assert nl2sql.measure_intent(q) == "value", q


def test_indonesian_count_wordings():
    for q in ("berapa banyak pesanan bulan ini",
              "berapa banyaknya item aktif",
              "berapa unit stok tersedia",
              "berapa pcs terkirim kemarin",
              "berapa karton keluar dari gudang",
              "ada berapa item di gudang",
              "jumlah unit terjual",
              "total koli inbound bulan ini"):
        assert nl2sql.measure_intent(q) == "count", q


def test_bare_indonesian_berapa_stays_neutral():
    """'berapa stok gudang' genuinely does not say value-or-units.

    Guessing here would invent an intent the sentence never carried, so nothing is forced
    and the curated catalog/business contract decides as before."""
    for q in ("berapa stok gudang",
              "berapa pesanan masuk hari ini",
              "berapa inventory Acme hari ini"):
        assert nl2sql.measure_intent(q) is None, q


def test_neutral_and_both_wordings_force_nothing():
    # No cue at all -> the model keeps its normal freedom to pick the measure.
    assert nl2sql.measure_intent("Total GMV per marketplace bulan ini") is None
    assert nl2sql.measure_intent("") is None
    assert nl2sql.measure_intent(None) is None
    # Asking for BOTH must not drop half the answer.
    assert nl2sql.measure_intent("How many units and how much value do we hold?") is None


def test_colloquial_count_wording_on_a_money_metric_forces_nothing():
    """Indonesian says 'berapa banyak GMV' where English says 'how much GMV'.

    GMV/revenue have no meaningful unit count, and acme_store_order_mp holds `quantity`
    right next to `gmv` — forcing the count reading here would answer the wrong column."""
    assert nl2sql.measure_intent("berapa banyak GMV bulan Juli") is None
    assert nl2sql.measure_intent("berapa banyak revenue bulan ini") is None
    # A genuine unit question on a non-monetary entity still resolves to count.
    assert nl2sql.measure_intent("berapa banyak unit terkirim bulan Juli") == "count"


def test_non_monetary_how_much_is_not_hijacked():
    """'how much time/space/percent' are not money questions."""
    for q in ("How much time does the SOP take?",
              "How much space is left in the warehouse?",
              "How much longer until delivery?",
              "How much capacity do we have?"):
        assert nl2sql.measure_intent(q) is None, q
    # Indonesian equivalents carry no value cue either.
    assert nl2sql.measure_intent("berapa lama proses inbound") is None
    assert nl2sql.measure_intent("berapa kali retur terjadi") is None


def test_substring_does_not_match():
    """A cue must be a phrase, not a fragment inside another word."""
    assert nl2sql.measure_intent("show much_value column") is None
    assert nl2sql.measure_intent("how manyfold the growth is") is None


# ---- instruction blocks ----

def test_value_block_demands_currency_and_forbids_inventing_it():
    block = nl2sql.measure_instructions("How much inventory Acme has?")
    assert "MONETARY" in block
    # The honesty clause is the whole safety of this feature: no monetary column in the
    # catalog (live: the IMS inventory tables carry quantity only) must NOT become a
    # fabricated rupiah figure, nor a unit count silently passed off as the value.
    assert "do NOT invent" in block
    assert "LABELLED as a quantity" in block


def test_count_block_prefers_quantity():
    block = nl2sql.measure_instructions("How many orders last month?")
    assert "QUANTITY" in block
    assert "COUNT DISTINCT" in block


def test_neutral_question_adds_no_block():
    assert nl2sql.measure_instructions("Total GMV per marketplace") == ""


# ---- prompt wiring (the block must reach the SQL-writing prompt, not only the narrative) ----

_MDL = {
    "tables": {"inventory_snapshot": ["item_id", "available_stock", "report_date"]},
    "allowed_tables": ["inventory_snapshot"],
}


def test_sql_agent_prompt_carries_value_intent():
    prompt = sql_agent._system_prompt(_MDL, question="How much inventory Acme has?")
    assert "MEASURE INTENT ('how much' = VALUE" in prompt


def test_sql_agent_prompt_carries_count_intent():
    prompt = sql_agent._system_prompt(_MDL, question="How many units does Acme have?")
    assert "MEASURE INTENT ('how many' = COUNT" in prompt


def test_sql_agent_prompt_stays_clean_when_neutral():
    prompt = sql_agent._system_prompt(_MDL, question="Total stock per item")
    assert "MEASURE INTENT" not in prompt


def test_sql_agent_prompt_without_question_is_unchanged():
    """Sub-goal loops call _system_prompt with no question; it must not crash."""
    assert "MEASURE INTENT" not in sql_agent._system_prompt(_MDL)
