import duckdb
from ..limits import governed_max_rows
from .base import Connector


class DuckDBConnector(Connector):
    dialect = "duckdb"

    def __init__(self, conn: dict):
        self.path = conn.get("path", "demo.duckdb")

    def namespace(self) -> dict:
        # introspect() scans table_schema='main'; that is the whole MDL.
        return {"catalog": "", "schema": "main"}

    def run(self, sql: str):
        con = duckdb.connect(self.path, read_only=True)
        try:
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            cap = governed_max_rows()
            rows = [list(r) for r in (cur.fetchmany(cap) if cap else cur.fetchall())]
            return cols, rows
        finally:
            con.close()          # released even when execute()/fetch raises

    def introspect(self) -> dict:
        con = duckdb.connect(self.path, read_only=True)
        try:
            tabs = [r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()]
            out = {}
            for t in tabs:
                out[t] = [r[0] for r in con.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name=? ORDER BY ordinal_position",
                    [t]).fetchall()]
            return out
        finally:
            con.close()

    def discover(self) -> dict:
        con = duckdb.connect(self.path, read_only=True)
        try:
            rel_rows = con.execute(
                """SELECT table_schema, table_name, table_type
                   FROM information_schema.tables
                   WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
                   ORDER BY table_schema, table_name"""
            ).fetchall()
            col_rows = con.execute(
                """SELECT table_schema, table_name, column_name, data_type,
                          is_nullable, ordinal_position
                   FROM information_schema.columns
                   WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
                   ORDER BY table_schema, table_name, ordinal_position"""
            ).fetchall()
        finally:
            con.close()
        columns = {}
        for namespace, table, name, data_type, nullable, ordinal in col_rows:
            columns.setdefault((namespace, table), []).append({
                "name": name,
                "data_type": data_type,
                "nullable": str(nullable).upper() == "YES",
                "ordinal": int(ordinal),
            })
        relations = [{
            "namespace": namespace,
            "name": table,
            "kind": "view" if "VIEW" in str(kind).upper() else "table",
            "estimated_rows": None,
            "estimated_bytes": None,
            "columns": columns.get((namespace, table), []),
            "primary_key": [],
            "foreign_keys": [],
        } for namespace, table, kind in rel_rows]
        return {"dialect": self.dialect, "relations": relations}
