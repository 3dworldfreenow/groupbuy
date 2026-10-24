"""Pricing engine. All arithmetic uses Decimal; never floats."""
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from ..constants import VendorStatus
from ..extensions import db
from ..models import VendorProduct, VendorProfile
from ..utils import MONEY_Q, QTY_Q, ZERO, money, qty, to_decimal
from .core import DomainError

HUNDRED = Decimal("100")


def parse_decimal(value, label, minimum=None, maximum=None, places=2, required=True):
    d = to_decimal(value)
    if d is None:
        if required:
            raise DomainError(f"{label} must be a number.")
        return None
    if not d.is_finite():
        raise DomainError(f"{label} must be a number.")
    if minimum is not None and d < minimum:
        raise DomainError(f"{label} must be at least {minimum}.")
    if maximum is not None and d > maximum:
        raise DomainError(f"{label} must be at most {maximum}.")
    q = Decimal(1).scaleb(-places)
    if d != d.quantize(q):
        raise DomainError(f"{label} allows at most {places} decimal places.")
    return d.quantize(q)


def tier_price(vendor_product, quantity):
    """Unit price for ``quantity`` using the best tier whose minimum is met."""
    quantity = to_decimal(quantity, ZERO)
    price = money(vendor_product.price_per_unit)
    for tier in sorted(vendor_product.tiers, key=lambda t: t.min_qty):
        if quantity >= tier.min_qty:
            price = money(tier.price_per_unit)
    return price


def line_total(quantity, unit_price):
    return money(qty(quantity) * money(unit_price))


def percent_of(amount, percent):
    return money(money(amount) * to_decimal(percent, ZERO) / HUNDRED)


def allocate(total, weights, quantum=MONEY_Q):
    """Split ``total`` across ``weights`` proportionally (largest remainder).

    The parts always sum exactly to ``total`` (in units of ``quantum``).
    """
    total = to_decimal(total, ZERO).quantize(quantum, rounding=ROUND_HALF_UP)
    weights = [max(to_decimal(w, ZERO), ZERO) for w in weights]
    if not weights:
        return []
    wsum = sum(weights)
    if wsum == 0:
        weights = [Decimal(1)] * len(weights)
        wsum = Decimal(len(weights))
    raw = [total * w / wsum for w in weights]
    floors = [r.quantize(quantum, rounding=ROUND_DOWN) for r in raw]
    remainder_units = int(((total - sum(floors)) / quantum).to_integral_value())
    order = sorted(range(len(raw)), key=lambda i: (raw[i] - floors[i], weights[i]), reverse=True)
    for i in order[:remainder_units]:
        floors[i] += quantum
    return floors


def allocate_quantities(offered, requested):
    """Allocate an offered quantity across customers' requested quantities.

    Full requests are honoured when the offer covers demand; otherwise each
    customer receives a proportional share (never more than requested).
    """
    offered = qty(offered)
    requested = [qty(r) for r in requested]
    demand = sum(requested, ZERO)
    if offered >= demand:
        return requested
    parts = allocate(offered, requested, quantum=QTY_Q)
    return [min(p, r) for p, r in zip(parts, requested)]


def indicative_prices(product_ids=None):
    """Lowest and highest listed catalogue price per product across approved vendors.

    These are vendor list prices for information only; the final price is set
    by the accepted offer for a group order.
    """
    q = (db.session.query(VendorProduct)
         .join(VendorProfile, VendorProfile.id == VendorProduct.vendor_id)
         .filter(VendorProfile.status == VendorStatus.APPROVED, VendorProduct.is_available.is_(True)))
    if product_ids is not None:
        q = q.filter(VendorProduct.product_id.in_(list(product_ids) or [0]))
    result = {}
    for vp in q.all():
        best = money(vp.price_per_unit)
        for t in vp.tiers:
            best = min(best, money(t.price_per_unit))
        lo, hi = result.get(vp.product_id, (None, None))
        lo = best if lo is None else min(lo, best)
        hi = money(vp.price_per_unit) if hi is None else max(hi, money(vp.price_per_unit))
        result[vp.product_id] = (lo, hi)
    return result


def price_customer_orders(go, bid, customer_orders):
    """Compute item allocations and the price breakdown for every customer order.

    Returns a list of dicts (one per customer order) without mutating anything.
    """
    bid_items = {bi.group_order_item_id: bi for bi in bid.items}
    # Proportional allocation per group order item.
    alloc = {}  # (co.id, goi_id) -> qty
    for goi in go.items:
        if goi.dropped:
            continue
        lines = [(co, it) for co in customer_orders for it in co.items if it.group_order_item_id == goi.id]
        offered = bid_items[goi.id].offered_qty if goi.id in bid_items else ZERO
        parts = allocate_quantities(offered, [it.quantity for _, it in lines])
        for (co, it), part in zip(lines, parts):
            alloc[(co.id, goi.id)] = part

    results = []
    for co in customer_orders:
        lines = []
        subtotal = ZERO
        for it in co.items:
            bi = bid_items.get(it.group_order_item_id)
            allocated = alloc.get((co.id, it.group_order_item_id), ZERO)
            unit = money(bi.unit_price) if bi else ZERO
            lt = line_total(allocated, unit)
            subtotal += lt
            lines.append({"item": it, "allocated_qty": allocated, "unit_price": unit, "line_total": lt})
        results.append({"order": co, "lines": lines, "subtotal": money(subtotal)})

    shares = allocate(bid.delivery_fee, [r["subtotal"] for r in results])
    for r, share in zip(results, shares):
        co = r["order"]
        r["delivery_fee"] = share if r["subtotal"] > 0 else ZERO.quantize(MONEY_Q)
        r["home_delivery_fee"] = money(bid.home_delivery_fee) if co.fulfilment_method == "home" else money(0)
        r["service_fee"] = percent_of(r["subtotal"], go.service_fee_percent)
        r["total"] = money(r["subtotal"] + r["delivery_fee"] + r["home_delivery_fee"] + r["service_fee"])
    # If some orders received nothing, re-spread their unused delivery share.
    unused = money(bid.delivery_fee) - sum((r["delivery_fee"] for r in results), ZERO)
    if unused > 0:
        payers = [r for r in results if r["subtotal"] > 0]
        for r, extra in zip(payers, allocate(unused, [p["subtotal"] for p in payers])):
            r["delivery_fee"] += extra
            r["total"] += extra
    return results


def bid_customer_cost(go, bid):
    """Total cost to customers for a bid (goods + fees), used for comparisons."""
    home_count = sum(1 for co in go.live_customer_orders if co.fulfilment_method == "home")
    goods = money(bid.subtotal)
    service = percent_of(goods, go.service_fee_percent)
    return money(goods + money(bid.delivery_fee) + money(bid.home_delivery_fee) * home_count + service)


def apply_pricing(co, priced):
    for line in priced["lines"]:
        it = line["item"]
        it.allocated_qty = line["allocated_qty"]
        it.unit_price = line["unit_price"]
        it.line_total = line["line_total"]
    co.subtotal = priced["subtotal"]
    co.delivery_fee = priced["delivery_fee"]
    co.home_delivery_fee = priced["home_delivery_fee"]
    co.service_fee = priced["service_fee"]
    co.total = priced["total"]


def clear_pricing(co):
    for it in co.items:
        it.allocated_qty = None
        it.unit_price = None
        it.line_total = None
    co.subtotal = co.delivery_fee = co.home_delivery_fee = co.service_fee = co.total = money(0)
