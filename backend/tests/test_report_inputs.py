"""Report inputs: exports attached for MBR reports, read through the governed path, kept
for their TTL so the person can generate again, then gone.

Pinned here:
  * the chunked upload API only lets the creating app key + end user add, close, delete
    or use an input, takes chunks in any order but refuses a gap at close, refuses
    oversized / renamed chunks, and keeps the bytes encrypted at rest;
  * a closed input may feed several reports until it expires (a report keeps it at least
    RUN_GRACE_SEC); the job leaves it in place and purges expired ones; the live-input
    cap drops the oldest used set no report is reading, never one still waiting;
  * the files' rows replace the warehouse table of the same name for that report, feed
    the file-only pages, and every query over them is governed.
"""
import io
import sys
import time
import zipfile
from datetime import date, datetime
from pathlib import Path

import duckdb
import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import job_handlers, jobs, store                        # noqa: E402
from app.connectors.factory import make_connector                # noqa: E402
from app.main import app                                         # noqa: E402
from app.reports import file_source, inputs, mbr_engine          # noqa: E402
from app.reports.uploads.parse import NO_ROWS                    # noqa: E402
from app.reports.uploads.schema import Bundle, FileResult        # noqa: E402
from app.schema_scan import scan_connector                       # noqa: E402

client = TestClient(app)
AK = {"X-Admin-Key": "admin-dev-key"}
SECRET_MARK = b"BUYER-ROW-MARKER"


@pytest.fixture
def world(tmp_path):
    path = str(tmp_path / "wh.duckdb")
    con = duckdb.connect(path)
    con.execute("""CREATE TABLE acme_store_order_mp(marketplace VARCHAR, brand VARCHAR,
        order_id VARCHAR, order_date DATE, month_id INTEGER, order_status VARCHAR, nmv DOUBLE,
        discount_platform DOUBLE, quantity DOUBLE, sku_id VARCHAR, product_name VARCHAR)""")
    con.execute("""INSERT INTO acme_store_order_mp VALUES
        ('Shopee', 'Acmesweet', 'w1', DATE '2026-09-03', 202609, 'Selesai', 900000, 0, 3, 'S1', 'Milk'),
        ('TikTok', 'Acmesweet', 'w2', DATE '2026-09-04', 202609, 'Selesai', 500000, 0, 2, 'S1', 'Milk')""")
    con.execute("""CREATE TABLE acme_store_live_mp(brand VARCHAR, live_date DATE, gmv DOUBLE,
        orders DOUBLE)""")
    con.execute("INSERT INTO acme_store_live_mp VALUES ('Acmesweet', DATE '2026-09-02', 1, 1)")
    con.close()
    tid = store.create_tenant("inputs-test")
    store.create_role(tid, "analyst", {"full_access": True, "can_see_pii": False})
    mdl = scan_connector(make_connector("duckdb", {"path": path}))
    did = store.create_datasource(tid, "duckdb", "Acme L2", {"path": path}, mdl)
    biz = store.create_business(tid, "MBR", "Monthly business review")
    store.set_business_knowledge(biz, did, {"allowed_tables": ["acme_store_order_mp",
                                                               "acme_store_live_mp"]})
    store.set_role_businesses(tid, "analyst", [biz])
    store.set_tenant_config(tid, "mbr", {"status_map": {"selesai": "processed"}})
    yield {"tid": tid}
    store.delete_tenant(tid)


def _hdr(tid, sub="u1"):
    key = client.post("/admin/apikeys", headers=AK,
                      json={"tenant_id": tid, "label": "pytest"}).json()["key"]
    token = pyjwt.encode({"sub": sub, "role": "analyst"}, "test-secret", algorithm="HS256")
    return {"X-API-Key": key, "Authorization": f"Bearer {token}"}


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("QBR/Monthly Transaction/Shopee/Order_X_Shopee_Sep_2026.csv",
                    b"No. Pesanan,Status Pesanan\n1," + SECRET_MARK + b"\n")
        zf.writestr("QBR/notes.txt", b"hello")
    return buf.getvalue()


def _fake_look(path):
    if path.endswith(".csv"):
        return {"path": path, "kind": "orders", "marketplace": "Shopee", "month": "2026-09"}, ""
    return None, "not an export the review reads"


