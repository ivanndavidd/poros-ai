"""MBR report phase 1: governed template queries -> report model -> Markdown package.

The properties pinned here:
  * numbers come from governed queries (the business slice is enforced: a table outside
    it is a documented gap, never read);
  * missing tables, columns and settings degrade sections to partial/placeholder and are
    listed in OPEN-ITEMS — the report still ships;
  * order status groups come from the values in the data (settings win over the keyword
    guess, and guesses are flagged for the owner);
  * caller input cannot break out of a SQL literal;
  * the job + consumer API round trip stores encrypted files only the requester can read.
"""
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb
import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import jobs, store                                      # noqa: E402
from app.connectors.factory import make_connector                # noqa: E402
from app.main import app                                         # noqa: E402
from app.reports import markdown, mbr_config, mbr_engine         # noqa: E402
from app.reports.sqlkit import Month, ReportInputError, clean_brand, lit   # noqa: E402
from app.schema_scan import scan_connector                       # noqa: E402

client = TestClient(app)
AK = {"X-Admin-Key": "admin-dev-key"}

ORDER_COLS = """marketplace VARCHAR, brand VARCHAR, order_id VARCHAR, order_item_id VARCHAR,
    order_date DATE, month_id INTEGER, paid_date DATE, rts_date DATE, order_status VARCHAR,
    sku_id VARCHAR, product_name VARCHAR, customer_id VARCHAR, quantity DOUBLE, nmv DOUBLE,
    gmv DOUBLE, new_gmv DOUBLE, discount_platform DOUBLE, subsidi_mp DOUBLE,
    cancel_reason_groupping VARCHAR, cancel_by VARCHAR, payment_method VARCHAR,
    provinsi_match_clean VARCHAR, new_product_type VARCHAR"""


def _order_line(n, m, day, sku, price):
    od = date(m.year, m.month, day)
    status = "Selesai" if n % 7 else "Dibatalkan"
    cancelled = status == "Dibatalkan"
    v = price * (2.0 if m.mid >= 202609 else 1.0)                 # Acme era doubles sales
    return ("TikTok" if n % 2 else "Shopee", "Acmesmile", f"o{n}", f"i{n}", od,
            m.mid, od, od + timedelta(days=n % 3), status, sku,
            f"Product {sku}", f"c{n % 5}", 1.0, v, v * 1.1, None, 1000.0, 0.0,
            "Buyer" if cancelled else None, "buyer" if cancelled else None,
            "COD" if n % 3 else "Bank Transfer", "DKI Jakarta", "Toothpaste")


def _orders(con):
    con.execute(f"CREATE TABLE acme_store_order_mp({ORDER_COLS})")
    rows, n = [], 0
    for back in range(0, 13):                       # Sep 2025 .. Sep 2026
        m = Month(2026, 9).shift(-back)
        for day in (3, 10, 17):
            for sku, price in (("SKU-A", 100_000.0), ("SKU-B", 40_000.0)):
                n += 1
                rows.append(_order_line(n, m, day, sku, price))
    rows.append(("Shopee", "Other", "x1", "x1", date(2026, 9, 5), 202609, date(2026, 9, 5),
                 date(2026, 9, 5), "Selesai", "SKU-Z", "Other product", "z", 1.0, 9e9, 9e9,
                 None, 0.0, 0.0, None, None, "COD", "Bali", "Other"))
    rows.append(("Shopee", "Acmesmile", "u1", "u1", date(2026, 9, 6), 202609, None, None,
                 "Belum Dibayar", "SKU-A", "Product SKU-A", "c9", 1.0, 5e5, 5e5, None, 0.0,
                 0.0, None, None, "COD", "Bali", "Toothpaste"))
    for day in (2, 5):                              # the month after the review: "so far"
        n += 1
        od = date(2026, 10, day)
        rows.append(("TikTok", "Acmesmile", f"o{n}", f"i{n}", od, 202610, od, od, "Selesai",
                     "SKU-A", "Product SKU-A", "c1", 1.0, 200_000.0, 220_000.0, None, 1000.0,
                     0.0, None, None, "COD", "DKI Jakarta", "Toothpaste"))
    con.executemany("INSERT INTO acme_store_order_mp VALUES (" + ",".join("?" * 23) + ")", rows)


@pytest.fixture
def world(tmp_path):
    path = str(tmp_path / "mbr.duckdb")
    con = duckdb.connect(path)
    _orders(con)
    con.execute("""CREATE TABLE acme_store_order_mp_ai_curated(brand VARCHAR, order_id VARCHAR,
        order_date DATE, gmv DOUBLE, nmv DOUBLE)""")
    con.execute("INSERT INTO acme_store_order_mp_ai_curated VALUES "
                "('ACMESMILE','o1',DATE '2026-09-03', 100.0, 90.0)")
    con.execute("""CREATE TABLE vw_inventory_order_wh_daily_snapshot(brand_ref_dpim VARCHAR,
        report_date DATE, sto_item_code VARCHAR, sto_item_name VARCHAR,
        sto_available_stock DOUBLE, max_hpp DOUBLE, orderqty30 DOUBLE, po_ship_date DATE,
        po_quantity DOUBLE)""")
    con.execute("""INSERT INTO vw_inventory_order_wh_daily_snapshot VALUES
        ('ACMESMILE', DATE '2026-09-29', 'I1', 'Item 1', 100, 10000, 300, NULL, 0),
        ('ACMESMILE', DATE '2026-09-30', 'I1', 'Item 1', 90, 10000, 300, DATE '2026-10-05', 500),
        ('ACMESMILE', DATE '2026-09-30', 'I2', 'Item 2', 1000, 5000, 30, NULL, 0)""")
    # a table the MBR business is NOT allowed to read: must never be queried
    con.execute("CREATE TABLE acme_store_wallet_mp(brand VARCHAR, month_id INTEGER, wallet_gmv DOUBLE)")
    # feeds that exist but are not loaded for the brand: zero everywhere
    con.execute("""CREATE TABLE acme_finance_pnl_mp(brand VARCHAR, order_date DATE, nmv DOUBLE,
        net_revenue DOUBLE, mp_fee DOUBLE, affiliate_fee DOUBLE, cogm DOUBLE)""")
    con.execute("INSERT INTO acme_finance_pnl_mp VALUES "
                "('Acmesmile', DATE '2026-09-05', 500000, 450000, 0, 0, 0)")
    con.execute("""CREATE TABLE acme_store_ads_performance(brand VARCHAR, date DATE, source VARCHAR,
        ads_cost_idr DOUBLE, ads_gmv_idr DOUBLE, clicks DOUBLE, views DOUBLE)""")
    con.execute("INSERT INTO acme_store_ads_performance VALUES "
                "('Acmesmile', DATE '2026-09-05', 'Ads Performance', 0, 0, 0, 0)")
    # a September order still on its way: processed, but not final
    con.execute("INSERT INTO acme_store_order_mp (marketplace, brand, order_id, order_item_id, "
                "order_date, month_id, paid_date, order_status, sku_id, product_name, "
                "customer_id, quantity, nmv, gmv) VALUES ('TikTok', 'Acmesmile', 'f1', 'f1', "
                "DATE '2026-09-29', 202609, DATE '2026-09-29', 'Dikirim', 'SKU-A', "
                "'Product SKU-A', 'c1', 1, 50000, 55000)")
    con.close()

    tid = store.create_tenant("MBR-test")
    store.create_role(tid, "analyst", {"full_access": True, "can_see_pii": False})
    mdl = scan_connector(make_connector("duckdb", {"path": path}))
    did = store.create_datasource(tid, "duckdb", "Acme L2", {"path": path}, mdl)
    biz = store.create_business(tid, "MBR", "Monthly business review")
    store.set_business_knowledge(biz, did, {"allowed_tables": [
        "acme_store_order_mp", "acme_store_order_mp_ai_curated",
        "vw_inventory_order_wh_daily_snapshot", "acme_finance_pnl_mp",
        "acme_store_ads_performance"]})
    store.set_role_businesses(tid, "analyst", [biz])
    store.set_tenant_config(tid, "mbr", {
        "brands": {"acmesmile": {"takeover_date": "2026-09-01",
                              "targets": {"2026-09": {"nmv": 1_000_000}},
                              "campaigns": [{"name": "9.9", "start": "2026-09-10",
                                             "end": "2026-09-10"}]}},
        "status_map": {"selesai": "processed"},
    })
    yield {"tid": tid, "did": did, "biz": biz}
    store.delete_tenant(tid)


def _model(world, brand="ACMESMILE"):
    run = mbr_engine.prepare(world["tid"], "analyst", brand, "2026-09", "MBR")
    return run, mbr_engine.build(run)


def _section(model, sid):
    return next(s for s in model["sections"] if s["id"] == sid)


# ---- inputs ----
def test_literals_cannot_break_out_and_inputs_are_validated():
    assert lit("O'Neil") == "'O''Neil'"
    with pytest.raises(ReportInputError):
        lit("a\x00b")
    with pytest.raises(ReportInputError):
        Month.parse("2026-13")
    with pytest.raises(ReportInputError):
        clean_brand("   ")
    assert Month(2026, 1).shift(-1).key == "2025-12"
    assert Month(2026, 9).shift(-12).mid == 202509


def test_status_groups_settings_win_and_guesses_are_flagged():
    assert mbr_config.classify_status("Selesai", {"selesai": "processed"}) == ("processed", True)
    assert mbr_config.classify_status("Dibatalkan", {}) == ("cancelled", False)
    assert mbr_config.classify_status("Belum Dibayar", {}) == ("unpaid", False)


SHOPEE_OPEN = "Pesanan diterima, namun Pembeli masih dapat mengajukan pengembalian hingga {}."


