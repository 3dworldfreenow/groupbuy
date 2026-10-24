"""Quality assurance, delivery, attachments."""
from ..extensions import db
from ..utils import json_loads, utcnow
from .base import MONEY, QTY, TimestampMixin


class InventoryBatch(db.Model):
    """A lot of produce sourced by a vendor. Information is vendor-declared."""
    __tablename__ = "inventory_batches"
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id"), nullable=False, index=True)
    group_order_id = db.Column(db.Integer, db.ForeignKey("group_orders.id"), index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    grade_id = db.Column(db.Integer, db.ForeignKey("product_grades.id"))
    lot_code = db.Column(db.String(60), nullable=False)
    quantity = db.Column(QTY, nullable=False)
    source_name = db.Column(db.String(160))          # farm, mandi or supplier
    origin = db.Column(db.String(160))
    harvest_date = db.Column(db.Date)
    sourced_on = db.Column(db.Date)
    purchase_cost = db.Column(MONEY)                 # optional, feeds cost reports
    notes = db.Column(db.String(500))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    vendor = db.relationship("VendorProfile")
    product = db.relationship("Product")
    grade = db.relationship("ProductGrade")
    group_order = db.relationship("GroupOrder")

    __table_args__ = (
        db.UniqueConstraint("vendor_id", "lot_code", name="uq_batch_vendor_lot"),
        db.CheckConstraint("quantity > 0", name="batch_qty_pos"),
    )


class QualityInspection(db.Model):
    __tablename__ = "quality_inspections"
    id = db.Column(db.Integer, primary_key=True)
    group_order_id = db.Column(db.Integer, db.ForeignKey("group_orders.id"), nullable=False, index=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("inventory_batches.id"))
    inspector_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    stage = db.Column(db.String(16), nullable=False, default="pre_dispatch")  # pre_dispatch / on_delivery
    verification = db.Column(db.String(20), nullable=False)  # vendor_declared / platform_verified
    result = db.Column(db.String(12), nullable=False)
    checklist = db.Column(db.Text)
    defect_notes = db.Column(db.Text)
    spoilage_qty = db.Column(QTY, nullable=False, default=0)
    delivery_condition = db.Column(db.String(300))
    placed_on_hold = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    group_order = db.relationship("GroupOrder")
    batch = db.relationship("InventoryBatch")
    inspector = db.relationship("User")
    items = db.relationship("InspectionItem", back_populates="inspection", cascade="all, delete-orphan",
                            order_by="InspectionItem.id")

    @property
    def checklist_data(self):
        return json_loads(self.checklist, {})


class InspectionItem(db.Model):
    __tablename__ = "inspection_items"
    id = db.Column(db.Integer, primary_key=True)
    inspection_id = db.Column(db.Integer, db.ForeignKey("quality_inspections.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    expected_qty = db.Column(QTY, nullable=False, default=0)
    received_qty = db.Column(QTY, nullable=False, default=0)
    accepted_qty = db.Column(QTY, nullable=False, default=0)
    rejected_qty = db.Column(QTY, nullable=False, default=0)
    notes = db.Column(db.String(300))

    inspection = db.relationship("QualityInspection", back_populates="items")
    product = db.relationship("Product")

    __table_args__ = (
        db.CheckConstraint("accepted_qty >= 0 AND rejected_qty >= 0 AND received_qty >= 0",
                           name="inspection_qty_nonneg"),
    )


class Delivery(TimestampMixin, db.Model):
    """Fulfilment of one customer order (community pickup or home delivery)."""
    __tablename__ = "deliveries"
    id = db.Column(db.Integer, primary_key=True)
    group_order_id = db.Column(db.Integer, db.ForeignKey("group_orders.id"), nullable=False, index=True)
    customer_order_id = db.Column(db.Integer, db.ForeignKey("customer_orders.id"), nullable=False, unique=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id"), nullable=False, index=True)
    method = db.Column(db.String(10), nullable=False)
    pickup_point_id = db.Column(db.Integer, db.ForeignKey("pickup_points.id"))
    address_text = db.Column(db.String(600))
    window_start = db.Column(db.DateTime)
    window_end = db.Column(db.DateTime)
    fee = db.Column(MONEY, nullable=False, default=0)
    assigned_to = db.Column(db.String(120))
    status = db.Column(db.String(12), nullable=False, default="scheduled", index=True)
    provider = db.Column(db.String(30), nullable=False, default="manual")
    provider_ref = db.Column(db.String(120))
    attempts = db.Column(db.Integer, nullable=False, default=0)
    dispatched_at = db.Column(db.DateTime)
    delivered_at = db.Column(db.DateTime)
    received_by = db.Column(db.String(120))
    proof_notes = db.Column(db.String(500))
    condition_notes = db.Column(db.String(500))
    failure_reason = db.Column(db.String(300))
    customer_confirmed_at = db.Column(db.DateTime)

    group_order = db.relationship("GroupOrder")
    customer_order = db.relationship("CustomerOrder", back_populates="delivery")
    vendor = db.relationship("VendorProfile")
    pickup_point = db.relationship("PickupPoint")

    @property
    def on_time(self):
        if not self.delivered_at or not self.window_end:
            return None
        return self.delivered_at <= self.window_end


class Attachment(db.Model):
    """Private uploaded file (photo/proof) linked to an entity. Served only
    through an authorization-checked route."""
    __tablename__ = "attachments"
    id = db.Column(db.Integer, primary_key=True)
    entity_type = db.Column(db.String(30), nullable=False)
    entity_id = db.Column(db.Integer, nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False, unique=True)
    content_type = db.Column(db.String(80), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    caption = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    __table_args__ = (db.Index("ix_attachments_entity", "entity_type", "entity_id"),)
