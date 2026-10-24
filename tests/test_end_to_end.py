"""End-to-end workflows through the HTTP interface.

1. Stage 1: founder-operated vendor, single offer, full lifecycle to closed order.
2. Stage 2: multi-vendor sealed bidding with automatic transparent scoring.
"""
from datetime import timedelta
from decimal import Decimal

from app.constants import COStatus, GOStatus
from app.models import (BuyingGroup, CommissionRecord, CustomerOrder, VendorBid,
                        VendorSettlementRecord)
from app.utils import utcnow
from tests.conftest import login

FMT = "%Y-%m-%dT%H:%M"
IST = timedelta(hours=5, minutes=30)   # app timezone for datetime-local inputs


def local(dt):
    return (dt + IST).strftime(FMT)


def as_user(client, user):
    client.post("/logout")
    assert login(client, user.email).status_code == 302


def test_single_vendor_end_to_end(client, f, db):
    alice, bob, carol = f.customer("Alice"), f.customer("Bob"), f.customer("Carol")
    founder = f.vendor(platform_owned=True, name="Founder Produce")
    admin = f.admin()
    loc = f.location()
    now = utcnow()

    # 1–3. Alice creates a group + order round and sets products/targets
    as_user(client, alice)
    r = client.post("/groups/new", data={
        "name": "Elm Court", "location_id": loc.id, "pickup_point_id": loc.pickup_points[0].id, "is_public": "y",
        "title": "Elm Court week 1", "order_deadline": local(now + timedelta(days=1)),
        "delivery_window_start": local(now + timedelta(days=2)),
        "delivery_window_end": local(now + timedelta(days=2, hours=4)),
        "fulfilment_options": "both", "selection_mode": "organiser"})
    assert r.status_code == 302
    group = db.session.query(BuyingGroup).filter_by(name="Elm Court").one()
    go = group.current_order
    for slug, target in (("potatoes", "100"), ("onions", "60"), ("tomatoes", "40")):
        r = client.post(f"/group-orders/{go.id}/items", data={
            "product_id": f.product(slug).id, "target_qty": target, "min_qty": "10", "max_qty": "200"})
        assert r.status_code == 302
    assert client.post(f"/group-orders/{go.id}/open").status_code == 302
    db.session.refresh(go)
    assert go.status == GOStatus.COLLECTING and len(go.items) == 3
    items = {i.product.slug: i.id for i in go.items}

    # 4–5. Members join and submit quantities
    def order(user, pot, on, tom, method="pickup", address_id=None):
        as_user(client, user)
        if group.membership_for(user) is None:
            assert client.post(f"/join/{group.invite_code}").status_code == 302
        data = {f"qty_{items['potatoes']}": pot, f"qty_{items['onions']}": on, f"qty_{items['tomatoes']}": tom,
                "fulfilment_method": method}
        if address_id:
            data["address_id"] = address_id
        r = client.post(f"/group-orders/{go.id}/my-order", data=data)
        assert r.status_code == 302, r.data

    order(alice, "40", "20", "15")
    order(bob, "35", "25", "15")
    # Carol wants home delivery: add an address first
    as_user(client, carol)
    r = client.post("/addresses/new", data={"label": "Home", "recipient_name": "Carol", "phone": "+91 98000 00000",
                                            "line1": "12 Elm Court", "city": "Bengaluru", "postal_code": "560001",
                                            "location_id": 0, "is_default": "y"})
    assert r.status_code == 302
    order(carol, "25", "15", "10", method="home", address_id=carol.addresses[0].id)

    # 6. Aggregate demand is computed continuously: 100 + 60 + 40 = 200 kg
    r = client.get(f"/group-orders/{go.id}")
    assert r.status_code == 200
    from app.services.groups import progress
    assert progress(go)["total_qty"] == Decimal("200.000")

    # 7–8. Organiser closes early → frozen and validated → offers stage
    as_user(client, alice)
    assert client.post(f"/group-orders/{go.id}/close-early").status_code == 302
    db.session.refresh(go)
    assert go.status == GOStatus.BIDDING and go.bidding_mode == "single_vendor"

    # 9. Founder vendor publishes an offer
    as_user(client, founder.user)
    form = {"grade_id": f.grade().id, "origin": "Kolar", "substitution_policy": "none", "delivery_fee": "150.00",
            "home_delivery_fee": "30.00", "estimated_delivery_at": local(go.delivery_window_start),
            "expires_at": local(go.bidding_deadline + timedelta(days=2)), "notes": "Fresh stock"}
    for i in go.items:
        form[f"qty_{i.id}"] = str(i.frozen_qty)
        form[f"price_{i.id}"] = {"potatoes": "25.00", "onions": "36.00", "tomatoes": "32.00"}[i.product.slug]
    r = client.post(f"/vendor/opportunities/{go.id}/bid", data=form)
    assert r.status_code == 302 and r.location.endswith("/vendor/bids")

    # Bidding deadline passes (deadline worker / lazy processing closes bidding)
    go.bidding_deadline = utcnow() - timedelta(seconds=1)
    db.session.commit()

    # 11–12. Members compare; organiser selects the eligible offer
    as_user(client, alice)
    r = client.get(f"/group-orders/{go.id}/offers")
    assert r.status_code == 200 and b"Founder Produce" in r.data and b"Platform-owned" in r.data
    db.session.refresh(go)
    assert go.status == GOStatus.SELECTION
    bid = db.session.query(VendorBid).filter_by(group_order_id=go.id).one()
    assert client.post(f"/group-orders/{go.id}/award", data={"bid_id": bid.id}).status_code == 302
    db.session.refresh(go)
    assert go.status == GOStatus.AWARDED and go.active_award.platform_owned_vendor

    # 13. Customers confirm → payment requests generated
    for user in (alice, bob, carol):
        as_user(client, user)
        co = db.session.query(CustomerOrder).filter_by(group_order_id=go.id, user_id=user.id).one()
        assert co.status == COStatus.AWAITING_CONFIRMATION
        data = {"fulfilment_method": co.fulfilment_method}
        if co.fulfilment_method == "home":
            data["address_id"] = user.addresses[0].id
        assert client.post(f"/orders/{co.id}/confirm", data=data).status_code == 302
        db.session.refresh(co)
        assert co.status == COStatus.CONFIRMED and co.payment_status == "pending"
    carol_co = db.session.query(CustomerOrder).filter_by(group_order_id=go.id, user_id=carol.id).one()
    assert carol_co.home_delivery_fee == Decimal("30.00")
    assert carol_co.total == carol_co.subtotal + carol_co.delivery_fee + Decimal("30.00") + carol_co.service_fee

    # 14. Vendor accepts and sources; 15. admin inspects (platform-verified)
    as_user(client, founder.user)
    assert client.post(f"/vendor/awarded/{go.id}/accept").status_code == 302
    assert client.post(f"/vendor/awarded/{go.id}/batches", data={
        "product_id": f.product("potatoes").id, "lot_code": "LOT-001", "quantity": "100",
        "source_name": "Kolar mandi", "harvest_date": utcnow().date().isoformat()}).status_code == 302
    assert client.post(f"/vendor/awarded/{go.id}/start-qc").status_code == 302
    as_user(client, admin)
    data = {"stage": "pre_dispatch", "result": "pass", "check_freshness": "1", "spoilage_qty": "0"}
    for i in go.items:
        data.update({f"expected_{i.id}": str(i.frozen_qty), f"received_{i.id}": str(i.frozen_qty),
                     f"accepted_{i.id}": str(i.frozen_qty), f"rejected_{i.id}": "0"})
    assert client.post(f"/admin/quality/{go.id}", data=data).status_code == 302
    db.session.refresh(go)
    assert go.status == GOStatus.QUALITY_CHECK

    # 16. Packed and dispatched
    as_user(client, founder.user)
    assert client.post(f"/vendor/awarded/{go.id}/packed").status_code == 302
    assert client.post(f"/vendor/awarded/{go.id}/dispatch", data={"assigned_to": "Van"}).status_code == 302
    db.session.refresh(go)
    assert go.status == GOStatus.DISPATCHED

    # 17. Delivery confirmed (vendor proof and customer confirmation)
    from app.models import Delivery
    deliveries = db.session.query(Delivery).filter_by(group_order_id=go.id).all()
    for d in deliveries:
        if d.customer_order.user_id != carol.id:
            assert client.post(f"/vendor/deliveries/{d.id}/delivered",
                               data={"received_by": "Member", "proof_notes": "Collected"}).status_code == 302
    as_user(client, carol)
    assert client.post(f"/orders/{carol_co.id}/received").status_code == 302
    db.session.refresh(go)
    assert go.status == GOStatus.DELIVERED

    # 18. A shortage report, resolved with a refund
    as_user(client, bob)
    bob_co = db.session.query(CustomerOrder).filter_by(group_order_id=go.id, user_id=bob.id).one()
    assert client.post(f"/orders/{bob_co.id}/complaint", data={
        "complaint_type": "shortage", "description": "Received 1 kg fewer onions than ordered",
        "quantity_affected": "1"}).status_code == 302
    as_user(client, admin)
    for co in go.customer_orders:
        assert client.post(f"/admin/customer-orders/{co.id}/payment",
                           data={"amount": str(co.total), "method": "upi", "reference": f"UPI{co.id}"}).status_code == 302
    from app.models import Complaint
    c = db.session.query(Complaint).one()
    assert client.post(f"/admin/complaints/{c.id}", data={"resolution": "refund", "notes": "Refund 1 kg onions",
                                                          "refund_amount": "36.00",
                                                          "charged_to": "vendor"}).status_code == 302
    db.session.refresh(bob_co)
    assert bob_co.payment_status == "partially_refunded"

    # 19–20. Close and record revenue, commission and vendor payable
    for user in (alice, bob):
        co = db.session.query(CustomerOrder).filter_by(group_order_id=go.id, user_id=user.id).one()
        as_user(client, user)
        client.post(f"/orders/{co.id}/received")
    as_user(client, admin)
    assert client.post(f"/admin/group-orders/{go.id}/close").status_code == 302
    db.session.refresh(go)
    assert go.status == GOStatus.CLOSED
    comm = db.session.query(CommissionRecord).filter_by(group_order_id=go.id).one()
    sett = db.session.query(VendorSettlementRecord).filter_by(group_order_id=go.id).one()
    assert comm.base_amount == Decimal("5940.00") - Decimal("36.00")
    assert sett.refunds_deducted == Decimal("36.00")
    assert sett.delivery_fees == Decimal("180.00")          # 150 group fee + 30 home delivery
    r = client.get("/admin/")
    assert r.status_code == 200
    r = client.get("/admin/reports/group-orders.csv")
    assert b"Elm Court" in r.data

    # Customer can review the vendor after delivery
    as_user(client, alice)
    alice_co = db.session.query(CustomerOrder).filter_by(group_order_id=go.id, user_id=alice.id).one()
    assert client.post(f"/orders/{alice_co.id}/review", data={"rating": "5", "comment": "Great"}).status_code == 302
    assert client.get(f"/orders/{alice_co.id}/receipt").status_code == 200