def _put(h, iid, f, c, data, name="QBR.zip"):
    return client.put(f"/v1/reports/inputs/{iid}/files/{f}/chunks/{c}", content=data,
                      headers={**h, "X-File-Name": name,
                               "Content-Type": "application/octet-stream"})


def test_upload_close_and_use_again(world, monkeypatch):
    monkeypatch.setattr(inputs, "_look", _fake_look)
    h, other = _hdr(world["tid"]), _hdr(world["tid"], "u2")
    made = client.post("/v1/reports/inputs", headers=h)
    assert made.status_code == 201, made.text
    iid = made.json()["input_id"]
    assert made.json()["chunk_bytes"] == inputs.CHUNK_BYTES
    blob = _zip()
    half = len(blob) // 2
    assert _put(h, iid, 0, 1, blob[half:]).status_code == 200          # any order (parallel)
    assert _put(h, iid, 0, 0, blob[:half], name="Other.zip").status_code == 400   # renamed
    gap = client.post(f"/v1/reports/inputs/{iid}/close", headers=h)
    assert gap.status_code == 409 and gap.json()["detail"]["code"] == "INPUT_INCOMPLETE"
    assert _put(h, iid, 0, 0, blob[:half]).status_code == 200
    assert _put(h, iid, 0, 0, blob[:half]).status_code == 200          # a retry replaces it
    assert _put(other, iid, 1, 0, b"x", name="a.csv").status_code == 404    # not theirs
    assert client.post(f"/v1/reports/inputs/{iid}/close", headers=other).status_code == 404
    early = client.post("/v1/reports/mbr", headers=h,
                        json={"brand": "acmesweet", "period": "2026-09", "business": "MBR",
                              "inputs": iid})
    assert early.status_code == 409 and early.json()["detail"]["code"] == "INPUT_NOT_CLOSED"

    closed = client.post(f"/v1/reports/inputs/{iid}/close", headers=h).json()
    assert closed["status"] == "closed"
    assert closed["summary"]["recognized"] == 1 and closed["summary"]["ignored"] == 1
    assert closed["summary"]["kinds"] == {"orders": ["Shopee"]}
    assert closed["summary"]["latest_month"] == "2026-09"
    assert _put(h, iid, 1, 0, b"x", name="a.csv").status_code == 409         # closed

    c = store._c()
    try:
        raw = [bytes(r["data"]) for r in c.execute(
            "SELECT data FROM report_input_parts WHERE input_id=?", (iid,)).fetchall()]
    finally:
        c.close()
    assert raw and all(SECRET_MARK not in r for r in raw)                 # encrypted at rest

    body = {"brand": "acmesweet", "period": "2026-09", "business": "MBR", "inputs": iid}
    assert client.post("/v1/reports/mbr", headers=other, json=body).status_code == 404
    assert client.post("/v1/reports/mbr", headers=h, json=body).status_code == 202
    row = store.get_report_input(world["tid"], iid)
    assert row["status"] == "used" and row["parts"]
    # generate again (a fix, another month) with the same files, no new upload
    again = client.post("/v1/reports/mbr", headers=h, json={**body, "period": "2026-08"})
    assert again.status_code == 202, again.text
    assert client.post("/v1/reports/mbr", headers=other, json=body).status_code == 404


def test_a_report_near_the_end_keeps_its_files(world):
    tid = world["tid"]
    iid = _closed_input(tid)
    c = store._c()
    try:
        c.execute("UPDATE report_inputs SET expires=? WHERE id=?", (time.time() + 60, iid))
        c.commit()
    finally:
        c.close()
    store.use_report_input(tid, iid, time.time() + inputs.RUN_GRACE_SEC)
    assert store.get_report_input(tid, iid)["expires"] > time.time() + inputs.RUN_GRACE_SEC - 5
    later = store.get_report_input(tid, iid)["expires"]
    store.use_report_input(tid, iid, time.time() + 10)           # never shortens it
    assert store.get_report_input(tid, iid)["expires"] == pytest.approx(later)


