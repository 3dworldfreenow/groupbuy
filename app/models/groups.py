"""Buying groups, group orders (buying rounds) and customer orders."""
from ..constants import COStatus, GOStatus, PaymentStatus
from ..extensions import db
from ..utils import utcnow
from .base import MONEY, MONEY_PCT, QTY, TimestampMixin


class BuyingGroup(TimestampMixin, db.Model):
    __tablename__ = "buying_groups"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(1000))
    organiser_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False, index=True)
    pickup_point_id = db.Column(db.Integer, db.ForeignKey("pickup_points.id"))
    invite_code = db.Column(db.String(16), unique=True, nullable=False)
    is_public = db.Column(db.Boolean, nullable=False, default=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    organiser = db.relationship("User")
    location = db.relationship("Location")
    pickup_point = db.relationship("PickupPoint")
    memberships = db.relationship("GroupMembership", back_populates="group", cascade="all, delete-orphan")
    orders = db.relationship("GroupOrder", back_populates="group", order_by="GroupOrder.id.desc()")

    @property
    def active_members(self):
        return [m for m in self.memberships if m.status == "active"]

    def membership_for(self, user):
        if user is None or not getattr(user, "id", None):
            return None
        for m in self.memberships:
            if m.user_id == user.id and m.status == "active":
                return m
        return None

    @property
    def current_order(self):
        for o in self.orders:
            if o.status in GOStatus.ACTIVE:
                return o
        return None


class GroupMembership(db.Model):
    __tablename__ = "group_memberships"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("buying_groups.id", ondelete="CASCADE"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    role = db.Column(db.String(16), nullable=False, default="member")   # organiser / member
    status = db.Column(db.String(16), nullable=False, default="active")  # active / left / removed
    joined_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    group = db.relationship("BuyingGroup", back_populates="memberships")
    user = db.relationship("User")

    __table_args__ = (db.UniqueConstraint("group_id", "user_id", name="uq_membership_group_user"),)


class GroupOrder(TimestampMixin, db.Model):
    """One buying round of a group: demand collection, offers, fulfilment."""
    __tablename__ = "group_orders"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("buying_groups.id"), nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    status = db.Column(db.String(20), nullable=False, default=GOStatus.DRAFT, index=True)
    currency = db.Column(db.String(3), nullable=False)
    order_deadline = db.Column(db.DateTime, nullable=False)
    bidding_deadline = db.Column(db.DateTime)
    delivery_window_start = db.Column(db.DateTime, nullable=False)
    delivery_window_end = db.Column(db.DateTime, nullable=False)
    fulfilment_options = db.Column(db.String(10), nullable=False, default="both")
    pickup_point_id = db.Column(db.Integer, db.ForeignKey("pickup_points.id"))
    allow_partial = db.Column(db.Boolean, nullable=False, default=False)
    min_fulfilment_percent = db.Column(MONEY_PCT, nullable=False, default=100)
    selection_mode = db.Column(db.String(16), nullable=False, default="organiser")
    bidding_mode = db.Column(db.String(16))
    service_fee_percent = db.Column(MONEY_PCT, nullable=False, default=0)
    notes = db.Column(db.String(1000))

    frozen_at = db.Column(db.DateTime)
    bidding_opened_at = db.Column(db.DateTime)
    selection_started_at = db.Column(db.DateTime)
    awarded_at = db.Column(db.DateTime)
    dispatched_at = db.Column(db.DateTime)
    delivered_at = db.Column(db.DateTime)
    closed_at = db.Column(db.DateTime)
    cancelled_at = db.Column(db.DateTime)
    cancellation_reason = db.Column(db.String(500))
    hold_from_status = db.Column(db.String(20))
    hold_reason = db.Column(db.String(500))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    # Optimistic locking: every UPDATE checks and bumps the version, so two
    # conflicting requests can never both succeed (e.g. double award).
    version = db.Column(db.Integer, nullable=False, default=1)
    __mapper_args__ = {"version_id_col": version}

    group = db.relationship("BuyingGroup", back_populates="orders")
    pickup_point = db.relationship("PickupPoint")
    items = db.relationship("GroupOrderItem", back_populates="group_order", cascade="all, delete-orphan",
                            order_by="GroupOrderItem.id")
    customer_orders = db.relationship("CustomerOrder", back_populates="group_order")
    bids = db.relationship("VendorBid", back_populates="group_order")
    award_decisions = db.relationship("AwardDecision", back_populates="group_order",
                                      order_by="AwardDecision.id.desc()")

    __table_args__ = (
        db.CheckConstraint("delivery_window_end >= delivery_window_start", name="delivery_window_order"),
        db.CheckConstraint("min_fulfilment_percent >= 0 AND min_fulfilment_percent <= 100",
                           name="min_fulfilment_range"),
    )

    @property
    def status_label(self):
        return GOStatus.LABELS.get(self.status, self.status)

    @property
    def active_award(self):
        for a in self.award_decisions:
            if a.status == "active":
                return a
        return None

    @property
    def awarded_bid(self):
        award = self.active_award
        return award.bid if award else None

    @property
    def live_customer_orders(self):
        return [co for co in self.customer_orders if co.status != COStatus.CANCELLED]


class GroupOrderItem(db.Model):
    __tablename__ = "group_order_items"
    id = db.Column(db.Integer, primary_key=True)
    group_order_id = db.Column(db.Integer, db.ForeignKey("group_orders.id", ondelete="CASCADE"),
                               nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    min_grade_id = db.Column(db.Integer, db.ForeignKey("product_grades.id"))
    target_qty = db.Column(QTY, nullable=False)
    min_qty = db.Column(QTY, nullable=False, default=0)      # minimum aggregate to proceed
    max_qty = db.Column(QTY)                                 # cap on aggregate demand
    per_customer_max = db.Column(QTY)
    frozen_qty = db.Column(QTY)                              # aggregate at freeze time
    dropped = db.Column(db.Boolean, nullable=False, default=False)   # minimum not reached

    group_order = db.relationship("GroupOrder", back_populates="items")
    product = db.relationship("Product")
    min_grade = db.relationship("ProductGrade")

    __table_args__ = (
        db.UniqueConstraint("group_order_id", "product_id", name="uq_go_item_product"),
        db.CheckConstraint("target_qty > 0", name="target_pos"),
        db.CheckConstraint("min_qty >= 0", name="min_nonneg"),
        db.CheckConstraint("max_qty IS NULL OR max_qty >= min_qty", name="max_ge_min"),
    )


class CustomerOrder(TimestampMixin, db.Model):
    __tablename__ = "customer_orders"
    id = db.Column(db.Integer, primary_key=True)
    group_order_id = db.Column(db.Integer, db.ForeignKey("group_orders.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    status = db.Column(db.String(24), nullable=False, default=COStatus.OPEN, index=True)
    fulfilment_method = db.Column(db.String(10), nullable=False, default="pickup")
    address_id = db.Column(db.Integer, db.ForeignKey("addresses.id", ondelete="SET NULL"))
    delivery_address_snapshot = db.Column(db.String(600))
    currency = db.Column(db.String(3), nullable=False)
    subtotal = db.Column(MONEY, nullable=False, default=0)
    delivery_fee = db.Column(MONEY, nullable=False, default=0)        # share of the group delivery fee
    home_delivery_fee = db.Column(MONEY, nullable=False, default=0)
    service_fee = db.Column(MONEY, nullable=False, default=0)          # platform fee
    total = db.Column(MONEY, nullable=False, default=0)
    payment_status = db.Column(db.String(24), nullable=False, default=PaymentStatus.NOT_REQUESTED)
    priced_at = db.Column(db.DateTime)
    confirmed_at = db.Column(db.DateTime)
    delivered_at = db.Column(db.DateTime)
    cancelled_at = db.Column(db.DateTime)
    cancel_reason = db.Column(db.String(300))

    group_order = db.relationship("GroupOrder", back_populates="customer_orders")
    user = db.relationship("User")
    address = db.relationship("Address")
    items = db.relationship("CustomerOrderItem", back_populates="customer_order", cascade="all, delete-orphan",
                            order_by="CustomerOrderItem.id")
    payments = db.relationship("PaymentRecord", back_populates="customer_order", order_by="PaymentRecord.id")
    refunds = db.relationship("Refund", back_populates="customer_order", order_by="Refund.id")
    delivery = db.relationship("Delivery", back_populates="customer_order", uselist=False)

    __table_args__ = (
        db.UniqueConstraint("group_order_id", "user_id", name="uq_customer_order_go_user"),
        db.CheckConstraint("total >= 0", name="total_nonneg"),
    )

    @property
    def status_label(self):
        return COStatus.LABELS.get(self.status, self.status)

    @property
    def reference(self):
        return f"GB-{self.group_order_id:04d}-{self.id:05d}"


class CustomerOrderItem(db.Model):
    __tablename__ = "customer_order_items"
    id = db.Column(db.Integer, primary_key=True)
    customer_order_id = db.Column(db.Integer, db.ForeignKey("customer_orders.id", ondelete="CASCADE"),
                                  nullable=False, index=True)
    group_order_item_id = db.Column(db.Integer, db.ForeignKey("group_order_items.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(QTY, nullable=False)              # requested
    allocated_qty = db.Column(QTY)                          # confirmed by the awarded offer
    unit_price = db.Column(MONEY)
    line_total = db.Column(MONEY)
    delivered_qty = db.Column(QTY)

    customer_order = db.relationship("CustomerOrder", back_populates="items")
    group_order_item = db.relationship("GroupOrderItem")
    product = db.relationship("Product")

    __table_args__ = (
        db.UniqueConstraint("customer_order_id", "product_id", name="uq_co_item_product"),
        db.CheckConstraint("quantity > 0", name="qty_pos"),
    )
