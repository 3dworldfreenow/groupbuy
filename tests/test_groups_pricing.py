"""Groups, membership, aggregation, limits, cutoff enforcement and the pricing engine."""
from datetime import timedelta
from decimal import Decimal

import pytest

from app.constants import COStatus, GOStatus
from app.services import groups as group_svc
from app.services.core import DomainError, PermissionDenied
from app.services.pricing import allocate, allocate_quantities, price_customer_orders, tier_price
from app.models import PriceTier, VendorProduct
from app.utils import utcnow
from tests.conftest import login


def test_create_group_via_http_and_membership(client, f, db):
    alice = f.customer("Alice")
    login(client, alice.email)
    loc = f.location()
    now = utcnow() + timedelta(hours=5, minutes=30)  # app timezone is IST (UTC+5:30)
    fmt = "%Y-%m-%dT%H:%M"
    r = client.post("/groups/new", data={
        "name": "Maple Street", "description": "Neighbours", "location_id": loc.id,
        "pickup_point_id": loc.pickup_points[0].id, "is_public": "y", "title": "First round",
        "order_deadline": (now + timedelta(days=1)).strftime(fmt),
        "delivery_window_start": (now + timedelta(days=2)).strftime(fmt),
        "delivery_window_end": (now + timedelta(days=2, hours=3)).strftime(fmt),
        "fulfilment_options": "both", "selection_mode": "organiser",
    })
    assert r.status_code == 302, r.data
    from app.models import BuyingGroup
    g = db.session.query(BuyingGroup).filter_by(name="Maple Street").one()
    assert g.organiser_id == alice.id and g.membership_for(alice).role == "organiser"
    assert g.current_order.status == GOStatus.DRAFT
    bob = f.customer("Bob")
    client.post("/logout")
    login(client, bob.email)
    assert client.post(f"/join/{g.invite_code}").status_code == 302
    assert g.membership_for(bob) is not None


def test_aggregate_demand_matches_sample(scenario):
    go = scenario["go"]
    agg = group_svc.aggregate_demand(go)
    by_slug = {i.product.slug: agg[i.id] for i in go.items}
    assert by_slug == {"potatoes": Decimal("100.000"), "onions": Decimal("60.000"), "tomatoes": Decimal("40.000")}
    prog = group_svc.progress(go)
    assert prog["total_qty"] == Decimal("200.000") and prog["buyers"] == 3


def test_quantity_update_replaces_and_removes(scenario, f):
    go, alice = scenario["go"], scenario["alice"]
    f.order(go, alice, potatoes="10", onions="0", tomatoes="15")
    agg = {i.product.slug: v for i in go.items for k, v in group_svc.aggregate_demand(go).items() if k == i.id}
    assert agg["potatoes"] == Decimal("70.000") and agg["onions"] == Decimal("40.000")


def test_per_customer_limit_step_and_minimum(scenario, f):
    go, alice = scenario["go"], scenario["alice"]
    with pytest.raises(DomainError, match="per-customer limit"):
        f.order(go, alice, potatoes="51")   # product max per customer is 50
    with pytest.raises(DomainError, match="steps of"):
        f.order(go, alice, potatoes="10.3")
    with pytest.raises(DomainError, match="Minimum order"):
        f.order(go, alice, tomatoes="0.25")


def test_over_ordering_prevented_by_group_maximum(scenario, f, db):
    go = scenario["go"]
    dave = f.customer("Dave")
    group_svc.join_group(dave, go.group)
    db.session.commit()
    # tomatoes: max 60, already 40 ordered → only 20 remain
    with pytest.raises(DomainError, match="Only 20"):
        f.order(go, dave, tomatoes="21")
    db.session.rollback()
    f.order(go, dave, tomatoes="20")
    assert sum(group_svc.aggregate_demand(go).values()) == Decimal("220.000")


def test_order_cutoff_enforced(scenario, f, db):
    go, alice = scenario["go"], scenario["alice"]
    go.order_deadline = utcnow() - timedelta(minutes=1)
    db.session.commit()
    with pytest.raises(DomainError, match="deadline has passed"):
        f.order(go, alice, potatoes="5")


