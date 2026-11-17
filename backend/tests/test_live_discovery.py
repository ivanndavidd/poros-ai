"""Offline coverage for universal live discovery/profiling and semantic checks."""
from app import discovery
from app.connectors.base import Connector
from app.connectors.bigquery_conn import BigQueryConnector
from app.connectors.duckdb_conn import DuckDBConnector
from app.connectors.mysql_conn import MySQLConnector
from app.connectors.postgres_conn import PostgresConnector
from app.connectors.sqlserver_conn import SQLServerConnector


class _FakeConnector(Connector):
    dialect = "postgres"

    def __init__(self):
        self.sampled_columns = []

    def discover(self):
        return {
            "dialect": "postgres",
            "relations": [
                {
                    "namespace": "public",
                    "name": "sales_monthly",
                    "kind": "table",
                    "estimated_rows": 1000,
                    "estimated_bytes": 24000,
                    "columns": [
                        {"name": "id", "data_type": "bigint", "nullable": False},
                        {"name": "month_id", "data_type": "integer", "nullable": False},
                        {"name": "region", "data_type": "text", "nullable": True},
                        {"name": "revenue", "data_type": "numeric", "nullable": True},
                        {"name": "email", "data_type": "text", "nullable": True},
                        {"name": "new_live_column", "data_type": "text", "nullable": True},
                    ],
                    "primary_key": ["id"],
                    "foreign_keys": [],
                },
                {
                    "namespace": "public",
                    "name": "secret_payroll",
                    "kind": "table",
                    "columns": [{"name": "salary", "data_type": "numeric"}],
                    "primary_key": [],
                    "foreign_keys": [],
                },
            ],
        }

    def sample_rows(self, namespace, table, columns, limit):
        self.sampled_columns = list(columns)
        values = [
            [1, 202601, "Jawa", 100.0],
            [2, 202602, "Jawa", 120.0],
            [3, 202603, "Bali", None],
        ]
        indexes = {
            name: index
            for index, name in enumerate(["id", "month_id", "region", "revenue"])
        }
        return list(columns), [
            [row[indexes[column]] for column in columns]
            for row in values[:limit]
        ]


def _ds():
    return {
        "id": "ds_live",
        "name": "Live Sales",
        "type": "postgres",
        "conn": {"host": "example.invalid"},
    }


def _mdl():
    return {
        "tables": {
            "sales_monthly": ["id", "month_id", "region", "revenue", "email"],
        },
        "allowed_tables": ["sales_monthly"],
        "pii_columns": ["email"],
        "descriptions": {
            "sales_monthly": "Official monthly sales performance by region.",
        },
        "grain": {
            "sales_monthly": "One row per region and month after order deduplication.",
        },
        "metrics": [{
            "name": "Revenue",
            "definition": "SUM(sales_monthly.revenue)",
        }],
    }


def test_live_discovery_is_role_scoped_and_reports_schema_drift(monkeypatch):
    connector = _FakeConnector()
    discovery.clear_cache()
    monkeypatch.setattr(discovery, "make_connector", lambda *_args: connector)

    result = discovery.discover_datasource(
        _ds(), _mdl(), query="revenue region", limit=10, refresh=True,
    )

    assert result["ok"] is True
    assert result["authorized_relation_count"] == 1
    assert result["hidden_relation_count"] == 1
    assert result["relation_index"] == ["sales_monthly"]
    relation = result["relations"][0]
    assert relation["table"] == "sales_monthly"
    assert relation["primary_key"] == ["id"]
    assert relation["schema_drift"]["live_not_registered_count"] == 1
    assert {column["name"] for column in relation["columns"]} == {
        "id", "month_id", "region", "revenue", "email",
    }


def test_profile_is_bounded_excludes_pii_and_finds_shape(monkeypatch):
    connector = _FakeConnector()
    discovery.clear_cache()
    monkeypatch.setattr(discovery, "make_connector", lambda *_args: connector)

    result = discovery.profile_relation(
        _ds(), _mdl(), "sales_monthly", sample_size=50, refresh=True,
    )

    assert result["sample_based"] is True
    assert result["sample_rows"] == 3
    assert "email" not in connector.sampled_columns
    assert result["excluded_pii_columns"] == ["email"]
    assert "id" in result["candidate_keys_from_sample"]
    roles = {column["name"]: column["semantic_role"] for column in result["columns"]}
    assert roles["revenue"] == "measure"
    assert roles["month_id"] == "identifier"
    region = next(column for column in result["columns"] if column["name"] == "region")
    assert region["top_values"][0] == {"value": "Jawa", "count": 2}
    assert result["semantic"]["grain"]


def test_semantic_verification_keeps_uncurated_meanings_as_assumptions():
    verified = discovery.verify_semantics(_mdl(), [{
        "concept": "official revenue",
        "table": "sales_monthly",
        "columns": ["revenue", "region"],
    }])
    assert verified["ok"] is True
    assert verified["checks"][0]["status"] == "verified"
    assert verified["checks"][0]["support"]["metrics"]

    partial_mdl = {**_mdl(), "descriptions": {}, "grain": {}, "metrics": []}
    partial = discovery.verify_semantics(partial_mdl, [{
        "concept": "official revenue",
        "table": "sales_monthly",
        "columns": ["revenue"],
    }])
    assert partial["checks"][0]["status"] == "partial"
    assert "assumption" in partial["checks"][0]["reason"]


def test_every_sql_connector_has_a_rich_discovery_override():
    for connector_class in (
        DuckDBConnector, PostgresConnector, MySQLConnector,
        SQLServerConnector, BigQueryConnector,
    ):
        assert connector_class.discover is not Connector.discover


def test_duckdb_live_discovery_and_profile_work_end_to_end(tmp_path):
    import duckdb

    path = tmp_path / "discovery.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        """CREATE TABLE sales (
             id INTEGER, month_id INTEGER, region VARCHAR, revenue DOUBLE, email VARCHAR
           )"""
    )
    con.executemany(
        "INSERT INTO sales VALUES (?, ?, ?, ?, ?)",
        [
            (1, 202601, "Jawa", 100.0, "one@example.test"),
            (2, 202602, "Bali", 120.0, "two@example.test"),
        ],
    )
    con.close()
    ds = {
        "id": "ds_duck",
        "name": "Duck Live",
        "type": "duckdb",
        "conn": {"path": str(path)},
    }
    mdl = {
        "tables": {
            "sales": ["id", "month_id", "region", "revenue", "email"],
        },
        "allowed_tables": ["sales"],
        "pii_columns": ["email"],
    }
    discovery.clear_cache()

    catalog = discovery.discover_datasource(ds, mdl, query="sales revenue", refresh=True)
    profile = discovery.profile_relation(ds, mdl, "sales", sample_size=20)

    assert catalog["relations"][0]["table"] == "sales"
    assert profile["sample_rows"] == 2
    assert profile["excluded_pii_columns"] == ["email"]
    assert all(column["name"] != "email" for column in profile["columns"])