def test_used_sets_make_room_unless_a_report_reads_them(world):
    tid = world["tid"]
    key = client.post("/admin/apikeys", headers=AK,
                      json={"tenant_id": tid, "label": "pytest"}).json()
    h = {"X-API-Key": key["key"], "Authorization": "Bearer " + pyjwt.encode(
        {"sub": "u1", "role": "analyst"}, "test-secret", algorithm="HS256")}
    key_id = store.get_api_key(key["key"])["key"]
    sets = [_closed_input(tid, key=key_id) for _ in range(inputs.MAX_LIVE_INPUTS)]
    capped = client.post("/v1/reports/inputs", headers=h)        # none used yet: waiting
    assert capped.status_code == 429 and capped.json()["detail"]["code"] == "TOO_MANY_INPUTS"
    for iid in sets:
        store.use_report_input(tid, iid, 0)
    jobs.enqueue("mbr_report", tid, {"inputs": sets[0]}, actor_id="u1", app_key_id=key_id)
    made = client.post("/v1/reports/inputs", headers=h)
    assert made.status_code == 201, made.text
    assert store.get_report_input(tid, sets[0]) is not None      # a queued report reads it
    assert store.get_report_input(tid, sets[1]) is None          # oldest idle used set went
    assert store.get_report_input(tid, sets[2]) is not None


def test_complete_needs_every_file_and_chunk():
    def row(*parts):
        return {"parts": [{"file_no": f, "chunk_no": c, "name": f"f{f}.zip", "size": 1}
                          for f, c in parts]}
    inputs.check_complete(row((0, 0), (0, 1), (1, 0)))
    for broken in (row((0, 0), (2, 0)), row((0, 0), (0, 2)), row((1, 0))):
        with pytest.raises(inputs.InputError) as exc:
            inputs.check_complete(broken)
        assert exc.value.code == "INPUT_INCOMPLETE"


def test_chunk_limits_names_and_live_input_cap(world, monkeypatch):
    h = _hdr(world["tid"])
    iid = client.post("/v1/reports/inputs", headers=h).json()["input_id"]
    assert _put(h, iid, 0, 0, b"x", name="evil/../a.csv").status_code == 400
    assert _put(h, iid, 0, 0, b"x", name="report.exe").status_code == 400
    assert _put(h, iid, 0, 0, b"").status_code == 400
    monkeypatch.setattr(inputs, "CHUNK_BYTES", 4)
    assert _put(h, iid, 0, 0, b"12345").status_code == 413
    assert client.post(f"/v1/reports/inputs/{iid}/close", headers=h).status_code == 400  # no files
    for _ in range(inputs.MAX_LIVE_INPUTS - 1):
        assert client.post("/v1/reports/inputs", headers=h).status_code == 201
    capped = client.post("/v1/reports/inputs", headers=h)
    assert capped.status_code == 429 and capped.json()["detail"]["code"] == "TOO_MANY_INPUTS"
    assert client.delete(f"/v1/reports/inputs/{iid}", headers=h).json() == {"ok": True}
    assert client.post("/v1/reports/inputs", headers=h).status_code == 201


def _row(**kw):
    base = {"marketplace": "Shopee", "month_id": 202609, "order_id": "o1", "line_id": "l1",
            "order_status": "Selesai", "status_group": "processed",
            "created_at": datetime(2026, 9, 3, 8), "paid_at": datetime(2026, 9, 3, 9),
            "shipped_at": datetime(2026, 9, 4, 9), "delivered_at": datetime(2026, 9, 6, 9),
            "seller_sku": "dvdva", "quantity": 2.0, "item_value_after_seller_discount": 100_000.0,
            "shipping_fee_buyer": 10_000.0, "shipping_discount_seller": 2_000.0,
            "warehouse": "Marunda", "province": "DKI Jakarta"}
    base.update(kw)
    return base


