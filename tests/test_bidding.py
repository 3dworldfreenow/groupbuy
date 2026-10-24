"""Vendor approval, bid submission/revision, sealed privacy, scoring and award concurrency."""
import threading
from datetime import timedelta
from decimal import Decimal

import pytest

from app.constants import BidStatus, COStatus, GOStatus
from app.models import AwardDecision
from app.services import bidding as bid_svc
from app.services.core import ConcurrencyError, DomainError, PermissionDenied
from app.services.support import set_vendor_status
from app.utils import utcnow
from tests.conftest import login


@pytest.fixture
def bidding(scenario, f):
    f.freeze(scenario["go"])
    assert scenario["go"].status == GOStatus.BIDDING
    return scenario


# ---------------------------------------------------------------- approval

def test_vendor_approval_flow(f, db, multi_vendor, bidding):
    pending = f.vendor(approved=False, name="Pending Co")
    go = bidding["go"]
    with pytest.raises(PermissionDenied, match="approved"):
        f.bid(pending, go)
    db.session.rollback()
    admin = bidding["admin"]
    with pytest.raises(DomainError, match="reason"):
        set_vendor_status(pending, admin, "rejected", "")
    set_vendor_status(pending, admin, "approved")
    db.session.commit()
    assert f.bid(pending, go).status == BidStatus.SUBMITTED
    set_vendor_status(pending, admin, "suspended", "Quality failures")
    db.session.commit()
    with pytest.raises(PermissionDenied):
        f.bid(pending, go)


def test_admin_approves_vendor_over_http(client, f, db):
    admin = f.admin()
    v = f.vendor(approved=False)
    login(client, admin.email)
    r = client.post(f"/admin/vendors/{v.id}", data={"action": "status", "status": "approved"})
    assert r.status_code == 302
    db.session.refresh(v)
    assert v.status == "approved"
    from app.models import AuditLog
    assert db.session.query(AuditLog).filter_by(action="vendor.status", entity_id=v.id).count() == 1


def test_stage1_only_platform_vendor_can_bid(f, bidding):
    external = f.vendor(name="External")
    with pytest.raises(PermissionDenied, match="Multi-vendor"):
        f.bid(external, bidding["go"])
    assert f.bid(bidding["founder"], bidding["go"]).is_platform_owned


# -------------------------------------------------------- submission rules

def test_bid_submission_and_revision(f, bidding, db):
    go, founder = bidding["go"], bidding["founder"]
    b = f.bid(founder, go)
    assert b.revision == 1 and b.subtotal == Decimal("5940.00") and b.total_offered_qty == Decimal("200.000")
    b2 = f.bid(founder, go, prices={"potatoes": "24.00", "onions": "36.00", "tomatoes": "32.00"})
    assert b2.id == b.id and b2.revision == 2 and b2.subtotal == Decimal("5840.00")
    assert len(b2.items) == 3
    bid_svc.withdraw_bid(founder, b2)
    db.session.commit()
    assert b2.status == BidStatus.WITHDRAWN
    assert f.bid(founder, go).status == BidStatus.SUBMITTED   # can re-submit before deadline


def test_bid_validation_rules(f, bidding, db):
    go, founder = bidding["go"], bidding["founder"]
    with pytest.raises(DomainError, match="full quantity"):
        f.bid(founder, go, qty_factor=Decimal("0.5"))
    db.session.rollback()
    with pytest.raises(DomainError, match="more"):
        f.bid(founder, go, qty_factor=Decimal("1.1"))
    db.session.rollback()
    with pytest.raises(DomainError, match="valid at least"):
        f.bid(founder, go, expires=go.bidding_deadline + timedelta(hours=1))
    db.session.rollback()
    small = f.vendor(platform_owned=True, capacity="50", name="Small founder")
    with pytest.raises(DomainError, match="capacity"):
        f.bid(small, go)


def test_partial_fulfilment_only_when_allowed(f, db):
    alice = f.customer()
    g = f.group(alice)
    go = f.group_order(g, alice, items=[("potatoes", "100", "10", None)], allow_partial=True, min_fulfilment="60")
    f.order(go, alice, potatoes="50")
    f.freeze(go)
    founder = f.vendor(platform_owned=True)
    with pytest.raises(DomainError, match="at least 60"):
        f.bid(founder, go, qty_factor=Decimal("0.5"))
    db.session.rollback()
    b = f.bid(founder, go, qty_factor=Decimal("0.8"))
    assert b.total_offered_qty == Decimal("40.000")
    f.close_bidding(go)
    bid_svc.award(go, alice, b)
    db.session.commit()
    co = go.customer_orders[0]
    assert co.items[0].allocated_qty == Decimal("40.000")
    assert co.subtotal == Decimal("1000.00")


