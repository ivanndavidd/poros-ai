"""What each missing item will say once its data arrives: the good, bad and neutral drafts.

The production contract (section 11) and the executive framework (section 15) say a
missing input must not stop at "XX": the producer drafts in advance what will be said if
the evidence turns out favourable, unfavourable or mixed, so the page can be finished the
day the data lands. These drafts are fixed per page, written in plain business language
and carry NO figure (a number here would be invented); they stay explicitly conditional.

Each item also gets the kind of visual the page will hold (``ghost``), which the deck
draws as a grey outline with XX values while the data is pending.
"""
from __future__ import annotations

# kind of visual the page will hold once its data arrives
GHOST = {
    "scorecard": "column", "monthly_trend": "line", "daily": "column", "after_acme": "column",
    "marketplace": "column", "traffic": "line", "traffic:funnel": "funnel",
    "price_vs_competitor": "table", "products": "column", "products:traffic": "table",
    "product_funnel": "matrix",
    "margin": "column", "creators": "table", "live_video": "column", "ads": "column",
    "mp_economics": "column", "fulfilment": "column", "cancellation": "column",
    "payment": "column", "geography": "column", "geography:delivery": "table",
    "customers": "column",
    "customer_service": "line", "inventory": "table", "month_so_far": "column",
    "month_ahead": "column", "month_ahead:plan": "waterfall",
}

# the visual named on the page when the item itself does not say
VISUAL = {
    "monthly_trend": "GMV and NMV by month for 13 months",
    "traffic": "visitors, conversion and NMV by month",
    "product_funnel": "products by traffic and conversion in four groups",
    "margin": "NMV, cost of goods and margin by month",
    "creators": "top creators by orders and GMV, month on month",
    "live_video": "LIVE and video orders and GMV by month",
    "ads": "ads spend and the GMV it returned, month on month",
    "mp_economics": "fees as a share of settled GMV by month",
    "fulfilment": "orders by time to ready to ship, month on month",
    "cancellation": "cancellations by who caused them, month on month",
    "payment": "NMV and cancel rate by payment method",
    "geography": "NMV and delivery time by area",
    "customers": "new and returning buyers by month",
    "customer_service": "inbound lead time and chat response time",
    "inventory": "stock value, units and days of cover per product",
    "month_so_far": "NMV a day so far against the review month",
}