def _bundle() -> Bundle:
    b = Bundle()
    b.add("upload_orders", [
        _row(),
        _row(order_id="o2", line_id="l2", marketplace="TikTok",
             item_value_after_seller_discount=50_000.0, shipping_fee_buyer=0.0,
             warehouse="Cikarang"),
        _row(order_id="o3", line_id="l3", status_group="cancelled", order_status="Batal",
             cancel_by="buyer", delivered_at=None, item_value_after_seller_discount=30_000.0,
             shipping_fee_buyer=0.0),
        _row(order_id="p1", line_id="p1", month_id=202608,
             item_value_after_seller_discount=80_000.0, shipping_fee_buyer=5_000.0),
    ])
    b.add("acme_store_live_mp", [
        {"marketplace": "Shopee", "live_date": date(2026, 9, 5), "gmv": 700_000.0,
         "orders": 7.0, "owner": "official"},
        {"marketplace": "TikTok", "live_date": date(2026, 8, 5), "gmv": 300_000.0,
         "orders": 3.0, "owner": "affiliate"}])
    b.add("upload_po", [
        {"created_date": date(2026, 9, 1), "gr_date": date(2026, 9, 4), "reference_number": "PO1",
         "sku": "DVDVA", "ordered_quantity": 100.0, "delivered_quantity": 80.0,
         "open_quantity": 20.0}])
    b.add("upload_srp", [{"sku": "DVDVA", "product_name": "Acmesweet Vanilla 170g",
                          "price_date": date(2026, 9, d), "srp": 54_900.0} for d in (1, 2)])
    b.add("upload_inventory", [{"report_date": date(2026, 10, 21), "sku": "DVDVA",
                                "item_code": "I-1", "item_name": "Acmesweet Vanilla",
                                "available_qty": 40.0, "hpp": 30_000.0, "order_qty_30": 60.0}])
    b.files = [FileResult(path="QBR/x.xlsx", kind="orders", marketplace="Shopee",
                          month="2026-09", table="upload_orders", rows=4, used=True),
               FileResult(path="QBR/old.xls", reason="old Excel format (.xls); save it as .xlsx")]
    return b


def test_an_account_whose_export_is_empty_stopped_it_is_not_missing(world, monkeypatch):
    # The TikTok affiliate LIVE export for Sep was attached and held no rows: LIVE on that
    # account stopped, which is a reason to ask for, not data to chase.
    bundle = _bundle()
    bundle.files.append(FileResult(
        path="QBR/Live/Tiktok/Live_Affiliate_Acmesweet_Tiktok_Sep_2026.xlsx", kind="live",
        marketplace="TikTok", month="2026-09", reason=NO_ROWS))
    monkeypatch.setattr(file_source, "_load", lambda tid, iid: bundle)
    run = mbr_engine.prepare(world["tid"], "analyst", "acmesweet", "2026-09", "MBR",
                             inputs=_closed_input(world["tid"]))
    try:
        live = _section(mbr_engine.build(run), "live_video")
    finally:
        run.close()
    assert "TikTok, affiliates LIVE had no sessions in Sep 2026" in live["headline"]
    assert "has no data" not in live["headline"]
    assert not any("TikTok" in m["what"] for m in live["missing"])
    assert any("stopped after Aug 2026" in f["text"] for f in live["facts"])


def _closed_input(tid, actor="u1", key=""):
    row = store.create_report_input(tid, key, actor, 3600)
    store.add_report_input_part(tid, row["id"], 0, 0, "QBR.zip", b"PK-not-read-by-the-fake")
    store.close_report_input(tid, row["id"], {"files": [], "summary": {}})
    return row["id"]


def _section(model, sid):
    return next(s for s in model["sections"] if s["id"] == sid)


