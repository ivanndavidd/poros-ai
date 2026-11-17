"""Offline tests for the multi-datasource router (orchestrator._route_datasource).

Pure heuristic (no LLM, no DB): scores a question against each datasource's DISTINCTIVE
structural terms (table/column names + reference values). Fixes the bug where every question
hit the first datasource, so marketplace-sales questions were answered from an inventory DB.
"""
from app.agent import orchestrator as orch

INV = {"id": "ds_inv", "name": "IMS Acme", "type": "bigquery", "mdl": {
    "tables": {"datamart_acme_ims_inbound": ["Quantity", "Storage_Site_Name", "sales_channel"],
               "datamart_acme_ims_outbound": ["Quantity", "Storage_Site_Name", "sales_channel", "warehouse_name"]},
    "reference_values": {}}}
MP = {"id": "ds_mp", "name": "Acme L2", "type": "bigquery", "mdl": {
    "tables": {"acme_store_order_mp": ["marketplace", "gmv", "nmv", "provinsi_match_clean", "brand", "quantity"],
               "acme_store_product_mp": ["marketplace", "sales_revenue"]},
    "reference_values": {"marketplace": ["TikTok", "Tokopedia", "Shopee", "Blibli"]}}}


def test_marketplace_question_routes_to_mp():
    assert orch._route_datasource("t", "heatmap sales per region dari Shopee", [INV, MP])["id"] == "ds_mp"
    assert orch._route_datasource("t", "berapa total GMV Blibli", [INV, MP])["id"] == "ds_mp"
    assert orch._route_datasource("t", "penjualan per marketplace bulan ini", [INV, MP])["id"] == "ds_mp"


def test_inventory_question_routes_to_inventory():
    # 'sales_channel' is distinctive to the inventory DB; marketplace terms absent
    assert orch._route_datasource("t", "outbound per sales_channel di storage site", [INV, MP])["id"] == "ds_inv"


def test_single_datasource_returned_as_is():
    assert orch._route_datasource("t", "apa saja", [MP])["id"] == "ds_mp"
    assert orch._route_datasource("t", "apa saja", []) is None


def test_no_distinctive_match_falls_back_to_first():
    # a question with no distinctive term keeps the first datasource (previous behavior)
    assert orch._route_datasource("t", "halo apa kabar", [INV, MP])["id"] == "ds_inv"


# Acme-shaped fixtures WITH a curation layer: everyday words ('pesanan', 'order', 'gudang')
# only exist in descriptions/metrics/examples, never in table/column names.
INV_DESC = {**INV, "mdl": {**INV["mdl"], "descriptions": {
    "datamart_acme_ims_outbound": "Pergerakan stok keluar gudang (outbound) per storage site"}}}
MP_DESC = {**MP, "mdl": {**MP["mdl"],
    "descriptions": {"acme_store_order_mp": "Pesanan (order) marketplace harian: GMV, NMV, quantity"},
    "metrics": [{"name": "total_pesanan", "definition": "COUNT(*)", "note": "jumlah pesanan marketplace"}],
    "examples": [{"q": "berapa penjualan bulan lalu?", "sql": "SELECT SUM(gmv) FROM acme_store_order_mp"}]}}


def test_generic_order_word_routes_via_curated_vocabulary():
    # live regression (Acme t_demo01): 'pesanan' matches no table/column token, so the
    # question fell back to the FIRST datasource (inventory) and answered COUNT=0.
    # With MDL descriptions/metrics in the term bags it must reach the sales datasource.
    assert orch._route_datasource("t", "berapa jumlah pesanan bulan Mei 2026?", [INV_DESC, MP_DESC])["id"] == "ds_mp"
    assert orch._route_datasource("t", "total penjualan minggu ini", [INV_DESC, MP_DESC])["id"] == "ds_mp"
    assert orch._route_datasource("t", "stok keluar gudang hari ini", [INV_DESC, MP_DESC])["id"] == "ds_inv"


def test_structural_match_outweighs_description_vocabulary():
    # one structural token (outbound, weight 1.0) must beat one description word
    # (pesanan, weight 0.25) — a heavily-described datasource cannot hijack routing.
    assert orch._route_datasource("t", "outbound pesanan", [INV_DESC, MP_DESC])["id"] == "ds_inv"


def test_shared_description_words_are_not_distinctive():
    # a word appearing in BOTH curation layers ('harian') must not score for either side,
    # so the question keeps the previous first-datasource fallback.
    inv = {**INV, "mdl": {**INV["mdl"], "descriptions": {"datamart_acme_ims_inbound": "ringkasan harian"}}}
    mp = {**MP, "mdl": {**MP["mdl"], "descriptions": {"acme_store_order_mp": "ringkasan harian"}}}
    assert orch._route_datasource("t", "ringkasan harian dong", [inv, mp])["id"] == "ds_inv"


