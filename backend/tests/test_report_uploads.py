"""Marketplace export parsing for one MBR run (app.reports.uploads), fully offline.

Every fixture is synthetic and built in memory: tiny .xlsx packages written with zipfile
and hand-written sheet XML, .xls workbooks written with xlwt (a test-only dependency),
csv strings and in-memory zips. No real export, name or
value is copied here; the "SECRET" markers stand in for buyer identity so a test can
prove none of it reaches a row.
"""
from __future__ import annotations

import datetime as dt
import io
import zipfile
from xml.sax.saxutils import escape

import pytest

from app.reports.uploads import classify, load_files
from app.reports.uploads import parse as parse_mod
from app.reports.uploads import values as V
from app.reports.uploads import xls, xlsx
from app.reports.uploads.parse import NESTED_ZIP, UNKNOWN
from app.reports.uploads.sheets import NOT_EXCEL

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
SHEET_REL = REL_NS + "/worksheet"
SECRET = "SECRET"
STATUS_COL = "Status Pesanan"
PERIOD_COL = "Periode Data"
SALES_COL = "Penjualan(Pesanan Dibuat)"
SEP_1 = "2026-09-01 10:22"
SEP_1_DMY = "01/09/2026 10:22:33"
SEP_5 = "05 Sep 2026"
OCT_21 = "Oct 21, 2026"
SEP = "2026-09"
PRODUCT_A = "Produk A"
PRICE_100K = "100.000"
FIFTY_K = "50000.00"
ONE_K = "1.000"
PCT = "4,16%"
CITY = "KOTA REAL"
SHOPEE_CSV = "Order_B_Shopee_Sep_2026.csv"
SHOPEE_XLSX = "Order_B_Shopee_Sep_2026.xlsx"


# ---- fixture builders ----
class Rich:
    """A shared string written as rich-text runs, with a phonetic run that must be skipped."""

    def __init__(self, *runs: str):
        self.runs = runs


def _col(i: int) -> str:
    out, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(65 + r) + out
    return out


class _Strings:
    def __init__(self):
        self.items: list[str] = []
        self.index: dict = {}

    def id(self, value) -> int:
        key = ("rich",) + value.runs if isinstance(value, Rich) else ("text", value)
        if key not in self.index:
            self.index[key] = len(self.items)
            if isinstance(value, Rich):
                runs = "".join(f"<r><t>{escape(t)}</t></r>" for t in value.runs)
                self.items.append(f"<si>{runs}<rPh><t>phonetic</t></rPh></si>")
            else:
                self.items.append(f'<si><t xml:space="preserve">{escape(value)}</t></si>')
        return self.index[key]


def _cell(ref: str, value, strings: _Strings, inline: bool) -> str:
    if isinstance(value, bool):
        kind, body = ' t="b"', f"<v>{int(value)}</v>"
    elif isinstance(value, (int, float)):
        kind, body = "", f"<v>{value}</v>"
    elif inline and not isinstance(value, Rich):
        kind, body = ' t="inlineStr"', f"<is><t>{escape(value)}</t></is>"
    else:
        kind, body = ' t="s"', f"<v>{strings.id(value)}</v>"
    return f'<c r="{ref}"{kind}>{body}</c>'


def _sheet_xml(rows, strings: _Strings, inline: bool, split_rows: bool, doctype: bool) -> str:
    body = []
    for r, row in enumerate(rows, 1):
        cells = [_cell(f"{_col(c)}{r}", v, strings, inline)
                 for c, v in enumerate(row) if v is not None]
        if split_rows:                        # one <row> element per cell, same r
            body.extend(f'<row r="{r}">{c}</row>' for c in cells)
        else:
            body.append(f'<row r="{r}">{"".join(cells)}</row>')
    head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    if doctype:
        head += '<!DOCTYPE lol [<!ENTITY lol "lol">]>'
    return (f'{head}<worksheet xmlns="{NS}"><dimension ref="A1"/>'
            f'<sheetData>{"".join(body)}</sheetData></worksheet>')


def make_xlsx(sheets: dict, inline=False, split_rows=False, doctype=False) -> bytes:
    strings = _Strings()
    parts = {}
    entries, rels = [], []
    for n, (name, rows) in enumerate(sheets.items(), 1):
        parts[f"xl/worksheets/sheet{n}.xml"] = _sheet_xml(rows, strings, inline, split_rows,
                                                         doctype)
        entries.append(f'<sheet name="{escape(name)}" sheetId="{n}" r:id="rId{n}"/>')
        rels.append(f'<Relationship Id="rId{n}" Type="{SHEET_REL}" '
                    f'Target="worksheets/sheet{n}.xml"/>')
    parts["xl/workbook.xml"] = (f'<workbook xmlns="{NS}" xmlns:r="{REL_NS}"><sheets>'
                                f'{"".join(entries)}</sheets></workbook>')
    parts["xl/_rels/workbook.xml.rels"] = (f'<Relationships xmlns="{PKG_REL}">'
                                           f'{"".join(rels)}</Relationships>')
    parts["xl/sharedStrings.xml"] = f'<sst xmlns="{NS}">{"".join(strings.items)}</sst>'
    parts["[Content_Types].xml"] = "<Types/>"
    return make_zip(parts)


def make_zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data.encode("utf-8") if isinstance(data, str) else data)
    return buf.getvalue()


def make_csv(rows, delimiter=",") -> bytes:
    def cell(v):
        text = "" if v is None else str(v)
        if text.startswith("=") or not any(ch in text for ch in (delimiter, '"', "\n")):
            return text                       # ="123" is written raw, as Blibli does
        return '"' + text.replace('"', '""') + '"'
    return "\n".join(delimiter.join(cell(v) for v in row) for row in rows).encode("utf-8")


def load(name: str, data: bytes, **kw):
    bundle = load_files([(name, data)], **kw)
    return bundle, bundle.files[0]


def rows_of(bundle, table: str) -> list[dict]:
    return bundle.tables.get(table, [])


def assert_no_secret(bundle):
    for table, rows in bundle.tables.items():
        for row in rows:
            for value in row.values():
                assert SECRET not in str(value), f"buyer identity leaked into {table}"