def test_received_with_an_open_return_window_is_sold_not_returned():
    # staging DSOL Sep 2026: 73 dated spellings of this status were read as returns,
    # IDR 337 m of September NMV went missing and the scorecard said "NMV fell 26.9%"
    for day in ("2026-10-13", "2025-10-01"):
        assert mbr_config.classify_status(SHOPEE_OPEN.format(day), {}) == ("processed", False)
    assert mbr_config.classify_status("Pengembalian Dana", {}) == ("returned", False)
    assert mbr_config.classify_status("Package Returned", {}) == ("returned", False)
    # the owner confirms the status once, for every date
    owner = mbr_config._status_map({SHOPEE_OPEN.format("<date>"): "processed",
                                    "dikembalikan": "returned"})
    assert mbr_config.classify_status(SHOPEE_OPEN.format("2026-01-11"), owner) == \
        ("processed", True)
    assert mbr_config.status_key(SHOPEE_OPEN.format("2026-10-13")) == \
        mbr_config.status_key(SHOPEE_OPEN.format("2025-12-31"))
    from app.reports import labels as L
    assert L.status_label(SHOPEE_OPEN.format("2026-10-13")) == "Received (return window open)"
    assert L.status_list([SHOPEE_OPEN.format("2026-10-13"), SHOPEE_OPEN.format("2025-10-01"),
                          "Selesai", "Diterima", "ready_to_ship", "canceled"]) == \
        "Received (return window open), Completed, Received, Ready to ship, Cancelled"


def test_dated_spellings_of_one_status_are_one_row_to_confirm():
    from types import SimpleNamespace
    from app.reports import mbr_sections
    rows = [{"order_status": "Selesai", "line_count": 10, "gmv": 100.0},
            {"order_status": SHOPEE_OPEN.format("2026-10-13"), "line_count": 3, "gmv": 30.0},
            {"order_status": SHOPEE_OPEN.format("2026-10-11"), "line_count": 2, "gmv": 20.0}]
    run = SimpleNamespace(month=Month.parse("2026-09"), brand="DSOL",
                          config=SimpleNamespace(status_map={}),
                          query=lambda *a: {"ok": True, "rows": rows})
    assert mbr_sections.discover_statuses(run)
    assert len(run.statuses["processed"]) == 3               # every spelling reaches the SQL
    shown = {r["value"]: r for r in run.status_rows}
    merged = shown[SHOPEE_OPEN.format("<date>")]
    assert (merged["lines"], merged["gmv"], merged["variants"]) == (5, 50.0, 2)
    assert merged["group"] == "processed" and len(run.status_rows) == 2


def test_csat_all_zero_is_not_recorded_and_left_out_of_the_score():
    # staging DSOL Sep 2026: Shopee's 188 chats carried no score and read "0.0% CSAT",
    # pulling the headline to 42%
    from app.reports import mbr_sections
    sec = {"tables": [], "facts": [], "headline": ""}
    rec = {"key": "customer_service", "rows": [
        {"platform": "shopee", "chats": 188, "avg_response_seconds": 98, "csat": 0, "csat_max": 0},
        {"platform": "tiktok", "chats": 144, "avg_response_seconds": 197, "csat": 0.96875,
         "csat_max": 1}]}
    mbr_sections._cs_rows(sec, rec, Month.parse("2026-09"))
    rows = {r[0]: r for r in sec["tables"][0]["rows"]}
    assert rows["Shopee"][3] == "Not recorded" and rows["TikTok"][3] == pytest.approx(96.875)
    assert "97% satisfaction where rated" in sec["headline"]
    assert any("Shopee chats carry no satisfaction score" in f["text"] for f in sec["facts"])


def test_product_lines_merge_spellings_and_one_named_line_is_no_split():
    from types import SimpleNamespace
    from app.reports import mbr_sections
    rows = [{"line": None, "nmv_cur": 698.8, "nmv_prev": 697.4},
            {"line": "ACMESWEET", "nmv_cur": 483.3, "nmv_prev": 475.4},
            {"line": "Acmesweet ", "nmv_cur": 35.3, "nmv_prev": 31.1}]
    merged = mbr_sections._merged_lines(rows)
    assert merged == {"Not recorded": [698.8, 697.4],
                      "Acmesweet": [pytest.approx(518.6), pytest.approx(506.5)]}
    run = SimpleNamespace(month=Month.parse("2026-09"), brand="ACMESWEET",
                          statuses={"processed": ["Selesai"]},
                          missing_columns=lambda t, cols: [c for c in cols if c != "sub_brand_new"],
                          query=lambda *a: {"ok": True, "rows": rows, "key": "product_line_mix"})
    sec = {"tables": [], "facts": [], "queries": []}
    mbr_sections._product_lines(run, sec)
    assert sec["tables"] == []                        # only "Acmesweet" is named: no page
    two = rows + [{"line": "Acmesweet Gold", "nmv_cur": 120.0, "nmv_prev": 100.0}]
    run.query = lambda *a: {"ok": True, "rows": two, "key": "product_line_mix"}
    mbr_sections._product_lines(run, sec)
    names = [r[0] for r in sec["tables"][0]["rows"]]
    assert names.count("Acmesweet") == 1 and "Acmesweet Gold" in names


def test_ads_reads_a_text_date_column_through_govern():
    from app.governance.engine import govern
    from app.reports.sqlkit import day_of
    text_day = day_of("bigquery", "date")      # whatever the scan says: it was wrong once
    assert text_day == "SAFE_CAST(SUBSTR(CAST(date AS STRING), 1, 10) AS DATE)"
    sql = (f"SELECT (EXTRACT(YEAR FROM {text_day}) * 100 + EXTRACT(MONTH FROM {text_day})) "
           f"AS month_id, SUM(ads_cost_idr) AS cost FROM acme_store_ads_performance "
           f"WHERE {text_day} BETWEEN DATE '2026-07-01' AND DATE '2026-09-30' GROUP BY 1")
    mdl = {"tables": {"acme_store_ads_performance": ["date", "ads_cost_idr", "brand"]},
           "allowed_tables": ["acme_store_ads_performance"], "pii_columns": []}
    assert "SAFE_CAST" in govern(sql, "bigquery", mdl, {"can_see_pii": False})
    con = duckdb.connect()
    con.execute("CREATE TABLE ads(date VARCHAR, cost DOUBLE)")
    con.execute("INSERT INTO ads VALUES ('2026-09-03', 5), ('2026-08-31 23:00:00', 7), "
                "('not a date', 9)")
    duck_day = day_of("duckdb", "date")
    window = "BETWEEN DATE '2026-08-01' AND DATE '2026-09-30'"
    assert con.execute(f"SELECT SUM(cost) FROM ads WHERE {duck_day} {window}").fetchone()[0] == 12
    con.execute("CREATE TABLE ads_typed(date DATE, stamped TIMESTAMP, cost DOUBLE)")
    con.execute("INSERT INTO ads_typed VALUES ('2026-09-03', '2026-09-03 10:00:00', 5), "
                "('2026-07-31', '2026-07-31 23:00:00', 7)")
    for col in ("date", "stamped"):             # a real DATE or TIMESTAMP reads the same way
        got = con.execute(f"SELECT SUM(cost) FROM ads_typed WHERE {day_of('duckdb', col)} "
                          f"{window}").fetchone()[0]
        assert got == 5


def test_ads_page_survives_the_split_column_leaving_the_table():
    """Staging renamed acme_store_ads_performance.source: after a rescan the page splits by
    the column the scan knows, or shows totals, instead of failing on 'source'."""
    from types import SimpleNamespace
    from app.reports import mbr_sections
    con = duckdb.connect()
    con.execute("CREATE TABLE acme_store_ads_performance(brand VARCHAR, date VARCHAR, "
                "store VARCHAR, ads_cost_idr DOUBLE, ads_gmv_idr DOUBLE, clicks DOUBLE, "
                "views DOUBLE)")
    con.execute("INSERT INTO acme_store_ads_performance VALUES "
                "('ACMESWEET', '2026-09-03', 'Shopee', 100, 500, 10, 90), "
                "('ACMESWEET', '2026-09-04 08:00:00', 'TikTok', 50, 100, 5, 40)")
    cols = {"brand", "date", "store", "ads_cost_idr", "ads_gmv_idr", "clicks", "views"}

    def query(key, table, columns, build):
        if key != "ads_monthly":
            return {"ok": True, "rows": [], "key": key}
        cur = con.execute(build("duckdb"))
        names = [c[0] for c in cur.description]
        return {"ok": True, "key": key, "rows": [dict(zip(names, r)) for r in cur.fetchall()]}

    run = SimpleNamespace(month=Month.parse("2026-09"), brand="ACMESWEET", queries={},
                          statuses={"processed": ["Selesai"], "cancelled": [], "in_flight": []},
                          missing_columns=lambda t, c: [x for x in c if x not in cols],
                          query=query)
    sec = mbr_sections.sec_ads(run)
    ads = sec["tables"][0]
    assert ads["columns"][1] == "Store"
    assert {r[1] for r in ads["rows"]} == {"Shopee", "TikTok"}
    assert sec["headline"].startswith("Ads spend in Sep 2026 was IDR 150")
    cols.discard("store")                                  # no split column left: totals
    sec = mbr_sections.sec_ads(run)
    assert [r[1] for r in sec["tables"][0]["rows"]] == ["All ads"]


def test_ads_page_falls_back_to_totals_when_the_scan_is_older_than_the_warehouse():
    """Package 4: the scan still listed `source`, the warehouse had renamed it, BigQuery
    refused the query and the page stayed empty until someone rescanned. Totals now, and an
    open item asks for the rescan."""
    from types import SimpleNamespace
    from app.reports import mbr_sections
    con = duckdb.connect()
    con.execute("CREATE TABLE acme_store_ads_performance(brand VARCHAR, date VARCHAR, "
                "store VARCHAR, ads_cost_idr DOUBLE, ads_gmv_idr DOUBLE, clicks DOUBLE, "
                "views DOUBLE)")
    con.execute("INSERT INTO acme_store_ads_performance VALUES "
                "('ACMESWEET', '2026-09-03', 'Shopee', 100, 500, 10, 90), "
                "('ACMESWEET', '2026-09-04', 'TikTok', 50, 100, 5, 40)")
    scanned = {"brand", "date", "source", "ads_cost_idr", "ads_gmv_idr", "clicks", "views"}
    keys = []

    def query(key, table, columns, build):
        keys.append(key)
        if not key.startswith("ads_monthly"):
            return {"ok": True, "rows": [], "key": key}
        try:
            cur = con.execute(build("duckdb"))
        except duckdb.Error:                             # what BigQuery says, verbatim
            return {"ok": False, "rows": [], "key": key, "code": "DRY_RUN_ERROR",
                    "error": "400 POST https://bigquery.googleapis.com/...: Unrecognized "
                             "name: source; Did you mean store? at [1:180]"}
        names = [c[0] for c in cur.description]
        return {"ok": True, "key": key, "rows": [dict(zip(names, r)) for r in cur.fetchall()]}

    run = SimpleNamespace(month=Month.parse("2026-09"), brand="ACMESWEET", queries={},
                          statuses={"processed": ["Selesai"], "cancelled": [], "in_flight": []},
                          missing_columns=lambda t, c: [x for x in c if x not in scanned],
                          query=query)
    sec = mbr_sections.sec_ads(run)
    assert keys[:2] == ["ads_monthly", "ads_monthly_total"]
    assert [r[1] for r in sec["tables"][0]["rows"]] == ["All ads"]
    assert sec["headline"].startswith("Ads spend in Sep 2026 was IDR 150")
    split = next(m for m in sec["missing"] if m["what"] == "ads split by source")
    assert "rescan" in split["needs"] and split["fallback"] == "ads totals"