def test_attached_files_feed_the_pages_through_the_governed_path(world, monkeypatch):
    monkeypatch.setattr(file_source, "_load", lambda tid, iid: _bundle())
    iid = _closed_input(world["tid"])
    run = mbr_engine.prepare(world["tid"], "analyst", "acmesweet", "2026-09", "MBR", inputs=iid)
    folder = Path(run.tables["upload_orders"].datasource["conn"]["path"]).parent
    try:
        model = mbr_engine.build(run)
    finally:
        run.close()
    assert not folder.exists()                                   # working copy removed

    files = [q for q in model["queries"] if q.get("datasource") == file_source.SOURCE_NAME]
    assert files and all(q["ok"] and q.get("sql") for q in files)   # governed + ran
    orders = _section(model, "files_orders")
    gmv = next(t for t in orders["tables"] if t["id"] == "gmv_shipping")
    assert sum(r[5] for r in gmv["rows"]) == pytest.approx(190_000.0)   # 180k items + 10k ship
    assert "GMV with shipping was IDR 190,000" in orders["headline"]
    recon = next(t for t in orders["tables"] if t["id"] == "files_vs_warehouse")
    shopee = next(r for r in recon["rows"] if r[0] == "Shopee")
    assert shopee[1] == pytest.approx(100_000.0) and shopee[2] == pytest.approx(900_000.0)

    live = _section(model, "live_video")
    assert any("(attached files)" in t["title"] for t in live["tables"])
    sept = next(r for r in live["tables"][0]["rows"] if r[0] == "Sep 2026")
    assert sept[1] == "Shopee, own account"
    assert sept[3] == pytest.approx(700_000.0)                    # the file, not the warehouse 1
    # an account with LIVE before and nothing this month is missing data, not a fall
    # (package 4: the Sep Shopee LIVE file was a copy of the empty Aug one)
    assert "TikTok, affiliates LIVE has no data for Sep 2026" in live["headline"]
    assert any(m["what"] == "TikTok, affiliates LIVE for Sep 2026" for m in live["missing"])
    assert run.uploads["replaced"] and "acme_store_live_mp" in run.uploads["replaced"]

    delivery = _section(model, "files_delivery")
    assert "3.0 days after payment" in delivery["headline"]
    supply = _section(model, "files_supply")
    assert "80.0%" in supply["headline"]
    srp = _section(model, "files_srp")
    # 150k over 4 units on both marketplaces; the cancelled line does not count
    assert srp["tables"][0]["rows"][0][3] == pytest.approx(37_500.0)
    assert "31.7% below SRP" in srp["headline"]
    inventory = _section(model, "inventory")                     # a snapshot after the month
    stock = next(t for t in inventory["tables"] if t["id"] == "inventory")
    assert "(attached files)" in stock["title"] and stock["rows"][0][2] == pytest.approx(40.0)
    used = _section(model, "files_used")
    assert used["headline"].startswith("1 of 2 attached files")
    assert model["uploads"]["tables"]["upload_orders"] == 4


def test_export_reconciles_on_sold_and_names_what_it_cannot_measure(world, monkeypatch):
    """Package 3 (DSOL Sep 2026): the export side counted only completed orders while the
    warehouse NMV also counts orders still shipping, so Shopee read 17% short; Lazada has no
    shipped time (its days to ship read 0.0); Shopee has no delivered time at all; a field
    no export carries summed to 0."""
    def bundle():
        b = _bundle()
        b.add("upload_orders", [
            _row(order_id="s9", line_id="s9", order_status="Sedang Dikirim",
                 status_group="in_flight", delivered_at=None, shipped_at=None,
                 item_value_after_seller_discount=40_000.0, shipping_fee_buyer=None),
            _row(order_id="z1", line_id="z1", marketplace="Lazada", shipped_at=None,
                 item_value_after_seller_discount=20_000.0, shipping_fee_buyer=None),
            _row(order_id="b1", line_id="b1", marketplace="Blibli", delivered_at=None,
                 item_value_after_seller_discount=10_000.0, shipping_fee_buyer=None)])
        return b
    monkeypatch.setattr(file_source, "_load", lambda tid, iid: bundle())
    run = mbr_engine.prepare(world["tid"], "analyst", "acmesweet", "2026-09", "MBR",
                             inputs=_closed_input(world["tid"]))
    try:
        model = mbr_engine.build(run)
    finally:
        run.close()
    orders = _section(model, "files_orders")
    recon = next(t for t in orders["tables"] if t["id"] == "files_vs_warehouse")
    assert recon["title"].startswith("Order value counted as sold")
    shopee = next(r for r in recon["rows"] if r[0] == "Shopee")
    assert shopee[1] == pytest.approx(140_000.0)          # completed 100k + still shipping 40k
    gmv = next(t for t in orders["tables"] if t["id"] == "gmv_shipping")
    # no export in the bundle carries the marketplace's shipping discount: not a 0
    assert {r[7] for r in gmv["rows"]} == {"Not recorded"}
    blibli = next(r for r in gmv["rows"] if r[0] == "Blibli")
    assert blibli[4] == "Not recorded" and blibli[5] == pytest.approx(10_000.0)

    delivery = _section(model, "files_delivery")
    days = next(t for t in delivery["tables"] if t["id"] == "files_delivery")
    assert "(no delivered time in the Blibli export)" in days["title"]
    lazada = next(r for r in days["rows"] if r[0] == "Lazada")
    assert lazada[2] == "Not recorded" and lazada[3] == "Not recorded" and lazada[4] > 0
    assert "Lazada, Shopee and TikTok orders only" in delivery["headline"]
    assert any("Blibli order export carries no delivered time" in f["text"]
               for f in delivery["facts"])