# ---- the xlsx reader ----
def test_reader_shared_inline_rich_sparse_and_types():
    data = make_xlsx({"S": [["name", None, "qty", "flag"],
                            [Rich("Ri", "ch"), None, 3, True],
                            [None, None, 2.5]]})
    wb = xlsx.Workbook(data)
    assert wb.sheet_names == ["S"]
    rows = list(wb.rows())
    assert rows[0] == ["name", None, "qty", "flag"]
    assert rows[1] == ["Rich", None, 3, True]          # phonetic run skipped
    assert rows[2] == [None, None, 2.5]                 # empty cells keep their place
    inline = xlsx.Workbook(make_xlsx({"S": [["a", None, "c"]]}, inline=True))
    assert list(inline.rows("S")) == [["a", None, "c"]]


def test_reader_merges_one_cell_per_row_element():
    data = make_xlsx({"S": [["h1", "h2", "h3"], ["1", "2", "3"]]}, split_rows=True)
    assert list(xlsx.Workbook(data).rows()) == [["h1", "h2", "h3"], ["1", "2", "3"]]


def test_reader_refuses_doctype_and_caps_columns():
    bad = make_xlsx({"S": [["a"]]}, doctype=True)
    with pytest.raises(xlsx.XlsxError, match="DOCTYPE"):
        list(xlsx.Workbook(bad).rows())
    _, res = load("Order_X_Shopee_Sep_2026.xlsx", bad)
    assert not res.used and "DOCTYPE" in res.reason
    wide = xlsx.Workbook(make_xlsx({"S": [list("abcdef")]}), max_cols=3)
    assert list(wide.rows()) == [["a", "b", "c"]]
    capped = xlsx.Workbook(make_xlsx({"S": [["a"], ["b"], ["c"]]}), max_rows=2)
    assert len(list(capped.rows("S"))) == 2 and capped.truncated["S"]
    assert xlsx.is_xlsx(make_xlsx({"S": [["a"]]})) and not xlsx.is_xlsx(make_zip({"a.txt": "x"}))


# ---- values ----
@pytest.mark.parametrize("raw, decimal, expected", [
    ("212.493.871", ",", 212493871.0),
    ("\xa0257,00", ",", 257.0),
    ("Rp 1.234.567", ",", 1234567.0),
    ("-Rp12.500", ",", -12500.0),
    ("1,234.50", ",", 1234.5),
    ("45.5", ".", 45.5),
    ("(1.000)", ",", -1000.0),
    ("-", ",", None),
    ("", ",", None),
    (None, ",", None),
    (7, ",", 7.0),
])
def test_number_formats(raw, decimal, expected):
    assert V.number(raw, decimal) == expected


def test_percent_and_decimal_detection():
    assert V.percent(PCT) == pytest.approx(4.16)
    assert V.percent("13.17%", ".") == pytest.approx(13.17)
    assert V.detect_decimal([FIFTY_K, "12.5"]) == "."
    assert V.detect_decimal(["1.234.567", PCT]) == ","


@pytest.mark.parametrize("raw, expected", [
    (OCT_21, dt.datetime(2026, 10, 21)),
    (SEP_1, dt.datetime(2026, 9, 1, 10, 22)),
    (SEP_1_DMY, dt.datetime(2026, 9, 1, 10, 22, 33)),
    ("21/10/2026", dt.datetime(2026, 10, 21)),
    ("21 Sep 2026 10:22", dt.datetime(2026, 9, 21, 10, 22)),
    ("22-Jan-2026", dt.datetime(2026, 1, 22)),
    ("2026/09/30/ 21:46", dt.datetime(2026, 9, 30, 21, 46)),
    (46290, dt.datetime(2026, 9, 25)),                  # an Excel date number
    ("not a date", None),
])
def test_timestamps(raw, expected):
    assert V.timestamp(raw) == expected


# ---- classify ----
@pytest.mark.parametrize("path, kind, marketplace, month", [
    ("QBR/Monthly Performance/Shopee/Order_Brand_Shopee_SEP_2026_RAW OBI.xlsx",
     "orders", "Shopee", SEP),
    ("QBR/Product Performance/Shopee/Product_Brand_Shopee_Aug_20026.xlsx",
     "product", "Shopee", "2026-08"),
    ("QBR/Live/Tiktok Tokopedia/Live_Affiliate_Brnad_Tiktok_Feb_2026.xlsx",
     "live", "TikTok", "2026-02"),
    ("QBR/Product Performance/Shopee/Live_Official_Brand_Shopee_May_2026.csv",
     "live", "Shopee", "2026-05"),
    ("Wallet & Settlement/Tiktok Tokopedia/Wallet_B_Tiktok_Tokopedia_Apr_2026 (1).xlsx",
     "wallet", "TikTok/Tokopedia", "2026-04"),
    ("Affiliate Performance/Shopee/AffiliateTrx_B_Shopee_Jul_2026.csv",
     "affiliate", "Shopee", "2026-07"),
    ("DSO Inventory as of 21 Oct 2026.csv", "inventory", "", "2026-10"),
    ("KNO PO Service Level.csv", "po", "", ""),
    ("SRPMaster_BRAND_2026.xlsx", "srp", "", ""),
    ("Video/Shopee/Video_Official_B_Shopee_Sep_20206.csv", "video", "Shopee", ""),
])
def test_classify_from_names(path, kind, marketplace, month):
    got = classify(path)
    assert (got["kind"], got["marketplace"], got["month"]) == (kind, marketplace, month)


def test_classify_from_header_and_title_range():
    assert classify("export.csv", [["No. Pesanan", STATUS_COL, "Harga Awal"]])["kind"] \
        == "orders"
    got = classify("Product_B_Tiktok_Aug_2026.xlsx", [["2026-09-01 ~ 2026-09-30"]])
    assert got["month"] == SEP and "2026-08" in got["note"]


