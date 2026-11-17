"""Settlement / wallet exports -> acme_store_wallet_mp, one row per marketplace and month.

Every marketplace's settlement is a list of money lines. Each known line maps to one table
column (the dicts below, one comment per line); lines that are marketplace fees also count
in ``mp_fee`` ("marketplace fees, total", affiliate commission kept apart, the way the MBR
economics page adds them). A line with no column of its own goes to ``other_fee`` and is
named in a warning, so nothing lands silently in a named fee. Signs are the export's own:
negative is money taken from the seller. Blibli's invoice is the one export that prints its
deductions as positive amounts next to a net total; they are negated so the table keeps the
same sign rule for every marketplace.

The month is the SETTLEMENT month (when the money was released), not the order month.
"""
from __future__ import annotations

from collections import defaultdict

from . import values as V
from .schema import TABLES
from .sheets import Spec, col, records

MP = "mp"            # the line also counts in mp_fee
_OTHER = "other_fee"
# Revenue-side columns, and discounts the TikTok export lists only for information.
_NOT_FEES = {"total_settlement_amount", "revenue_wallet", "wallet_gmv", "seller_voucher",
             "platform_subsidi"}
_MONEY = tuple(c for c, t in TABLES["acme_store_wallet_mp"]
               if t == "DOUBLE")

# ---- Shopee: the Summary sheet of the income report ("Laporan Penghasilan") ----
# Group rows have their label in column A and the amount in column D; detail rows have the
# label in column B and the amount in column C. Totals are read from the group rows, fees
# from the detail rows, so no line is counted twice.
SHOPEE_TOTALS = {
    "1 total pendapatan": ("revenue_wallet",),          # revenue after refunds and vouchers
    "total biaya pengiriman": ("shipping_fee",),         # net of every shipping detail line
    "3 total yang dilepas": ("total_settlement_amount",),
}
SHOPEE_LINES = {
    "harga asli produk": ("wallet_gmv",),               # item value (see the next line)
    # The layout used until mid 2026 lists the price before the seller's product discount
    # and the discount apart; the newer layout nets it into "Harga Asli Produk". Adding the
    # (negative) discount gives the item value after the seller discount in both.
    "total diskon produk": ("wallet_gmv",),
    "voucher disponsor oleh penjual": ("seller_voucher",),
    "voucher co fund disponsor oleh penjual": ("seller_voucher",),
    "diskon produk dari shopee": ("platform_subsidi",),
    "biaya administrasi termasuk ppn 11": ("commission_fee", MP),
    "biaya proses pesanan": ("order_processing_fee", MP),
    "biaya kampanye": ("promotion_fee", MP),
    "biaya komisi ams": ("affiliate_fee",),             # Shopee's affiliate programme
    "biaya pembayaran": (_OTHER, MP),                   # payment fee: no column of its own
    "spaylater xtra 0 service fee": (_OTHER, MP),
    "biaya transaksi": (_OTHER, MP),
    "biaya layanan promo xtra": (_OTHER, MP),
    "biaya layanan": (_OTHER, MP),                      # older layout: one service-fee line
    "biaya program hemat biaya kirim": (_OTHER, MP),
    "cashback koin disponsori penjual": (_OTHER,),      # seller-funded coins, not a voucher
    "cashback koin co fund disponsori penjual": (_OTHER,),
    "biaya isi saldo otomatis dari penghasilan": (_OTHER,),   # ads balance top-up
    "premi": (_OTHER,),                                 # shipping insurance premium
    "pph 22": (_OTHER,),                                # income tax withheld
}
SHOPEE_PREFIX = {
    "biaya gratis ongkir xtra": (_OTHER, MP),           # one line per parcel size category
    "biaya layanan pre order": (_OTHER, MP),            # pre-order service fee, per channel
    "layanan produk pre order": (_OTHER, MP),
}
SHOPEE_IGNORED = {   # detail lines already inside a total read above
    "jumlah pengembalian dana ke pembeli", "ongkir dibayar pembeli",
    "ongkir yang diteruskan oleh shopee ke jasa kirim", "diskon ongkir ditanggung jasa kirim",
    "gratis ongkir dari shopee", "ongkos kirim pengembalian barang",
    "kembali ke biaya pengiriman pengirim", "pengembalian biaya kirim",
}

