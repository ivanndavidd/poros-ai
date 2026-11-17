from ..limits import db_connect_timeout_sec, db_statement_timeout_ms, governed_max_rows
from .base import Connector


class MySQLConnector(Connector):
    dialect = "mysql"

    def __init__(self, conn: dict):
        self.c = conn

    def _connect(self):
        import pymysql  # optional: pip install pymysql
        timeout = db_connect_timeout_sec()
        con = pymysql.connect(host=self.c.get("host", "localhost"), port=int(self.c.get("port", 3306)),
                              database=self.c.get("database"), user=self.c.get("user"),
                              password=self.c.get("password"), cursorclass=pymysql.cursors.Cursor,
                              connect_timeout=timeout, read_timeout=timeout, write_timeout=timeout)
        statement_ms = db_statement_timeout_ms()
        if statement_ms:
            # Server-side ceiling for SELECTs; harmless on servers that don't know it.
            try:
                with con.cursor() as cur:
                    cur.execute("SET SESSION MAX_EXECUTION_TIME=%s", (statement_ms,))
            except Exception:
                pass
        return con

    def namespace(self) -> dict:
        # MySQL has no catalog level: `db.table` names the database, which is the schema.
        return {"catalog": "", "schema": self.c.get("database") or ""}

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
        con = self._connect()
        try:
            cur = con.cursor()
            cur.execute("SELECT table_name, column_name FROM information_schema.columns "
                        "WHERE table_schema=%s ORDER BY table_name, ordinal_position",
                        (self.c.get("database"),))
            out = {}
            for t, col in cur.fetchall():
                out.setdefault(t, []).append(col)
            return out
        finally:
            con.close()

    def discover(self) -> dict:
        database = self.c.get("database")
        con = self._connect()
        try:
            cur = con.cursor()
            cur.execute(
                """SELECT table_schema,table_name,table_type,table_rows,
                          COALESCE(data_length,0)+COALESCE(index_length,0)
                   FROM information_schema.tables
                   WHERE table_schema=%s ORDER BY table_name""",
                (database,),
            )
            rel_rows = cur.fetchall()
            cur.execute(
                """SELECT table_schema,table_name,column_name,column_type,is_nullable,
                          ordinal_position,column_key
                   FROM information_schema.columns
                   WHERE table_schema=%s ORDER BY table_name,ordinal_position""",
                (database,),
            )
            col_rows = cur.fetchall()
            cur.execute(
                """SELECT table_schema,table_name,column_name,
                          referenced_table_schema,referenced_table_name,referenced_column_name
                   FROM information_schema.key_column_usage
                   WHERE table_schema=%s AND referenced_table_name IS NOT NULL""",
                (database,),
            )
            fk_rows = cur.fetchall()
        finally:
            con.close()
        columns, primary, foreign = {}, {}, {}
        for namespace, table, name, data_type, nullable, ordinal, key in col_rows:
            columns.setdefault((namespace, table), []).append({
                "name": name, "data_type": data_type,
                "nullable": nullable == "YES", "ordinal": int(ordinal),
            })
            if key == "PRI":
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
                "kind": "view" if "VIEW" in str(kind).upper() else "table",
                "estimated_rows": int(rows) if rows is not None else None,
                "estimated_bytes": int(size) if size is not None else None,
                "columns": columns.get((namespace, table), []),
                "primary_key": primary.get((namespace, table), []),
                "foreign_keys": foreign.get((namespace, table), []),
            } for namespace, table, kind, rows, size in rel_rows],
        }