# ---- orders ----
SHOPEE_HEADER = ["No. Pesanan", STATUS_COL, "Alasan Pembatalan", "Waktu Pesanan Dibuat",
                 "Nama Produk", "Harga Awal", "Jumlah", "Diskon Dari Penjual",
                 "Diskon Dari Shopee", "Ongkos Kirim Dibayar oleh Pembeli", "Total Pembayaran",
                 "Username (Pembeli)", "Nama Penerima", "No. Telepon", "Alamat Pengiriman",
                 "Kota/Kabupaten", "Provinsi"]
PII = [f"buyer_{SECRET}", f"{SECRET} Name", f"0812{SECRET}", f"Jl. {SECRET} 1"]


def _shopee_line(order, status, created, product, price, qty, disc, ship, total, cancel=""):
    return [order, status, cancel, created, product, price, qty, disc, "0", ship, total,
            *PII, "KOTA BANDUNG", "JAWA BARAT"]


SHOPEE_ROWS = [
    SHOPEE_HEADER,
    _shopee_line("260901AAA111", "Selesai", SEP_1, PRODUCT_A, PRICE_100K, "2",
                 "10.000", "15.000", "205.000"),
    _shopee_line("260901AAA111", "Selesai", SEP_1, "Produk B", "50.000", "1",
                 "0", "15.000", "205.000"),
    _shopee_line("260902BBB222", "Batal", "2026-09-02 08:00", PRODUCT_A, PRICE_100K, "1", "0",
                 "0", "0", cancel="Dibatalkan oleh Pembeli. Alasan: Ubah pesanan"),
    _shopee_line("260903CCC333", "Pesanan diterima, namun Pembeli masih dapat mengajukan "
                 "pengembalian hingga 2026-09-10", "2026-09-03 09:00", PRODUCT_A, PRICE_100K,
                 "1", "0", "9.000", "109.000"),
    _shopee_line("260904DDD444", "Belum Bayar", "2026-09-04 09:00", "Produk B", "50.000", "1",
                 "0", "9.000", "59.000"),
]


def test_shopee_orders_first_line_money_status_and_no_pii():
    bundle, res = load("Order_Brand_Shopee_Sep_2026.xlsx", make_xlsx({"orders": SHOPEE_ROWS}))
    assert res.used and res.kind == "orders" and res.rows == 5
    lines = rows_of(bundle, "upload_orders")
    first, second = lines[0], lines[1]
    assert first["shipping_fee_buyer"] == 15000 and first["order_amount"] == 205000
    assert second["shipping_fee_buyer"] is None and second["order_amount"] is None
    assert first["item_value_after_seller_discount"] == 190000
    assert [r["status_group"] for r in lines] == [
        "processed", "processed", "cancelled", "processed", "unpaid"]
    assert lines[2]["cancel_by"] == "buyer" and lines[2]["cancel_reason"] == "Ubah pesanan"
    assert first["order_status"] == "Selesai" and first["city"] == "KOTA BANDUNG"
    assert first["province"] == "Jawa Barat"          # one spelling with TikTok's "West Java"
    assert first["created_at"] == dt.datetime(2026, 9, 1, 10, 22)
    assert {r["month_id"] for r in lines} == {202609}
    assert first["line_id"] == "260901AAA111-1" and second["line_id"] == "260901AAA111-2"
    assert_no_secret(bundle)


TIKTOK_HEADER = ["Order ID", "Order Status", "Order Substatus", "SKU ID", "Seller SKU",
                 "Product Name", "Quantity", "SKU Unit Original Price",
                 "SKU Subtotal Before Discount", "SKU Platform Discount", "SKU Seller Discount",
                 "Shipping Fee After Discount", "Order Amount", "Created Time", "Cancel By",
                 "Buyer Username", "Recipient", "Phone #", "Detail Address", "Province",
                 "Regency and City", "Purchase Channel", "Order Channel", "Creator Handle"]
TIKTOK_DESCRIPTIONS = ["Platform unique order ID.", "The status of the order."] + [
    "Description."] * (len(TIKTOK_HEADER) - 2)


def _tiktok_line(order, status, sub, created, channel, cancel_by=""):
    return [order, status, sub, "1729000000000000001", "SKU-1", "Produk", "1", "50000",
            "50000", "0", "5000", "9000", "104000", created, cancel_by, *PII, "Jawa Barat",
            "Bandung", channel, "Videos", "creator_handle"]


def test_tiktok_orders_description_row_channel_and_split_rows():
    rows = [TIKTOK_HEADER, TIKTOK_DESCRIPTIONS,
            _tiktok_line("5800000000000001", "Selesai", "", SEP_1_DMY, "Tokopedia"),
            _tiktok_line("5800000000000001", "Selesai", "", SEP_1_DMY, "Tokopedia"),
            _tiktok_line("5800000000000002", "Dibatalkan", "", "02/09/2026 11:00:00", "TikTok",
                         cancel_by="User"),
            _tiktok_line("5800000000000003", "Dikirim", "Terkirim", "03/09/2026 12:00:00",
                         "TikTok")]
    data = make_xlsx({"OrderSKUList": rows}, split_rows=True)
    bundle, res = load("Order_Brand_Tiktok_Tokopedia_Sep_2026.xlsx", data)
    lines = rows_of(bundle, "upload_orders")
    assert res.used and len(lines) == 4                  # the description row is skipped
    assert [r["marketplace"] for r in lines] == ["Tokopedia", "Tokopedia", "TikTok", "TikTok"]
    assert lines[0]["shipping_fee_buyer"] == 9000 and lines[1]["shipping_fee_buyer"] is None
    assert lines[0]["created_at"] == dt.datetime(2026, 9, 1, 10, 22, 33)
    assert [r["status_group"] for r in lines] == ["processed", "processed", "cancelled",
                                                  "processed"]
    assert lines[2]["cancel_by"] == "buyer" and lines[0]["creator_handle"] == "creator_handle"
    assert_no_secret(bundle)