# ---- TikTok Shop / Tokopedia: the "Order details" sheet, one row per order settled ----
TIKTOK_COLUMNS = {
    "total settlement amount": ("total_settlement_amount",),
    "total revenue": ("revenue_wallet",),
    "subtotal after seller discounts": ("wallet_gmv",),
    "platform commission fee": ("commission_fee", MP),
    "mall service fee": ("os_fee", MP),                 # the Mall (official store) fee
    "dynamic commission": ("dynamic_fee", MP),
    "order processing fee": ("order_processing_fee", MP),
    "campaign resource fee": ("promotion_fee", MP),
    "brands crazy deals flash sale service fee": ("promotion_fee", MP),
    "shipping cost": ("shipping_fee",),                 # net of the shipping detail columns
    "affiliate commission": ("affiliate_fee",),
    "affiliate partner commission": ("affiliate_fee",),
    "affiliate shop ads commission": ("affiliate_fee",),
    "affiliate partner shop ads commission": ("affiliate_fee",),
    "affiliate commission deposit": ("affiliate_fee",),
    "affiliate commission refund": ("affiliate_fee",),
    "seller co funded voucher discount": ("seller_voucher",),
    "seller co funded voucher discount refund": ("seller_voucher",),
    "refund of seller co funded voucher discount": ("seller_voucher",),     # older name
    "platform discount": ("platform_subsidi",),
    "platform discounts": ("platform_subsidi",),                            # older name
    "platform discount refund": ("platform_subsidi",),
    "refund of platform discounts": ("platform_subsidi",),
    "platform co funded voucher discount": ("platform_subsidi",),
    "platform co funded voucher discounts": ("platform_subsidi",),
    "platform co funded voucher discount refund": ("platform_subsidi",),
    "refund of platform co funded voucher discounts": ("platform_subsidi",),
    "bonus cashback service fee": (_OTHER, MP),
    "exclusive benefits access fee": (_OTHER, MP),
    "voucher xtra service fee": (_OTHER, MP),
    "pre order service fee": (_OTHER, MP),
    "payment fee": (_OTHER, MP),
    "credit card installment handling fee": (_OTHER, MP),
    "shipping fee program service fee": (_OTHER, MP),
    "growth xtra program service fee": (_OTHER, MP),
    "live specials service fee": (_OTHER, MP),
    "growth xtra program super service fee": (_OTHER, MP),
    "eams program service fee": (_OTHER, MP),
    "dilayani tokopedia fee": (_OTHER, MP),
    "dilayani tokopedia handling fee": (_OTHER, MP),
    "paylater program fee": (_OTHER, MP),
    "installation service fee": (_OTHER, MP),
    "platform special service fee": (_OTHER, MP),
    "managed service plan per order fee": (_OTHER, MP),
    "gmv max ad fee": (_OTHER,),                        # ads paid from the settlement
    "gmv max coupon": (_OTHER,),
    "gmv max coupon sales tax": (_OTHER,),
    "managed service plan sales tax": (_OTHER,),
    "article 22 income tax withheld": (_OTHER,),
    "failed delivery shipping fee": (_OTHER,),
    "buyer fault return shipping fee": (_OTHER,),
    "insurance fee": (_OTHER,),
    "adjustment amount": (_OTHER,),                     # platform adjustments to the payout
    "ajustment amount": (_OTHER,),                      # the same, misspelt by older exports
}
# Informational, or already inside a column above. Checked on real exports: settlement
# minus revenue equals the sum of the fee columns mapped above, without these.
TIKTOK_IGNORED = {
    "subtotal before discount", "subtotal before discounts", "seller discounts",
    "refund amount after discount", "refund amount before discount",
    "refund subtotal after seller discounts", "refund subtotal before seller discounts",
    "refund of seller discounts", "total fees", "estimated package weight g",
    "actual package weight g", "customer shipping cost before discounts",
    "commission fee before savings", "savings from ad spend", "other commission savings",
    "distance item fee from horizon program", "customer payment", "customer refund",
    "seller shipping cost discount", "estimated package weight", "chargeable package weight",
    "shipping costs passed on to the logistics provider",
    "replacement shipping fee passed on to the customer",
    "exchange shipping fee passed on to the customer", "shipping cost borne by the platform",
    "shipping cost paid by the customer", "refunded shipping cost paid by the customer",
    "return shipping costs passed on to the customer", "shipping cost subsidy",
    "distance shipping fee from horizon program", "logistics service fee",
    "insurance reimbursement", "order adjustment id", "related order id",
}

