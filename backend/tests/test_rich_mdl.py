"""Rich connector discovery -> additive Poros MDL metadata."""

from app.mdl_merge import merge_scan
from app.nl2sql import catalog_for_prompt, pruned_view
from app.schema_scan import build_mdl_from_catalog, scan_connector


def _catalog():
    return {
        "dialect": "bigquery",
        "relations": [
            {
                "namespace": "sales",
                "name": "orders",
                "kind": "table",
                "description": "Canonical paid marketplace orders",
                "estimated_rows": 1000,
                "estimated_bytes": 4096,
                "partitioning": {"type": "DAY", "field": "paid_at"},
                "clustering": ["marketplace"],
                "primary_key": ["order_id"],
                "columns": [
                    {"name": "order_id", "data_type": "STRING", "nullable": False,
                     "ordinal": 1, "description": "Unique order identifier"},
                    {"name": "paid_at", "data_type": "TIMESTAMP", "nullable": True,
                     "ordinal": 2, "description": "Payment timestamp"},
                    {"name": "customer_email", "data_type": "STRING", "nullable": True,
                     "ordinal": 3, "description": "Customer email"},
                ],
                "foreign_keys": [{
                    "column": "order_id", "references_namespace": "finance",
                    "references_table": "payments", "references_column": "order_id",
                }],
            },
            {
                "namespace": "finance",
                "name": "payments",
                "kind": "table",
                "description": "Payment records",
                "columns": [
                    {"name": "order_id", "data_type": "STRING", "nullable": False,
                     "ordinal": 1},
                    {"name": "amount", "data_type": "NUMERIC", "nullable": False,
                     "ordinal": 2},
                ],
                "primary_key": ["order_id"],
                "foreign_keys": [],
            },
        ],
    }


def test_build_rich_mdl_keeps_legacy_shape_and_adds_metadata():
    mdl = build_mdl_from_catalog(_catalog())

    assert mdl["tables"]["orders"] == ["order_id", "paid_at", "customer_email"]
    assert "customer_email" in mdl["pii_columns"]
    assert mdl["descriptions"]["orders"] == "Canonical paid marketplace orders"
    assert mdl["descriptions"]["orders.paid_at"] == "Payment timestamp"
    assert mdl["column_metadata"]["orders.paid_at"]["data_type"] == "TIMESTAMP"
    assert mdl["table_metadata"]["orders"]["partitioning"]["field"] == "paid_at"
    assert mdl["relationships"] == [{
        "from_table": "orders", "from_column": "order_id",
        "to_table": "payments", "to_column": "order_id",
        "to_namespace": "finance", "cardinality": "many-to-one",
        "source": "declared_constraint",
    }]


def test_catalog_prompt_renders_types_partitioning_and_relationships():
    prompt = catalog_for_prompt(build_mdl_from_catalog(_catalog()))

    assert "paid_at [TIMESTAMP, nullable]: Payment timestamp" in prompt
    assert "orders: partitioned by paid_at; clustered by marketplace" in prompt
    assert "orders.order_id -> payments.order_id (many-to-one)" in prompt


def test_pruned_view_does_not_leak_unselected_technical_metadata():
    mdl = build_mdl_from_catalog(_catalog())
    view, dropped = pruned_view(mdl, "payment amount", keep_n=1)

    assert set(view["tables"]) == {"payments"}
    assert set(view["table_metadata"]) == {"payments"}
    assert all(key.startswith("payments.") for key in view["column_metadata"])
    assert view["relationships"] == []
    assert "orders" in dropped


def test_rescan_preserves_curated_descriptions_but_fills_discovered_gaps():
    fresh = build_mdl_from_catalog(_catalog())
    old = {
        **fresh,
        "descriptions": {"orders": "Admin-approved business description"},
        "metrics": [{"name": "Paid orders", "definition": "COUNT(DISTINCT order_id)"}],
        "routing_aliases": ["3P", "third party"],
    }
    merged, _pruned = merge_scan(old, fresh)

    assert merged["descriptions"]["orders"] == "Admin-approved business description"
    assert merged["descriptions"]["orders.paid_at"] == "Payment timestamp"
    assert merged["metrics"] == old["metrics"]
    assert merged["routing_aliases"] == old["routing_aliases"]


def test_scan_connector_falls_back_when_rich_discovery_is_not_permitted():
    class Connector:
        def discover(self):
            raise PermissionError("statistics catalog denied")

        def introspect(self):
            return {"orders": ["order_id", "amount"]}

    mdl = scan_connector(Connector())
    assert mdl["tables"] == {"orders": ["order_id", "amount"]}
    assert "column_metadata" not in mdl


def test_prompt_hides_metrics_that_reference_removed_tables_or_columns():
    mdl = build_mdl_from_catalog(_catalog())
    mdl["metrics"] = [
        {"name": "Revenue", "definition": "SUM(amount) dari payments"},
        {"name": "Old inventory", "definition": "SUM(stock_qty) dari removed_inventory"},
        {"name": "Old anonymous metric", "definition": "SUM(removed_amount)"},
    ]

    prompt = catalog_for_prompt(mdl)
    assert "Revenue: SUM(amount) dari payments" in prompt
    assert "Old inventory" not in prompt
    assert "Old anonymous metric" not in prompt