def _duck_run(con, scanned, **extra):
    """A run whose queries hit a DuckDB connection, with `scanned` as the scan's columns."""
    from types import SimpleNamespace

    def query(key, table, columns, build):
        lost = [c for c in columns if c not in scanned]
        if lost:
            return {"ok": False, "rows": [], "key": key, "table": table,
                    "code": "COLUMN_MISSING", "error": f"{table} has no column(s) {lost}"}
        cur = con.execute(build("duckdb"))
        names = [c[0] for c in cur.description]
        return {"ok": True, "key": key, "rows": [dict(zip(names, r)) for r in cur.fetchall()]}

    return SimpleNamespace(month=Month.parse("2026-09"), brand="ACMESWEET", queries={},
                           statuses={"processed": ["Selesai"], "cancelled": [], "in_flight": []},
                           missing_columns=lambda t, c: [x for x in c if x not in scanned],
                           query=query, **extra)


def test_ads_page_reads_cost_and_return_when_clicks_left_the_table():
    """Package 5 (after the Acme L2 rescan): the scan no longer lists `clicks`, and the
    whole ads page was a placeholder although cost and ads GMV were there."""
    from app.reports import mbr_sections
    con = duckdb.connect()
    con.execute("CREATE TABLE acme_store_ads_performance(brand VARCHAR, date VARCHAR, "
                "source VARCHAR, ads_cost_idr DOUBLE, ads_gmv_idr DOUBLE)")
    con.execute("INSERT INTO acme_store_ads_performance VALUES "
                "('ACMESWEET', '2026-09-03', 'Shopee', 100, 500)")
    run = _duck_run(con, {"brand", "date", "source", "ads_cost_idr", "ads_gmv_idr"})
    sec = mbr_sections.sec_ads(run)
    ads = sec["tables"][0]
    assert ads["columns"] == ["Month", "Source", "Cost", "Ads GMV", "ROAS"]
    assert ads["rows"][0][2:] == [100, 500, 5.0]
    assert sec["headline"].startswith("Ads spend in Sep 2026 was IDR 100")


def test_traffic_page_reads_the_names_the_rebuilt_table_uses():
    """Package 5: the rescan of the traffic table lists none of order_month, total_orders,
    total_nmv. The first month/orders/NMV name the scan knows is read instead, and a month
    held as YYYYMM or as a date both land on the right month."""
    from types import SimpleNamespace
    from app.reports import mbr_sections
    con = duckdb.connect()
    con.execute("CREATE TABLE acme_customer_traffic_conversion(client VARCHAR, month_id INTEGER, "
                "visitors DOUBLE, orders DOUBLE)")
    con.execute("INSERT INTO acme_customer_traffic_conversion VALUES "
                "('ACMESWEET', 202608, 1000, 20), ('ACMESWEET', 202609, 1200, 30), "
                "('OTHER', 202609, 9, 9)")
    cfg = SimpleNamespace(brand=SimpleNamespace(client_names=["ACMESWEET"]))
    run = _duck_run(con, {"client", "month_id", "visitors", "orders"}, config=cfg)
    sec = mbr_sections.sec_traffic(run)
    traffic = sec["tables"][0]
    assert traffic["columns"] == ["Month", "Visitors", "Views", "Orders", "Conversion %"]
    assert [r[0] for r in traffic["rows"]] == ["Aug 2026", "Sep 2026"]
    assert traffic["rows"][1][1:4] == [1200, "Not recorded", 30]
    assert "conversion moved from 2.00% to 2.50%" in sec["headline"]

    con.execute("CREATE OR REPLACE TABLE acme_customer_traffic_conversion(client VARCHAR, "
                "order_month DATE, visitors DOUBLE, views DOUBLE, total_orders DOUBLE, "
                "total_nmv DOUBLE)")
    con.execute("INSERT INTO acme_customer_traffic_conversion VALUES "
                "('ACMESWEET', '2026-09-01', 100, 400, 5, 900), "
                "('ACMESWEET', '2025-08-01', 1, 1, 1, 1)")             # outside the window
    run = _duck_run(con, {"client", "order_month", "visitors", "views", "total_orders",
                          "total_nmv"}, config=cfg)
    traffic = mbr_sections.sec_traffic(run)["tables"][0]
    assert traffic["columns"][-1] == "NMV"
    assert traffic["rows"] == [["Sep 2026", 100, 400, 5, 5.0, 900]]


def test_a_missing_column_names_what_the_scan_does_list():
    """So the data team can map the page to the new names in one round, not two."""
    from app.reports import mbr_sections
    rec = {"key": "traffic_conversion", "table": "acme_customer_traffic_conversion",
           "code": "COLUMN_MISSING", "error": "has no column(s) order_month",
           "scanned": ["client", "month_id", "visitors"]}
    code, _needs, detail = mbr_sections._classify(rec)
    assert code == "COLUMN_MISSING"
    assert detail.endswith("The scan lists: client, month_id, visitors")


def test_ads_money_held_as_text_reads_as_numbers():
    """Package 6: the warehouse turned ads_cost_idr/ads_gmv_idr into text and SUM over text
    failed the dry run. Plain number text is read; text that is not a number is named as
    such, never shown as zero spend."""
    from app.reports import mbr_sections
    con = duckdb.connect()
    con.execute("CREATE TABLE acme_store_ads_performance(brand VARCHAR, date VARCHAR, "
                "ads_cost_idr VARCHAR, ads_gmv_idr VARCHAR)")
    con.execute("INSERT INTO acme_store_ads_performance VALUES "
                "('ACMESWEET', '2026-09-03', '100', '500'), "
                "('ACMESWEET', '2026-09-04', '50.5', ''), "
                "('ACMESWEET', '2026-09-05', NULL, NULL)")
    run = _duck_run(con, {"brand", "date", "ads_cost_idr", "ads_gmv_idr"})
    sec = mbr_sections.sec_ads(run)
    assert sec["tables"][0]["rows"][0][2:4] == [150.5, 500]
    assert sec["headline"].startswith("Ads spend in Sep 2026 was IDR 150")

    con.execute("UPDATE acme_store_ads_performance SET ads_cost_idr = 'Rp 1.000', "
                "ads_gmv_idr = 'Rp 5.000'")
    sec = mbr_sections.sec_ads(run)
    assert not sec["tables"]
    gap = next(m for m in sec["missing"] if m["what"].startswith("ads cost and ads GMV"))
    assert gap["what"].endswith("as numbers") and "text" in gap["needs"]

    # package 7: a brand absent from the feed has no rows, which is not "only zeros"
    con.execute("DELETE FROM acme_store_ads_performance")
    sec = mbr_sections.sec_ads(_duck_run(con, {"brand", "date", "ads_cost_idr", "ads_gmv_idr"}))
    gap = next(m for m in sec["missing"] if m["what"].startswith("ads cost and ads GMV"))
    assert "has no rows for ACMESWEET" in gap["needs"] and "zero" not in gap["needs"]


def test_traffic_page_finds_the_brand_when_no_client_name_is_set():
    """Package 6: the traffic data names the CLIENT (the principal), not the brand, so the
    default client name (the brand) found no rows. The brand is looked for in the brand and
    store columns, the match is said, and other clients' rows never join."""
    from types import SimpleNamespace
    from app.reports import mbr_sections
    con = duckdb.connect()
    con.execute("CREATE TABLE acme_customer_traffic_conversion(client VARCHAR, store VARCHAR, "
                "month_id INTEGER, visitors DOUBLE, total_order DOUBLE)")
    con.execute("INSERT INTO acme_customer_traffic_conversion VALUES "
                "('PT PRINCIPAL', 'Acmesweet Official Store', 202609, 1000, 20), "
                "('PT PRINCIPAL', 'Other Brand Store', 202609, 9000, 90)")
    cfg = SimpleNamespace(brand=SimpleNamespace(client_names=["ACMESWEET"]))
    scanned = {"client", "store", "month_id", "visitors", "total_order"}
    sec = mbr_sections.sec_traffic(_duck_run(con, scanned, config=cfg))
    assert sec["tables"][0]["rows"] == [["Sep 2026", 1000, "Not recorded", 20, 2.0]]
    note = next(f for f in sec["facts"] if "matched by" in f["text"])
    assert "store names containing ACMESWEET" in note["text"]
    assert "pins it" not in note["text"]   # client_names filters the client, not the store

    cfg = SimpleNamespace(brand=SimpleNamespace(client_names=["PT OTHER"]))   # a name set in the settings is used as is
    sec = mbr_sections.sec_traffic(_duck_run(con, scanned, config=cfg))
    assert not sec["tables"][0]["rows"]
    gap = next(m for m in sec["missing"] if m["what"].startswith("traffic rows"))
    assert "store name" not in gap["needs"]

    con.execute("DELETE FROM acme_customer_traffic_conversion WHERE store LIKE 'Acmesweet%'")
    cfg = SimpleNamespace(brand=SimpleNamespace(client_names=["ACMESWEET"]))
    sec = mbr_sections.sec_traffic(_duck_run(con, scanned, config=cfg))
    gap = next(m for m in sec["missing"] if m["what"].startswith("traffic rows"))
    assert "no client, brand or store name" in gap["needs"]

    # Kept per client only (staging: client Acmehealth sells more brands than Acmesweet): the
    # ask is a brand column, never "set the client name", which would mix brands in
    scanned -= {"store"}
    sec = mbr_sections.sec_traffic(_duck_run(con, scanned, config=cfg))
    gap = next(m for m in sec["missing"] if m["what"].startswith("traffic rows"))
    assert gap["needs"].startswith("a brand or store column") and "several brands" in gap["needs"]
    assert "only when that client sells this brand alone" in gap["detail"]