# ---- Lazada: one row per fee line ("Nama Biaya" / "Fee Name") ----
LAZADA_FEES = {
    "omset penjualan": ("wallet_gmv", "revenue_wallet"),
    "item price credit": ("wallet_gmv", "revenue_wallet"),
    "pemotongan omset penjualan karena retur": ("revenue_wallet",),   # returns
    "reversal item price": ("revenue_wallet",),
    "komisi": ("commission_fee", MP),
    "commission": ("commission_fee", MP),
    "komisi retur": ("commission_fee", MP),
    "reversal commission": ("commission_fee", MP),
    "order processing fee": ("order_processing_fee", MP),
    "reversal order processing fee": ("order_processing_fee", MP),
    "biaya promosi voucher": ("seller_voucher",),
    "promotional charges vouchers": ("seller_voucher",),
    "biaya afiliasi bersponsor": ("affiliate_fee",),
    "sponsored affiliates": ("affiliate_fee",),
    "biaya pengiriman pengembalian barang pelanggan": ("shipping_fee",),
    "wrong shipping fee adjustment": ("shipping_fee",),
    "biaya transaksi": (_OTHER, MP),                   # payment fee
    "payment fee": (_OTHER, MP),
    "biaya free shipping max": (_OTHER, MP),           # free-shipping programme fee
    "free shipping max fee": (_OTHER, MP),
    "seller virtual credit co fund voucher": (_OTHER,),
    "lazada funded commission from seller virtual credit": (_OTHER,),
}

# ---- Blibli: the settlement invoice, one row per order item ----
BLIBLI_COLUMNS = {   # index of the "Total" column: 0 = gross item value, 1 = net paid out
    "gross": ("wallet_gmv", "revenue_wallet"),
    "net": ("total_settlement_amount",),
    "total margin": ("commission_fee", MP),            # Blibli's commission is its margin
    "promo shipping shipping promo": (_OTHER, MP),
    "promo biaya pembayaran payment fee promo": (_OTHER, MP),
    "biaya transaksi transaction fee": (_OTHER, MP),
    "pph 23 wht 23": (_OTHER,),                        # tax withheld, refunded later
}
BLIBLI_DEDUCTIONS = ("total margin", "promo shipping shipping promo",
                     "promo biaya pembayaran payment fee promo", "biaya transaksi transaction fee",
                     "pph 23 wht 23")


class _Months:
    """Running sums per (marketplace, month_id), plus the lines that went to other_fee."""

    def __init__(self):
        self.sums: dict = defaultdict(lambda: defaultdict(float))
        self.fees: dict = defaultdict(float)     # every line that is not revenue-side
        self.other: dict[str, float] = defaultdict(float)
        self.unknown: dict[str, float] = defaultdict(float)

    def add(self, marketplace: str, month: int, target: tuple, amount: float, label: str):
        row = self.sums[(marketplace, month)]
        for column in target:
            if column == MP:
                row["mp_fee"] += amount
            else:
                row[column] += amount
        if not set(target) & _NOT_FEES:
            self.fees[(marketplace, month)] += amount
        if _OTHER in target:
            self.other[label] += amount

    def unknown_line(self, marketplace: str, month: int, amount: float, label: str):
        self.sums[(marketplace, month)][_OTHER] += amount
        self.fees[(marketplace, month)] += amount
        self.unknown[label] += amount

    def reconcile(self) -> list[str]:
        """Settlement minus revenue must equal the fee lines; say so when it does not."""
        notes = []
        for key, sums in sorted(self.sums.items()):
            gap = sums["total_settlement_amount"] - sums["revenue_wallet"] - self.fees[key]
            if abs(gap) > 1:
                notes.append(f"{key[0]} {key[1]}: the fee lines differ from settlement minus "
                             f"revenue by {gap:,.0f}")
        return notes

    def rows(self) -> list[dict]:
        out = []
        for (marketplace, month), sums in sorted(self.sums.items()):
            row = dict.fromkeys(c for c, _ in TABLES["acme_store_wallet_mp"])
            row.update(marketplace=marketplace, month_id=month)
            row.update({c: sums.get(c, 0.0) for c in _MONEY})
            out.append(row)
        return out

    def warnings(self) -> list[str]:
        notes = []
        if self.other:
            names = ", ".join(sorted(self.other))
            notes.append(f"counted in other fees (no column of their own): {names}")
        if self.unknown:
            names = ", ".join(sorted(self.unknown))
            notes.append(f"settlement lines Poros does not know, counted in other fees: {names}")
        return notes


