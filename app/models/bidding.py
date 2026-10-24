"""Vendor bids, votes and award decisions."""
from ..constants import BidStatus
from ..extensions import db
from ..utils import json_loads, utcnow
from .base import MONEY, MONEY_PCT, QTY, SCORE, TimestampMixin


class VendorBid(TimestampMixin, db.Model):
    """A sealed offer for a whole group order. One per vendor per group order;
    revisions update the row and increment ``revision``."""
    __tablename__ = "vendor_bids"
    id = db.Column(db.Integer, primary_key=True)
    group_order_id = db.Column(db.Integer, db.ForeignKey("group_orders.id"), nullable=False, index=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id"), nullable=False, index=True)
    status = db.Column(db.String(16), nullable=False, default=BidStatus.SUBMITTED, index=True)
    grade_id = db.Column(db.Integer, db.ForeignKey("product_grades.id"), nullable=False)
    origin = db.Column(db.String(160))
    substitution_policy = db.Column(db.String(20), nullable=False, default="none")
    currency = db.Column(db.String(3), nullable=False)
    delivery_fee = db.Column(MONEY, nullable=False, default=0)          # whole group, shared
    home_delivery_fee = db.Column(MONEY, nullable=False, default=0)     # per home-delivery customer
    subtotal = db.Column(MONEY, nullable=False, default=0)
    total_offered_qty = db.Column(QTY, nullable=False, default=0)
    estimated_delivery_at = db.Column(db.DateTime, nullable=False)
    submitted_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    commission_rate = db.Column(MONEY_PCT, nullable=False)
    is_platform_owned = db.Column(db.Boolean, nullable=False, default=False)
    notes = db.Column(db.String(1000))
    revision = db.Column(db.Integer, nullable=False, default=1)
    score = db.Column(SCORE)
    score_breakdown = db.Column(db.Text)
    eligible = db.Column(db.Boolean)
    eligibility_notes = db.Column(db.String(1000))
    # Internal audit metadata (never shown to other vendors or customers)
    submitted_ip = db.Column(db.String(64))
    submitted_user_agent = db.Column(db.String(255))

    group_order = db.relationship("GroupOrder", back_populates="bids")
    vendor = db.relationship("VendorProfile")
    grade = db.relationship("ProductGrade")
    items = db.relationship("VendorBidItem", back_populates="bid", cascade="all, delete-orphan",
                            order_by="VendorBidItem.id")

    __table_args__ = (
        db.UniqueConstraint("group_order_id", "vendor_id", name="uq_bid_go_vendor"),
        db.CheckConstraint("delivery_fee >= 0", name="delivery_fee_nonneg"),
        db.CheckConstraint("home_delivery_fee >= 0", name="home_fee_nonneg"),
    )

    @property
    def status_label(self):
        return BidStatus.LABELS.get(self.status, self.status)

    @property
    def breakdown(self):
        return json_loads(self.score_breakdown, {})

    @property
    def is_expired(self):
        return self.expires_at is not None and self.expires_at <= utcnow()


class VendorBidItem(db.Model):
    __tablename__ = "vendor_bid_items"
    id = db.Column(db.Integer, primary_key=True)
    bid_id = db.Column(db.Integer, db.ForeignKey("vendor_bids.id", ondelete="CASCADE"), nullable=False, index=True)
    group_order_item_id = db.Column(db.Integer, db.ForeignKey("group_order_items.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    offered_qty = db.Column(QTY, nullable=False)
    unit_price = db.Column(MONEY, nullable=False)
    line_total = db.Column(MONEY, nullable=False)
    origin = db.Column(db.String(120))

    bid = db.relationship("VendorBid", back_populates="items")
    group_order_item = db.relationship("GroupOrderItem")
    product = db.relationship("Product")

    __table_args__ = (
        db.UniqueConstraint("bid_id", "group_order_item_id", name="uq_bid_item"),
        db.CheckConstraint("offered_qty >= 0", name="offered_nonneg"),
        db.CheckConstraint("unit_price >= 0", name="unit_price_nonneg"),
    )


class OfferVote(db.Model):
    __tablename__ = "offer_votes"
    id = db.Column(db.Integer, primary_key=True)
    group_order_id = db.Column(db.Integer, db.ForeignKey("group_orders.id"), nullable=False, index=True)
    bid_id = db.Column(db.Integer, db.ForeignKey("vendor_bids.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (db.UniqueConstraint("group_order_id", "user_id", name="uq_vote_go_user"),)


class AwardDecision(db.Model):
    """Immutable record of why an order was awarded to a bid.

    A partial unique index guarantees at most one *active* award per group order
    at the database level, in addition to optimistic locking on GroupOrder.
    """
    __tablename__ = "award_decisions"
    id = db.Column(db.Integer, primary_key=True)
    group_order_id = db.Column(db.Integer, db.ForeignKey("group_orders.id"), nullable=False)
    bid_id = db.Column(db.Integer, db.ForeignKey("vendor_bids.id"), nullable=False)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id"), nullable=False)
    method = db.Column(db.String(16), nullable=False)
    decided_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    decided_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    explanation = db.Column(db.Text, nullable=False)
    scores_snapshot = db.Column(db.Text)
    platform_owned_vendor = db.Column(db.Boolean, nullable=False, default=False)
    disclosure = db.Column(db.Text)
    status = db.Column(db.String(10), nullable=False, default="active")   # active / revoked
    revoked_at = db.Column(db.DateTime)
    revoked_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    revoke_reason = db.Column(db.String(500))

    group_order = db.relationship("GroupOrder", back_populates="award_decisions")
    bid = db.relationship("VendorBid")
    vendor = db.relationship("VendorProfile")
    decided_by = db.relationship("User", foreign_keys=[decided_by_id])

    __table_args__ = (
        db.Index(
            "uq_award_active_per_order", "group_order_id", unique=True,
            sqlite_where=db.text("status = 'active'"),
            postgresql_where=db.text("status = 'active'"),
        ),
    )

    @property
    def snapshot(self):
        return json_loads(self.scores_snapshot, [])
