"""SQL console: an admin runs SELECT scripts and reads the rows, governed like /v1/query.

What is worth pinning is that the editor is not a side door: every statement runs as a
tenant role through the same execute_governed path the agent uses, so the connection
allowlist, the role's authorized tables, PII, business narrowing and read-only all hold,
and every statement leaves an audit row naming who ran it.
"""
import secrets
import sys
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store                                          # noqa: E402
from app.connectors.factory import make_connector              # noqa: E402
from app.main import app                                       # noqa: E402
from app.routers.sql_console import split_statements           # noqa: E402
from app.schema_scan import scan_connector                     # noqa: E402

client = TestClient(app)
AK = {"X-Admin-Key": "admin-dev-key"}

OFFLINE, MARKETPLACE, HIDDEN = "offline_sales", "mp_orders", "salary_secret"


@pytest.fixture
def world(tmp_path):
    path = str(tmp_path / "console.duckdb")
    con = duckdb.connect(path)
    con.execute(f"CREATE TABLE {OFFLINE}(month_id INTEGER, gmv DOUBLE, buyer_email VARCHAR)")
    con.execute(f"INSERT INTO {OFFLINE} VALUES (202608, 100.0, 'a@b.com'), (202609, 120.0, 'c@d.com')")
    con.execute(f"CREATE TABLE {MARKETPLACE}(month_id INTEGER, gmv DOUBLE, brand VARCHAR)")
    con.execute(f"INSERT INTO {MARKETPLACE} VALUES (202609, 250.0, 'ACMESWEET'), (202609, 90.0, 'X;Y')")
    con.execute(f"CREATE TABLE {HIDDEN}(amount DOUBLE)")
    con.execute(f"INSERT INTO {HIDDEN} VALUES (1.0)")
    con.close()

    tid = store.create_tenant("SQLC-console")
    store.create_role(tid, "analyst", {"full_access": True, "can_see_pii": True})
    store.create_role(tid, "viewer", {"can_see_pii": False, "authorized_tables": [MARKETPLACE]})
    mdl = scan_connector(make_connector("duckdb", {"path": path}))
    mdl["allowed_tables"] = [OFFLINE, MARKETPLACE]            # the third table is never allowed
    mdl["pii_columns"] = ["buyer_email"]
    did = store.create_datasource(tid, "duckdb", "Acme L2", {"path": path}, mdl)
    three_p = store.create_business(tid, "3P", "Marketplace")
    store.set_business_knowledge(three_p, did, {"allowed_tables": [MARKETPLACE]})
    store.set_role_businesses(tid, "analyst", [three_p])
    yield {"tid": tid, "did": did, "path": path}
    store.delete_tenant(tid)


def _run(w, sql, **body):
    return client.post(f"/admin/tenants/{w['tid']}/sql", headers=AK,
                       json={"sql": sql, "datasource_id": w["did"], **body})


def test_split_statements_respects_strings_and_comments():
    sql = "SELECT ';' AS a;\n-- a comment; still a comment\nSELECT 2;\n;  \n/* only a comment */"
    assert split_statements(sql, "duckdb") == ["SELECT ';' AS a", "-- a comment; still a comment\nSELECT 2"]
    assert split_statements("  ", "duckdb") == []


def test_catalog_lists_only_allowed_tables_with_role_flags(world):
    cat = client.get(f"/admin/tenants/{world['tid']}/sql/catalog?role=viewer", headers=AK).json()
    assert cat["role"] == "viewer" and {r["name"] for r in cat["roles"]} == {"analyst", "viewer"}
    tables = {t["name"]: t for t in cat["datasources"][0]["tables"]}
    assert set(tables) == {OFFLINE, MARKETPLACE}                 # the unallowed table is absent
    assert tables[OFFLINE]["role_denied"] and not tables[MARKETPLACE]["role_denied"]
    email = next(c for c in tables[OFFLINE]["columns"] if c["name"] == "buyer_email")
    assert email["pii"] and email["blocked"]

    cat = client.get(f"/admin/tenants/{world['tid']}/sql/catalog?role=analyst&business=3P",
                     headers=AK).json()
    assert cat["business"]["name"] == "3P"
    assert [t["name"] for t in cat["datasources"][0]["tables"]] == [MARKETPLACE]


