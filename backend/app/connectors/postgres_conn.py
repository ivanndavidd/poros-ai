from ..limits import db_connect_timeout_sec, db_statement_timeout_ms, governed_max_rows
from .base import Connector


class PostgresConnector(Connector):
    dialect = "postgres"

    def __init__(self, conn: dict):
        self.c = conn

    def _connect(self):
        import psycopg2  # opsional: pip install psycopg2-binary
        # Server-side ceiling: a runaway governed aggregation cannot pin the tenant's
        # database or this worker indefinitely, and a dead host fails fast instead of
        # holding a request thread.
        options = ""
        timeout_ms = db_statement_timeout_ms()
        if timeout_ms:
            options = f"-c statement_timeout={timeout_ms}"
        return psycopg2.connect(host=self.c.get("host"), port=self.c.get("port", 5432),
                                dbname=self.c.get("database"), user=self.c.get("user"),
                                password=self.c.get("password"),
                                connect_timeout=db_connect_timeout_sec(),
                                options=options)

    def namespace(self) -> dict:
        # introspect() scans exactly this schema, so it is the only one the MDL describes.
        return {"catalog": self.c.get("database") or "",
                "schema": self.c.get("schema") or "public"}

    def run(self, sql: str):
        # try/finally, matching discover() below: `con.close()` as the last statement on
        # the line leaked the connection whenever execute()/fetch raised, and repeated
        # failures then exhausted the tenant database's connection pool.
        con = self._connect()
        try:
            cur = con.cursor()
            cur.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            cap = governed_max_rows()
            # Second line of defence behind govern()'s LIMIT: never materialise more.
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
                        (self.namespace()["schema"],))
            out = {}
            for t, c in cur.fetchall():
                out.setdefault(t, []).append(c)
            return out
        finally:
            con.close()

    def discover(self) -> dict:
        con = self._connect()
        try:
            cur = con.cursor()
            cur.execute(
                """SELECT t.table_schema, t.table_name, t.table_type,
                          s.n_live_tup::bigint,
                          CASE WHEN c.oid IS NULL OR c.relkind NOT IN ('r','m','p') THEN NULL
                               ELSE pg_total_relation_size(c.oid)::bigint END
                   FROM information_schema.tables t
                   LEFT JOIN pg_namespace n ON n.nspname=t.table_schema
                   LEFT JOIN pg_class c ON c.relnamespace=n.oid AND c.relname=t.table_name
                   LEFT JOIN pg_stat_user_tables s
                          ON s.schemaname=t.table_schema AND s.relname=t.table_name
                   WHERE t.table_schema NOT IN ('pg_catalog','information_schema')
                   ORDER BY t.table_schema,t.table_name"""
            )
            rel_rows = cur.fetchall()
            cur.execute(
                """SELECT table_schema,table_name,column_name,data_type,is_nullable,ordinal_position
                   FROM information_schema.columns
                   WHERE table_schema NOT IN ('pg_catalog','information_schema')
                   ORDER BY table_schema,table_name,ordinal_position"""
            )
            col_rows = cur.fetchall()
            cur.execute(
                """SELECT ns.nspname, tab.relname, att.attname
                   FROM pg_constraint con
                   JOIN pg_class tab ON tab.oid=con.conrelid
                   JOIN pg_namespace ns ON ns.oid=tab.relnamespace
                   JOIN unnest(con.conkey) WITH ORDINALITY AS key(attnum,ord) ON TRUE
                   JOIN pg_attribute att ON att.attrelid=tab.oid AND att.attnum=key.attnum
                   WHERE con.contype='p'
                   ORDER BY ns.nspname,tab.relname,key.ord"""
            )
            pk_rows = cur.fetchall()
            cur.execute(
                """SELECT src_ns.nspname,src.relname,src_att.attname,
                          dst_ns.nspname,dst.relname,dst_att.attname
                   FROM pg_constraint con
                   JOIN pg_class src ON src.oid=con.conrelid
                   JOIN pg_namespace src_ns ON src_ns.oid=src.relnamespace
                   JOIN pg_class dst ON dst.oid=con.confrelid
                   JOIN pg_namespace dst_ns ON dst_ns.oid=dst.relnamespace
                   JOIN unnest(con.conkey,con.confkey) AS key(src_num,dst_num) ON TRUE
                   JOIN pg_attribute src_att
                        ON src_att.attrelid=src.oid AND src_att.attnum=key.src_num
                   JOIN pg_attribute dst_att
                        ON dst_att.attrelid=dst.oid AND dst_att.attnum=key.dst_num
                   WHERE con.contype='f'"""
            )
            fk_rows = cur.fetchall()
        finally:
            con.close()
        columns, primary, foreign = {}, {}, {}
        for namespace, table, name, data_type, nullable, ordinal in col_rows:
            columns.setdefault((namespace, table), []).append({
                "name": name, "data_type": data_type,
                "nullable": nullable == "YES", "ordinal": int(ordinal),
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
                "kind": "view" if "VIEW" in str(kind).upper() else "table",
                "estimated_rows": int(rows) if rows is not None else None,
                "estimated_bytes": int(size) if size is not None else None,
                "columns": columns.get((namespace, table), []),
                "primary_key": primary.get((namespace, table), []),
                "foreign_keys": foreign.get((namespace, table), []),
            } for namespace, table, kind, rows, size in rel_rows],
        }
