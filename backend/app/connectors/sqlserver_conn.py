from ..limits import db_connect_timeout_sec, db_statement_timeout_ms, governed_max_rows
from .base import Connector


class SQLServerConnector(Connector):
    dialect = "tsql"

    def __init__(self, conn: dict):
        self.c = conn

    def _connect(self):
        import pyodbc  # optional: pip install pyodbc (+ an ODBC driver)
        driver = self.c.get("driver", "ODBC Driver 17 for SQL Server")
        # Runtime secret decrypted from the tenant's secret-backed config. ODBC
        # requires it in the in-memory connection string; it is not hardcoded.
        password = self.c.get("password")
        cs = (f"DRIVER={{{driver}}};SERVER={self.c.get('host')},{self.c.get('port', 1433)};"
              f"DATABASE={self.c.get('database')};UID={self.c.get('user')};PWD={password}")  # NOSONAR
        con = pyodbc.connect(cs, timeout=db_connect_timeout_sec())
        statement_ms = db_statement_timeout_ms()
        if statement_ms:
            con.timeout = max(1, statement_ms // 1000)   # per-statement query timeout
        return con

    def _schema(self) -> str:
        return self.c.get("schema") or "dbo"

    def namespace(self) -> dict:
        return {"catalog": self.c.get("database") or "", "schema": self._schema()}

    def run(self, sql: str):
        con = self._connect()
        try:
            cur = con.cursor()
            cur.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            cap = governed_max_rows()
            rows = [list(r) for r in (cur.fetchmany(cap) if cap else cur.fetchall())]
            return cols, rows
        finally:
            con.close()

    def introspect(self) -> dict:
        # Scope to ONE schema. Without the filter, two schemas sharing a table name were
        # merged into a single bare-named MDL entry, so one table's columns (and its PII
        # classification) silently became the other's.
        con = self._connect()
        try:
            cur = con.cursor()
            cur.execute("SELECT table_name, column_name FROM information_schema.columns "
                        "WHERE table_schema=? ORDER BY table_name, ordinal_position",
                        (self._schema(),))
            out = {}
            for t, col in cur.fetchall():
                out.setdefault(t, []).append(col)
            return out
        finally:
            con.close()

    def sample_rows(self, namespace: str, table: str, columns: list[str], limit: int):
        selected = ", ".join(self.quote_identifier(column) for column in columns)
        relation = self.qualified_relation(namespace, table)
        sql = f"SELECT TOP {max(1, min(int(limit), 1000))} {selected} FROM {relation}"
        return self.run(sql)

    def discover(self) -> dict:
        con = self._connect()
        try:
            cur = con.cursor()
            cur.execute(
                """SELECT s.name,t.name,'TABLE',
                          SUM(CASE WHEN p.index_id IN (0,1) THEN p.rows ELSE 0 END),
                          SUM(a.total_pages)*8192
                   FROM sys.tables t
                   JOIN sys.schemas s ON s.schema_id=t.schema_id
                   LEFT JOIN sys.indexes i ON i.object_id=t.object_id
                   LEFT JOIN sys.partitions p
                          ON p.object_id=i.object_id AND p.index_id=i.index_id
                   LEFT JOIN sys.allocation_units a ON a.container_id=p.partition_id
                   GROUP BY s.name,t.name
                   UNION ALL
                   SELECT s.name,v.name,'VIEW',NULL,NULL
                   FROM sys.views v JOIN sys.schemas s ON s.schema_id=v.schema_id
                   ORDER BY 1,2"""
            )
            rel_rows = cur.fetchall()
            cur.execute(
                """SELECT s.name,o.name,c.name,ty.name,c.is_nullable,c.column_id
                   FROM sys.objects o
                   JOIN sys.schemas s ON s.schema_id=o.schema_id
                   JOIN sys.columns c ON c.object_id=o.object_id
                   JOIN sys.types ty ON ty.user_type_id=c.user_type_id
                   WHERE o.type IN ('U','V')
                   ORDER BY s.name,o.name,c.column_id"""
            )
            col_rows = cur.fetchall()
            cur.execute(
                """SELECT s.name,t.name,c.name
                   FROM sys.tables t
                   JOIN sys.schemas s ON s.schema_id=t.schema_id
                   JOIN sys.indexes i ON i.object_id=t.object_id AND i.is_primary_key=1
                   JOIN sys.index_columns ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id
                   JOIN sys.columns c ON c.object_id=t.object_id AND c.column_id=ic.column_id
                   ORDER BY s.name,t.name,ic.key_ordinal"""
            )
            pk_rows = cur.fetchall()
            cur.execute(
                """SELECT src_s.name,src_t.name,src_c.name,
                          dst_s.name,dst_t.name,dst_c.name
                   FROM sys.foreign_key_columns fkc
                   JOIN sys.tables src_t ON src_t.object_id=fkc.parent_object_id
                   JOIN sys.schemas src_s ON src_s.schema_id=src_t.schema_id
                   JOIN sys.columns src_c
                        ON src_c.object_id=src_t.object_id
                       AND src_c.column_id=fkc.parent_column_id
                   JOIN sys.tables dst_t ON dst_t.object_id=fkc.referenced_object_id
                   JOIN sys.schemas dst_s ON dst_s.schema_id=dst_t.schema_id
                   JOIN sys.columns dst_c
                        ON dst_c.object_id=dst_t.object_id
                       AND dst_c.column_id=fkc.referenced_column_id"""
            )
            fk_rows = cur.fetchall()
        finally:
            con.close()
        columns, primary, foreign = {}, {}, {}
        for namespace, table, name, data_type, nullable, ordinal in col_rows:
            columns.setdefault((namespace, table), []).append({
                "name": name, "data_type": data_type,
                "nullable": bool(nullable), "ordinal": int(ordinal),
            })
        for namespace, table, name in pk_rows:
            primary.setdefault((namespace, table), []).append(name)
        for src_ns, src, src_col, dst_ns, dst, dst_col in fk_rows:
            foreign.setdefault((src_ns, src), []).append({
                "column": src_col, "references_namespace": dst_ns,
                "references_table": dst, "references_column": dst_col,
            })
        return {
            "dialect": self.dialect,
            "relations": [{
                "namespace": namespace,
                "name": table,
                "kind": "view" if str(kind).upper() == "VIEW" else "table",
                "estimated_rows": int(rows) if rows is not None else None,
                "estimated_bytes": int(size) if size is not None else None,
                "columns": columns.get((namespace, table), []),
                "primary_key": primary.get((namespace, table), []),
                "foreign_keys": foreign.get((namespace, table), []),
            } for namespace, table, kind, rows, size in rel_rows],
        }
