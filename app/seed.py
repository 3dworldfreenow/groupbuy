"""Reference data and demonstration data.

Sample prices below are illustrative demo values only — they are NOT market prices.
"""
from datetime import timedelta
from decimal import Decimal

from .constants import GOStatus, Roles, SelectionMode, VendorStatus
from .extensions import db
from .models import (BuyingGroup, Location, PickupPoint, PriceTier, Product, ProductCategory, ProductGrade, User,
                     VendorProduct)
from .utils import utcnow

CATEGORIES = [
    ("Vegetables", "vegetables", "Everyday vegetables bought in bulk", 1),
    ("Leafy greens", "leafy-greens", "Greens and herbs, best ordered for quick delivery", 2),
    ("Fruit", "fruit", "Seasonal fruit", 3),
]

GRADES = [
    ("PREMIUM", "Premium", 3, "Uniform size and colour, firm, no visible defects, max 2% minor blemishes.", "2"),
    ("STANDARD", "Standard", 2, "Good quality, minor size variation, max 5% minor blemishes, no rot.", "5"),
    ("ECONOMY", "Economy", 1, "Mixed sizes, cosmetic blemishes allowed, max 10% defects, no rot or mould.", "10"),
]

# name, slug, category slug, unit, emoji, min, step, max per customer
PRODUCTS = [
    ("Potatoes", "potatoes", "vegetables", "kg", "🥔", "1", "0.5", "50"),
    ("Onions", "onions", "vegetables", "kg", "🧅", "1", "0.5", "50"),
    ("Tomatoes", "tomatoes", "vegetables", "kg", "🍅", "0.5", "0.5", "25"),
    ("Carrots", "carrots", "vegetables", "kg", "🥕", "0.5", "0.5", "20"),
    ("Cucumbers", "cucumbers", "vegetables", "kg", "🥒", "0.5", "0.5", "20"),
    ("Garlic", "garlic", "vegetables", "kg", "🧄", "0.25", "0.25", "5"),
    ("Green chillies", "green-chillies", "vegetables", "kg", "🌶️", "0.25", "0.25", "3"),
    ("Cauliflower", "cauliflower", "vegetables", "piece", "🥦", "1", "1", "10"),
    ("Spinach", "spinach", "leafy-greens", "bunch", "🥬", "1", "1", "10"),
    ("Coriander", "coriander", "leafy-greens", "bunch", "🌿", "1", "1", "10"),
    ("Bananas", "bananas", "fruit", "dozen", "🍌", "1", "1", "10"),
    ("Apples", "apples", "fruit", "kg", "🍎", "1", "0.5", "20"),
    ("Oranges", "oranges", "fruit", "kg", "🍊", "1", "0.5", "20"),
    ("Mangoes", "mangoes", "fruit", "kg", "🥭", "1", "0.5", "20"),
]

LOCATIONS = [
    ("Green Park", "Bengaluru", "Karnataka", [("Green Park Community Hall", "Main Road, near the park gate",
                                               "Collect between the delivery window hours")]),
    ("Lakeview", "Bengaluru", "Karnataka", [("Lakeview Clubhouse", "Lakeview Apartments, Tower B lobby", None)]),
    ("Riverside", "Pune", "Maharashtra", [("Riverside Society Office", "Riverside Society, Gate 2", None)]),
]

# Illustrative demo list prices (not market prices): product slug -> (price, [(min_qty, tier price)])
DEMO_PRICES = {
    "potatoes": ("30.00", [("50", "27.00"), ("100", "25.00")]),
    "onions": ("40.00", [("50", "36.00")]),
    "tomatoes": ("35.00", [("30", "32.00")]),
    "carrots": ("45.00", []),
    "spinach": ("20.00", []),
    "bananas": ("60.00", []),
    "apples": ("150.00", []),
}


def seed_reference():
    """Idempotent: categories, grades, products, locations and pickup points."""
    from .services.support import get_role
    for r in Roles.ALL:
        get_role(r)
    cats = {}
    for name, slug, desc, order in CATEGORIES:
        c = db.session.query(ProductCategory).filter_by(slug=slug).first()
        if c is None:
            c = ProductCategory(name=name, slug=slug, description=desc, sort_order=order)
            db.session.add(c)
        cats[slug] = c
    for code, name, rank, spec, defect in GRADES:
        if not db.session.query(ProductGrade).filter_by(code=code).first():
            db.session.add(ProductGrade(code=code, name=name, rank=rank, specification=spec,
                                        max_defect_percent=Decimal(defect)))
    db.session.flush()
    for name, slug, cat, unit, emoji, mn, step, mx in PRODUCTS:
        if not db.session.query(Product).filter_by(slug=slug).first():
            db.session.add(Product(name=name, slug=slug, category_id=cats[cat].id, unit=unit, emoji=emoji,
                                   min_customer_qty=Decimal(mn), qty_step=Decimal(step),
                                   max_customer_qty=Decimal(mx),
                                   description=f"Fresh {name.lower()}, sold per {unit}."))
    for name, city, state, points in LOCATIONS:
        loc = db.session.query(Location).filter_by(name=name, city=city).first()
        if loc is None:
            loc = Location(name=name, city=city, state=state)
            db.session.add(loc)
            db.session.flush()
        for pname, addr, instr in points:
            if not db.session.query(PickupPoint).filter_by(location_id=loc.id, name=pname).first():
                db.session.add(PickupPoint(location_id=loc.id, name=pname, address_text=addr, instructions=instr))
    db.session.commit()