def test_multi_vendor_bidding_auto_award(client, f, db, multi_vendor):
    alice, bob = f.customer("Alice"), f.customer("Bob")
    group = f.group(alice, [bob])
    go = f.group_order(group, alice, selection_mode="auto_score")
    f.order(go, alice, potatoes="50", onions="30", tomatoes="20")
    f.order(go, bob, potatoes="50", onions="30", tomatoes="20")
    f.freeze(go)
    assert go.bidding_mode == "multi_vendor"
    founder = f.vendor(platform_owned=True, name="Founder Produce")
    rival = f.vendor(name="Rival Farms")
    pending = f.vendor(approved=False, name="Pending Traders")

    # Pending vendors cannot even open the bid page
    as_user(client, pending.user)
    assert client.get(f"/vendor/opportunities/{go.id}/bid").status_code == 403

    def submit(vendor, prices, grade="STANDARD"):
        as_user(client, vendor.user)
        form = {"grade_id": f.grade(grade).id, "substitution_policy": "same_grade", "delivery_fee": "100.00",
                "home_delivery_fee": "0", "estimated_delivery_at": local(go.delivery_window_start),
                "expires_at": local(go.bidding_deadline + timedelta(days=2))}
        for i in go.items:
            form[f"qty_{i.id}"] = str(i.frozen_qty)
            form[f"price_{i.id}"] = prices[i.product.slug]
        return client.post(f"/vendor/opportunities/{go.id}/bid", data=form)

    assert submit(founder, {"potatoes": "26.00", "onions": "37.00", "tomatoes": "33.00"}).status_code == 302
    assert submit(rival, {"potatoes": "25.00", "onions": "36.00", "tomatoes": "32.00"}, "PREMIUM").status_code == 302
    # Rival revises before the deadline
    assert submit(rival, {"potatoes": "24.50", "onions": "36.00", "tomatoes": "32.00"}, "PREMIUM").status_code == 302
    assert db.session.query(VendorBid).filter_by(group_order_id=go.id).count() == 2

    # Deadline passes; the scheduled worker closes bidding and applies the auto-score rule
    go.bidding_deadline = utcnow() - timedelta(seconds=1)
    db.session.commit()
    from app.services.support import process_deadlines
    assert process_deadlines() == 1
    db.session.commit()
    db.session.refresh(go)
    assert go.status == GOStatus.AWARDED
    award = go.active_award
    assert award.vendor.business_name == "Rival Farms" and award.method == "auto_score"
    assert not award.platform_owned_vendor
    assert len(award.snapshot) == 2 and "highest transparent score" in award.explanation
    rival_bid = db.session.get(VendorBid, award.bid_id)
    assert rival_bid.revision == 2
    statuses = sorted(b.status for b in go.bids)
    assert statuses == ["awarded", "not_selected"]

    # Members see the explanation; the losing vendor sees only its own result
    as_user(client, bob)
    r = client.get(f"/group-orders/{go.id}/offers")
    assert r.status_code == 200 and b"Rival Farms" in r.data
    as_user(client, founder.user)
    r = client.get("/vendor/bids")
    assert r.status_code == 200 and b"24.50" not in r.data