def test_lazada_orders_one_row_per_unit_and_masked_city():
    rows = [["orderItemId", "orderNumber", "status", "sellerSku", "itemName", "createTime",
             "unitPrice", "sellerDiscountTotal", "shippingFee", "paidPrice", "customerName",
             "shippingAddress", "shippingCity", "billingCity", "shippingRegion"],
            ["111", "900001", "delivered", "SKU1", "Item", "21 Sep 2026 10:22", FIFTY_K,
             "-5000.00", "0.00", "45000.00", SECRET, SECRET, "Kota*****", CITY, ""],
            ["112", "900002", "canceled", "SKU1", "Item", "22 Sep 2026 10:22", FIFTY_K,
             "0.00", "0.00", FIFTY_K, SECRET, SECRET, "Kota*****", CITY, ""]]
    bundle, res = load("Order_B_Lazada_Sep_2026.csv", make_csv(rows))
    first, second = rows_of(bundle, "upload_orders")
    assert res.used and first["quantity"] == 1 and first["seller_discount"] == 5000
    assert first["item_value_after_seller_discount"] == 45000
    assert first["city"] == CITY and first["status_group"] == "processed"
    assert second["status_group"] == "cancelled" and first["line_id"] == "111"
    assert_no_secret(bundle)


def test_blibli_orders_escaped_ids_and_finished_return():
    header = ["No. Order", "No. Order Item", "Tanggal Order", "Merchant SKU", "Nama produk",
              "Total Barang", "Harga item pesanan", "Total harga item pesanan", "Diskon",
              "Order Status", "Nama Pemesan", "Alamat", "Kota", "Provinsi", "Status retur"]
    rows = [header,
            ['="12345678"', '="12345678-1"', "21/09/2026 10:00", "SKU", "Prod", "2", "25000",
             "50000", "-5000", "Terkirim", SECRET, SECRET, "Kota", "Prov", ""],
            ['="12345679"', '="12345679-1"', "22/09/2026 10:00", "SKU", "Prod", "1", "25000",
             "25000", "0", "Terkirim", SECRET, SECRET, "Kota", "Prov", "FINISHED"]]
    bundle, res = load("Order_B_Blibli_Sep_2026.csv", make_csv(rows, ";"))
    first, second = rows_of(bundle, "upload_orders")
    assert res.used and first["order_id"] == "12345678" and first["line_id"] == "12345678-1"
    assert first["item_value_after_seller_discount"] == 45000
    assert first["status_group"] == "processed" and second["status_group"] == "returned"
    assert_no_secret(bundle)


# ---- product performance ----
def test_shopee_product_prefers_its_sheet_and_skips_variations():
    header = ["Kode Produk", "Produk", "Kode Variasi", "Total Penjualan (Pesanan Dibuat) (IDR)",
              "Pesanan Dibuat", "Halaman Produk Dilihat", "Tingkat Konversi (Pesanan yang Dibuat)"]
    data = make_xlsx({
        "Ringkasan": [["Kode Produk", "Produk", "Halaman Produk Dilihat",
                       "Total Penjualan (Pesanan Dibuat) (IDR)"], ["1", "x", "1", "1"]],
        "Produk dengan Performa Terbaik": [
            header, ["1001", "Prod A", "-", "1.234.567", "10", "500", PCT],
            ["1001", "Prod A", "2001", "600.000", "5", "250", "2,00%"]]})
    bundle, res = load("Product_B_Shopee_Sep_2026.xlsx", data)
    (row,) = rows_of(bundle, "acme_store_product_mp")
    assert res.used and row["sales_revenue"] == 1234567 and row["cvr"] == pytest.approx(4.16)
    assert row["marketplace"] == "Shopee" and row["month_id"] == 202609
    assert row["brand"] is None                      # the loader stamps the brand


def test_tiktok_product_title_group_row_and_first_block():
    rows = [["2026-09-01 ~ 2026-09-30"],
            [None, None, "Semua", None, None, None, None, "LIVE penjual", None],
            ["ID Produk", "Nama", "GMV", "Pesanan", "Produk terjual", "Impresi produk",
             "Klik produk", "GMV", "Pesanan"],
            ["1729000000000000001", "Prod", "Rp1.000.000", "5", "6", ONE_K, "100",
             "Rp400.000", "2"]]
    bundle, res = load("Product_B_Tiktok_Sep_2026.xlsx", make_xlsx({"Sheet1": rows}))
    (row,) = rows_of(bundle, "acme_store_product_mp")
    assert res.used and res.month == SEP
    assert row["sales_revenue"] == 1_000_000 and row["orders"] == 5
    assert row["impressions"] == 1000 and row["marketplace"] == "TikTok"


def _seller_centre_product(*ids: str) -> bytes:
    """The product report TikTok Shop and Tokopedia both write (one row per product id)."""
    sources = ("tab Toko", "dari LIVE", "dari video", "dari kartu produk")
    header = (["ID", "Produk", "GMV", "Produk terjual", "Pesanan"]
              + [f"Tayangan halaman {s}" for s in sources]
              + [f"Tayangan halaman unik {s}" for s in sources]
              + ["Pembeli produk unik tab Toko", "Pembeli produk unik dari LIVE",
                 "Pembeli produk unik dari video", "Pembeli unik dari kartu produk"])
    rows = [[pid, "Prod", "500000", "3", "3", "10", "20", "30", "40",
             "5", "5", "5", "5", "1", "0", "1", "0"] for pid in ids]
    return make_csv([header, *rows])


def test_tokopedia_product_sums_traffic_sources():
    bundle, _ = load("Product_B_Tokopedia_Sep_2026.csv",
                       _seller_centre_product("1729000000000000002"))
    (out,) = rows_of(bundle, "acme_store_product_mp")
    assert out["total_views_page"] == 100 and out["visitors"] == 20
    assert out["cvr"] == pytest.approx(10.0) and out["marketplace"] == "Tokopedia"


def test_product_named_for_both_shops_is_the_shop_whose_report_is_missing():
    bundle = load_files([
        ("Product_B_Tokopedia_Mar_2026.csv", _seller_centre_product("1729000000000000003")),
        ("Product_B_TikTok_Tokopedia_Mar_2026.csv", _seller_centre_product("1729000000000000004")),
        ("Product_B_TikTok_Tokopedia_Jan_2026.csv", _seller_centre_product("1729000000000000005")),
    ])
    shops = {r["product_sku"]: r["marketplace"] for r in rows_of(bundle, "acme_store_product_mp")}
    assert shops == {"1729000000000000003": "Tokopedia", "1729000000000000004": "TikTok",
                     "1729000000000000005": "Tokopedia"}
    _, mar, jan = bundle.files
    assert mar.marketplace == "TikTok" and "attached as its own file" in mar.warnings[-1]
    assert jan.marketplace == "TikTok/Tokopedia" and "usually comes from" in jan.warnings[-1]


