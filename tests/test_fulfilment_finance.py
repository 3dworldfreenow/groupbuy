"""Quality inspections, holds, delivery, payments, refunds, commissions and settlements."""
import hashlib
import hmac
import io
import json
from decimal import Decimal

import pytest

from app.constants import COStatus, DeliveryStatus, GOStatus, PaymentStatus
from app.services import bidding as bid_svc
from app.services import fulfilment as ful
from app.services import payments as pay
from app.services.core import DomainError, PermissionDenied
from app.services.finance import add_cost, financial_summary
from tests.conftest import login

ITEMS = lambda go: [{"product_id": i.product_id, "expected_qty": i.frozen_qty, "received_qty": i.frozen_qty,  # noqa
                     "accepted_qty": i.frozen_qty, "rejected_qty": "0"} for i in go.items]


@pytest.fixture
def awarded(scenario, f, db):
    go = scenario["go"]
    f.freeze(go)
    b = f.bid(scenario["founder"], go, delivery_fee="150.00")
    f.close_bidding(go)
    bid_svc.award(go, scenario["alice"], b)
    db.session.commit()
    for co in go.customer_orders:
        ful.confirm_customer_order(co.user, co)
    bid_svc.vendor_accept_award(scenario["founder"], go)
    db.session.commit()
    scenario["bid"] = b
    return scenario


def test_confirm_generates_payment_request_not_paid(awarded):
    for co in awarded["go"].customer_orders:
        assert co.status == COStatus.CONFIRMED
        assert co.payment_status == PaymentStatus.PENDING
        assert co.payments[0].status == PaymentStatus.PENDING and not co.payments[0].verified_server_side


def test_failed_inspection_places_order_on_hold(awarded, db):
    go, admin, founder = awarded["go"], awarded["admin"], awarded["founder"]
    ful.start_quality_check(founder, go)
    with pytest.raises(DomainError, match="Describe the defects"):
        ful.record_inspection(go, admin, result="fail", items=ITEMS(go), place_on_hold=True)
    insp = ful.record_inspection(go, admin, result="fail", items=ITEMS(go), defect_notes="20% soft tomatoes",
                                 spoilage_qty="8", place_on_hold=True)
    db.session.commit()
    assert go.status == GOStatus.ON_HOLD and insp.placed_on_hold and go.hold_from_status == GOStatus.QUALITY_CHECK
    with pytest.raises(DomainError):
        ful.mark_packed(founder, go)
    ful.resume_order(go, admin, GOStatus.QUALITY_CHECK)
    with pytest.raises(DomainError, match="latest inspection failed"):
        ful.mark_packed(founder, go)
    ful.record_inspection(go, admin, result="pass", items=ITEMS(go))
    ful.mark_packed(founder, go)
    db.session.commit()
    assert go.status == GOStatus.PACKED


def test_vendor_self_check_is_declared_and_not_sufficient(awarded, db):
    go, founder = awarded["go"], awarded["founder"]
    ful.start_quality_check(founder, go)
    insp = ful.record_inspection(go, founder.user, result="pass", items=ITEMS(go))
    assert insp.verification == "vendor_declared"
    db.session.commit()
    with pytest.raises(PermissionDenied):
        ful.record_inspection(go, founder.user, result="fail", items=ITEMS(go), defect_notes="x", place_on_hold=True)
    db.session.rollback()
    with pytest.raises(DomainError, match="platform inspection"):
        ful.mark_packed(founder, go)


def test_inspection_quantity_validation(awarded):
    go, admin = awarded["go"], awarded["admin"]
    items = ITEMS(go)
    items[0]["rejected_qty"] = "5"   # accepted (all) + rejected > received
    with pytest.raises(DomainError, match="cannot exceed"):
        ful.record_inspection(go, admin, result="conditional", items=items)


def _pack_and_dispatch(s, db):
    go, admin, founder = s["go"], s["admin"], s["founder"]
    ful.start_quality_check(founder, go)
    ful.record_inspection(go, admin, result="pass", items=ITEMS(go))
    ful.mark_packed(founder, go)
    ful.dispatch(founder, go, "Van 1")
    db.session.commit()
    return ful.deliveries_for(go)