def test_non_member_cannot_order(scenario, f):
    outsider = f.customer()
    with pytest.raises(PermissionDenied):
        f.order(scenario["go"], outsider, potatoes="5")


def test_freeze_drops_items_below_minimum_and_opens_bidding(f, db):
    alice, bob = f.customer(), f.customer()
    g = f.group(alice, [bob])
    go = f.group_order(g, alice, items=[("potatoes", "100", "50", None), ("onions", "60", "30", None)])
    f.order(go, alice, potatoes="30", onions="10")
    f.order(go, bob, potatoes="30")
    f.freeze(go)
    assert go.status == GOStatus.BIDDING
    items = {i.product.slug: i for i in go.items}
    assert items["potatoes"].frozen_qty == Decimal("60.000") and not items["potatoes"].dropped
    assert items["onions"].dropped
    assert all(co.status == COStatus.LOCKED for co in go.customer_orders)
    assert all(it.product.slug == "potatoes" for co in go.customer_orders for it in co.items)


def test_freeze_cancels_when_no_minimum_met(f):
    alice = f.customer()
    g = f.group(alice)
    go = f.group_order(g, alice, items=[("potatoes", "100", "50", None)])
    f.order(go, alice, potatoes="10")
    f.freeze(go)
    assert go.status == GOStatus.CANCELLED
    assert go.customer_orders[0].status == COStatus.CANCELLED


def test_invalid_status_transition_rejected(scenario, db):
    from app.services.core import transition_group_order
    with pytest.raises(DomainError, match="Invalid group order status"):
        transition_group_order(scenario["go"], GOStatus.DELIVERED)


def test_tier_pricing(f, db):
    vendor = f.vendor()
    vp = VendorProduct(vendor_id=vendor.id, product_id=f.product("potatoes").id, grade_id=f.grade().id,
                       price_per_unit=Decimal("30.00"), currency="INR")
    vp.tiers = [PriceTier(min_qty=Decimal("50"), price_per_unit=Decimal("27.00")),
                PriceTier(min_qty=Decimal("100"), price_per_unit=Decimal("25.00"))]
    db.session.add(vp)
    db.session.commit()
    assert tier_price(vp, Decimal("10")) == Decimal("30.00")
    assert tier_price(vp, Decimal("50")) == Decimal("27.00")
    assert tier_price(vp, Decimal("99.999")) == Decimal("27.00")
    assert tier_price(vp, Decimal("150")) == Decimal("25.00")
    assert isinstance(tier_price(vp, 1), Decimal)


def test_allocation_is_exact():
    parts = allocate(Decimal("100.00"), [Decimal("1"), Decimal("1"), Decimal("1")])
    assert sum(parts) == Decimal("100.00") and sorted(parts) == [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")]
    q = allocate_quantities(Decimal("50"), [Decimal("40"), Decimal("35"), Decimal("25")])
    assert sum(q) == Decimal("50.000") and all(a <= b for a, b in zip(q, [40, 35, 25]))
    assert allocate_quantities(Decimal("200"), [Decimal("40"), Decimal("60")]) == [Decimal("40.000"), Decimal("60.000")]


def test_price_breakdown_for_group(scenario, f):
    go = scenario["go"]
    f.freeze(go)
    bid = f.bid(scenario["founder"], go, delivery_fee="150.00")
    priced = price_customer_orders(go, bid, go.customer_orders)
    assert sum(p["delivery_fee"] for p in priced) == Decimal("150.00")
    alice = [p for p in priced if p["order"].user_id == scenario["alice"].id][0]
    # 40*25 + 20*36 + 15*32 = 1000 + 720 + 480
    assert alice["subtotal"] == Decimal("2200.00")
    assert alice["total"] == alice["subtotal"] + alice["delivery_fee"] + alice["service_fee"]
    assert all(isinstance(p["total"], Decimal) for p in priced)