def test_blibli_product_title_rows_and_nbsp_numbers():
    rows = [["Laporan Produk"], [],
            ["Kode SKU Blibli", "Nama produk", "Penjualan (Rp)", "Halaman dikunjungi", "CVR",
             "Pesanan"],
            ["ABC-1000-0001", "Prod", "\xa01.234,50", "12", "8,33%", "1"]]
    bundle, _ = load("Product_B_Blibli_Sep_2026.xlsx", make_xlsx({"S": rows}))
    (row,) = rows_of(bundle, "acme_store_product_mp")
    assert row["sales_revenue"] == pytest.approx(1234.5) and row["cvr"] == pytest.approx(8.33)


def make_xls(rows: list) -> bytes:
    """A one-sheet BIFF .xls; a cell that is a number is written as a number cell."""
    xlwt = pytest.importorskip("xlwt")
    book = xlwt.Workbook()
    sheet = book.add_sheet("Produk")
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            if value is not None:
                sheet.write(r, c, value)
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def test_lazada_product_xls_skips_sku_rows():
    header = ["Kinerja Produk", "Nama Produk", "URL", "Seller SKU", "SKU ID", "Pengunjung",
              "Tayangan Halaman Produk", "Unit yang ditambahkan ke troli", "Pembeli",
              "Pesanan", "Unit Terjual", "Pendapatan", "Tingkat Konversi"]
    rows = [["Sumber Data : Lazada - Bisnis Analisis - Produk - Jangka Waktu "
             "Kinerja2026-09-01_2026-09-30"], [], ["Data SKU berada di bawah data produk."], [],
            [], header,
            ["5900000001", "Prod A", "https://example.test/a", "-", "-", "198", "672", "29",
             "2", "2", "4", "1176940", "1.01%"],
            ["5900000001", "Prod A", "https://example.test/a", "SKU-A", "11400000001", "-", "-",
             "29", "2", "2", "4", "1176940", "-"],
            [5900000002, "Prod B", "https://example.test/b", "-", "-", 50, 80, 3, 1, 1, 1,
             250000.0, "2.00%"]]
    bundle, res = load("Product_B_Lazada_Sep_2026.xls", make_xls(rows))
    got = {r["product_sku"]: r for r in rows_of(bundle, "acme_store_product_mp")}
    assert res.used and res.month == SEP and set(got) == {"5900000001", "5900000002"}
    a, b = got["5900000001"], got["5900000002"]
    assert a["sales_revenue"] == 1176940 and a["total_views_page"] == 672
    assert a["visitors"] == 198 and a["cvr"] == pytest.approx(1.01) and a["orders"] == 2
    assert b["sales_revenue"] == 250000 and b["total_views_page"] == 80
    assert {r["marketplace"] for r in got.values()} == {"Lazada"}


def test_xls_reader_cells_and_row_cap():
    data = make_xls([["a", 3, 2.5, True], [None, "x"], ["c"]])
    book = xls.Workbook(data, max_rows=2)
    assert book.sheet_names == ["Produk"]
    got = list(book.rows())
    assert got[0] == ["a", 3, 2.5, True] and got[1][:2] == [None, "x"] and len(got) == 2
    assert book.truncated["Produk"] is True


# ---- affiliate ----
def test_affiliate_transactions_both_marketplaces_and_summary_not_used():
    shopee = make_csv([["Kode Pesanan", STATUS_COL, "Waktu Pesanan", "Nama Produk",
                        "Username Affiliate", "Nilai Pembelian(Rp)",
                        "Estimasi Komisi per Produk(Rp)", "Platform"],
                       ["2609AAA1", "Selesai", "2026-09-05 10:00:00", "Prod", "creator_one",
                        "100000", "5000", "Shopeevideo-Shopee"]])
    tiktok = make_xlsx({"S": [["ID Pesanan", STATUS_COL, "Waktu Dibuat", "Produk",
                               "Nama pengguna kreator", "Jenis Konten", "Payment Amount",
                               "Perkiraan pembayaran komisi standar",
                               "Perkiraan pembayaran komisi Iklan Toko", "Platform"],
                              ["5800000000000009", "Selesai", "05/09/2026 10:00:00", "Prod",
                               "creator_two", "Live Streaming", "80000", "4000", "1000",
                               "TOKOPEDIA"]]})
    summary = make_csv([["Username Affiliate", "Omzet Penjualan(Rp)"], ["creator_one", "1000"]])
    bundle = load_files([("AffiliateTrx_B_Shopee_Sep_2026.csv", shopee),
                         ("AffiliateTrx_B_Tiktok_Sep_2026.xlsx", tiktok),
                         ("AffiliatePerf_B_Shopee_Sep_2026.csv", summary)])
    shopee_row, tiktok_row = rows_of(bundle, "acme_store_affiliatetrx_mp")
    assert shopee_row["content_type"] == "video" and shopee_row["month_id"] == 202609
    assert tiktok_row["marketplace"] == "Tokopedia" and tiktok_row["commission"] == 5000
    assert tiktok_row["content_type"] == "live"
    assert not bundle.files[2].used and "summary" in bundle.files[2].reason


# ---- LIVE and video ----
def test_shopee_live_group_row_and_owner_from_name():
    rows = [[PERIOD_COL, "", "Data Utama", "", "", "Konversi", ""],
            [PERIOD_COL, "User Id", SALES_COL, "Pesanan(Pesanan Dibuat)",
             "Produk Terjual(Pesanan Dibuat)", "Penonton", "Penonton Aktif"],
            ["01-09-2026", "375000000", "Rp1.234.567", "3", "4", "100", "20"]]
    bundle, res = load("Live_Official_B_Shopee_Sep_2026.csv", make_csv(rows))
    (row,) = rows_of(bundle, "acme_store_live_mp")
    assert res.used and row["live_date"] == dt.date(2026, 9, 1) and row["gmv"] == 1234567
    assert row["owner"] == "official" and row["marketplace"] == "Shopee"


