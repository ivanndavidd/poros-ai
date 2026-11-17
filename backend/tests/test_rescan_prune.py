"""Rescan must not keep curation for tables the fresh scan no longer returns.

The live bug (tenant Acme Assistant / datasource "Acme L2"): a rescan dropped
`sample_inventory_daily_snapshot` from `tables` and `allowed_tables`, but kept its
descriptions, grain, reference values, exemplar SQL and business concept. The catalog
prompt still taught the model to query it, and govern() then rejected the generated SQL
as not-allowlisted — a blocked answer with no visible cause.

Vanished curation is PARKED (mdl_merge.QUARANTINE_KEY), not deleted, so a table that
disappeared because a scan partially failed does not cost the admin their hand-written
catalog.

Run:  cd backend && python -m pytest tests/test_rescan_prune.py -q
Fully offline.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

from app import mdl_merge, nl2sql, semantic_contract  # noqa: E402
from app.mdl_merge import QUARANTINE_KEY, ScanLostAllTables, merge_scan  # noqa: E402

PHANTOM = "sample_inventory_daily_snapshot"
PHANTOM_SQL = (
    f"SELECT sto_item_name, sto_available_stock FROM {PHANTOM} "
    f"WHERE report_date = (SELECT MAX(report_date) FROM {PHANTOM}) "
    "ORDER BY sto_available_stock DESC"
)


def _fresh():
    """What the connector returns today — no PHANTOM."""
    return {
        "tables": {
            "acme_store_order_mp": ["marketplace", "gmv", "month_id"],
            "acme_store_stock": ["sku", "available_stock", "report_date"],
        },
        "allowed_tables": ["acme_store_order_mp", "acme_store_stock"],
        "pii_columns": [],
        "denied_functions": ["version", "current_user"],
        "allowed_functions": [],
        "descriptions": {"acme_store_order_mp": "Pesanan marketplace (hasil scan)"},
    }


def _fresh_with_phantom():
    """The same scan after the missing table becomes visible again."""
    fresh = _fresh()
    fresh["tables"][PHANTOM] = [
        "sto_item_code", "sto_item_name", "sto_available_stock", "item_active",
        "has_expired", "report_date",
    ]
    fresh["allowed_tables"] = sorted(fresh["tables"])
    return fresh


def _stored(**over):
    """The MDL as stored: live curation plus curation for a table that vanished."""
    mdl = {
        **_fresh(),
        "descriptions": {
            "acme_store_order_mp": "Pesanan marketplace (kurasi admin)",
            "gmv": "Gross merchandise value",                 # bare LIVE column key
            PHANTOM: "Snapshot stok harian",
            f"{PHANTOM}.sto_item_code": "Kode item",
            "sto_available_stock": "Stok tersedia",           # bare DEAD column key
        },
        "grain": {
            "acme_store_order_mp": "1 baris = 1 order item",
            PHANTOM: "1 baris = 1 item per hari",
        },
        "reference_values": {
            "marketplace": ["Shopee", "Tokopedia"],
            f"{PHANTOM}.item_active": ["Y", "N"],
        },
        "metrics": [{"name": "GMV", "definition": "SUM(gmv) FROM acme_store_order_mp"}],
        "examples": [
            {"q": "gmv per marketplace",
             "sql": "SELECT marketplace, SUM(gmv) FROM acme_store_order_mp GROUP BY 1"},
            {"q": "stok terkini", "sql": PHANTOM_SQL},
        ],
        "business_concepts": [
            {"name": "GMV", "table": "acme_store_order_mp", "metric": "gmv",
             "aggregation": "SUM", "time_dimension": "month_id"},
            {"name": "Stok tersedia", "table": PHANTOM, "metric": "sto_available_stock",
             "aggregation": "SUM"},
        ],
        "routing_aliases": ["Acme", "L2"],
    }
    mdl.update(over)
    return mdl


def _concept(mdl, name):
    return next(
        (c for c in mdl.get("business_concepts") or [] if c.get("name") == name), None
    )


# ---- the reported bug ----
def test_vanished_table_curation_leaves_the_live_catalog():
    merged, _ = merge_scan(_stored(), _fresh())

    assert PHANTOM not in merged["descriptions"]
    assert f"{PHANTOM}.sto_item_code" not in merged["descriptions"]
    assert "sto_available_stock" not in merged["descriptions"]     # bare dead column
    assert PHANTOM not in merged["grain"]
    assert f"{PHANTOM}.item_active" not in merged["reference_values"]
    assert all(PHANTOM not in e["sql"] for e in merged["examples"])
    assert _concept(merged, "Stok tersedia") is None


def test_live_curation_survives_the_prune():
    merged, _ = merge_scan(_stored(), _fresh())

    assert merged["descriptions"]["acme_store_order_mp"] == "Pesanan marketplace (kurasi admin)"
    assert merged["descriptions"]["gmv"] == "Gross merchandise value"   # bare LIVE column
    assert merged["grain"]["acme_store_order_mp"] == "1 baris = 1 order item"
    assert merged["reference_values"]["marketplace"] == ["Shopee", "Tokopedia"]
    assert [e["q"] for e in merged["examples"]] == ["gmv per marketplace"]
    assert _concept(merged, "GMV")["metric"] == "gmv"
    assert merged["metrics"] == _stored()["metrics"]
    assert merged["routing_aliases"] == ["Acme", "L2"]


def test_phantom_table_no_longer_reaches_the_llm_prompt():
    stored = _stored()
    # the regression this guards: the stale catalog DID teach the model that table
    assert PHANTOM in nl2sql.catalog_for_prompt(stored)

    merged, _ = merge_scan(stored, _fresh())
    assert PHANTOM not in nl2sql.catalog_for_prompt(merged)
    assert PHANTOM not in nl2sql.describe_tables(merged, ["acme_store_stock"])


def test_pruned_report_names_everything_that_was_parked():
    _, pruned = merge_scan(_stored(), _fresh())

    assert pruned["vanished_tables"] == [PHANTOM]
    assert pruned["dropped"]["descriptions"] == sorted(
        [PHANTOM, f"{PHANTOM}.sto_item_code", "sto_available_stock"]
    )
    assert pruned["dropped"]["grain"] == [PHANTOM]
    assert pruned["dropped"]["reference_values"] == [f"{PHANTOM}.item_active"]
    assert pruned["dropped"]["examples"] == ["stok terkini"]
    assert pruned["dropped"]["business_concepts"] == ["Stok tersedia"]
    assert pruned["dropped_total"] == 7
    assert pruned["quarantined"] is True
    assert any("Stok tersedia" in note for note in pruned["notes"])


# ---- quarantine, not deletion ----
def test_parked_curation_is_stored_verbatim_for_recovery():
    merged, _ = merge_scan(_stored(), _fresh())
    parked = merged[QUARANTINE_KEY]

    assert parked["descriptions"][PHANTOM] == "Snapshot stok harian"
    assert parked["grain"][PHANTOM] == "1 baris = 1 item per hari"
    assert parked["reference_values"][f"{PHANTOM}.item_active"] == ["Y", "N"]
    assert parked["examples"] == [{"q": "stok terkini", "sql": PHANTOM_SQL}]
    assert parked["business_concepts"][0]["table"] == PHANTOM


def test_a_returning_table_gets_its_curation_back():
    parked_mdl, _ = merge_scan(_stored(), _fresh())
    merged, pruned = merge_scan(parked_mdl, _fresh_with_phantom())

    assert merged["descriptions"][PHANTOM] == "Snapshot stok harian"
    assert merged["descriptions"][f"{PHANTOM}.sto_item_code"] == "Kode item"
    assert merged["grain"][PHANTOM] == "1 baris = 1 item per hari"
    assert merged["reference_values"][f"{PHANTOM}.item_active"] == ["Y", "N"]
    assert PHANTOM_SQL in [e["sql"] for e in merged["examples"]]
    assert pruned["restored"]["examples"] == ["stok terkini"]
    assert pruned["restored_total"] == 6
    assert pruned["dropped_total"] == 0        # nothing NEW was lost


def test_a_returning_table_is_not_silently_re_allowlisted():
    """Allowlist narrowing still wins: its contract waits for the admin, parked not lost."""
    parked_mdl, _ = merge_scan(_stored(), _fresh())
    merged, pruned = merge_scan(parked_mdl, _fresh_with_phantom())

    assert PHANTOM not in merged["allowed_tables"]
    assert _concept(merged, "Stok tersedia") is None
    assert merged[QUARANTINE_KEY]["business_concepts"][0]["name"] == "Stok tersedia"
    assert any("non-allowlisted" in note for note in pruned["notes"])

    # the admin re-approves the table (PATCH /mdl) and rescans again
    approved = {**merged, "allowed_tables": sorted([*merged["allowed_tables"], PHANTOM])}
    revived, pruned = merge_scan(approved, _fresh_with_phantom())
    assert _concept(revived, "Stok tersedia")["metric"] == "sto_available_stock"
    assert pruned["restored"]["business_concepts"] == ["Stok tersedia"]
    assert QUARANTINE_KEY not in revived


def test_repeated_rescans_neither_churn_nor_re_alarm():
    first, first_report = merge_scan(_stored(), _fresh())
    second, second_report = merge_scan(first, _fresh())

    assert second[QUARANTINE_KEY] == first[QUARANTINE_KEY]
    assert second["descriptions"] == first["descriptions"]
    assert second["examples"] == first["examples"]
    assert second_report["dropped_total"] == 0        # already parked, not news
    assert second_report["still_parked_total"] == first_report["dropped_total"]


def test_admin_edits_win_over_a_parked_copy():
    parked_mdl, _ = merge_scan(_stored(), _fresh())
    edited = {
        **parked_mdl,
        "descriptions": {**parked_mdl["descriptions"], PHANTOM: "Definisi baru admin"},
    }
    merged, _ = merge_scan(edited, _fresh_with_phantom())

    assert merged["descriptions"][PHANTOM] == "Definisi baru admin"


def test_a_mass_loss_is_parked_in_full_while_the_report_stays_readable():
    """The quarantine must never drop entries — capping it would delete the curation it
    exists to protect (Acme L2 lost 84 tables at once). Only the REPORT is truncated."""
    dead = mdl_merge._MAX_REPORTED * 4
    stored = _stored(
        descriptions={f"dead_table_{i}.col": f"desc {i}" for i in range(dead)},
        grain={}, reference_values={}, examples=[], business_concepts=[],
    )
    merged, pruned = merge_scan(stored, _fresh())

    assert len(merged[QUARANTINE_KEY]["descriptions"]) == dead      # nothing lost
    assert len(pruned["dropped"]["descriptions"]) == mdl_merge._MAX_REPORTED
    assert pruned["dropped_total"] == dead                          # the COUNT stays exact
    # and every one of them survives a round trip back into the live catalog
    assert len(mdl_merge.reinstate(merged)["descriptions"]) == dead + 1


# ---- examples ----
def test_example_touching_a_live_table_is_kept_but_flagged():
    mixed = {"q": "stok vs order",
             "sql": f"SELECT o.gmv FROM acme_store_order_mp o JOIN {PHANTOM} s "
                    "ON s.sto_item_code = o.sku"}
    merged, pruned = merge_scan(_stored(examples=[mixed]), _fresh())

    assert merged["examples"] == [mixed]        # deleting a mostly-good query is worse
    assert any(PHANTOM in note and "stok vs order" in note for note in pruned["notes"])


def test_cte_aliases_and_table_free_examples_are_not_mistaken_for_dead_tables():
    examples = [
        {"q": "gmv lewat cte",
         "sql": "WITH agg AS (SELECT SUM(gmv) g FROM acme_store_order_mp) SELECT g FROM agg"},
        {"q": "tanggal hari ini", "sql": "SELECT CURRENT_DATE()"},
    ]
    merged, pruned = merge_scan(_stored(examples=examples), _fresh())

    assert merged["examples"] == examples
    assert pruned["dropped"]["examples"] == []


def test_broken_example_sql_is_still_attributed_to_its_table():
    """A hand-typed exemplar need not parse for its dead table to be recognised."""
    examples = [{"q": "rusak", "sql": f"SELECT * FROM {PHANTOM} )))"}]
    merged, pruned = merge_scan(_stored(examples=examples), _fresh())

    assert merged.get("examples", []) == []
    assert pruned["dropped"]["examples"] == ["rusak"]
    assert mdl_merge._sql_tables("SELECT * FROM ((((") == set()   # no table, no verdict


# ---- business concepts reuse the existing contract checker ----
def test_concept_whose_column_vanished_is_parked_with_the_checker_reason():
    stored = _stored(business_concepts=[
        {"name": "Order lama", "table": "acme_store_order_mp", "metric": "removed_column",
         "aggregation": "SUM"},
    ])
    merged, pruned = merge_scan(stored, _fresh())

    assert merged.get("business_concepts", []) == []
    assert merged[QUARANTINE_KEY]["business_concepts"][0]["name"] == "Order lama"
    assert any("unknown column" in note for note in pruned["notes"])


def test_partition_definitions_triages_instead_of_failing_the_whole_list():
    mdl = _fresh()
    kept, rejected = semantic_contract.partition_definitions(mdl, [
        {"name": "GMV", "table": "acme_store_order_mp", "metric": "gmv"},
        {"name": "Stok", "table": PHANTOM, "metric": "sto_available_stock"},
    ])

    assert [item["name"] for item in kept] == ["GMV"]
    assert rejected[0]["definition"]["name"] == "Stok"
    assert "unknown table" in rejected[0]["reason"]
    # the all-or-nothing path the admin editor uses is unchanged
    with pytest.raises(ValueError):
        semantic_contract.validate_definitions(mdl, [
            {"name": "Stok", "table": PHANTOM, "metric": "sto_available_stock"},
        ])


# ---- scan safety ----
def test_a_scan_that_returns_nothing_is_refused_not_merged():
    with pytest.raises(ScanLostAllTables):
        merge_scan(_stored(), {"tables": {}, "allowed_tables": []})


def test_first_scan_of_an_empty_database_is_still_allowed():
    merged, pruned = merge_scan({}, {"tables": {}, "allowed_tables": []})

    assert merged["tables"] == {}
    assert pruned["dropped_total"] == 0


# ---- carried-over discovery metadata ----
def test_metadata_carried_from_a_richer_scan_is_filtered_to_live_tables():
    stored = _stored(
        table_metadata={"acme_store_order_mp": {"kind": "table"}, PHANTOM: {"kind": "view"}},
        column_metadata={"acme_store_order_mp.gmv": {"data_type": "NUMERIC"},
                         f"{PHANTOM}.sto_item_code": {"data_type": "STRING"}},
        relationships=[
            {"from_table": "acme_store_order_mp", "from_column": "sku",
             "to_table": PHANTOM, "to_column": "sto_item_code"},
        ],
    )
    merged, _ = merge_scan(stored, _fresh())      # fresh scan lost catalog-view access

    assert set(merged["table_metadata"]) == {"acme_store_order_mp"}
    assert set(merged["column_metadata"]) == {"acme_store_order_mp.gmv"}
    assert merged.get("relationships", []) == []


def test_allowlist_and_pii_merge_is_unchanged():
    stored = _stored(allowed_tables=["acme_store_order_mp"], pii_columns=["sto_item_name"])
    merged, _ = merge_scan(stored, _fresh())

    assert merged["allowed_tables"] == ["acme_store_order_mp"]   # narrowing preserved
    assert "sto_item_name" in merged["pii_columns"]             # masking never loosens


def test_mdl_keys_the_scanner_knows_nothing_about_survive():
    """Acme L2 carries a hand-written `metadata_enrichment` note; a rescan must not eat it."""
    note = {"technical_source": "connector_discovery", "typed_columns": 497}
    merged, _ = merge_scan(_stored(metadata_enrichment=note, kind="sql"), _fresh())

    assert merged["metadata_enrichment"] == note
    assert merged["kind"] == "sql"


def test_admin_edited_function_policy_is_not_reset_by_a_rescan():
    stored = _stored(denied_functions=["version", "current_user", "session_user"],
                     allowed_functions=["safe_cast"])
    merged, _ = merge_scan(stored, _fresh())

    assert merged["denied_functions"] == ["version", "current_user", "session_user"]
    assert merged["allowed_functions"] == ["safe_cast"]


def test_targeted_repair_parks_one_table_and_leaves_the_rest_alone():
    """A catalog that lost many tables at once gets one verdict at a time."""
    stored = _stored(grain={
        PHANTOM: "1 baris = 1 item per hari",
        "other_missing_table": "1 baris = 1 kejadian",
    })
    merged, _ = merge_scan(stored, _fresh())
    mine, rest = mdl_merge.select_curation(merged[QUARANTINE_KEY], [PHANTOM])
    repaired = mdl_merge.reinstate({**merged, QUARANTINE_KEY: rest})

    assert set(mine["grain"]) == {PHANTOM}
    assert repaired["grain"]["other_missing_table"] == "1 baris = 1 kejadian"
    assert PHANTOM not in repaired["grain"]
    assert PHANTOM not in repaired["descriptions"]
    assert mine["examples"] == [{"q": "stok terkini", "sql": PHANTOM_SQL}]
    assert PHANTOM_SQL not in [e["sql"] for e in repaired["examples"]]


def test_a_clean_rescan_reports_nothing_and_stores_no_quarantine():
    stored = {**_fresh(), "grain": {"acme_store_order_mp": "1 baris = 1 order item"}}
    merged, pruned = merge_scan(stored, _fresh())

    assert QUARANTINE_KEY not in merged
    assert pruned["dropped_total"] == 0 and pruned["restored_total"] == 0
    assert pruned["vanished_tables"] == [] and pruned["notes"] == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