# (if good, if bad, if neutral); {brand}, {month} and {next} are filled per report
DRAFTS = {
    "scorecard": (
        "NMV reached XX% of the {month} target: the plan held, and this run rate is the base "
        "for the {next} target.",
        "NMV reached only XX% of the {month} target: the gap is named lever by lever on the C "
        "pages, with the recovery plan and its owner.",
        "NMV landed close to the {month} target: on plan, so the question becomes which lever "
        "lifts {next}."),
    "monthly_trend": (
        "The 13 month line rises into {month}: momentum is building and the latest month "
        "sets a new base.",
        "The 13 month line falls into {month}: the decline is named and the C pages say which "
        "lever reverses it.",
        "The 13 month line is flat into {month}: the store holds its level, and growth has to "
        "come from a new lever."),
    "daily": (
        "Campaign days lift NMV a day well above other days: the calendar works, and the next "
        "campaign repeats the same mechanics.",
        "Campaign days barely lift NMV above other days: the mechanics or the stock behind them "
        "change before the next campaign.",
        "Campaign days lift NMV a little: they hold the month up without changing its level, so "
        "ordinary days need a lever too."),
    "after_acme": (
        "An average month with Acme beats the months before the takeover: Acme lifted the store, "
        "and that gain is the base of the next plan.",
        "An average month with Acme trails the months before the takeover: the handover cost "
        "momentum, and the recovery plan names what Acme changes and by when.",
        "An average month with Acme matches the months before: Acme held the store through the "
        "handover, and growth now comes from the levers on the C pages."),
    "marketplace": (
        "LIVE, video and affiliate orders grow their share: content is bringing new demand "
        "inside the marketplace.",
        "Product card orders carry the store while content channels shrink: demand rests on "
        "search, and the content engine needs restarting.",
        "The channel mix holds: growth came through the same channels, so the next choice is "
        "which one to scale."),
    "traffic": (
        "Visitors and conversion both rise: the store pulls more buyers and turns more of them "
        "into orders.",
        "Visitors or conversion fall: the page names which one, and the fix targets that one "
        "first.",
        "Visitors and conversion hold: NMV moved on price or basket size, not on traffic, so the "
        "next lever is reach."),
    "traffic:funnel": (
        "One traffic source turns clicks into orders far better than the rest: budget and "
        "content move towards it.",
        "Every source loses buyers at the same step: the fix sits on the product page or the "
        "price, not in buying more traffic.",
        "Sources convert alike: the mix question becomes cost per buyer, not conversion."),
    "price_vs_competitor": (
        "Our selling price sits at or below Shopee after seller discount on most pareto "
        "products: price is not what holds conversion back.",
        "Our selling price sits above Shopee on the pareto products: price explains part of the "
        "conversion gap, and the ask is funded parity on the focus products.",
        "Our price matches Shopee on most products with a few gaps: the gaps are listed product "
        "by product and closed one at a time."),
    "products": (
        "Focus products grow their share of NMV: the mix moves the way {brand} wants it to.",
        "Focus products lose share of NMV: the store grows on products {brand} does not "
        "prioritise, so the plan moves traffic and stock to the focus range.",
        "Focus products hold their share: on plan, and the next step is a lever that lifts them "
        "faster than the rest."),
    "products:traffic": (
        "The products that made 80% of NMV gained page views and held conversion: demand grew, "
        "not only price.",
        "The products that made 80% of NMV lost page views while their price rose: NMV leans on "
        "price, and traffic is the next lever to fix.",
        "Page views held flat on the main products: the NMV change came from price and "
        "conversion, which the price table already shows."),
    "product_funnel": (
        "Star products carry most of the revenue and a few Hidden gems are ready for more "
        "traffic: the next move is to feed them.",
        "Rough diamonds take most of the traffic without converting: page, price or offer is "
        "fixed before more traffic is bought.",
        "Traffic and conversion are spread evenly: no product stands out, so the plan picks two "
        "to test."),
    "margin": (
        "Margin after cost of goods holds or improves: growth is profitable and the mix can "
        "carry more spend.",
        "Margin after cost of goods thins as sales grow: growth is bought with discount or mix, "
        "and the plan protects margin on the hero products.",
        "Margin holds level: sales and cost moved together; the next question is which products "
        "carry it."),
    "creators": (
        "Creators bring a growing share of orders at a commission the margin can carry: the "
        "best ones are scaled.",
        "Creator orders shrink or cost more than they return: commission is renegotiated or the "
        "weakest creators are replaced.",
        "Creator orders hold: steady but not growing, so the plan adds creators for the focus "
        "products."),
    "live_video": (
        "LIVE and video orders grow: content is becoming a sales channel of its own.",
        "LIVE and video orders fall: the content calendar slipped, and the plan restores it "
        "first.",
        "LIVE and video orders hold: content keeps its share; the next step ties it to the "
        "focus products."),
    "ads": (
        "Ads return more GMV for every rupiah spent: the budget can grow on the best campaigns.",
        "Ads return less for every rupiah spent: the weakest campaigns are cut and the budget "
        "moves to those that return more.",
        "Ads return about the same: spend and return moved together, so a shift is tested "
        "before any budget is added."),
    "mp_economics": (
        "Fees take a smaller share of settled GMV: more of each sale reaches the brand.",
        "Fees take a larger share of settled GMV: the new fee lines are named, and price or the "
        "programme mix absorbs them.",
        "Fees take the same share: the economics of a sale did not change this month."),
    "mp_economics:voucher": (
        "The corrected voucher figure is small against settled GMV: vouchers are not what moves "
        "the economics.",
        "The corrected voucher figure is large against settled GMV: vouchers are a real cost "
        "line and the plan caps them.",
        "The corrected voucher figure is in line with past months: it is tracked, no action."),
    "fulfilment": (
        "More orders are ready to ship the same day: fulfilment is a strength to show the brand.",
        "Fewer orders are ready to ship the same day: the delay is traced to the warehouse step "
        "and fixed by a date.",
        "Fulfilment speed holds: the store keeps its service level."),
    "cancellation": (
        "Seller cancellations fall: fewer sales are lost to stock or fulfilment.",
        "Seller cancellations rise: the top reason is named, with the fix and its owner.",
        "Cancellations hold: no new cause appears this month."),
    "payment": (
        "Cash on delivery keeps a low cancel rate: it widens reach without losing orders.",
        "Cash on delivery orders cancel far more than others: it is limited where the losses "
        "concentrate.",
        "The payment mix holds: no action this month."),
    "geography": (
        "Orders outside Java grow and arrive on time: the store reaches new areas without a "
        "delivery penalty.",
        "Areas with slow delivery lose share: delivery time, not demand, is the brake there.",
        "The area mix holds: the store sells where it sold before."),
    "geography:delivery": (
        "Delivery outside Java is close to Java's: distance is not holding the store back.",
        "Delivery outside Java takes far longer and those areas lose share: the courier or "
        "warehouse choice is fixed there before more demand is bought.",
        "Delivery times hold by area: no change to logistics this month."),
    "customers": (
        "New buyers grow and more of them come back: the base widens and sticks.",
        "New buyers fall or do not come back: growth leans on the same buyers, so the plan "
        "funds acquisition.",
        "New and returning buyers hold: the base is stable."),
    "customer_service": (
        "Stock arrives in time and chats are answered fast: operations support the sales plan.",
        "Inbound is slow or chats wait long: the delay is traced and fixed before the next "
        "campaign.",
        "Inbound and service hold: no operational brake this month."),
    "inventory": (
        "Stock covers the plan on every focus product: nothing holds {next} back.",
        "Focus products run short against the plan: the purchase order is the first ask of "
        "{brand}.",
        "Stock covers most products with a few at risk: those are listed with their reorder "
        "date."),
    "month_so_far": (
        "{next} opens above the {month} daily pace: the month is on track.",
        "{next} opens below the {month} daily pace: the plan's first move starts now.",
        "{next} opens at the {month} pace: on track only if the planned levers start this "
        "week."),
    "month_ahead": (
        "The current pace reaches the {next} target: the plan protects it.",
        "The current pace falls short of the {next} target: the gap a day is named, with each "
        "lever's share of it.",
        "The current pace lands near the {next} target: the plan needs one more lever to be "
        "safe."),
    "month_ahead:plan": (
        "The building blocks add up to the target with room to spare: the plan is funded.",
        "The building blocks fall short: the gap the levers cannot close is shown as a block "
        "the brand funds.",
        "The building blocks just reach the target: every lever has to land."),
}

# pages that explain the report itself have nothing to draft
_SKIP = {"synthesis", "gmv_definition", "reconciliation", "status_map", "orders"}


def _brand(brand) -> str:
    b = str(brand or "")
    return b.title() if b.isupper() else b


def attach(sections: list, brand, month, nxt) -> None:
    """Give every open item its three drafts, its intended visual and its ghost kind."""
    values = {"brand": _brand(brand), "month": month.label, "next": nxt.label}
    for sec in sections:
        sid = sec.get("id")
        if sid in _SKIP:
            continue
        for item in sec.get("missing") or []:
            if item.get("what") != "section computation":
                _draft_item(item, sid, values)


def _draft_item(item: dict, sid: str, values: dict) -> None:
    key = f"{sid}:{item['key']}" if item.get("key") else sid
    drafts = DRAFTS.get(key) or DRAFTS.get(sid)
    if not drafts:
        return
    good, bad, neutral = (t.format(**values) for t in drafts)
    item.update({"if_good": good, "if_bad": bad, "if_neutral": neutral})
    item["ghost"] = GHOST.get(key) or GHOST.get(sid) or "column"
    if not item.get("visual") and VISUAL.get(sid):
        item["visual"] = VISUAL[sid]
