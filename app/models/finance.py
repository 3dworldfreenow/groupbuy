"""Payments, refunds, commissions, settlements and cost records."""
from ..extensions import db
from ..utils import utcnow
from .base import MONEY, MONEY_PCT, TimestampMixin


class PaymentRecord(TimestampMixin, db.Model):
    __tablename__ = "payment_records"
    id = db.Column(db.Integer, primary_key=True)
    customer_order_id = db.Column(db.Integer, db.ForeignKey("customer_orders.id"), nullable=False, index=True)
    amount = db.Column(MONEY, nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    provider = db.Column(db.String(30), nullable=False, default="manual")
    method = db.Column(db.String(30))               # cash / upi / bank_transfer / card ...
    provider_ref = db.Column(db.String(120))
    status = db.Column(db.String(16), nullable=False, default="pending", index=True)
    # True only after the server confirmed the payment (admin verification for
    # manual payments, signed webhook or API lookup for gateways).
    verified_server_side = db.Column(db.Boolean, nullable=False, default=False)
    initiated_at = db.Column(db.DateTime)
    confirmed_at = db.Column(db.DateTime)
    failure_reason = db.Column(db.String(300))
    recorded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    notes = db.Column(db.String(500))

    customer_order = db.relationship("CustomerOrder", back_populates="payments")
    recorded_by = db.relationship("User")

    __table_args__ = (db.CheckConstraint("amount > 0", name="payment_amount_pos"),)


class Refund(db.Model):
    __tablename__ = "refunds"
    id = db.Column(db.Integer, primary_key=True)
    customer_order_id = db.Column(db.Integer, db.ForeignKey("customer_orders.id"), nullable=False, index=True)
    payment_id = db.Column(db.Integer, db.ForeignKey("payment_records.id"))
    complaint_id = db.Column(db.Integer, db.ForeignKey("complaints.id"))
    amount = db.Column(MONEY, nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    reason = db.Column(db.String(500), nullable=False)
    charged_to = db.Column(db.String(10), nullable=False, default="vendor")   # vendor / platform
    status = db.Column(db.String(16), nullable=False, default="processed")
    reference = db.Column(db.String(120))
    processed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    processed_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    customer_order = db.relationship("CustomerOrder", back_populates="refunds")
    complaint = db.relationship("Complaint", back_populates="refunds")

    __table_args__ = (db.CheckConstraint("amount > 0", name="refund_amount_pos"),)


class CommissionRecord(db.Model):
    __tablename__ = "commission_records"
    id = db.Column(db.Integer, primary_key=True)
    group_order_id = db.Column(db.Integer, db.ForeignKey("group_orders.id"), nullable=False, unique=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id"), nullable=False, index=True)
    bid_id = db.Column(db.Integer, db.ForeignKey("vendor_bids.id"), nullable=False)
    base_amount = db.Column(MONEY, nullable=False)       # goods value net of vendor-charged refunds
    rate_percent = db.Column(MONEY_PCT, nullable=False)
    amount = db.Column(MONEY, nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    vendor = db.relationship("VendorProfile")
    group_order = db.relationship("GroupOrder")


class VendorSettlementRecord(db.Model):
    __tablename__ = "vendor_settlements"
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id"), nullable=False, index=True)
    group_order_id = db.Column(db.Integer, db.ForeignKey("group_orders.id"), nullable=False, unique=True)
    goods_amount = db.Column(MONEY, nullable=False)
    delivery_fees = db.Column(MONEY, nullable=False)
    refunds_deducted = db.Column(MONEY, nullable=False)
    commission_amount = db.Column(MONEY, nullable=False)
    net_payable = db.Column(MONEY, nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    status = db.Column(db.String(10), nullable=False, default="pending")
    paid_at = db.Column(db.DateTime)
    reference = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    vendor = db.relationship("VendorProfile")
    group_order = db.relationship("GroupOrder")


class CostRecord(db.Model):
    """Operating costs: founder-vendor purchase costs, delivery, packaging,
    spoilage and platform operating costs."""
    __tablename__ = "cost_records"
    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(16), nullable=False, index=True)
    amount = db.Column(MONEY, nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    group_order_id = db.Column(db.Integer, db.ForeignKey("group_orders.id"), index=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id"), index=True)
    description = db.Column(db.String(300), nullable=False)
    incurred_on = db.Column(db.Date, nullable=False)
    recorded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    group_order = db.relationship("GroupOrder")
    vendor = db.relationship("VendorProfile")

    __table_args__ = (db.CheckConstraint("amount > 0", name="cost_amount_pos"),)