def test_buy_n_listings_are_packs_and_packs_compare_like_for_like():
    """Package 6: "[Buy 3] ..." listings were counted as singles, and the pack ladder's
    NMV per piece mixed a 50 g wafer with a 950 g tin, which the narration read as packs
    being a cheap entry point. Packs now compare with the SAME product sold as 1 piece."""
    from types import SimpleNamespace
    from app.reports import labels as L, mbr_sections as S
    assert L.pack_of("[Buy 3] Acmesweet Wafer Vanilla 50g") == 3
    assert L.pack_of("Beli 2 Gratis 1 Acmesweet Jumbo 950 g") == 3
    assert L.pack_label(1) == L.ONE_PIECE == "1 piece"
    assert L.product_base("[Buy 3] Acmesweet Wafer Vanilla 50g", "ACMESWEET") == \
        L.product_base("Acmesweet Wafer Vanilla 50g", "ACMESWEET") == "wafer vanilla 50g"
    run = SimpleNamespace(brand="ACMESWEET",
                          config=SimpleNamespace(brand=SimpleNamespace(focus_skus={})))
    rows = [{"sku_id": "1", "product_name": "Acmesweet Wafer Vanilla 50g",
             "units_cur": 100, "nmv_cur": 1_000_000},
            {"sku_id": "2", "product_name": "[Buy 3] Acmesweet Wafer Vanilla 50g",
             "units_cur": 50, "nmv_cur": 1_350_000},
            {"sku_id": "3", "product_name": "Acmesweet Jumbo Vanilla 950g",
             "units_cur": 10, "nmv_cur": 3_000_000}]
    out = S._pack_ladder(run, rows)
    assert [r[0] for r in out] == ["1 piece", "Pack of 3"]
    sec = {"facts": []}
    head = S._pack_price_finding(run, sec, "sku_performance", rows, out)
    assert head == "a pack of 3 sold 10.0% below the 1 piece price per piece"
    assert "Wafer vanilla 50g: IDR 9,000 a piece in the pack against IDR 10,000 for 1 piece" \
        in sec["facts"][-1]["text"]
    sec = {"facts": []}                         # no product sold both ways: share only
    head = S._pack_price_finding(run, sec, "sku_performance", rows[1:], S._pack_ladder(run, rows[1:]))
    assert head == "pack of 3 listings made 31.0% of NMV"


def test_margin_names_a_month_the_finance_data_has_only_partly_booked():
    """Package 4: the finance data booked IDR 1.00 bn NMV for Sep against 1.22 bn in this
    review (Aug matched), and the narration read the lower amounts as a profit decline."""
    from app.reports import mbr_sections as S
    pnl = [{"month_id": 202608, "nmv": 1201.7e6, "net_revenue": 1201.7e6, "cogm": 698.6e6},
           {"month_id": 202609, "nmv": 1003.1e6, "net_revenue": 1002.9e6, "cogm": 591.2e6}]
    monthly = [{"month_id": 202608, "nmv": 1203.9e6},
               {"month_id": 202609, "nmv": 1217.3e6, "nmv_in_flight": 240.7e6}]
    sec = S.sec_margin(_CannedRun({"pnl_monthly": pnl, "orders_monthly": monthly}))
    assert sec["headline"].endswith(", a month the finance data has only partly booked")
    note = next(f["text"] for f in sec["facts"] if f["label"] == "INTERPRETATION")
    assert "IDR 1.00 bn NMV for Sep 2026, 18% lower than the IDR 1.22 bn in this review" in note
    assert "while Aug 2026 matches within 0.2%" in note and "IDR 240.7 m" in note
    assert "the margin rate compares and the amounts do not" in note
    assert sec["tables"][0]["columns"][6:] == ["NMV, this review", "Finance vs this review %"]
    pnl[1]["nmv"] = 1210e6                                  # booked: reads as before
    sec = S.sec_margin(_CannedRun({"pnl_monthly": pnl, "orders_monthly": monthly}))
    assert "partly" not in sec["headline"]
    assert all(f["label"] == "FACT" for f in sec["facts"])


def test_a_unit_sold_for_almost_nothing_is_named_not_made_the_price_spread():
    """Package 4: "the widest spread was Jumbo Vanilla 950g, from IDR 100 to IDR 289,710"."""
    from app.reports import mbr_sections as S
    out = [[1, "A", "Jumbo Vanilla 950g", 284374.0, 287016.0, -0.9, 100.0, 289710.0,
            261084.0, 289710.0],
           [2, "B", "Vanilla 570g", 187944.0, 188717.0, -0.4, 53952.0, 190119.0,
            175900.0, 190119.0]]
    sec = {"facts": [], "tables": [{"source": "sku_performance"}]}
    S._pareto_takeaway(sec, out, (Month(2026, 9), Month(2026, 8)), S._widest_spread(out), True)
    text = " ".join(f["text"] for f in sec["facts"])
    assert "the widest spread in Sep 2026 was Vanilla 570g, from IDR 53,952" in text
    assert "Jumbo Vanilla 950g at IDR 100 against IDR 284,374 on average" in text


def test_province_spellings_of_every_marketplace_are_one_province():
    from app.reports import labels
    same = ["JAWA BARAT", "Jawa Barat", "West Java", "West Java Province", " west  java "]
    assert {labels.province_label(p) for p in same} == {"Jawa Barat"}
    assert labels.province_label("NANGGROE ACEH DARUSSALAM (NAD)") == "Aceh"
    assert labels.province_label("D.I. Aceh") == "Aceh"
    assert {labels.province_label(p) for p in (
        "Jakarta", "Jakarta Province", "Daerah Khusus Ibukota Jakarta", "DKI JAKARTA")} == \
        {"DKI Jakarta"}
    assert {labels.province_label(p) for p in (
        "D.I. Yogyakarta", "DI YOGYAKARTA", "Daerah Istimewa Yogyakarta", "Yogyakarta")} == \
        {"DI Yogyakarta"}
    assert labels.province_label("NUSA TENGGARA BARAT (NTB)") == "Nusa Tenggara Barat"
    assert labels.province_label("Riau islands") == "Kepulauan Riau"
    assert labels.province_label("South Sumatra Province") == "Sumatera Selatan"
    assert labels.island("Central Kalimantan") == "Kalimantan"
    assert labels.province_label("") == "Not recorded"


# ---- the engine ----
def test_figures_come_from_the_brand_rows_only(world):
    _, model = _model(world)
    score = _section(model, "scorecard")
    nmv = next(r for r in score["tables"][0]["rows"] if r[0].startswith("NMV"))
    # Sep 2026: 6 lines (3 days x 2 SKUs), at most some cancelled; the 9e9 'Other' row and
    # the unpaid row must not be in NMV
    assert 0 < nmv[1] < 1_000_000
    assert nmv[3] is not None and nmv[3] > 0                    # MoM up (Acme era doubled)
    assert model["data_as_of"].startswith("2026-09")
    assert "unpaid" in {r["group"] for r in model["status_values"]}


def test_every_query_was_governed_and_the_slice_held(world):
    _, model = _model(world)
    assert model["queries"], "no query ran"
    tables = {q["table"] for q in model["queries"] if q["ok"]}
    assert "acme_store_wallet_mp" not in tables
    wallet = next(q for q in model["queries"] if q["table"] == "acme_store_wallet_mp")
    assert wallet["code"] == "TABLE_NOT_IN_SCOPE" and not wallet["ok"]
    assert _section(model, "mp_economics")["status"] in ("partial", "placeholder")


def test_missing_data_degrades_sections_and_the_report_ships(world):
    _, model = _model(world)
    stats = model["stats"]
    assert stats["complete"] >= 3
    assert stats["placeholder"] + stats["partial"] > 0
    price = _section(model, "price_vs_competitor")
    assert price["status"] == "placeholder" and price["missing"][0]["needs"]
    inv = _section(model, "inventory")
    assert inv["status"] in ("complete", "partial")
    assert inv["tables"][0]["title"].endswith("(top by value)")
    assert "30 Sep 2026" in inv["tables"][0]["title"]


def test_settings_drive_target_era_and_campaign_figures(world):
    _, model = _model(world)
    score = _section(model, "scorecard")
    assert any("target" in f["text"] for f in score["facts"])
    era = _section(model, "after_acme")
    assert era["tables"] and era["headline"]
    daily = _section(model, "daily")
    assert any(f["label"] == "INTERPRETATION" and "Campaign days" in f["text"]
               for f in daily["facts"])


def test_gmv_counts_every_status_like_the_reference(world):
    _, model = _model(world)
    rows = {r[0]: r for r in _section(model, "scorecard")["tables"][0]["rows"]}
    assert not any("Gross order value" in k for k in rows)        # one GMV, the reference one
    gmv, nmv = rows["GMV"][1], rows["NMV"][1]
    assert gmv > nmv                       # cancelled + unpaid lines count in GMV, not in NMV
    sql = next(q for q in model["queries"] if q["key"] == "orders_monthly")["sql"]
    assert "discount_platform" in sql and "listed_gmv" in sql
    gd = _section(model, "gmv_definition")
    assert gd["tables"] and any("every order status" in f["text"] for f in gd["facts"])
    assert any("shipping" in f["text"] for f in gd["facts"])
    assert model["data_pulled_at"]
    assert "Data pulled from the warehouse at" in markdown.evidence(model)


def test_shipping_orders_are_called_out_as_not_final(world):
    _, model = _model(world)
    facts = _section(model, "scorecard")["facts"]
    assert any("still in a shipping status (Shipped)" in f["text"] for f in facts)