def test_empty_exports_of_two_months_are_both_read_not_dropped_as_copies():
    # ACMESWEET package 4: the Aug and Sep Shopee LIVE exports held headers only, so they
    # were byte for byte the same and Sep was dropped as "the same file as" Aug.
    empty = make_csv([[PERIOD_COL, "User Id", SALES_COL, "Penonton", "Penonton Aktif"]])
    full = make_csv([[PERIOD_COL, SALES_COL, "Penonton", "Penonton Aktif"],
                     ["01-07-2026", "1", "1", "1"]])
    bundle = load_files([("Live_Official_B_Shopee_Aug_2026.csv", empty),
                         ("Live_Official_B_Shopee_Sep_2026.csv", empty),
                         ("Live_Official_B_Shopee_Jul_2026.csv", full),
                         ("Live_Official_B_Shopee_Jul_2026 (1).csv", full)])
    aug, sep, jul, copy = bundle.files
    assert (aug.month, aug.reason) == ("2026-08", parse_mod.NO_ROWS)
    assert (sep.month, sep.reason) == ("2026-09", parse_mod.NO_ROWS)
    assert jul.used and copy.reason.startswith("the same file as")   # a real copy still is one


def test_tiktok_live_title_row_and_affiliate_owner():
    rows = [["Date Range: 2026-09-01 ~ 2026-09-30\n"],
            ["ID Kreator", "Kreator", "Nama panggilan", "Waktu Live", "GMV dari LIVE (Rp)",
             "Pesanan Dibayar", "Produk Terjual", "Penonton"],
            ["1", "Display Name", "handle_one", "2026/09/30/ 21:46", "250000", "2", "3", "500"]]
    bundle, _ = load("Live_Affiliate_B_Tiktok_Sep_2026.xlsx", make_xlsx({"Sheet1": rows}))
    (row,) = rows_of(bundle, "acme_store_live_mp")
    assert row["creator"] == "handle_one" and row["owner"] == "affiliate"
    assert row["live_date"] == dt.date(2026, 9, 30) and row["gmv"] == 250000


def test_tiktok_video_is_dated_on_its_report_period():
    rows = [["[Rentang Tanggal]: 2026-09-01 ~ 2026-09-30\n"],
            ["Nama Kreator", "ID Kreator", "ID Video", "Waktu", "VV", "GMV dari video (Rp)",
             "Pesanan SKU teratribusi"],
            ["creator", "1", "7500000000000000001", "2025/01/12 12:00:15", "782", "1703735", "3"]]
    bundle, _ = load("Video_Affiliate_B_Tiktok_Sep_2026.xlsx", make_xlsx({"Sheet1": rows}))
    (row,) = rows_of(bundle, "acme_store_video_mp")
    assert row["video_date"] == dt.date(2026, 9, 1)     # not the 2025 posting date
    assert row["views"] == 782 and row["gmv"] == 1703735 and row["owner"] == "affiliate"
    shopee = make_csv([[PERIOD_COL, "User Id", SALES_COL,
                        "Pesanan(Pesanan Dibuat)", "Ditonton"],
                       ["02-09-2026", "1", "Rp10.000", "1", "50"]])
    bundle, _ = load("Video_Official_B_Shopee_Sep_2026.csv", shopee)
    (row,) = rows_of(bundle, "acme_store_video_mp")
    assert row["video_date"] == dt.date(2026, 9, 2) and row["gmv"] == 10000


# ---- wallet / settlement ----
def _wallet(bundle, marketplace, month):
    return next(r for r in rows_of(bundle, "acme_store_wallet_mp")
                if r["marketplace"] == marketplace and r["month_id"] == month)


def test_shopee_wallet_summary_mapping_and_unknown_line():
    rows = [["Laporan Penghasilan"], [], ["Rincian Laporan"], ["Dari", "2026-09-01"],
            ["ke", "2026-09-30"], ["Ringkasan Penghasilan", None, None, "Rp"],
            ["1. Total Pendapatan", None, None, 900], ["Subtotal Pesanan", None, None, 950],
            [None, "Harga Asli Produk", 1000], [None, "Total Diskon Produk", -50],
            [None, "Voucher disponsor oleh Penjual", -10],
            [None, "Cashback Koin disponsori Penjual", -40],
            ["2. Total Pengeluaran", None, None, -200],
            ["Total Biaya Pengiriman", None, None, -20], [None, "Ongkir Dibayar Pembeli", 30],
            ["Biaya Platform", None, None, -170],
            [None, "Biaya Administrasi (termasuk PPN 11%)", -100],
            [None, "Biaya Proses Pesanan", -30], [None, "Biaya Komisi AMS", -40],
            [None, "Biaya Misteri Baru", -10],
            ["3. Total yang Dilepas", None, None, 700]]
    data = make_xlsx({"Summary": rows, "Penghasilan": [["Lihat berdasarkan"], ["Order"]]})
    bundle, res = load("Wallet_B_Shopee_Sep_2026.xlsx", data)
    row = _wallet(bundle, "Shopee", 202609)
    assert res.used and row["wallet_gmv"] == 950 and row["revenue_wallet"] == 900
    assert row["commission_fee"] == -100 and row["order_processing_fee"] == -30
    assert row["mp_fee"] == -130 and row["affiliate_fee"] == -40
    assert row["shipping_fee"] == -20 and row["seller_voucher"] == -10
    assert row["other_fee"] == -50 and row["total_settlement_amount"] == 700
    assert any("Biaya Misteri Baru" in w for w in res.warnings)


TIKTOK_WALLET_HEADER = ["Order/Adjustment ID", "Type", "Order settled time",
                        "Total settlement amount", "Total Revenue",
                        "Subtotal after seller discounts", "Total Fees",
                        "Platform commission fee", "Shipping cost", "Affiliate Commission",
                        "Adjustment amount", "Brand new fee", "Customer payment", "Order source"]