def test_script_runs_each_statement_governed(world):
    r = _run(world, f"SELECT brand, gmv FROM {MARKETPLACE} WHERE brand = 'X;Y';\n"
                    f"SELECT month_id, SUM(gmv) AS gmv FROM {OFFLINE} GROUP BY 1 ORDER BY 1",
             role="analyst")
    assert r.status_code == 200, r.text
    first, second = r.json()["results"]
    assert first["ok"] and first["rows"] == [["X;Y", 90.0]]
    assert "LIMIT" in first["safe_sql"].upper()                  # govern's row cap was applied
    assert second["columns"] == ["month_id", "gmv"] and second["row_count"] == 2


def test_write_statements_and_unallowed_tables_are_refused(world):
    res = _run(world, f"DELETE FROM {MARKETPLACE}; SELECT * FROM {HIDDEN}",
               role="analyst").json()["results"]
    assert [x["ok"] for x in res] == [False, False]
    assert res[0]["code"] == "READ_ONLY"
    con = duckdb.connect(world["path"], read_only=True)
    assert con.execute(f"SELECT COUNT(*) FROM {MARKETPLACE}").fetchone()[0] == 2
    con.close()


def test_role_policy_holds_pii_and_authorized_tables(world):
    res = _run(world, f"SELECT buyer_email FROM {OFFLINE}; SELECT * FROM {MARKETPLACE}; "
                      f"SELECT gmv FROM {OFFLINE}", role="viewer").json()["results"]
    assert not res[0]["ok"]                                       # PII / unauthorized table
    assert res[1]["ok"] and res[1]["row_count"] == 2
    assert not res[2]["ok"] and res[2]["code"] == "ROLE_TABLE_DENIED"


def test_business_narrows_and_an_ungranted_business_is_refused(world):
    res = _run(world, f"SELECT gmv FROM {OFFLINE}", role="analyst", business="3P").json()["results"]
    assert not res[0]["ok"]
    assert _run(world, "SELECT 1", role="viewer", business="3P").status_code == 409


def test_every_statement_is_audited_with_who_ran_it(world):
    _run(world, f"SELECT gmv FROM {MARKETPLACE}; DROP TABLE {OFFLINE}", role="analyst")
    rows = store.list_audit_events(world["tid"], "console_sql")
    assert [r["status"] for r in rows] == ["error", "ok"]        # newest first
    assert rows[1]["detail"]["sql"].startswith("SELECT gmv") and rows[1]["role"] == "analyst"


def test_a_manager_without_the_tenant_cannot_query_it(world):
    login = secrets.token_urlsafe(12)                 # throwaway, generated per run
    r = client.post("/auth/users", headers=AK, json={"email": "mgr@sqlc-test.local", "name": "mgr",
                                                    "role": "tenant_manager", "password": login,
                                                    "provider": "local"})
    assert r.status_code == 200, r.text
    try:
        client.cookies.clear()
        assert client.post("/auth/login", json={"email": "mgr@sqlc-test.local",
                                                "password": login}).status_code == 200
        r = client.post(f"/admin/tenants/{world['tid']}/sql",
                        json={"sql": f"SELECT gmv FROM {MARKETPLACE}", "role": "analyst"})
        assert r.status_code == 403
        assert client.get(f"/admin/tenants/{world['tid']}/sql/catalog").status_code == 403
    finally:
        client.cookies.clear()
        for u in store.list_console_users():
            if u["email"] == "mgr@sqlc-test.local":
                store.delete_console_user(u["id"])


def test_bad_input_is_a_clear_error(world):
    assert _run(world, "  ").json()["detail"]["code"] == "NO_SQL"
    assert _run(world, "SELECT 1", role="nobody").status_code == 404
    assert _run(world, ";".join(["SELECT 1"] * 11)).json()["detail"]["code"] == "TOO_MANY_STATEMENTS"