# ---- Shopee ----
SHOPEE_SPEC = Spec("shopee_wallet", "wallet", "Shopee", {
    "label": col("Rincian Laporan"),
}, required=("label",), key="label", sheets=(V.norm("Summary"),))


def _shopee_month(rows: list, fallback: int | None) -> tuple[int | None, list[str]]:
    dates = {}
    for row in rows:
        label = V.norm(row[0]) if row else ""
        if label in ("dari", "ke") and len(row) > 1:
            dates[label] = V.date(row[1])
    start, end = dates.get("dari"), dates.get("ke")
    if start and end and V.month_id(start) == V.month_id(end):
        return V.month_id(start), []
    if start and end:
        return fallback, [f"the report runs {start} to {end}; it is filed under its name's month"]
    return fallback, []


def _shopee_line(row) -> tuple[str, float | None, bool]:
    """(label, amount, is_group_row) of one summary row."""
    if len(row) >= 4 and V.text(row[0]) and row[3] is not None:
        return V.norm(row[0]), V.number(row[3]), True
    if len(row) >= 3 and V.text(row[1]):
        return V.norm(row[1]), V.number(row[2]), False
    return "", None, False


def _shopee_target(label: str, group: bool):
    if group:
        return SHOPEE_TOTALS.get(label, ())
    if label in SHOPEE_LINES:
        return SHOPEE_LINES[label]
    for prefix, target in SHOPEE_PREFIX.items():
        if label.startswith(prefix):
            return target
    return None if label not in SHOPEE_IGNORED else ()


def build_shopee(match, ctx) -> tuple[list[dict], list[str]]:
    rows = match.title_rows + [match.header] + list(match.rows)
    month, notes = _shopee_month(rows, ctx.month_id)
    acc = _Months()
    for row in rows:
        label, amount, group = _shopee_line(row)
        if not label or amount is None:
            continue
        target = _shopee_target(label, group)
        if target is None:
            acc.unknown_line("Shopee", month, amount, V.text(row[1]) or label)
        elif target:
            acc.add("Shopee", month, target, amount, label)
    return acc.rows(), notes + acc.warnings()


# ---- TikTok / Tokopedia ----
TIKTOK_SPEC = Spec("tiktok_wallet", "wallet", "TikTok", {
    "settled": col("Order settled time", kind="date"),
    "source": col("Order source"),
    "total": col("Total settlement amount", kind="num"),
}, required=("settled", "total"), key="settled", sheets=(V.norm("Order details"),))


def _tiktok_marketplace(source) -> str:
    # Adjustments carry no order source; they belong to the TikTok Shop wallet.
    return "Tokopedia" if "tokopedia" in str(source or "").lower() else "TikTok"


def build_tiktok(match, ctx) -> tuple[list[dict], list[str]]:
    header = [V.norm(h) for h in match.header]
    settled_i = match.columns["settled"][0]
    source_i = match.columns.get("source", [None])[0]
    acc = _Months()
    for row in match.rows:
        settled = V.date(_cell(row, settled_i))
        if not settled:
            continue
        key = (_tiktok_marketplace(_cell(row, source_i)), V.month_id(settled))
        for i, name in enumerate(header):
            _tiktok_cell(acc, key, name, _cell(row, i))
    return acc.rows(), acc.warnings() + acc.reconcile()