def test_tiktok_wallet_months_sources_and_reconciliation():
    rows = [TIKTOK_WALLET_HEADER,
            ["1001", "Order", "2026/09/03", "800", "1000", "1000", "-200", "-150", "-30", "-20",
             "0", "0", "1000", "TikTok Shop"],
            ["1002", "Order", "2026/09/04", "400", "500", "500", "-100", "-60", "-10", "-30",
             "0", "0", "500", "Tokopedia"],
            ["1003", "Adjustment", "2026/10/01", "-5", "0", "0", "0", "0", "0", "0", "-5", "0",
             "0", ""],
            ["1004", "Order", "2026/10/02", "90", "100", "100", "-10", "0", "0", "0", "0",
             "-10", "100", "TikTok Shop"]]
    bundle, res = load("Wallet_B_Tiktok_Tokopedia_Sep_2026.xlsx",
                       make_xlsx({"Order details": rows, "Reports": [["x"]]}))
    sep, toko, octo = (_wallet(bundle, "TikTok", 202609), _wallet(bundle, "Tokopedia", 202609),
                       _wallet(bundle, "TikTok", 202610))
    assert sep["total_settlement_amount"] == 800 and sep["commission_fee"] == -150
    assert sep["mp_fee"] == -150 and sep["shipping_fee"] == -30 and sep["affiliate_fee"] == -20
    assert toko["revenue_wallet"] == 500 and toko["total_settlement_amount"] == 400
    assert octo["other_fee"] == -15 and octo["total_settlement_amount"] == 85
    assert any("brand new fee" in w for w in res.warnings)
    assert not any("differ" in w for w in res.warnings)
    broken = [row[:] for row in rows]
    broken[1][3] = "700"                       # settlement no longer = revenue + fees
    bundle, res = load("Wallet_B_Tiktok_Sep_2026.xlsx", make_xlsx({"Order details": broken}))
    assert any("differ" in w for w in res.warnings)


def test_lazada_wallet_lines_by_transaction_month():
    rows = [["Periode Laporan", "Tanggal Transaksi ", "Nama Biaya", "Jumlah (Termasuk Pajak)"],
            ["x", SEP_5, "Omset Penjualan\n\n", "100000"],
            ["x", SEP_5, "Komisi", "-5000"],
            ["x", SEP_5, "Biaya Transaksi", "-2000"],
            ["x", "02 Oct 2026", "Omset Penjualan", "50000"],
            ["x", "02 Oct 2026", "Biaya Aneh", "-100"]]
    bundle, res = load("Wallet_B_Lazada_Sep_2026.xlsx", make_xlsx({"Income Overview": rows}))
    sep, octo = _wallet(bundle, "Lazada", 202609), _wallet(bundle, "Lazada", 202610)
    assert sep["wallet_gmv"] == 100000 and sep["commission_fee"] == -5000
    assert sep["mp_fee"] == -7000 and sep["other_fee"] == -2000
    assert sep["total_settlement_amount"] == 93000 and octo["total_settlement_amount"] == 49900
    assert any("Biaya Aneh" in w for w in res.warnings)


def test_blibli_wallet_invoice_two_totals_and_paid_month():
    header = [None, None, None, "Trxn", "Nomor pesanan (Order ID)", None,
              "Nomor item pesanan (Order item ID)", "Nama produk (Product name)", "Seller SKU",
              "Jumlah (Quantity)", "Harga satuan (Price per unit)", "Total", "Regular Margin",
              "Special Margin", "Add on Margin", "Total Margin", "Promo shipping (Shipping promo)",
              "Promo biaya pembayaran (Payment fee promo)", "Biaya transaksi (Transaction fee)",
              "PPH 23 (WHT 23)", "Total", "Status", "Tanggal terkirim (Delivered date)",
              "Tanggal terbayar (Paid date)"]
    line = [None, None, None, 1, "12345678", None, "12345678-1", "Prod", "SKU", 1, 73800, 73800,
            3838, 0, 5018, 8856, 0, 0, 0, 0, 64944, "Settled", 46284, 46290]
    rows = [[], [None, None, None, "PT Example"], [], header, line,
            [None, None, None, None, "Grand Total Settlement"] + [None] * 15 + [64944]]
    bundle, res = load("Wallet_B_Blibli_Sep_2026.xlsx", make_xlsx({"Sheet1": rows}))
    row = _wallet(bundle, "Blibli", 202609)
    assert res.used and row["wallet_gmv"] == 73800 and row["commission_fee"] == -8856
    assert row["mp_fee"] == -8856 and row["total_settlement_amount"] == 64944


# ---- inventory, purchase orders, SRP ----
def test_inventory_hpp_fallback_and_top_share():
    rows = [["reporting_date", "SKU", "item_code", "item_name", "recommended action",
             "hpp_after_disc num", "Sales Contribution (value)", "hpp_accurate", "available_qty",
             "orderqty14", "orderqty30", "doi_30"],
            [OCT_21, "SKU-1", "SKU-1_X", "Item", "restock", "10000", "Top 080%", "",
             "100", "10", "20", "45.5"],
            [OCT_21, "SKU-2", "SKU-2_X", "Item 2", "hold", "", "", "12000", "5", "0", "0",
             "999.12345678"]]
    bundle, res = load("BRD Inventory as of 21 Oct 2026.csv", make_csv(rows))
    first, second = rows_of(bundle, "upload_inventory")
    assert res.used and first["report_date"] == dt.date(2026, 10, 21)
    assert first["hpp"] == 10000 and second["hpp"] == 12000
    assert first["sales_contribution"] == pytest.approx(80.0) and second["sales_contribution"] is None
    assert first["doi_30"] == pytest.approx(45.5)


def test_purchase_orders():
    rows = [["created_date", "supplier_code", "reference_number", "warehouse_sku_code",
             "warehouse_sku_name", "warehouse", "gr_date", "ordered_quantity",
             "delivered_quantity", "open_quantity"],
            ["Oct 1, 2026", "SUP", "PO/X/2026/0001", "SKU1", "Item", "WH A", "Oct 5, 2026",
             "100", "90", "10"]]
    bundle, res = load("BRD PO Service Level.csv", make_csv(rows))
    (row,) = rows_of(bundle, "upload_po")
    assert res.used and row["sku"] == "SKU1" and row["open_quantity"] == 10
    assert row["gr_date"] == dt.date(2026, 10, 5)