def test_unloaded_feeds_are_missing_data_not_zero(world):
    _, model = _model(world)
    margin = _section(model, "margin")
    assert not any("Margin" in c for t in margin["tables"] for c in t["columns"])
    assert not any("margin" in f["text"].lower() for f in margin["facts"])   # no "100% margin"
    assert margin["headline"] == "No margin yet: cost of goods is not loaded for ACMESMILE"
    # two "NMV" columns side by side say which is which, and how far apart they are
    assert margin["tables"][0]["columns"][1:4] == [
        "NMV, finance data", "Net revenue, finance data", "NMV, this review"]
    assert "finance data books" in margin["facts"][0]["text"]
    assert any("cost of goods" in m["what"] for m in margin["missing"])
    ads = _section(model, "ads")
    assert ads["status"] == "placeholder" and not ads["tables"]
    assert any("only zero" in m["needs"] for m in ads["missing"])


def test_unknown_brand_is_a_placeholder_not_a_crash(world):
    _, model = _model(world, brand="NOPE")
    assert model["sections"][0]["status"] == "placeholder"
    assert "OPEN" in markdown.open_items(model).upper()


def test_markdown_is_english_traced_and_has_no_bare_gaps(world):
    _, model = _model(world)
    ev = markdown.evidence(model)
    oi = markdown.open_items(model, {"x": "abc"})
    assert "Evidence and Reasoning: ACMESMILE, Sep 2026" in ev
    assert "**FACT**" in ev and "```sql" in ev
    assert "Why it matters" in oi and "What is needed" in oi
    assert markdown.delivery_status(model) in ("DELIVER_WITH_WARNINGS", "QUALITY_FAIL_DELIVERED",
                                               "PASS")
    assert "sha256 abc" in oi


# ---- job + consumer API ----
def _hdr(tid):
    key = client.post("/admin/apikeys", headers=AK,
                      json={"tenant_id": tid, "label": "pytest"}).json()["key"]
    token = pyjwt.encode({"sub": "u1", "role": "analyst"}, "test-secret", algorithm="HS256")
    return {"X-API-Key": key, "Authorization": f"Bearer {token}"}


def test_api_round_trip_stores_encrypted_files(world):
    h = _hdr(world["tid"])
    bad = client.post("/v1/reports/mbr", headers=h, json={"brand": "x", "period": "2026-9"})
    assert bad.status_code == 422
    r = client.post("/v1/reports/mbr", headers=h,
                    json={"brand": "acmesmile", "period": "2026-09", "business": "MBR"})
    assert r.status_code == 202, r.text
    rid = r.json()["report_id"]
    assert jobs.run_one("pytest-mbr") is True
    st = client.get(f"/v1/reports/{rid}", headers=h).json()
    assert st["status"] == "succeeded", st
    names = {f["name"] for f in st["files"]}
    assert {"EVIDENCE-AND-REASONING.md", "OPEN-ITEMS-AND-REVIEW.md"} <= names
    md = client.get(f"/v1/reports/{rid}/files/EVIDENCE-AND-REASONING.md", headers=h)
    assert md.status_code == 200 and b"ACMESMILE" in md.content
    c = store._c()
    try:
        raw = c.execute("SELECT data FROM report_files WHERE job_id=? AND name=?",
                        (rid, "EVIDENCE-AND-REASONING.md")).fetchone()["data"]
    finally:
        c.close()
    assert b"ACMESMILE" not in bytes(raw)                      # encrypted at rest
    other = _hdr(world["tid"])                              # another app key, same tenant
    assert client.get(f"/v1/reports/{rid}", headers=other).status_code == 404
    mine = client.get("/v1/reports", headers=h).json()["reports"]
    assert [r["id"] for r in mine] == [rid]
    assert mine[0]["brand"] == "ACMESMILE" and mine[0]["period"] == "2026-09"
    assert "EVIDENCE-AND-REASONING.md" in {f["name"] for f in mine[0]["files"]}
    assert client.get("/v1/reports", headers=other).json()["reports"] == []


def test_api_refuses_an_ungranted_business(world):
    h = _hdr(world["tid"])
    r = client.post("/v1/reports/mbr", headers=h,
                    json={"brand": "acmesmile", "period": "2026-09", "business": "3P"})
    assert r.status_code in (403, 404, 409)


# ---- live staging findings (13 Oct 2026) ----
def test_report_queries_get_their_own_byte_ceiling_on_a_copy(world):
    from app.reports.mbr_engine import TableRef
    run = mbr_engine.prepare(world["tid"], "analyst", "ACMESMILE", "2026-09", "MBR")
    ds = {"id": "x", "type": "bigquery", "conn": {"project": "p", "max_bytes_billed": 2}}
    out = run._report_source(TableRef("t", ds, set(), "bigquery"))
    assert out["conn"]["max_bytes_billed"] == mbr_config.DEFAULT_MAX_QUERY_BYTES
    assert ds["conn"]["max_bytes_billed"] == 2              # the shared datasource is untouched


def test_cost_limit_and_schema_drift_are_gaps_not_failures():
    from app.reports import mbr_sections as S
    sec = S.section("x", "A", "x")
    S.use(sec, {"key": "wallet_monthly", "table": "t", "ok": False, "code": "COST_LIMIT",
                "error": "query would scan ~4 GB"})
    gap = sec["missing"][0]
    assert sec["status"] == "partial" and "too large" in gap["needs"]
    assert "not partitioned" in gap["detail"]                # the trace is for the data team
    assert gap["what"] == "Marketplace settlements data"     # never the query key
    sec = S.section("y", "A", "y")
    S.use(sec, {"key": "traffic_conversion", "table": "t", "ok": False,
                "code": "DRY_RUN_ERROR", "error": "400 Unrecognized name: order_month at [1:231]"})
    gap = sec["missing"][0]
    assert sec["status"] == "partial" and "rescan" in gap["needs"]
    assert "order_month" in gap["detail"] and "order_month" not in gap["needs"]
    sec = S.section("z", "A", "z")
    S.use(sec, {"key": "q", "table": "t", "ok": False, "code": "ERROR", "error": "boom"})
    assert sec["status"] == "failed"


# ---- phase 2: workbook ----
def test_workbook_has_live_formulas_native_charts_and_qc(world):
    import io
    import os
    import zipfile
    from app.reports import xlsx_writer
    _, model = _model(world)
    data = xlsx_writer.build(model)
    if os.environ.get("MBR_XLSX_OUT"):                # visual check: open in Excel
        Path(os.environ["MBR_XLSX_OUT"]).write_bytes(data)
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    wb = z.read("xl/workbook.xml").decode()
    sheets = re.findall(r'<sheet name="([^"]+)"', wb)
    assert sheets[:2] == ["README", "Storyline"]
    assert {"Open Items", "QC &amp; Reconciliation", "Queries"} <= set(sheets)
    assert any(n.startswith("xl/charts/chart") for n in names)      # native charts
    xml = "".join(z.read(n).decode() for n in names if n.startswith("xl/worksheets/sheet"))
    assert "<f>" in xml and "ABS(" in xml                            # live MoM formulas
    assert "PASS" in xml                                             # QC formulas with cache
    assert all(len(s) <= 31 for s in sheets)


def test_api_package_includes_the_workbook(world):
    h = _hdr(world["tid"])
    rid = client.post("/v1/reports/mbr", headers=h,
                      json={"brand": "acmesmile", "period": "2026-09", "business": "MBR"}).json()["report_id"]
    assert jobs.run_one("pytest-mbr-xlsx") is True
    files = client.get(f"/v1/reports/{rid}", headers=h).json()["files"]
    xlsx = [f for f in files if f["name"].endswith(".xlsx")]
    assert xlsx and xlsx[0]["size"] > 5000
    got = client.get(f"/v1/reports/{rid}/files/{xlsx[0]['name']}", headers=h)
    assert got.status_code == 200 and got.content[:2] == b"PK"
    deck = [f for f in files if f["name"].endswith(".pptx")]           # phase 3
    assert deck and deck[0]["name"] == "ACMESMILE_Sep_2026_Business_Review.pptx"


# ---- phase 3: deck ----
def test_deck_is_native_clean_and_traced(world):
    import io
    import os
    from pptx import Presentation
    from app.reports import pptx_writer
    _, model = _model(world)
    data = pptx_writer.build(model)
    if os.environ.get("MBR_DECK_OUT"):                # visual check: render with PowerPoint
        Path(os.environ["MBR_DECK_OUT"]).write_bytes(data)
    assert pptx_writer.qc(data) == []                 # no dashes, >= 13 pt, notes everywhere
    slides = list(Presentation(io.BytesIO(data)).slides)
    assert len(slides) >= 20
    charts = [sh for s in slides for sh in s.shapes if getattr(sh, "has_chart", False)
              and sh.has_chart]
    assert len(charts) >= 3                           # trend, daily, marketplace at least
    notes = [s.notes_slide.notes_text_frame.text for s in slides]
    assert all("Trace:" in n and "Transition:" in n for n in notes)
    assert any("workbook sheet" in n for n in notes)
    cover = " ".join(sh.text_frame.text for sh in slides[0].shapes if sh.has_text_frame)
    assert "acmesmile" in cover.lower() and "IDR" in cover
    pics = [sh.name for s in slides for sh in s.shapes if sh.shape_type == 13]
    assert "Poros bg" in pics and "Poros icon" in pics   # house backgrounds + icons only


def test_deck_qc_catches_dashes_small_text_and_missing_notes():
    import io
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from app.reports import pptx_writer
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[6])
    r = s.shapes.add_textbox(0, 0, Inches(3), Inches(1)).text_frame.paragraphs[0].add_run()
    r.text, r.font.size = "Sales — up", Pt(10)
    buf = io.BytesIO()
    prs.save(buf)
    issues = pptx_writer.qc(buf.getvalue())
    assert any("dash" in i for i in issues) and any("under 13" in i for i in issues)
    assert any("speaker notes" in i for i in issues)
    assert pptx_writer.clean("A – B - C—D") == "A, B, C, D"
    assert pptx_writer.clean("2026-09-30") == "2026-09-30"     # dates keep their hyphens


