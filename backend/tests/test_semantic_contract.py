import pytest

from app import semantic_contract
from app.agent import orchestrator
from app.nl2sql import catalog_for_prompt, pruned_view, rank_tables


def _mdl():
    return {
        "tables": {
            "acme_store_order_mp": ["order_date", "gmv", "quantity", "order_id"],
            "faas_invoice_dashboard_outbound": ["Order_Date", "Quantity"],
            "unrelated": ["day", "amount"],
        },
        "allowed_tables": [
            "acme_store_order_mp",
            "faas_invoice_dashboard_outbound",
            "unrelated",
        ],
        "descriptions": {
            "acme_store_order_mp": "Primary marketplace sales table.",
            "faas_invoice_dashboard_outbound": "Warehouse outbound movements.",
        },
        "business_concepts": [
            {
                "name": "sales",
                "synonyms": ["penjualan", "omzet"],
                "table": "acme_store_order_mp",
                "metric": "gmv",
                "time_dimension": "order_date",
                "aggregation": "SUM",
                "description": "Canonical marketplace sales value.",
            },
            {
                "name": "sales volume",
                "synonyms": ["volume penjualan", "unit terjual"],
                "table": "acme_store_order_mp",
                "metric": "quantity",
                "time_dimension": "order_date",
                "aggregation": "SUM",
                "description": "Units sold.",
            },
        ],
    }


def test_contract_resolution_is_deterministic_and_prefers_specific_phrase():
    assert semantic_contract.resolve_mdl(_mdl(), "monthly sales this year")[0]["metric"] == "gmv"
    specific = semantic_contract.resolve_mdl(_mdl(), "monthly sales volume this year")
    assert [item["name"] for item in specific] == ["sales volume"]
    assert specific[0]["metric"] == "quantity"


def test_contract_validation_rejects_stale_or_non_allowlisted_schema():
    mdl = _mdl()
    with pytest.raises(ValueError, match="unknown column"):
        semantic_contract.validate_definitions(mdl, [{
            "name": "sales", "table": "acme_store_order_mp", "metric": "revenue",
        }])
    mdl["allowed_tables"] = ["unrelated"]
    with pytest.raises(ValueError, match="non-allowlisted"):
        semantic_contract.validate_definitions(mdl, mdl["business_concepts"][:1])


def test_formula_contract_validates_columns_and_enforces_exact_curated_formula():
    mdl = _mdl()
    mdl["tables"]["acme_store_order_mp"].append("new_gmv")
    definition = {
        "name": "GMV",
        "synonyms": ["gross merchandise value"],
        "table": "acme_store_order_mp",
        "formula": "SUM(CASE WHEN new_gmv IS NULL THEN gmv ELSE new_gmv END)",
        "time_dimension": "order_date",
        "description": "Formula GMV curated from Looker.",
    }
    contract = semantic_contract.validate_definitions(mdl, [definition])
    assert contract[0]["metric"] == ""
    assert contract[0]["formula"].startswith("SUM(CASE")

    resolved = semantic_contract.resolve_mdl(
        {**mdl, "business_concepts": contract}, "total GMV bulan ini",
    )
    wrong = "SELECT SUM(gmv) AS total_gmv FROM acme_store_order_mp"
    correct = (
        "SELECT SUM(CASE WHEN new_gmv IS NULL THEN gmv ELSE new_gmv END) AS total_gmv "
        "FROM acme_store_order_mp"
    )
    qualified = (
        "SELECT SUM(CASE WHEN o.new_gmv IS NULL THEN o.gmv ELSE o.new_gmv END) total_gmv "
        "FROM acme_store_order_mp AS o"
    )
    assert "requires SUM(CASE" in semantic_contract.sql_violation(
        wrong, "bigquery", resolved,
    )
    assert semantic_contract.sql_violation(correct, "bigquery", resolved) is None
    assert semantic_contract.sql_violation(qualified, "bigquery", resolved) is None


def test_formula_contract_rejects_unknown_columns_and_non_aggregate_expression():
    mdl = _mdl()
    with pytest.raises(ValueError, match="unknown column 'new_gmv'"):
        semantic_contract.validate_definitions(mdl, [{
            "name": "GMV", "table": "acme_store_order_mp",
            "formula": "SUM(COALESCE(new_gmv, gmv))",
        }])
    with pytest.raises(ValueError, match="must contain an aggregate"):
        semantic_contract.validate_definitions(mdl, [{
            "name": "GMV", "table": "acme_store_order_mp", "formula": "gmv",
        }])
    with pytest.raises(ValueError, match="must be one SQL expression"):
        semantic_contract.validate_definitions(mdl, [{
            "name": "GMV", "table": "acme_store_order_mp",
            "formula": "SUM(gmv) FROM unrelated",
        }])