def test_srp_master_one_row_per_sku_per_priced_day():
    head = ["Acme SKU", "Product Name", "Brand Name", "Product Type", "Composition",
            "SRP Per Date"]
    jan = [head, [None] * 5 + [1, 2, 3], [None] * 5 + [0, 0, 0],
           ["SKU1", "Prod", "Brand", "Type", "Comp", 10000, 10000, None],
           ["SKU2", "Prod 2", "Brand", "Type", "Comp", 0, 5000, 5000]]
    feb = [head, [None] * 5 + [28, 29, 30], [None] * 5 + [0, 0, 0],
           ["SKU1", "Prod", "Brand", "Type", "Comp", 11000, 11000, 11000]]
    data = make_xlsx({"Jan": jan, "Feb": feb, "Notes": [["free text"]]})
    bundle, res = load("SRPMaster_BRAND_2026.xlsx", data)
    rows = rows_of(bundle, "upload_srp")
    assert res.used and len(rows) == 5               # 29 and 30 Feb do not exist
    assert {(r["sku"], r["price_date"]) for r in rows if r["sku"] == "SKU2"} == {
        ("SKU2", dt.date(2026, 1, 2)), ("SKU2", dt.date(2026, 1, 3))}
    assert any("Notes" in w for w in res.warnings)


# ---- files that are not read, zips and limits ----
def test_broken_xls_unknown_and_empty_files_get_reasons():
    bundle = load_files([
        ("Product_B_Lazada_Sep_2026.xls", b"\xd0\xcf\x11\xe0" + b"\x00" * 64),
        ("notes.txt", b"hello world\nnothing to see here\n"),
        ("photo.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32),
        ("empty.csv", b""),
        ("Product_B_Lazada_Aug_2026.xls", b"<html><table><tr><td>1</td></tr></table></html>"),
    ])
    reasons = [f.reason for f in bundle.files]
    assert reasons[0] == xls.UNREADABLE and bundle.files[0].kind == "product"
    assert reasons[1] == UNKNOWN
    assert "not a csv, xlsx or zip" in reasons[2]
    assert reasons[3] == "the file is empty"
    assert reasons[4] == NOT_EXCEL
    assert not any(f.used for f in bundle.files) and bundle.tables == {}


def test_zip_folders_hidden_files_nested_zip_and_duplicates():
    orders = make_xlsx({"orders": SHOPEE_ROWS})
    archive = make_zip({
        "Raw/Monthly Transaction/Shopee/Order_B_Shopee_Sep_2026.xlsx": orders,
        "Raw/Monthly Transaction/Shopee/Order_B_Shopee_Sep_2026 (1).xlsx": orders,
        "Raw/Monthly Transaction/Shopee/Order_B_Shopee_Sep_2026_copy.csv": make_csv(SHOPEE_ROWS),
        "__MACOSX/Raw/._Order_B_Shopee_Sep_2026.xlsx": b"junk",
        "Raw/.DS_Store": b"junk",
        "Raw/inner.zip": make_zip({"a.csv": "a,b\n1,2"}),
    })
    bundle = load_files([("raw.zip", archive)])
    by_name = {f.path.rsplit("/", 1)[-1]: f for f in bundle.files}
    assert set(by_name) == {SHOPEE_XLSX, "Order_B_Shopee_Sep_2026 (1).xlsx",
                            "Order_B_Shopee_Sep_2026_copy.csv", "inner.zip"}
    assert by_name[SHOPEE_XLSX].used
    assert by_name["Order_B_Shopee_Sep_2026 (1).xlsx"].reason.startswith("the same file as")
    assert "already in" in by_name["Order_B_Shopee_Sep_2026_copy.csv"].reason
    assert by_name["inner.zip"].reason == NESTED_ZIP
    assert len(rows_of(bundle, "upload_orders")) == 5
    assert by_name[SHOPEE_XLSX].path.startswith("raw.zip/Raw/")


def test_zip_bomb_guard_entry_cap_and_damaged_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("bomb.csv", b"0" * (3 * 1024 * 1024))
        z.writestr(SHOPEE_CSV, make_csv(SHOPEE_ROWS))
    bundle = load_files([("x.zip", buf.getvalue())])
    bomb, orders = bundle.files
    assert not bomb.used and "expands far more" in bomb.reason
    assert orders.used
    small = load_files([("x.zip", buf.getvalue())], max_entry_bytes=100)
    assert all("larger than" in f.reason for f in small.files)
    _, res = load("broken.zip", b"PK\x03\x04 not really a zip")
    assert "cannot be opened" in res.reason


def test_row_limit_keeps_the_first_rows_and_says_so():
    bundle, res = load(SHOPEE_CSV, make_csv(SHOPEE_ROWS), max_total_rows=2)
    assert len(rows_of(bundle, "upload_orders")) == 2
    assert any("row limit" in w for w in res.warnings)
    bundle = load_files([(SHOPEE_CSV, make_csv(SHOPEE_ROWS)),
                         ("Order_B_Shopee_Aug_2026.csv", make_csv(SHOPEE_ROWS[:1] + [
                             _shopee_line("260801ZZZ999", "Selesai", "2026-08-01 10:00", "P",
                                          ONE_K, "1", "0", "0", ONE_K)]))],
                        max_total_rows=5)
    assert "row limit" in bundle.files[1].reason


def test_a_broken_file_never_raises(monkeypatch):
    def boom(match, ctx):
        raise RuntimeError("parser bug")
    kind = parse_mod.KINDS["orders"]
    monkeypatch.setitem(parse_mod.KINDS, "orders", parse_mod.Kind(kind.table, kind.specs, boom))
    bundle = load_files([(SHOPEE_CSV, make_csv(SHOPEE_ROWS)),
                         ("Live_Official_B_Shopee_Sep_2026.csv", make_csv([
                             [PERIOD_COL, SALES_COL, "Penonton",
                              "Penonton Aktif"], ["01-09-2026", "1", "1", "1"]]))])
    broken, live = bundle.files
    assert not broken.used and "could not be read" in broken.reason
    assert live.used