# ---- review against the reference package (13 Oct 2026): definitions, words, findings ----
def test_targets_typed_with_the_request_win_and_drive_the_pace(world):
    with pytest.raises(ReportInputError) as bad:
        mbr_config.request_targets({"2025-01": 1e9}, Month(2026, 9))
    assert bad.value.code == "REPORT_BAD_TARGET"
    with pytest.raises(ReportInputError):
        mbr_config.request_targets({"2026-10": -5}, Month(2026, 9))
    run = mbr_engine.prepare(world["tid"], "analyst", "ACMESMILE", "2026-09", "MBR",
                             targets={"2026-09": 2_000_000, "2026-10": 3_000_000})
    run.today = date(2026, 10, 13)
    model = mbr_engine.build(run)
    assert model["inputs"]["target"] == {"nmv": 2_000_000}       # the request beat settings
    assert model["inputs"]["target_source"] == {"2026-09": "request", "2026-10": "request"}
    assert "of the IDR 2.0 m target" in _section(model, "scorecard")["headline"]
    so_far = _section(model, "month_so_far")
    assert so_far["headline"].startswith("Oct 2026 so far: IDR 400,000 NMV in 12 days")
    ahead = _section(model, "month_ahead")
    assert "needs" in ahead["headline"] and "a day for the remaining 19 days" in ahead["headline"]


def test_api_takes_targets_and_refuses_a_bad_one(world):
    h = _hdr(world["tid"])
    bad = client.post("/v1/reports/mbr", headers=h, json={
        "brand": "acmesmile", "period": "2026-09", "business": "MBR", "targets": {"2027-01": 1e9}})
    assert bad.status_code == 400 and "REPORT_BAD_TARGET" in bad.text
    ok = client.post("/v1/reports/mbr", headers=h, json={
        "brand": "acmesmile", "period": "2026-09", "business": "MBR",
        "targets": {"2026-09": 1_000_000_000}})
    assert ok.status_code == 202, ok.text
    from app import secrets_util
    payload = secrets_util.decrypt_json(jobs.get(ok.json()["report_id"])["payload"])
    assert payload["targets"] == {"2026-09": 1e9}             # encrypted at rest, like all


def test_seller_cancellation_excludes_buyers_who_never_paid():
    from app.reports import labels as L
    assert L.cancel_group("payment overdue", "System") == "Buyer did not pay"
    assert L.cancel_group("fulfillment issues", "User") == "Seller or fulfilment"
    assert L.cancel_group("others", "Seller") == "Seller or fulfilment"
    assert L.cancel_group("change of mind", "User") == "Buyer"
    assert L.cancel_group("others", "System") == "System, other reasons"
    assert L.cancel_group("others", "") == "Not recorded"


def test_labels_read_like_business_language():
    from app.reports import labels as L
    name = "[EXCLUSIVE BY IVENA] - ACMESMILE Double Action Pasta Gigi 225 gr - Value Pack Isi 4 Pcs"
    assert L.short_product(name, "ACMESMILE") == "Double Action Pasta Gigi 225 gr, 4 pack (creator)"
    assert L.pack_of("Value Pack Acmesmile Double Action Pasta Gigi 4 x 225 g") == 4
    assert L.pack_of("ACMESMILE Pasta Gigi All Shiny White Lime Mint 140g x3") == 3
    assert L.pack_of("Twin Pack Acmesmile Double Action Mint 225 g") == 2
    assert L.pack_of("Value Pack Acmesmile Double Action Mint 120 g") is None
    assert L.pack_of("Acmesmile All Shiny White Pasta Gigi 140 Gr") == 1
    assert L.month_label(202509) == "Sep 2025" and L.month_label("2026-09-30") == "Sep 2026"
    assert L.payment_label("Bayar di tempat") == "Cash on delivery (COD)"
    assert L.payment_label("GoPay + TikTok Shop Balance") == "GoPay plus TikTok Shop balance"
    assert L.province_label("Dki Jakarta") == "DKI Jakarta"
    assert L.island("Di Yogyakarta") == "Java" and L.island("Bali") == "Bali and Nusa Tenggara"
    assert L.status_label("Dibatalkan") == "Cancelled"
    assert L.source_label("wallet_monthly, orders_monthly") == \
        "Marketplace settlements; Orders by month"


def test_daily_breakpoint_and_shares_never_overstate():
    from app.reports import mbr_sections as S
    k, m1, m2 = S._breakpoint([10.0] * 8 + [16.0] * 22)
    assert k == 8 and m1 == 10 and m2 == 16
    assert S._breakpoint([10.0, 11.0] * 15) is None              # no 25% shift, no claim
    assert S.share_text(99.96) == "99.9%" and S.share_text(0.01) == "under 0.1%"


def test_bridge_steps_sum_to_the_month(world):
    _, model = _model(world)
    bridge = _section(model, "nmv_bridge")
    steps = bridge["tables"][0]["rows"]
    assert abs(steps[0][1] + sum(r[1] for r in steps[1:-1]) - steps[-1][1]) < 1
    assert bridge["headline"].startswith("NMV rose")
    # package 7: "51%, up 0.6%" read as a share move; the share is compared with a share
    mix = _section(model, "product_mix")
    assert re.fullmatch(r"\w+ products made [\d.]+% of \w+ \d{4} NMV, against [\d.]+% in "
                        r"\w+ \d{4}", mix["headline"]), mix["headline"]
    said = next(f["text"] for f in mix["facts"] if f["text"].startswith(mix["headline"]))
    assert re.search(r"; \w+ NMV (up|down) ", said), said


def test_deck_speaks_business_and_tables_fit_the_page(world):
    import io
    from pptx import Presentation
    from pptx.util import Inches
    from app.reports import pptx_writer as W
    _, model = _model(world)
    prs = Presentation(io.BytesIO(W.build(model)))
    visible = []
    for slide in prs.slides:
        for sh in slide.shapes:
            if sh.has_text_frame:
                visible.append(sh.text_frame.text)
            if getattr(sh, "has_table", False) and sh.has_table:
                visible += [c.text for row in sh.table.rows for c in row.cells]
                bottom = sh.top + sum(r.height for r in sh.table.rows)
                assert bottom <= Inches(6.95), "table runs past the page body"
    text = "\n".join(visible)
    for raw in ("orders_monthly", "wallet_gmv", "mp_fee", "sku_performance", "202509",
                "Dibatalkan", "L2 ", "L3 ", "SUM(", "COALESCE", "(blank)", "TABLE_NOT_IN_SCOPE"):
        assert raw not in text, raw
    # a long product list keeps above the takeaway band and drops the SKU id column
    deck = W._Deck(model)
    s = deck.slide("t", "t")
    long = {"title": "x", "columns": ["SKU", "Product", "NMV"], "deck_columns": [1, 2],
            "rows": [[f"{i:019d}", "Double Action Pasta Gigi 225 gr Value Pack Isi 4 Pcs "
                      "Exclusive Creator Edition Mint", 1e8 - i] for i in range(25)]}
    clipped = W._table(s, long, 0.45, W.BODY_Y, 7.85)
    tbl = next(sh for sh in s.shapes if getattr(sh, "has_table", False) and sh.has_table)
    assert clipped and len(tbl.table.columns) == 2
    assert tbl.top + sum(r.height for r in tbl.table.rows) <= Inches(W.TAKEAWAY_Y)


def test_a_report_can_be_stopped_even_when_its_worker_died(world):
    h = _hdr(world["tid"])
    body = {"brand": "acmesmile", "period": "2026-09", "business": "MBR"}
    queued = client.post("/v1/reports/mbr", headers=h, json=body).json()["report_id"]
    assert client.delete(f"/v1/reports/{queued}", headers=_hdr(world["tid"])).status_code == 404
    assert client.delete(f"/v1/reports/{queued}", headers=h).status_code == 200
    assert jobs.get(queued)["status"] == "cancelled"
    assert client.delete(f"/v1/reports/{queued}", headers=h).status_code == 409
    # a running report whose pod died: the cancel has no worker to honour it, so the
    # next claim ends it instead of requeueing it into a "queued" that never moves
    orphan = client.post("/v1/reports/mbr", headers=h, json=body).json()["report_id"]
    c = store._c()
    try:
        c.execute("UPDATE operation_jobs SET status='running',lease_owner='dead-pod',"
                  "lease_until=? WHERE id=?", (1.0, orphan))
        c.commit()
    finally:
        c.close()
    assert client.delete(f"/v1/reports/{orphan}", headers=h).status_code == 200
    assert jobs.get(orphan)["status"] == "running"            # nobody alive to stop it
    shown = client.get(f"/v1/reports/{orphan}", headers=h).json()
    assert shown["phase"] == "cancelling" and shown["cancel_requested"] is True
    jobs.run_one("pytest-mbr-orphan")
    row = jobs.get(orphan)
    assert row["status"] == "cancelled" and row["payload"] is None
    listed = client.get("/v1/reports", headers=h).json()["reports"]
    assert [r["status"] for r in listed] == ["cancelled", "cancelled"]
    # the payload is gone, the name is not: the list still says which report it was
    assert {(r["brand"], r["period"]) for r in listed} == {("ACMESMILE", "2026-09")}


# ---- staging review, 13 Oct 2026: findings on the first real ACMESMILE package ----
class _CannedRun:
    """Just enough of mbr_engine.Run to feed one section canned rows (no warehouse)."""

    def __init__(self, rows: dict, today=date(2026, 10, 13), target=None):
        self.tenant_id, self.role, self.brand = "t", "analyst", "ACMESMILE"
        self.month = Month(2026, 9)
        cfg = {"brands": {"acmesmile": {"targets": {"2026-10": {"nmv": target}}}}} if target else {}
        self.config = mbr_config.parse(cfg, "ACMESMILE")
        self.statuses = {"processed": ["Selesai"], "cancelled": ["Dibatalkan"]}
        self.queries, self.rows, self.today, self.data_as_of = {}, rows, today, None

    def missing_columns(self, table, columns):
        return []

    def query(self, key, table, columns, build):
        if key not in self.queries:
            self.queries[key] = {"key": key, "table": table, "ok": True, "sql": build("duckdb") if build else None,
                                 "rows": [dict(r) for r in self.rows.get(key, [])]}
        return self.queries[key]