def test_count_distinct_contract_rejects_plain_count_but_allows_range_probe():
    mdl = _mdl()
    concept = semantic_contract.validate_definitions(mdl, [{
        "name": "active 3P",
        "synonyms": ["3P aktif"],
        "table": "acme_store_order_mp",
        "metric": "order_id",
        "aggregation": "COUNT_DISTINCT",
        "time_dimension": "order_date",
    }])
    contracts = semantic_contract.resolve_mdl(
        {**mdl, "business_concepts": concept}, "berapa active 3P?",
    )
    assert "requires COUNT_DISTINCT(order_id)" in semantic_contract.sql_violation(
        "SELECT COUNT(order_id) FROM acme_store_order_mp", "bigquery", contracts,
    )
    probe = "SELECT MIN(order_date), MAX(order_date), COUNT(*) FROM acme_store_order_mp"
    assert semantic_contract.sql_violation(probe, "bigquery", contracts) is None


def test_routing_alias_normalization_is_bounded_and_case_insensitive():
    assert semantic_contract.normalize_routing_aliases(
        [" 3P ", "third party", "3p", "", None],
    ) == ["3P", "third party"]


def test_contract_is_prominent_and_forces_canonical_table_through_pruning(monkeypatch):
    monkeypatch.setenv("CATALOG_KEEP_TABLES", "1")
    mdl = _mdl()
    view, dropped = pruned_view(mdl, "show monthly sales")
    assert list(view["tables"]) == ["acme_store_order_mp"]
    assert "faas_invoice_dashboard_outbound" in dropped
    assert rank_tables(mdl, "show monthly sales", limit=1) == ["acme_store_order_mp"]
    prompt = catalog_for_prompt(mdl, question="show monthly sales")
    assert "MANDATORY BUSINESS SEMANTIC CONTRACTS" in prompt
    assert "SUM(gmv) from acme_store_order_mp" in prompt


def test_fast_sql_contract_rejects_wrong_table_or_metric_but_allows_validation():
    contract = semantic_contract.resolve_mdl(_mdl(), "show monthly sales")
    wrong_table = (
        "SELECT FORMAT_DATE('%Y-%m', DATE(Order_Date)) month, SUM(Quantity) "
        "FROM faas_invoice_dashboard_outbound GROUP BY month"
    )
    wrong_metric = (
        "SELECT FORMAT_DATE('%Y-%m', order_date) month, SUM(quantity) "
        "FROM acme_store_order_mp GROUP BY month"
    )
    correct = (
        "SELECT FORMAT_DATE('%Y-%m', order_date) month, SUM(gmv) "
        "FROM acme_store_order_mp GROUP BY month"
    )
    validation = "SELECT MIN(order_date), MAX(order_date), COUNT(*) FROM acme_store_order_mp"
    assert "canonical table" in semantic_contract.sql_violation(wrong_table, "bigquery", contract)
    assert "requires SUM(gmv)" in semantic_contract.sql_violation(
        wrong_metric, "bigquery", contract,
    )
    assert semantic_contract.sql_violation(correct, "bigquery", contract) is None
    assert semantic_contract.sql_violation(validation, "bigquery", contract) is None


def test_multiple_independent_contracts_may_use_separate_canonical_queries():
    mdl = _mdl()
    mdl["business_concepts"].append({
        "name": "warehouse outbound",
        "synonyms": ["outbound gudang"],
        "table": "faas_invoice_dashboard_outbound",
        "metric": "Quantity",
        "time_dimension": "Order_Date",
        "aggregation": "SUM",
    })
    contracts = semantic_contract.resolve_mdl(
        mdl, "compare sales and warehouse outbound",
    )
    sales_sql = "SELECT SUM(gmv) FROM acme_store_order_mp"
    outbound_sql = "SELECT SUM(Quantity) FROM faas_invoice_dashboard_outbound"
    assert semantic_contract.sql_violation(sales_sql, "bigquery", contracts) is None
    assert semantic_contract.sql_violation(outbound_sql, "bigquery", contracts) is None


def test_datasource_router_prefers_contract_over_fuzzy_catalog_order():
    outbound = {
        "id": "outbound",
        "type": "bigquery",
        "mdl": {
            "tables": {"faas_invoice_dashboard_outbound": ["Quantity", "Order_Date"]},
            "allowed_tables": ["faas_invoice_dashboard_outbound"],
        },
    }
    canonical = {"id": "canonical", "type": "bigquery", "mdl": _mdl()}
    assert orchestrator._route_datasource(
        "tenant", "show monthly sales", [outbound, canonical],
    )["id"] == "canonical"


def test_one_stale_contract_does_not_disable_other_valid_contracts():
    mdl = _mdl()
    mdl["business_concepts"].append({
        "name": "stale inventory",
        "synonyms": ["stok lama"],
        "table": "removed_inventory_table",
        "metric": "quantity",
        "aggregation": "SUM",
    })

    resolved = semantic_contract.resolve_mdl(mdl, "show monthly sales")
    assert [item["name"] for item in resolved] == ["sales"]


def test_stale_specific_contract_does_not_fall_back_to_generic_alias():
    mdl = _mdl()
    mdl["business_concepts"].append({
        "name": "offline sales",
        "synonyms": ["penjualan offline", "omzet offline"],
        "table": "removed_offline_table",
        "metric": "revenue",
        "aggregation": "SUM",
    })

    assert semantic_contract.resolve_mdl(mdl, "show offline sales") == []
