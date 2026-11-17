"""Seed demo tenant 'Acme Corp' + DuckDB sample + roles + API keys.

Idempotent AND non-destructive: we seed ONLY into an EMPTY metadata store. If the
store already has ANY tenant (e.g. a real one you created in the console, or a prior
demo), we skip entirely and DO NOT touch the sample DB. This lets a launcher call
`python -m app.seed` on every boot safely — it self-skips once you have real data,
so it can never surprise you with an unwanted 'Acme Corp' next to your own tenants.
(The old guard only checked for the name 'Acme Corp', so deleting the demo and
creating your own tenant would make the next boot re-add Acme Corp.)

Want the demo back? Seed into a fresh/empty database (a new ADMIN_DB_URL).
"""
import os, duckdb
from . import store
from .connectors.factory import make_connector
from .schema_scan import build_mdl


def _build_sample_db(path: str) -> bool:
    """(Re)build the demo DuckDB. Returns False if the file is locked (Windows)."""
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as e:
        print(f"sample DB is locked, reusing existing file ({e})")
        return os.path.exists(path)
    con = duckdb.connect(path)
    con.execute("CREATE TABLE customers(customer_id INT, name VARCHAR, email VARCHAR, region VARCHAR)")
    con.execute("INSERT INTO customers VALUES (1,'Andi','andi@mail.com','Jakarta'),(2,'Budi','budi@mail.com','Bandung'),(3,'Citra','citra@mail.com','Jakarta'),(4,'Dewi','dewi@mail.com','Surabaya')")
    con.execute("CREATE TABLE orders(order_id INT, customer_id INT, amount DOUBLE, status VARCHAR, region VARCHAR)")
    con.execute("INSERT INTO orders VALUES (101,1,250000,'paid','Jakarta'),(102,1,150000,'paid','Jakarta'),(103,3,400000,'paid','Jakarta'),(104,2,300000,'paid','Bandung'),(105,2,120000,'paid','Bandung'),(106,4,500000,'paid','Surabaya')")
    con.execute("CREATE TABLE internal_costs(cost_id INT, item VARCHAR, amount DOUBLE)")
    con.execute("INSERT INTO internal_costs VALUES (1,'salary',9000000),(2,'rent',5000000)")
    con.close()
    return True


def run():
    store.init()
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.environ.get("ADMIN_SAMPLE_DB") or os.path.join(base, "sample_acme.duckdb")

    existing = store.list_tenants()
    if existing:
        names = ", ".join(t["name"] for t in existing[:5])
        print(f"Metadata store already has {len(existing)} tenant(s) [{names}...]; "
              "skip seed (non-empty store, sample DB untouched).")
        return

    _build_sample_db(path)
    tid = store.create_tenant("Acme Corp")
    conn = {"path": path}
    intro = make_connector("duckdb", conn).introspect()
    mdl = build_mdl(intro)
    mdl["allowed_tables"] = ["customers", "orders"]  # internal_costs intentionally OUTSIDE the allowlist
    store.create_datasource(tid, "duckdb", "Acme Data Warehouse", conn, mdl)
    store.create_role(tid, "analyst_jakarta", {"row_filters": {"region": "Jakarta"}, "can_see_pii": False, "denied_columns": []})
    store.create_role(tid, "regional_manager", {"row_filters": {}, "can_see_pii": False, "denied_columns": []})
    store.create_role(tid, "admin", {"row_filters": {}, "can_see_pii": True, "denied_columns": []})
    # External RBAC alias demo: a client system's own id ("SPV-01") maps onto our role,
    # so its JWTs work without renaming either side (see role_mappings / consumer._resolve_role).
    store.upsert_role_mapping(tid, "SPV-01", "analyst_jakarta")
    # One app key per ENVIRONMENT (not per role). The key only identifies the consumer
    # app; the end-user's role is sent per request via a verified JWT (strict Pattern B).
    k1 = store.create_api_key(tid, label="Production App Key", env="Production")
    k2 = store.create_api_key(tid, label="Development App Key", env="Development")
    print("SEED OK | tenant:", tid)
    print("API key (Production)  :", k1)
    print("API key (Development) :", k2)


if __name__ == "__main__":
    run()