def test_curated_check_compares_like_for_like():
    from app.reports import mbr_sections as S
    l2 = [{"month_id": 202609, "listed_gmv": 593.0, "nmv_all": 500.0, "nmv": 430.0}]
    same = _CannedRun({"orders_monthly": l2, "l3_orders_monthly": [
        {"month_id": 202609, "gmv": 593.0, "nmv": 500.0}]})
    sec = S.sec_reconciliation(same)
    # the old check compared curated all-status NMV with sold-only NMV: "16% higher"
    assert sec["headline"].startswith("Acme curated orders match the order history exactly")
    off = _CannedRun({"orders_monthly": l2, "l3_orders_monthly": [
        {"month_id": 202609, "gmv": 593.0, "nmv": 450.0}]})
    assert "differ from the order history by more than 1% in 1 of 1" in \
        S.sec_reconciliation(off)["headline"]


def test_a_campaign_spike_is_not_a_new_daily_level():
    from app.reports import mbr_sections as S
    days = [10.0] * 8 + [41.0] + [10.5] * 5 + [16.0] * 16
    rows = [{"order_date": f"2026-09-{i + 1:02d}", "gmv": v, "nmv": v * 1e6, "orders": 1}
            for i, v in enumerate(days)]
    sec = S.sec_daily(_CannedRun({"orders_daily": rows}))
    assert "from 15 Sep" in sec["headline"]                     # not "from 9 Sep"
    assert "9 Sep (9.9) peak of IDR 41.0 m" in sec["headline"]


def test_month_so_far_counts_only_loaded_days():
    from app.reports import mbr_sections as S
    monthly = [{"month_id": 202609, "nmv": 300e6}]
    loaded = [{"order_date": f"2026-10-{d:02d}", "gmv": 1, "nmv": 20e6, "orders": 5}
              for d in range(1, 12)]                            # 12 Oct not loaded yet
    run = _CannedRun({"orders_next_month": loaded, "orders_monthly": monthly}, target=1e9)
    so = S.sec_month_so_far(run)
    assert so["headline"].startswith("Oct 2026 so far: IDR 220.0 m NMV in 11 days")
    assert any("12 Oct is not loaded yet" in f["text"] for f in so["facts"])
    two = S.sec_month_so_far(_CannedRun({"orders_next_month": loaded[:10],
                                         "orders_monthly": monthly}))
    assert any("11 Oct to 12 Oct are not loaded yet" in f["text"] for f in two["facts"])
    assert "for the remaining 20 days" in S.sec_month_ahead(run)["headline"]
    # a quiet brand: a week without orders is real, not a load lag
    quiet = _CannedRun({"orders_next_month": loaded[:4], "orders_monthly": monthly})
    assert "in 12 days" in S.sec_month_so_far(quiet)["headline"]


def test_payment_other_methods_keep_last_years_share():
    from app.reports import mbr_sections as S
    rows = [{"month_id": 202609, "payment_method": f"M{i}", "all_orders": 1, "orders": 1,
             "cancelled": 0, "nmv": 100 - i} for i in range(10)]
    rows += [{"month_id": 202509, "payment_method": "DANA", "all_orders": 1, "orders": 1,
              "cancelled": 0, "nmv": 50},
             {"month_id": 202509, "payment_method": "M0", "all_orders": 1, "orders": 1,
              "cancelled": 0, "nmv": 50}]
    t = S.sec_payment(_CannedRun({"payment_mix": rows}))["tables"][0]
    other = t["rows"][-1]
    assert other[0] == "Other methods (2)" and other[5] == pytest.approx(50.0)   # DANA last year


def test_same_named_listings_are_told_apart():
    from app.reports import mbr_sections as S
    rows = [{"sku_id": "111", "product_name": "ACMESMILE Pasta Gigi 225 gr Isi 4", "marketplace": "tiktok"},
            {"sku_id": "222", "product_name": "ACMESMILE Pasta Gigi 225 gr Isi 4", "marketplace": "tokopedia"},
            {"sku_id": "333", "product_name": "ACMESMILE Pasta Gigi 225 gr Isi 4", "marketplace": "tiktok"},
            {"sku_id": "444", "product_name": "ACMESMILE Mint 120 g", "marketplace": "tiktok"}]
    run = _CannedRun({"sku_performance": rows})
    run.query("sku_performance", "t", (), None)
    names = [S._name(run, r) for r in rows]
    assert len(set(names[:3])) == 3
    assert names[1].endswith("(Tokopedia)") and "SKU ..0111" not in names[0]
    assert names[0].endswith("(TikTok, SKU ..111)") and names[3] == "Mint 120 g"


def test_deck_shows_the_target_and_reads_small_rates():
    from app.reports import pptx_writer as W
    from app.reports.xlsx_writer import _clean_sheet
    model = {"inputs": {"target": {"nmv": 1e9}}, "sections": [{
        "id": "scorecard", "tables": [{"columns": ["Measure", "Sep", "Aug", "MoM %", "LY", "YoY %"],
                                        "rows": [["NMV", 430.7e6, 552.9e6, -22.1, 270.7e6, 59.1]]}]}]}
    tile = next(x for x in W.signals(model) if x[0] == "NMV vs target")
    assert tile[1] == "43.1%" and tile[3] == "RED" and "IDR 1.00bn" in tile[2]
    assert W._cell(0.0443, "Conversion %", False)[0] == "0.04%"
    assert W._cell(25.0, "NMV difference %", False)[1] == W.C["muted"]   # a check, not news
    assert _clean_sheet("C5 Units, price and listings behind NMV") == "C5 Units, price and listings"
    assert _clean_sheet("F2 Plan and building blocks for Oct 2026") == "F2 Plan and building blocks"


# ---- the brand picker (request form combo box) ----
def test_brand_picker_lists_the_slices_brands_busiest_first(world, monkeypatch):
    """A picked brand is spelled the way the report filters it, so a typo can no longer
    queue a report that finds no orders."""
    mbr_engine._brand_cache.clear()
    cfg = store.get_tenant_config(world["tid"], "mbr")
    cfg["brands"]["New Brand"] = {}                       # configured, no orders yet
    store.set_tenant_config(world["tid"], "mbr", cfg)
    got = mbr_engine.brand_choices(world["tid"], "analyst", "MBR", today=date(2026, 10, 14))
    names = [b["name"] for b in got["brands"]]
    assert names == ["ACMESMILE", "OTHER", "NEW BRAND"], got
    assert got["source"] == "orders" and got["since"] == "2025-10"
    acmesmile = got["brands"][0]
    assert acmesmile["configured"] and acmesmile["last_month"] == "2026-10" and acmesmile["orders"] > 1
    assert got["brands"][1]["configured"] is False
    assert got["brands"][2] == {"name": "NEW BRAND", "orders": 0, "last_month": None,
                                "configured": True}

    calls = []                                             # cached: no second warehouse read
    monkeypatch.setattr(mbr_engine.registry, "execute_governed",
                        lambda *a, **k: calls.append(a) or {"ok": False})
    assert mbr_engine.brand_choices(world["tid"], "analyst", "MBR") == got and calls == []

    h = _hdr(world["tid"])
    r = client.get("/v1/reports/mbr/brands?business=MBR", headers=h)
    assert r.status_code == 200 and [b["name"] for b in r.json()["brands"]] == names
    assert client.get("/v1/reports/mbr/brands?business=3P", headers=h).status_code \
        in (403, 404, 409)                                 # an ungranted slice lists nothing

    cfg["brands"]["Later"] = {}                            # settings are read every time
    store.set_tenant_config(world["tid"], "mbr", cfg)
    assert "LATER" in [b["name"] for b in
                       mbr_engine.brand_choices(world["tid"], "analyst", "MBR")["brands"]]


def test_a_stale_brand_list_is_served_at_once_and_refreshed_behind(world, monkeypatch):
    """The read takes ~10 s on BigQuery: only the first request after a restart waits."""
    mbr_engine._brand_cache.clear()
    first = mbr_engine.brand_choices(world["tid"], "analyst", "MBR", today=date(2026, 10, 14))
    key = next(iter(mbr_engine._brand_cache))
    stamp, cached = mbr_engine._brand_cache[key]
    mbr_engine._brand_cache[key] = (stamp - mbr_engine.BRAND_CACHE_SEC - 1,
                                    {**cached, "brands": cached["brands"][:1]})
    started = []
    monkeypatch.setattr(mbr_engine.threading, "Thread",
                        lambda target, **k: type("T", (), {"start": lambda s: started.append(
                            target)})())
    stale = mbr_engine.brand_choices(world["tid"], "analyst", "MBR", today=date(2026, 10, 14))
    assert [b["name"] for b in stale["brands"]] == ["ACMESMILE"]       # served as it was
    assert len(started) == 1
    mbr_engine.brand_choices(world["tid"], "analyst", "MBR", today=date(2026, 10, 14))
    assert len(started) == 1                                       # one refresh at a time
    started[0]()                                                   # the background read
    fresh = mbr_engine.brand_choices(world["tid"], "analyst", "MBR", today=date(2026, 10, 14))
    assert fresh["brands"] == first["brands"]


def test_brand_picker_still_offers_configured_brands_when_orders_are_out_of_reach(world):
    mbr_engine._brand_cache.clear()
    store.set_business_knowledge(world["biz"], world["did"], {"allowed_tables": [
        "vw_inventory_order_wh_daily_snapshot"]})
    got = mbr_engine.brand_choices(world["tid"], "analyst", "MBR", today=date(2026, 10, 14))
    assert got["source"] == "none" and "no order table" in got["note"]
    assert [b["name"] for b in got["brands"]] == ["ACMESMILE"]
    assert mbr_engine._brand_cache == {}                   # a miss is not remembered


# ---- standards pass, 14 Oct 2026: the analyst's review standards (ACS_Review_Build_v4) ----
def _canned(rows, brand_cfg=None, absent=()):
    run = _CannedRun(rows)
    if brand_cfg is not None:
        run.config = mbr_config.parse({"brands": {"acmesmile": brand_cfg}}, "ACMESMILE")
    run.missing_columns = lambda table, cols: [c for c in cols if c in absent]
    return run