def _user(email, name, password, roles, phone="+91 90000 00000"):
    from .services.support import create_user
    u = db.session.query(User).filter_by(email=email).first()
    if u:
        return u
    return create_user(email=email, password=password, full_name=name, phone=phone, roles=roles)


def _vendor(user, business, *, platform_owned=False, status=VendorStatus.APPROVED, capacity="1000"):
    from .services.support import create_vendor_profile, set_vendor_status
    if user.vendor_profile:
        return user.vendor_profile
    vp = create_vendor_profile(user, business_name=business, contact_phone="+91 90000 11111",
                               business_address="Wholesale Market Yard, Bengaluru",
                               description=f"{business} supplies fresh produce to community groups.",
                               capacity_per_order=capacity, platform_owned=platform_owned)
    if not platform_owned and status == VendorStatus.APPROVED:
        set_vendor_status(vp, None, VendorStatus.APPROVED, "Demo vendor")
    return vp


def _listings(vendor, grade, factor="1.00"):
    for slug, (price, tiers) in DEMO_PRICES.items():
        p = db.session.query(Product).filter_by(slug=slug).one()
        if db.session.query(VendorProduct).filter_by(vendor_id=vendor.id, product_id=p.id).first():
            continue
        f = Decimal(factor)
        vp = VendorProduct(vendor_id=vendor.id, product_id=p.id, grade_id=grade.id,
                           price_per_unit=(Decimal(price) * f).quantize(Decimal("0.01")), currency="INR",
                           stock_qty=Decimal("500"), origin="Declared by vendor: Kolar district")
        vp.tiers = [PriceTier(min_qty=Decimal(m), price_per_unit=(Decimal(tp) * f).quantize(Decimal("0.01")))
                    for m, tp in tiers]
        db.session.add(vp)