def _tiktok_cell(acc: _Months, key: tuple, name: str, value) -> None:
    if not name or name in TIKTOK_IGNORED or name in ("order settled time", "order created time"):
        return
    amount = V.number(value, ".")        # this export writes plain numbers, "." decimals
    if not amount:
        return
    target = TIKTOK_COLUMNS.get(name)
    if target is None:
        acc.unknown_line(key[0], key[1], amount, name)
    else:
        acc.add(key[0], key[1], target, amount, name)


# ---- Lazada ----
LAZADA_SPEC = Spec("lazada_wallet", "wallet", "Lazada", {
    "fee": col("Nama Biaya", "Fee Name"),
    "amount": col("Jumlah (Termasuk Pajak)", "Amount", kind="num"),
    "date": col("Tanggal Transaksi", "Transaction Date", kind="date"),
    "released": col("Tanggal Dilepas", kind="date"),
}, required=("fee", "amount"), key="fee")


def build_lazada(match, ctx) -> tuple[list[dict], list[str]]:
    acc = _Months()
    for rec in records(match):
        when = rec.get("date") or rec.get("released")
        month = V.month_id(when) or ctx.month_id
        amount = rec.get("amount")
        if amount is None:
            continue
        label = V.norm(rec["fee"])
        target = LAZADA_FEES.get(label)
        # Every line is money in or out of the seller's balance: together they are the payout.
        acc.add("Lazada", month, ("total_settlement_amount",), amount, "")
        if target is None:
            acc.unknown_line("Lazada", month, amount, rec["fee"])
        else:
            acc.add("Lazada", month, target, amount, label)
    return acc.rows(), acc.warnings()


# ---- Blibli ----
BLIBLI_SPEC = Spec("blibli_wallet", "wallet", "Blibli", {
    "order_id": col("Nomor pesanan (Order ID)"),
    "margin": col("Total Margin", kind="num"),
    "paid": col("Tanggal terbayar (Paid date)", kind="date"),
}, required=("order_id", "margin"), key="order_id", key_pattern=r"\d")


def _blibli_columns(header: list) -> dict[str, int]:
    names = [V.norm(h) for h in header]
    totals = [i for i, n in enumerate(names) if n == "total"]
    out = {n: i for i, n in enumerate(names) if n in BLIBLI_COLUMNS}
    if totals:
        out["gross"] = totals[0]
    if len(totals) > 1:
        out["net"] = totals[1]
    return out


def build_blibli(match, ctx) -> tuple[list[dict], list[str]]:
    where = _blibli_columns(match.header)
    order_i = match.columns["order_id"][0]
    paid_i = match.columns.get("paid", [None])[0]
    acc = _Months()
    for row in match.rows:
        order = V.text(_cell(row, order_i)) or ""
        if not any(ch.isdigit() for ch in order):
            continue                 # blank, "Grand Total Settlement" and footer rows
        month = V.month_id(V.date(_cell(row, paid_i))) or ctx.month_id
        _blibli_row(acc, row, where, month)
    return acc.rows(), acc.warnings()


def _blibli_row(acc: _Months, row: list, where: dict[str, int], month) -> None:
    for name, i in where.items():
        amount = V.number(_cell(row, i))
        if not amount:
            continue
        if name in BLIBLI_DEDUCTIONS:
            amount = -amount     # the export writes margins and fees as positive deductions
        acc.add("Blibli", month, BLIBLI_COLUMNS[name], amount, name)


def _cell(row: list, i: int | None):
    """The row's value at column ``i``; None when the column is missing or the row short."""
    return row[i] if i is not None and i < len(row) else None


SPECS = [SHOPEE_SPEC, TIKTOK_SPEC, LAZADA_SPEC, BLIBLI_SPEC]
_BUILDERS = {"shopee_wallet": build_shopee, "tiktok_wallet": build_tiktok,
             "lazada_wallet": build_lazada, "blibli_wallet": build_blibli}


def build(match, ctx) -> tuple[list[dict], list[str]]:
    return _BUILDERS[match.spec.name](match, ctx)