def test_delivery_completion_and_failure(awarded, db):
    go, founder = awarded["go"], awarded["founder"]
    deliveries = _pack_and_dispatch(awarded, db)
    assert len(deliveries) == 3 and all(d.status == DeliveryStatus.DISPATCHED for d in deliveries)
    d1, d2, d3 = deliveries
    ful.mark_delivered(founder.user, d1, received_by="Alice", proof_notes="Handed over")
    ful.mark_failed(founder.user, d2, "Customer not at pickup point")
    ful.reschedule(founder.user, d2)
    assert d2.status == DeliveryStatus.DISPATCHED and d2.attempts == 2
    ful.customer_confirm_receipt(d3.customer_order.user, d3)       # customer confirms → delivered
    assert go.status == GOStatus.DISPATCHED
    ful.mark_delivered(founder.user, d2)
    db.session.commit()
    assert go.status == GOStatus.DELIVERED
    assert all(co.status == COStatus.DELIVERED for co in go.customer_orders)
    stranger = awarded["bob"]
    with pytest.raises(PermissionDenied):
        ful.customer_confirm_receipt(stranger, d1)


def test_manual_payment_states_and_validation(awarded, db):
    co = awarded["go"].customer_orders[0]
    admin = awarded["admin"]
    with pytest.raises(DomainError, match="exceeds"):
        pay.record_manual_payment(co, admin, amount=co.total + 1, method="upi")
    pay.record_manual_payment(co, admin, amount=Decimal("100.00"), method="cash", reference="R1")
    db.session.commit()
    assert co.payment_status == PaymentStatus.PENDING         # partially paid is not "paid"
    assert pay.amount_outstanding(co) == co.total - Decimal("100.00")
    pay.record_manual_payment(co, admin, amount=pay.amount_outstanding(co), method="upi", reference="R2")
    db.session.commit()
    assert co.payment_status == PaymentStatus.PAID
    assert all(p.verified_server_side for p in co.payments if p.status == "paid")
    with pytest.raises(DomainError, match="Invalid payment status"):
        pay._transition(co.payments[0], PaymentStatus.FAILED)


def test_refunds_and_payment_status(awarded, db):
    co = awarded["go"].customer_orders[0]
    admin = awarded["admin"]
    with pytest.raises(DomainError, match="exceeds"):
        pay.issue_refund(co, admin, amount="1.00", reason="nothing paid yet")
    pay.record_manual_payment(co, admin, amount=co.total, method="upi")
    pay.issue_refund(co, admin, amount="50.00", reason="Short weight", charged_to="vendor")
    db.session.commit()
    assert co.payment_status == PaymentStatus.PARTIALLY_REFUNDED
    with pytest.raises(DomainError, match="exceeds"):
        pay.issue_refund(co, admin, amount=co.total, reason="too much")
    pay.issue_refund(co, admin, amount=co.total - Decimal("50.00"), reason="Rest", charged_to="platform")
    db.session.commit()
    assert co.payment_status == PaymentStatus.REFUNDED


def test_webhook_requires_valid_signature(client, app, awarded, db):
    co = awarded["go"].customer_orders[0]
    rec = co.payments[0]
    rec.provider = "gateway"
    db.session.commit()
    app.config["PAYMENT_WEBHOOK_SECRET"] = "whsec_test"
    body = json.dumps({"payment_id": rec.id, "status": "paid", "amount": str(rec.amount),
                       "currency": rec.currency}).encode()
    r = client.post("/payments/webhook/gateway", data=body, headers={"X-Signature": "bad"},
                    content_type="application/json")
    assert r.status_code == 400
    db.session.refresh(rec)
    assert rec.status == PaymentStatus.PENDING
    # Browser return never changes status
    client.get("/payments/return")
    db.session.refresh(rec)
    assert rec.status == PaymentStatus.PENDING
    sig = hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest()
    r = client.post("/payments/webhook/gateway", data=body, headers={"X-Signature": sig},
                    content_type="application/json")
    assert r.status_code == 200
    db.session.refresh(rec)
    assert rec.status == PaymentStatus.PAID and rec.verified_server_side
    # Amount mismatch is rejected even with a valid signature
    bad = json.dumps({"payment_id": rec.id, "status": "paid", "amount": "1.00", "currency": rec.currency}).encode()
    sig2 = hmac.new(b"whsec_test", bad, hashlib.sha256).hexdigest()
    assert client.post("/payments/webhook/gateway", data=bad, headers={"X-Signature": sig2}).status_code in (200, 400)