def seed_demo(password):
    """Create demo accounts and group orders covering the whole workflow."""
    from .services import bidding, fulfilment, groups, payments, support
    from .services.finance import add_cost
    seed_reference()

    admin = _user("admin@example.com", "Demo Administrator", password, (Roles.ADMIN,))
    founder_user = _user("founder@example.com", "Founder (vendor operator)", password, ())
    founder = _vendor(founder_user, "FreshCircle Founder Produce", platform_owned=True)
    v2_user = _user("vendor2@example.com", "Ravi Wholesale", password, ())
    v2 = _vendor(v2_user, "Ravi Farm Fresh Wholesale")
    v3_user = _user("vendor3@example.com", "Pending Vendor", password, ())
    _vendor(v3_user, "Sunrise Agro Traders (pending)", status=VendorStatus.PENDING)
    alice = _user("alice@example.com", "Alice Sharma", password, (Roles.CUSTOMER,))
    bob = _user("bob@example.com", "Bob Kumar", password, (Roles.CUSTOMER,))
    carol = _user("carol@example.com", "Carol D'Souza", password, (Roles.CUSTOMER,))
    db.session.commit()

    premium = db.session.query(ProductGrade).filter_by(code="PREMIUM").one()
    standard = db.session.query(ProductGrade).filter_by(code="STANDARD").one()
    _listings(founder, standard)
    _listings(v2, premium, "1.05")
    for u in (alice, bob, carol):
        if not u.addresses:
            from .models import Address
            db.session.add(Address(user_id=u.id, label="Home", recipient_name=u.full_name, phone="+91 98000 00000",
                                   line1="Flat 12, Block A", city="Bengaluru", state="Karnataka",
                                   postal_code="560001", is_default=True))
    db.session.commit()

    if db.session.query(BuyingGroup).count():
        return {"admin": admin.email, "note": "Demo groups already exist; accounts refreshed."}

    green = db.session.query(Location).filter_by(name="Green Park").one()
    lake = db.session.query(Location).filter_by(name="Lakeview").one()
    potatoes, onions, tomatoes = (db.session.query(Product).filter_by(slug=s).one()
                                  for s in ("potatoes", "onions", "tomatoes"))
    now = utcnow()

    def new_round(group, organiser, title, days, mode=SelectionMode.ORGANISER):
        go = groups.create_group_order(group, organiser, title=title,
                                       order_deadline=now + timedelta(days=days),
                                       delivery_window_start=now + timedelta(days=days + 2),
                                       delivery_window_end=now + timedelta(days=days + 2, hours=4),
                                       selection_mode=mode)
        for p, target, mn, mx in ((potatoes, "100", "40", "150"), (onions, "60", "20", "100"),
                                  (tomatoes, "40", "15", "60")):
            groups.set_item(go, organiser, product=p, target_qty=target, min_qty=mn, max_qty=mx)
        groups.open_for_orders(go, organiser)
        return go

    def fill(go, members):
        items = {i.product_id: i.id for i in go.items}
        for user, q_pot, q_on, q_tom in members:
            groups.update_customer_quantities(go, user, {items[potatoes.id]: q_pot, items[onions.id]: q_on,
                                                         items[tomatoes.id]: q_tom})

    # 1) Completed historical round (gives dashboards real data) --------------
    hist_group = groups.create_group(alice, name="Lakeview Apartments", description="Weekly vegetable pooling",
                                     location=lake, pickup_point=lake.pickup_points[0])
    for u in (bob, carol):
        groups.join_group(u, hist_group)
    db.session.commit()
    go = new_round(hist_group, alice, "Lakeview weekly vegetables", 1)
    fill(go, [(alice, "40", "20", "15"), (bob, "35", "25", "15"), (carol, "25", "15", "10")])
    db.session.commit()
    go.order_deadline = utcnow() - timedelta(minutes=1)
    groups.freeze(go, None)
    items = {i.id: {"offered_qty": str(i.frozen_qty), "unit_price": p}
             for i, p in zip(go.items, ("25.00", "36.00", "32.00"))}
    bidding.submit_bid(founder, go, items=items, grade_id=standard.id, delivery_fee="150.00",
                       home_delivery_fee="20.00", estimated_delivery_at=go.delivery_window_start,
                       expires_at=go.bidding_deadline + timedelta(days=2), origin="Kolar (vendor-declared)")
    db.session.commit()
    go.bidding_deadline = utcnow() - timedelta(minutes=1)
    bidding.close_bidding(go, None)
    bidding.award(go, alice, bid=go.bids[0])
    db.session.commit()
    for co in go.customer_orders:
        fulfilment.confirm_customer_order(co.user, co)
    bidding.vendor_accept_award(founder, go)
    fulfilment.start_quality_check(founder, go)
    fulfilment.record_inspection(go, admin, result="pass", checklist={"freshness": True, "damage": True},
                                 items=[{"product_id": i.product_id, "expected_qty": i.frozen_qty,
                                         "received_qty": i.frozen_qty, "accepted_qty": i.frozen_qty,
                                         "rejected_qty": "0"} for i in go.items])
    fulfilment.mark_packed(founder, go)
    fulfilment.dispatch(founder, go, assigned_to="Founder van")
    db.session.commit()
    for d in fulfilment.deliveries_for(go):
        fulfilment.customer_confirm_receipt(d.customer_order.user, d)
        payments.record_manual_payment(d.customer_order, admin, amount=d.customer_order.total, method="upi",
                                       reference=f"DEMO-UPI-{d.id}")
    db.session.commit()
    support.create_review(alice, fulfilment.deliveries_for(go)[0].customer_order, 5, "Fresh and on time.")
    fulfilment.close_group_order(go, admin)
    add_cost(admin, category="purchase", amount="4200.00", description="Demo: mandi purchase for Lakeview order",
             group_order=go, vendor=founder)
    add_cost(admin, category="delivery", amount="120.00", description="Demo: fuel for delivery van", group_order=go)
    add_cost(admin, category="packaging", amount="80.00", description="Demo: bags and labels", group_order=go)
    db.session.commit()

    # 2) Live round collecting orders: 100 kg potatoes, 60 kg onions, 40 kg tomatoes = 200 kg
    green_group = groups.create_group(alice, name="Green Park Residents",
                                      description="Neighbours in Green Park pooling orders for fresh vegetables.",
                                      location=green, pickup_point=green.pickup_points[0])
    for u in (bob, carol):
        groups.join_group(u, green_group)
    db.session.commit()
    go2 = new_round(green_group, alice, "Green Park weekend vegetables", 2)
    fill(go2, [(alice, "40", "20", "15"), (bob, "35", "25", "15"), (carol, "25", "15", "10")])
    db.session.commit()

    # 3) Round in bidding with the founder's offer, for testing Stage 1/Stage 2 bidding
    river = db.session.query(Location).filter_by(name="Riverside").one()
    river_group = groups.create_group(bob, name="Riverside Society", description="Riverside families buying together.",
                                      location=river, pickup_point=river.pickup_points[0])
    groups.join_group(carol, river_group)
    db.session.commit()
    go3 = new_round(river_group, bob, "Riverside fortnightly order", 1, mode=SelectionMode.GROUP_VOTE)
    fill(go3, [(bob, "50", "30", "20"), (carol, "50", "30", "20")])
    db.session.commit()
    go3.order_deadline = utcnow() - timedelta(minutes=1)
    groups.freeze(go3, None)
    go3.bidding_deadline = utcnow() + timedelta(hours=12)
    items = {i.id: {"offered_qty": str(i.frozen_qty), "unit_price": p}
             for i, p in zip(go3.items, ("26.00", "37.00", "33.00"))}
    bidding.submit_bid(founder, go3, items=items, grade_id=standard.id, delivery_fee="120.00",
                       estimated_delivery_at=go3.delivery_window_start,
                       expires_at=go3.bidding_deadline + timedelta(days=2))
    db.session.commit()
    assert go.status == GOStatus.CLOSED
    return {"admin": admin.email}