def test_deadline_blocks_new_bids_and_withdrawal(f, bidding, db):
    go, founder = bidding["go"], bidding["founder"]
    b = f.bid(founder, go)
    go.bidding_deadline = utcnow() - timedelta(seconds=1)
    db.session.commit()
    with pytest.raises(PermissionDenied, match="deadline"):
        f.bid(founder, go)
    with pytest.raises(DomainError, match="before the bidding deadline"):
        bid_svc.withdraw_bid(founder, b)


# ------------------------------------------------------------ sealed bids

def test_sealed_bid_privacy(client, f, multi_vendor, bidding, db):
    go = bidding["go"]
    rival = f.vendor(name="Rival Fresh")
    f.bid(bidding["founder"], go, prices={"potatoes": "23.45", "onions": "36.00", "tomatoes": "32.00"})
    # Customers cannot see offers during bidding
    login(client, bidding["alice"].email)
    r = client.get(f"/group-orders/{go.id}/offers")
    assert r.status_code == 302
    assert b"23.45" not in client.get(f"/group-orders/{go.id}").data
    client.post("/logout")
    # A competing vendor sees only their own bid form, without competitor values
    login(client, rival.user.email)
    r = client.get(f"/vendor/opportunities/{go.id}/bid")
    assert r.status_code == 200 and b"23.45" not in r.data
    assert b"23.45" not in client.get("/vendor/bids").data
    client.post("/logout")
    # Administrators (who may run the founder vendor) cannot see values while bidding is open
    login(client, bidding["admin"].email)
    r = client.get(f"/admin/group-orders/{go.id}")
    assert r.status_code == 200 and b"23.45" not in r.data and b"5,785" not in r.data
    # Audit entries for bids carry no prices
    from app.models import AuditLog
    for log in db.session.query(AuditLog).filter(AuditLog.action.like("bid.%")):
        assert "23.45" not in (log.details or "")
    client.post("/logout")
    # After the deadline, members can compare offers
    f.close_bidding(go)
    login(client, bidding["alice"].email)
    r = client.get(f"/group-orders/{go.id}/offers")
    assert r.status_code == 200 and b"23.45" in r.data


# --------------------------------------------------------------- scoring

def test_scoring_considers_more_than_price(f, multi_vendor, bidding, db):
    go = bidding["go"]
    cheap = f.vendor(name="Cheap Economy")
    f.bid(cheap, go, prices={"potatoes": "24.00", "onions": "35.00", "tomatoes": "31.00"}, grade="ECONOMY")
    f.bid(bidding["founder"], go, grade="PREMIUM")
    f.close_bidding(go)
    bids = {b.vendor.business_name: b for b in bid_svc.eligible_bids(go)}
    cheap_b, premium_b = bids["Cheap Economy"], bids["Founder Produce"]
    assert Decimal(cheap_b.breakdown["components"]["price"]) == Decimal("1.0000")
    assert Decimal(premium_b.breakdown["components"]["quality"]) > Decimal(cheap_b.breakdown["components"]["quality"])
    assert set(premium_b.breakdown["weights"]) == {"price", "quality", "on_time", "cancellation", "rating"}
    assert Decimal(premium_b.breakdown["weights"]["price"]) < 1
    assert premium_b.score > cheap_b.score     # quality outweighs a small price difference


def test_expired_bid_cannot_be_awarded(f, bidding, db):
    go = bidding["go"]
    b = f.bid(bidding["founder"], go)
    f.close_bidding(go)
    b.expires_at = utcnow() - timedelta(seconds=1)
    db.session.commit()
    with pytest.raises(DomainError, match="expired"):
        bid_svc.award(go, bidding["alice"], b)


def test_expired_at_close_is_marked(f, bidding, db):
    go = bidding["go"]
    b = f.bid(bidding["founder"], go)
    b.expires_at = utcnow() - timedelta(seconds=1)
    db.session.commit()
    f.close_bidding(go)
    assert b.status == BidStatus.EXPIRED and b.eligible is False


# ----------------------------------------------------------------- award

def test_award_rules_and_conflict_of_interest(f, bidding, db):
    go, admin, alice, bob = bidding["go"], bidding["admin"], bidding["alice"], bidding["bob"]
    b = f.bid(bidding["founder"], go)
    f.close_bidding(go)
    with pytest.raises(PermissionDenied, match="cannot hand-pick"):
        bid_svc.award(go, admin, b)              # admin cannot hand-pick
    with pytest.raises(PermissionDenied):
        bid_svc.award(go, bob, b)                # non-organiser member cannot pick
    decision = bid_svc.award(go, alice, b)
    db.session.commit()
    assert go.status == GOStatus.AWARDED and b.status == BidStatus.AWARDED
    assert decision.platform_owned_vendor and decision.disclosure
    assert "organiser" in decision.explanation
    assert all(co.status == COStatus.AWAITING_CONFIRMATION and co.total > 0 for co in go.customer_orders)