def test_geography_points_to_the_delivery_times_the_exports_gave():
    from app.reports import file_sections
    from app.reports.mbr_sections import missing, section
    geo = section("geography", "D", "Geography")
    geo["tables"].append({"id": "province"})
    geo["status"] = "partial"
    geo["missing"].append(missing("delivery time by area", "why", "needs", key="delivery"))
    files = section("files_delivery", "D", "Delivery times, from the marketplace exports")
    file_sections.link_delivery([geo, files])          # no province table: nothing changes
    assert geo["missing"] and geo["status"] == "partial"
    files["tables"].append({"id": "files_province"})
    file_sections.link_delivery([geo, files])
    assert geo["missing"] == [] and geo["status"] == "complete"
    assert any("'Delivery times, from the marketplace exports'" in f["text"]
               for f in geo["facts"])


def test_file_pages_render_in_the_deck_workbook_and_evidence(world, monkeypatch):
    from app.reports import markdown, pptx_writer, xlsx_writer
    from pptx import Presentation
    monkeypatch.setattr(file_source, "_load", lambda tid, iid: _bundle())
    run = mbr_engine.prepare(world["tid"], "analyst", "acmesweet", "2026-09", "MBR",
                             inputs=_closed_input(world["tid"]))
    try:
        model = mbr_engine.build(run)
    finally:
        run.close()
    deck = pptx_writer.build(model)
    assert pptx_writer.qc(deck) == []
    text = " ".join(sh.text_frame.text for s in Presentation(io.BytesIO(deck)).slides
                    for sh in s.shapes if sh.has_text_frame)
    assert "GMV with shipping" in text
    assert "upload_" not in text and "files_orders" not in text     # business words only
    assert xlsx_writer.build(model)[:2] == b"PK"
    assert "Attached files" in markdown.evidence(model)


def test_the_job_keeps_the_files_and_purges_expired_ones(world, monkeypatch):
    monkeypatch.setattr(file_source, "_load", lambda tid, iid: _bundle())
    tid = world["tid"]
    payload = {"role": "analyst", "brand": "acmesweet", "period": "2026-09",
               "business": "MBR"}
    kept = _closed_input(tid)
    expired = store.create_report_input(tid, "", "u1", -1)["id"]   # creating purges older ones
    store.add_report_input_part(tid, expired, 0, 0, "a.csv", b"x")
    out =job_handlers._mbr_report({"id": "job_" + "b" * 32, "tenant_id": tid},
                                   {**payload, "inputs": kept}, None, None)
    assert out["uploads"]["files_used"] == 1
    assert store.get_report_input(tid, kept) is not None         # ready to generate again
    c = store._c()
    try:
        left = c.execute("SELECT COUNT(*) AS n FROM report_inputs WHERE id=?",
                         (expired,)).fetchone()["n"]
    finally:
        c.close()
    assert left == 0


def test_expired_inputs_are_purged_and_go_with_the_tenant(world):
    tid = world["tid"]
    old = store.create_report_input(tid, "", "u1", -1)["id"]
    store.add_report_input_part(tid, old, 0, 0, "a.csv", b"x")
    assert store.get_report_input(tid, old) is None               # expired reads as gone
    store.create_report_input(tid, "", "u1", 3600)                # creating purges expired
    c = store._c()
    try:
        left = c.execute("SELECT COUNT(*) AS n FROM report_input_parts WHERE input_id=?",
                         (old,)).fetchone()["n"]
    finally:
        c.close()
    assert left == 0
    live = _closed_input(tid)
    store.delete_tenant(tid)
    assert store.get_report_input(tid, live) is None