def test_numeric_tokens_are_not_routing_signals():
    # live regression: '15' from 'naik 15%' matched a number in the FIRST datasource's
    # catalog, tied the score, and hijacked a GMV question to the inventory DB.
    inv_with_number = {"id": "ds_inv", "name": "IMS", "type": "bigquery", "mdl": {
        "tables": {"datamart_inbound": ["Quantity", "sales_channel"]},
        "reference_values": {"limits": [15, 30, 90]}}}
    got = orch._route_datasource("t", "kalau GMV naik 15% jadi berapa?", [inv_with_number, MP])
    assert got["id"] == "ds_mp"


def test_curated_business_alias_routes_3p_to_datamart_even_when_not_first():
    datamart = {**INV, "name": "Datamart", "mdl": {
        **INV["mdl"],
        "routing_aliases": ["3P", "third party", "IMS client"],
    }}
    assert orch._route_datasource(
        "t", "berapa jumlah 3P aktif saat ini?", [MP, datamart],
    )["id"] == "ds_inv"
    assert orch._route_datasource(
        "t", "show third-party clients by warehouse", [MP, datamart],
    )["id"] == "ds_inv"


def test_datasource_name_and_bigquery_dataset_are_routing_terms():
    datamart = {**INV, "name": "Warehouse Operations", "conn": {"dataset": "Datamart"}}
    assert orch._route_datasource(
        "t", "gunakan dataset Datamart", [MP, datamart],
    )["id"] == "ds_inv"


# Live regression (Acme staging, 2026-08-18): "berapa gmv brand philips bulan Juli 2026" was
# answered from a 3PL COMPLAINT table. 'Philips' was a sampled reference VALUE there, which
# scored like a schema identifier, tied with the real GMV source and lost to list order.
DATAMART_3PL = {"id": "ds_datamart", "name": "Datamart", "type": "bigquery",
                "conn": {"project": "blibli", "dataset": "datamart"}, "mdl": {
    "tables": {"3pl_complaint_detail": ["proc_inst_id", "brand", "order_date", "grossordervalue"]},
    "reference_values": {"brand": ["Philips", "Samsung", "Sharp"]},
    "descriptions": {"3pl_complaint_detail": "Detail komplain pengiriman pihak ketiga"}}}
ACME_L3 = {"id": "ds_acme_l3", "name": "Acme L3", "type": "bigquery",
          "conn": {"project": "blibli", "dataset": "ACME_L3"}, "mdl": {
    "tables": {"acme_store_order_mp_ai_curated": ["order_id", "brand", "order_date", "gmv", "nmv"]},
    "metrics": [{"name": "GMV", "definition": "SUM(gmv) FROM acme_store_order_mp_ai_curated"},
                {"name": "NMV", "definition": "SUM(nmv) FROM acme_store_order_mp_ai_curated"}],
    "descriptions": {"acme_store_order_mp_ai_curated": "Order marketplace terkurasi"}}}


def test_kpi_question_beats_incidental_reference_value_match():
    for question in ("berapa gmv brand philips bulan Juli 2026",
                     "gmv philips", "berapa NMV Samsung Juli 2026"):
        assert orch._route_datasource("t", question, [DATAMART_3PL, ACME_L3])["id"] == "ds_acme_l3"


def test_value_match_still_routes_when_no_kpi_is_named():
    # the complaint datasource keeps questions that are actually about its own subject
    assert orch._route_datasource(
        "t", "berapa komplain 3pl untuk Philips", [DATAMART_3PL, ACME_L3],
    )["id"] == "ds_datamart"
    assert orch._route_datasource(
        "t", "total grossordervalue philips", [DATAMART_3PL, ACME_L3],
    )["id"] == "ds_datamart"


def test_curated_kpi_name_outranks_a_plain_column_name_elsewhere():
    # a datasource that merely HAS a 'gmv' column must not beat the one that DEFINES the KPI
    raw_gmv = {**DATAMART_3PL, "mdl": {
        **DATAMART_3PL["mdl"],
        "tables": {"3pl_complaint_detail": ["proc_inst_id", "brand", "gmv"]}}}
    assert orch._route_datasource(
        "t", "berapa gmv bulan ini", [raw_gmv, ACME_L3],
    )["id"] == "ds_acme_l3"


def test_same_kpi_defined_on_both_sources_falls_through_to_scoring():
    # ambiguous curation must not silently pick one source; token scoring decides instead
    both = {**DATAMART_3PL, "mdl": {
        **DATAMART_3PL["mdl"],
        "metrics": [{"name": "GMV", "definition": "SUM(grossordervalue)"}]}}
    got = orch._route_datasource("t", "berapa gmv order marketplace", [both, ACME_L3])
    assert got["id"] == "ds_acme_l3"
