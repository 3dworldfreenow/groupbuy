import os
import sys
from datetime import timedelta
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app  # noqa: E402
from app.constants import Roles  # noqa: E402
from app.extensions import db as _db  # noqa: E402
from app.models import Location, Product, ProductGrade  # noqa: E402
from app.security import limiter  # noqa: E402
from app.seed import seed_reference  # noqa: E402
from app.services import groups as group_svc  # noqa: E402
from app.services import settings as settings_svc  # noqa: E402
from app.services.support import create_user, create_vendor_profile, set_vendor_status  # noqa: E402
from app.utils import utcnow  # noqa: E402

PASSWORD = "Test-password-123"


@pytest.fixture
def app(tmp_path):
    app = create_app("testing", UPLOAD_FOLDER=str(tmp_path / "uploads"))
    with app.app_context():
        _db.create_all()
        seed_reference()
        yield app
        _db.session.remove()
        _db.drop_all()
    limiter.reset()


@pytest.fixture
def db(app):
    return _db


@pytest.fixture
def client(app):
    return app.test_client()


class Factory:
    def __init__(self, db):
        self.db = db
        self.n = 0

    def user(self, roles=(Roles.CUSTOMER,), name=None, email=None):
        self.n += 1
        u = create_user(email=email or f"user{self.n}@example.com", password=PASSWORD,
                        full_name=name or f"User {self.n}", phone="+91 90000 0000", roles=roles)
        self.db.session.commit()
        return u

    def customer(self, name=None):
        return self.user((Roles.CUSTOMER,), name)

    def admin(self):
        return self.user((Roles.ADMIN,), "Admin")

    def vendor(self, platform_owned=False, approved=True, capacity="1000", name=None):
        u = self.user((), name or "Vendor")
        vp = create_vendor_profile(u, business_name=name or f"Vendor {self.n}", contact_phone="+91 90000 11111",
                                   business_address="Market", capacity_per_order=capacity,
                                   platform_owned=platform_owned)
        if approved and not platform_owned:
            set_vendor_status(vp, None, "approved")
        self.db.session.commit()
        return vp

    def product(self, slug):
        return self.db.session.query(Product).filter_by(slug=slug).one()

    def grade(self, code="STANDARD"):
        return self.db.session.query(ProductGrade).filter_by(code=code).one()

    def location(self, name="Green Park"):
        return self.db.session.query(Location).filter_by(name=name).one()

    def group(self, organiser, members=(), name="Test Group"):
        loc = self.location()
        g = group_svc.create_group(organiser, name=name, description="", location=loc,
                                   pickup_point=loc.pickup_points[0])
        for m in members:
            group_svc.join_group(m, g)
        self.db.session.commit()
        return g

    def group_order(self, group, organiser, items=None, selection_mode="organiser", allow_partial=False,
                    min_fulfilment="100", open_now=True):
        now = utcnow()
        go = group_svc.create_group_order(group, organiser, title="Round",
                                          order_deadline=now + timedelta(days=1),
                                          delivery_window_start=now + timedelta(days=3),
                                          delivery_window_end=now + timedelta(days=3, hours=4),
                                          selection_mode=selection_mode, allow_partial=allow_partial,
                                          min_fulfilment_percent=min_fulfilment)
        items = items or [("potatoes", "100", "10", "150"), ("onions", "60", "10", "100"),
                          ("tomatoes", "40", "10", "60")]
        for slug, target, mn, mx in items:
            group_svc.set_item(go, organiser, product=self.product(slug), target_qty=target, min_qty=mn, max_qty=mx)
        if open_now:
            group_svc.open_for_orders(go, organiser)
        self.db.session.commit()
        return go

    def order(self, go, user, **by_slug):
        ids = {i.product.slug: i.id for i in go.items}
        group_svc.update_customer_quantities(go, user, {ids[s]: q for s, q in by_slug.items()})
        self.db.session.commit()

    def freeze(self, go):
        go.order_deadline = utcnow() - timedelta(seconds=1)
        group_svc.freeze(go, None)
        self.db.session.commit()

    def bid_items(self, go, prices, qty_factor=Decimal("1")):
        return {i.id: {"offered_qty": str((i.frozen_qty * qty_factor).quantize(Decimal("0.001"))),
                       "unit_price": prices[i.product.slug]} for i in go.items if not i.dropped}

    def bid(self, vendor, go, prices=None, grade="STANDARD", delivery_fee="100.00", qty_factor=Decimal("1"),
            expires=None, **kw):
        from app.services.bidding import submit_bid
        prices = prices or {"potatoes": "25.00", "onions": "36.00", "tomatoes": "32.00"}
        b = submit_bid(vendor, go, items=self.bid_items(go, prices, qty_factor), grade_id=self.grade(grade).id,
                       delivery_fee=delivery_fee, estimated_delivery_at=go.delivery_window_start,
                       expires_at=expires or go.bidding_deadline + timedelta(days=2), **kw)
        self.db.session.commit()
        return b

    def close_bidding(self, go):
        from app.services.bidding import close_bidding
        go.bidding_deadline = utcnow() - timedelta(seconds=1)
        close_bidding(go, None)
        self.db.session.commit()


@pytest.fixture
def f(db):
    return Factory(db)


@pytest.fixture
def multi_vendor(app):
    settings_svc.set_value("multi_vendor_enabled", True)
    _db.session.commit()


def login(client, email, password=PASSWORD):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


@pytest.fixture
def scenario(f):
    """Three customers in one group with the 100/60/40 kg sample demand (200 kg)."""
    alice, bob, carol = f.customer("Alice"), f.customer("Bob"), f.customer("Carol")
    group = f.group(alice, [bob, carol])
    go = f.group_order(group, alice)
    f.order(go, alice, potatoes="40", onions="20", tomatoes="15")
    f.order(go, bob, potatoes="35", onions="25", tomatoes="15")
    f.order(go, carol, potatoes="25", onions="15", tomatoes="10")
    founder = f.vendor(platform_owned=True, name="Founder Produce")
    admin = f.admin()
    return {"alice": alice, "bob": bob, "carol": carol, "group": group, "go": go, "founder": founder, "admin": admin}
