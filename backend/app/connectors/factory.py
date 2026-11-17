"""Connector factory + metadata for the typed connection forms in the admin UI.

To add a DB type: implement connectors/<db>.py (run + introspect) and register it
below, then add an entry to CONNECTOR_SPECS so the UI renders the right fields.
"""

# Field specs drive the typed connection forms (no more raw-JSON entry).
CONNECTOR_SPECS = {
    "duckdb":   {"label": "DuckDB (file)", "fields": [
        {"key": "path", "label": "File path", "placeholder": "/path/to/your.duckdb"}]},
    "postgres": {"label": "PostgreSQL", "fields": [
        {"key": "host", "label": "Host", "placeholder": "localhost"},
        {"key": "port", "label": "Port", "placeholder": "5432"},
        {"key": "database", "label": "Database"},
        {"key": "user", "label": "User"},
        {"key": "password", "label": "Password", "secret": True}]},
    "mysql":    {"label": "MySQL", "fields": [
        {"key": "host", "label": "Host", "placeholder": "localhost"},
        {"key": "port", "label": "Port", "placeholder": "3306"},
        {"key": "database", "label": "Database"},
        {"key": "user", "label": "User"},
        {"key": "password", "label": "Password", "secret": True}]},
    "sqlserver": {"label": "SQL Server", "fields": [
        {"key": "host", "label": "Host", "placeholder": "localhost"},
        {"key": "port", "label": "Port", "placeholder": "1433"},
        {"key": "database", "label": "Database"},
        {"key": "user", "label": "User"},
        {"key": "password", "label": "Password", "secret": True},
        {"key": "driver", "label": "ODBC Driver", "placeholder": "ODBC Driver 17 for SQL Server"}]},
    # Keyless-only form (org policy: no SA JSON keys). Base identity = ADC/WIF from the
    # SERVER env (GOOGLE_APPLICATION_CREDENTIALS / attached SA / gcloud login); per-tenant
    # isolation via impersonation. The CONNECTOR still accepts conn['credentials'] for
    # legacy stored configs and API callers — only the UI field is removed.
    "bigquery": {"label": "Google BigQuery", "fields": [
        {"key": "project", "label": "GCP Project ID", "placeholder": "fascc-499708"},
        {"key": "dataset", "label": "Dataset", "placeholder": "fascc_L1"},
        {"key": "location", "label": "Location", "placeholder": "US"},
        {"key": "impersonate_service_account",
         "label": "Impersonate service account (keyless — kosong = pakai identitas dasar server langsung)",
         "placeholder": "sa-tenant@project.iam.gserviceaccount.com"}]},
}

# SQL (queryable) datasource types — used to pick a default DB source for /v1/query.
SQL_TYPES = {"duckdb", "postgres", "mysql", "sqlserver", "bigquery"}


def make_connector(ds_type: str, conn: dict):
    t = (ds_type or "").lower()
    if t == "duckdb":
        from .duckdb_conn import DuckDBConnector; return DuckDBConnector(conn)
    if t == "postgres":
        from .postgres_conn import PostgresConnector; return PostgresConnector(conn)
    if t == "mysql":
        from .mysql_conn import MySQLConnector; return MySQLConnector(conn)
    if t == "sqlserver":
        from .sqlserver_conn import SQLServerConnector; return SQLServerConnector(conn)
    if t == "bigquery":
        from .bigquery_conn import BigQueryConnector; return BigQueryConnector(conn)
    raise ValueError(f"unsupported DB type: {ds_type}")