def test_duplicate_award_rejected(f, bidding, db):
    go, alice = bidding["go"], bidding["alice"]
    b = f.bid(bidding["founder"], go)
    f.close_bidding(go)
    bid_svc.award(go, alice, b)
    db.session.commit()
    with pytest.raises(DomainError, match="already"):
        bid_svc.award(go, alice, b)
    # The database also refuses a second active award for the same order
    db.session.add(AwardDecision(group_order_id=go.id, bid_id=b.id, vendor_id=b.vendor_id, method="organiser",
                                 explanation="x"))
    with pytest.raises(Exception):
        db.session.commit()
    db.session.rollback()


def test_concurrent_award_only_one_succeeds(tmp_path):
    """Two requests award the same order at the same time: exactly one wins."""
    from app import create_app
    from app.extensions import db
    from app.models import GroupOrder, User, VendorBid
    from app.seed import seed_reference
    from tests.conftest import Factory

    import os
    url = os.environ.get("TEST_DATABASE_URL")
    if url:  # PostgreSQL: real concurrent transactions with row locking
        app = create_app("testing", SQLALCHEMY_DATABASE_URI=url, UPLOAD_FOLDER=str(tmp_path))
    else:
        app = create_app("testing", SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path}/race.db",
                         SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 15, "check_same_thread": False}},
                         UPLOAD_FOLDER=str(tmp_path))
    with app.app_context():
        db.create_all()
        seed_reference()
        f = Factory(db)
        alice = f.customer()
        g = f.group(alice)
        go = f.group_order(g, alice, selection_mode="auto_score")
        f.order(go, alice, potatoes="40", onions="20", tomatoes="15")
        f.freeze(go)
        founder = f.vendor(platform_owned=True)
        f.bid(founder, go)
        f.close_bidding(go)
        go_id, alice_id = go.id, alice.id
        db.session.remove()

    barrier = threading.Barrier(2)
    results = []

    def worker():
        with app.app_context():
            go = db.session.get(GroupOrder, go_id)
            actor = db.session.get(User, alice_id)
            _ = [b.items for b in go.bids], go.items, go.customer_orders, go.award_decisions
            barrier.wait()
            try:
                bid_svc.award(go, actor)
                db.session.commit()
                results.append("ok")
            except (ConcurrencyError, DomainError) as exc:
                db.session.rollback()
                results.append(type(exc).__name__)
            except Exception as exc:  # database-level refusal (lock/unique) also counts as rejection
                db.session.rollback()
                results.append("db:" + type(exc).__name__)
            finally:
                db.session.remove()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1, results
    with app.app_context():
        assert db.session.query(AwardDecision).filter_by(group_order_id=go_id, status="active").count() == 1
        assert db.session.query(VendorBid).filter_by(status="awarded").count() == 1
        db.session.remove()
        db.drop_all()


def test_group_vote_award(f, multi_vendor, db):
    alice, bob, carol = f.customer(), f.customer(), f.customer()
    g = f.group(alice, [bob, carol])
    go = f.group_order(g, alice, selection_mode="group_vote")
    for u in (alice, bob, carol):
        f.order(go, u, potatoes="20")
    f.freeze(go)
    founder, rival = f.vendor(platform_owned=True), f.vendor(name="Rival")
    b1 = f.bid(founder, go)
    b2 = f.bid(rival, go, prices={"potatoes": "26.00", "onions": "36.00", "tomatoes": "32.00"})
    f.close_bidding(go)
    with pytest.raises(DomainError, match="No member has voted"):
        bid_svc.award(go, alice)
    bid_svc.cast_vote(bob, go, b2)
    bid_svc.cast_vote(carol, go, b2)
    bid_svc.cast_vote(alice, go, b1)
    db.session.commit()
    decision = bid_svc.award(go, alice)
    db.session.commit()
    assert decision.bid_id == b2.id and "2 of 3 votes" in decision.explanation


def test_vendor_decline_reopens_selection(f, multi_vendor, bidding, db):
    go = bidding["go"]
    rival = f.vendor(name="Rival")
    b1 = f.bid(bidding["founder"], go)
    b2 = f.bid(rival, go, prices={"potatoes": "26.00", "onions": "37.00", "tomatoes": "33.00"})
    f.close_bidding(go)
    bid_svc.award(go, bidding["alice"], b1)
    db.session.commit()
    bid_svc.vendor_decline_award(bidding["founder"], go, "Supply shortfall")
    db.session.commit()
    assert go.status == GOStatus.SELECTION and b1.status == BidStatus.DECLINED and b2.status == BidStatus.SUBMITTED
    assert all(co.status == COStatus.LOCKED and co.total == 0 for co in go.customer_orders)
    bid_svc.award(go, bidding["alice"], b2)
    db.session.commit()
    assert go.awarded_bid.id == b2.id
    assert bid_svc.vendor_history(bidding["founder"])["cancellations"] == 1