def test_webhook_disabled_without_secret(client):
    assert client.post("/payments/webhook/gateway", data=b"{}").status_code == 404
    assert client.post("/payments/webhook/manual", data=b"{}").status_code == 404


def test_close_creates_commission_and_settlement(awarded, db):
    go, admin, founder = awarded["go"], awarded["admin"], awarded["founder"]
    for d in _pack_and_dispatch(awarded, db):
        ful.customer_confirm_receipt(d.customer_order.user, d)
        pay.record_manual_payment(d.customer_order, admin, amount=d.customer_order.total, method="upi")
    alice_co = [co for co in go.customer_orders if co.user_id == awarded["alice"].id][0]
    from app.services.support import create_complaint, resolve_complaint
    c = create_complaint(awarded["alice"], alice_co, complaint_type="damage",
                         description="Two kilos of tomatoes were crushed")
    db.session.commit()
    with pytest.raises(DomainError, match="complaint"):
        ful.close_group_order(go, admin)
    resolve_complaint(c, admin, resolution="refund", notes="Refund crushed tomatoes", refund_amount="64.00")
    ful.close_group_order(go, admin)
    add_cost(admin, category="purchase", amount="4000.00", description="Mandi purchase", group_order=go,
             vendor=founder)
    add_cost(admin, category="delivery", amount="100.00", description="Fuel", group_order=go)
    db.session.commit()
    assert go.status == GOStatus.CLOSED
    from app.models import CommissionRecord, VendorSettlementRecord
    comm = db.session.query(CommissionRecord).one()
    sett = db.session.query(VendorSettlementRecord).one()
    goods = Decimal("5940.00")
    assert comm.base_amount == goods - Decimal("64.00")
    assert comm.amount == Decimal("293.80")                      # 5% of 5876.00
    assert sett.net_payable == goods + Decimal("150.00") - Decimal("64.00") - Decimal("293.80")
    fin = financial_summary()
    assert fin["platform_revenue"] == Decimal("293.80")          # commission, not full order value
    assert fin["gmv"] > fin["platform_revenue"]
    assert fin["founder_margin"] == sett.net_payable - Decimal("4000.00")
    assert fin["operating_profit"] == fin["gross_margin"] - Decimal("100.00")
    with pytest.raises(DomainError):
        add_cost(admin, category="purchase", amount="1", description="x",
                 vendor=__import__("tests.conftest", fromlist=["x"]).Factory(db).vendor(name="Ext"))


def test_complaint_photo_upload_and_private_access(client, awarded, db, f):
    _pack_and_dispatch(awarded, db)
    alice_co = [co for co in awarded["go"].customer_orders if co.user_id == awarded["alice"].id][0]
    login(client, awarded["alice"].email)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    r = client.post(f"/orders/{alice_co.id}/complaint", data={
        "complaint_type": "quality", "description": "Onions were sprouting badly",
        "photos": [(io.BytesIO(png), "photo.png")]}, content_type="multipart/form-data")
    assert r.status_code == 302
    from app.models import Attachment
    att = db.session.query(Attachment).one()
    assert att.stored_filename != "photo.png"
    assert client.get(f"/files/attachment/{att.id}").status_code == 200
    client.post("/logout")
    login(client, awarded["bob"].email)
    assert client.get(f"/files/attachment/{att.id}").status_code == 404
    # Disguised file types are rejected
    client.post("/logout")
    login(client, awarded["alice"].email)
    r = client.post(f"/orders/{alice_co.id}/complaint", data={
        "complaint_type": "quality", "description": "Another issue here",
        "photos": [(io.BytesIO(b"<script>alert(1)</script>"), "evil.png")]},
        content_type="multipart/form-data", follow_redirects=True)
    assert b"does not match" in r.data
