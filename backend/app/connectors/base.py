from typing import Optional


class Connector:
    dialect = "duckdb"
    max_bytes_billed: Optional[int] = None   # cost ceiling (BigQuery); None = no cost guard

    def run(self, sql: str):        # -> (columns: list[str], rows: list[list])
        raise NotImplementedError

    def introspect(self) -> dict:   # -> {table_name: [column,...]}
        raise NotImplementedError

    def dry_run(self, sql: str) -> Optional[int]:
        """Estimate bytes a query would process WITHOUT executing it.
        None = engine doesn't support cost estimation (no cost guard applied)."""
        return None

    def namespace(self) -> dict:
        """The single catalog/schema this datasource's MDL was scanned from.

        ``govern()`` uses it to reject a table reference qualified to somewhere else:
        the MDL is keyed by bare table names, so ``other_schema.customers`` would
        otherwise satisfy an allowlist entry for ``customers``. An empty string means
        the dialect has no such level, and an unrecognised qualifier is then refused.
        """
        return {"catalog": "", "schema": ""}

    def discover(self) -> dict:
        """Return normalized live metadata for backend-owned discovery tools.

        Connector implementations may enrich the relation/column dictionaries with
        engine-specific metadata.  The conservative fallback keeps every existing
        connector compatible and deliberately exposes no row values.
        """
        relations = []
        for table, columns in self.introspect().items():
            relations.append({
                "namespace": "",
                "name": str(table),
                "kind": "table",
                "estimated_rows": None,
                "estimated_bytes": None,
                "columns": [
                    {
                        "name": str(column),
                        "data_type": "",
                        "nullable": None,
                        "ordinal": index,
                    }
                    for index, column in enumerate(columns, 1)
                ],
                "primary_key": [],
                "foreign_keys": [],
            })
        return {"dialect": self.dialect, "relations": relations}

    def quote_identifier(self, value: str) -> str:
        """Quote one already-authorized identifier for this SQL dialect."""
        value = str(value)
        if self.dialect == "mysql":
            return "`" + value.replace("`", "``") + "`"
        if self.dialect == "tsql":
            return "[" + value.replace("]", "]]") + "]"
        if self.dialect == "bigquery":
            return "`" + value.replace("`", "") + "`"
        return '"' + value.replace('"', '""') + '"'

    def qualified_relation(self, namespace: str, name: str) -> str:
        parts = [part for part in (namespace, name) if part]
        if self.dialect == "bigquery":
            return "`" + ".".join(str(part).replace("`", "") for part in parts) + "`"
        return ".".join(self.quote_identifier(part) for part in parts)

    def sample_rows(self, namespace: str, table: str, columns: list[str], limit: int):
        """Return a bounded sample for backend profiling.

        Names are selected only after exact authorization/metadata validation by the
        caller.  This method never accepts predicates or arbitrary SQL from the model.
        """
        selected = ", ".join(self.quote_identifier(column) for column in columns)
        relation = self.qualified_relation(namespace, table)
        sql = f"SELECT {selected} FROM {relation} LIMIT {max(1, min(int(limit), 1000))}"
        return self.run(sql)