def test_open_items_carry_drafted_readings_and_a_designed_placeholder(world):
    from app.reports import drafts
    secs = [{"id": "traffic", "missing": [{"what": "store traffic", "key": "funnel"}]},
            {"id": "synthesis", "missing": [{"what": "x"}]}]
    drafts.attach(secs, "ACMESMILE", Month(2026, 9), Month(2026, 10))
    item = secs[0]["missing"][0]
    assert item["ghost"] == "funnel" and item["visual"]
    assert all(item[k] for k in ("if_good", "if_bad", "if_neutral"))
    assert "if_good" not in secs[1]["missing"][0]          # the synthesis is never drafted
    for text in (item["if_good"], item["if_bad"], item["if_neutral"]):
        bare = text.replace("XX%", "").replace("Sep 2026", "").replace("Oct 2026", "")
        assert "{" not in text and not re.search(r"\d", bare)
    _, model = _model(world)
    drafted = [m for s in model["sections"] for m in s["missing"] if m.get("if_good")]
    assert drafted                                         # a real run drafts its gaps
    assert "If good:" in markdown.open_items(model)


def test_quadrants_use_the_standard_names_lines_and_top_five():
    from app.reports import mbr_sections as S
    rows = ([{"product_sku": f"S{i}", "product_name": f"Star {i}", "views": 25000 + i,
              "cvr": 0.5, "revenue": 1e7 - i} for i in range(6)]
            + [{"product_sku": "G1", "product_name": "Gem", "views": 100, "cvr": 2.0,
                "revenue": 5e5},
               {"product_sku": "R1", "product_name": "Rough", "views": 30000, "cvr": 0.05,
                "revenue": 4e5},
               {"product_sku": "U1", "product_name": "Under", "views": 50, "cvr": 0.01,
                "revenue": 1e4}])
    sec = S.sec_product_funnel(_canned({"product_funnel": rows}))
    th, groups = sec["quadrants"]["thresholds"], sec["quadrants"]["groups"]
    assert tuple(groups) == ("Star", "Hidden gem", "Rough diamond", "Underperformer")
    assert th["cvr"] == pytest.approx(0.2) and "standard line" in th["cvr_basis"]
    assert th["views"] in (10000, 15000, 20000, 25000)      # snapped to the standard's range
    assert groups["Star"]["products"] == 6 and len(groups["Star"]["top"]) == 5
    assert [g["products"] for g in groups.values()][1:] == [1, 1, 1]
    assert sec["headline"].startswith("6 Star products made")
    assert "1 Rough diamond product takes" in " ".join(f["text"] for f in sec["facts"])
    assert "1 Hidden gem converts well" in sec["headline"]
    own = S.sec_product_funnel(_canned({"product_funnel": rows},
                                       {"quadrant": {"views": 500, "cvr": 1.0}}))
    assert own["quadrants"]["thresholds"]["views"] == 500
    assert own["quadrants"]["thresholds"]["cvr_basis"] == "set in the MBR settings"


def test_fulfilment_leads_with_the_average_and_compares_before_the_takeover():
    from app.reports import mbr_sections as S
    hist = [{"month_id": 202609, "days_to_rts": 0, "orders": 90},
            {"month_id": 202609, "days_to_rts": 1, "orders": 10},
            {"month_id": 202608, "days_to_rts": 0, "orders": 50},
            {"month_id": 202608, "days_to_rts": 2, "orders": 50}]
    before = [{"month_id": 202607, "days_to_rts": 1, "orders": 60},
              {"month_id": 202608, "days_to_rts": 4, "orders": 40}]
    sec = S.sec_fulfilment(_canned({"fulfilment_days": hist, "fulfilment_days_before": before},
                                   {"takeover_date": "2026-09-01"}))
    spread = next(t for t in sec["tables"] if t["id"] == "rts_spread")
    assert spread["columns"] == ["Measure", "Sep 2026", "Aug 2026",
                                 "Before Acme, Jul 2026 to Aug 2026"]
    by = {r[0]: r[1:] for r in spread["rows"]}
    assert by["Average days"] == [0.1, 1.0, 2.2]
    assert by["Median days"] == [0, 0, 1] and by["Slowest (days)"] == [1, 2, 4]
    assert sec["headline"].startswith("Sep 2026 orders were ready to ship in 0.10 days on "
                                      "average, against 1.00 in Aug 2026")
    assert any("Before the takeover (Jul 2026 to Aug 2026)" in f["text"] for f in sec["facts"])
    plain = S.sec_fulfilment(_canned({"fulfilment_days": hist}, {}))
    assert len(next(t for t in plain["tables"] if t["id"] == "rts_spread")["columns"]) == 3
    assert any("needs the Acme takeover date" in f["text"] for f in plain["facts"])


def test_pareto_shows_price_per_unit_range_and_states_the_traffic_gap():
    from app.reports import mbr_sections as S
    skus = [{"sku_id": "A", "product_name": "Pack A", "nmv_cur": 900.0, "nmv_prev": 800.0,
             "units_cur": 9.0, "units_prev": 10.0},
            {"sku_id": "B", "product_name": "Pack B", "nmv_cur": 100.0, "nmv_prev": 100.0,
             "units_cur": 2.0, "units_prev": 2.0}]
    prices = [{"sku_id": "A", "month_id": 202609, "price_low": 90.0, "price_high": 120.0},
              {"sku_id": "A", "month_id": 202608, "price_low": 70.0, "price_high": 90.0}]
    other_ids = [{"product_sku": "L1", "product_name": "Pack A", "views": 10, "cvr": 1.0,
                  "revenue": 5.0}]
    sec = S.sec_products(_canned({"sku_performance": skus, "sku_price_range": prices,
                                  "product_funnel": other_ids}, {}))
    t = next(t for t in sec["tables"] if t["id"] == "pareto_prices")
    assert t["columns"][3:10] == ["Price per unit Sep 2026", "Price per unit Aug 2026", "MoM %",
                                  "Lowest Sep 2026", "Highest Sep 2026", "Lowest Aug 2026",
                                  "Highest Aug 2026"]
    assert t["rows"][0][3:10] == [100.0, 80.0, 25.0, 90.0, 120.0, 70.0, 90.0]
    assert "SKU" not in [t["columns"][j] for j in t["deck_columns"]]
    gap = next(m for m in sec["missing"] if m.get("key") == "traffic")
    assert "do not match the order SKUs" in gap["needs"]   # never joined through names
    same_ids = [{"product_sku": "A", "views": 500, "cvr": 1.5, "revenue": 5.0}]
    joined = S.sec_products(_canned({"sku_performance": skus, "sku_price_range": prices,
                                     "product_funnel": same_ids}, {}))
    t2 = next(t for t in joined["tables"] if t["id"] == "pareto_prices")
    assert "Page views Sep 2026" in t2["columns"]
    assert not any(m.get("key") == "traffic" for m in joined["missing"])


def test_geography_pairs_delivery_time_or_says_what_is_missing():
    from app.reports import mbr_sections as S
    geo = [{"month_id": 202609, "province": "DKI Jakarta", "nmv": 800.0, "orders": 8},
           {"month_id": 202609, "province": "Sumatera Utara", "nmv": 200.0, "orders": 2},
           {"month_id": 202608, "province": "DKI Jakarta", "nmv": 700.0, "orders": 7}]
    without = S.sec_geography(_canned({"geography": geo}, {}, absent=S._DELIVERED))
    assert any(m.get("key") == "delivery" for m in without["missing"])
    assert next(t for t in without["tables"] if t["id"] == "province")["columns"][2] == \
        "Share %"
    days = [{"province": "DKI Jakarta", "days": 2.0, "orders": 8},
            {"province": "Sumatera Utara", "days": 5.0, "orders": 2}]
    run = _canned({"geography": geo, "geography_delivery": days}, {},
                  absent=S._DELIVERED[1:])
    sec = S.sec_geography(run)
    prov = next(t for t in sec["tables"] if t["id"] == "province")
    assert prov["columns"][-1] == "Paid to delivered, days"
    assert [r[-1] for r in prov["rows"]] == [2.0, 5.0]
    assert "delivered_date" in run.queries["geography_delivery"]["sql"]
    assert any("2.0 days from payment to delivery, against 5.0 days outside Java" in f["text"]
               for f in sec["facts"])


def test_buyer_history_start_is_stated_and_can_be_set():
    from app.reports import mbr_sections as S
    rows = {"new_buyers": [{"month_id": 202609, "new_buyers": 10}],
            "orders_monthly": [{"month_id": 202609, "buyers": 40}]}
    run = _canned(rows, {"customer_since": "2026-01"})
    sec = S.sec_customers(run)
    t = next(t for t in sec["tables"] if t["id"] == "new_buyers")
    assert t["title"].endswith("history since Jan 2026")
    assert [r[0] for r in t["rows"]][0] == "Mar 2026" and len(t["rows"]) == 7
    assert "BETWEEN 202601 AND 202609" in run.queries["new_buyers"]["sql"]
    assert any("the start set for this brand" in f["text"] for f in sec["facts"])
    default = S.sec_customers(_canned(rows, {}))
    assert any("Buyer history starts in Sep 2025" in f["text"] for f in default["facts"])
    with pytest.raises(ReportInputError):
        mbr_config.parse({"brands": {"acmesmile": {"customer_since": "Jan 2026"}}}, "ACMESMILE")
    with pytest.raises(ReportInputError):
        mbr_config.parse({"brands": {"acmesmile": {"quadrant": {"views": -1}}}}, "ACMESMILE")


def test_platforms_wear_their_colour_and_the_e_kicker_says_its_name_once():
    from app.reports import pptx_writer as W
    assert W._platform_colour("TikTok Shop") == "000000"
    assert W._platform_colour("Tokopedia") == "03AC0E"
    assert W._platform_colour("Java") is None
    sec = {"group": "E", "title": "Synthesis, asks and strategy"}
    assert W._kicker(sec, {"E": "Synthesis"}) == "E  |  Synthesis, asks and strategy"
    assert W._kicker({"group": "C", "title": "Ads"}, {"C": "Why did it change"}) == \
        "C  |  Why did it change: Ads"
