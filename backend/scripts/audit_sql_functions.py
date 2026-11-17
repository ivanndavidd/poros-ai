"""Inventory function names from historical audit SQL without printing SQL or data."""
import json

import sqlglot
from sqlglot import exp

from app import store
from app.governance.functions import _function_keys


c = store._c()
rows = c.execute(
    "SELECT detail FROM audit_log WHERE event='query' AND detail IS NOT NULL"
).fetchall()
c.close()

counts = {}
unparsed = 0
for row in rows:
    try:
        sql = (json.loads(row["detail"]) or {}).get("safe_sql") or ""
    except Exception:
        sql = ""
    ast = None
    for dialect in (None, "postgres", "mysql", "bigquery", "tsql", "duckdb"):
        try:
            ast = sqlglot.parse_one(sql, dialect=dialect)
            break
        except Exception:
            continue
    if ast is None:
        unparsed += 1
        continue
    for func in ast.find_all(exp.Func):
        name = sorted(_function_keys(func))[0]
        counts[name] = counts.get(name, 0) + 1

print({"functions": dict(sorted(counts.items())), "unparsed_queries": unparsed})
