"""Users, roles, profiles, addresses, locations and vendor records."""
from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from ..constants import Roles, VendorStatus
from ..extensions import db
from ..utils import utcnow
from .base import MONEY_PCT, QTY, TimestampMixin


class Role(db.Model):
    __tablename__ = "roles"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(32), unique=True, nullable=False)
    description = db.Column(db.String(200))


class RoleAssignment(db.Model):
    __tablename__ = "role_assignments"
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    granted_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    granted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    role = db.relationship("Role")


class User(UserMixin, TimestampMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(32))
    active = db.Column(db.Boolean, nullable=False, default=True)
    last_login_at = db.Column(db.DateTime)
    failed_logins = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime)

    role_assignments = db.relationship(
        "RoleAssignment", foreign_keys=[RoleAssignment.user_id],
        cascade="all, delete-orphan", lazy="selectin",
    )
    addresses = db.relationship("Address", back_populates="user", cascade="all, delete-orphan",
                                order_by="Address.id")
    customer_profile = db.relationship("CustomerProfile", back_populates="user", uselist=False,
                                       cascade="all, delete-orphan")
    vendor_profile = db.relationship("VendorProfile", back_populates="user", uselist=False,
                                     foreign_keys="VendorProfile.user_id")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self):
        return bool(self.active)

    @property
    def role_names(self):
        return {ra.role.name for ra in self.role_assignments}

    def has_role(self, *names):
        return bool(self.role_names.intersection(names))

    def add_role(self, role, granted_by=None):
        if role.name not in self.role_names:
            self.role_assignments.append(
                RoleAssignment(role=role, granted_by_id=granted_by.id if granted_by else None)
            )

    def remove_role(self, name):
        self.role_assignments = [ra for ra in self.role_assignments if ra.role.name != name]

    @property
    def is_admin(self):
        return self.has_role(Roles.ADMIN)

    @property
    def is_vendor(self):
        return self.has_role(Roles.VENDOR) and self.vendor_profile is not None

    @property
    def is_customer(self):
        return self.has_role(Roles.CUSTOMER)

    @property
    def default_address(self):
        for a in self.addresses:
            if a.is_default:
                return a
        return self.addresses[0] if self.addresses else None

    def __repr__(self):
        return f"<User {self.email}>"


class CustomerProfile(TimestampMixin, db.Model):
    __tablename__ = "customer_profiles"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    preferred_location_id = db.Column(db.Integer, db.ForeignKey("locations.id"))
    household_size = db.Column(db.Integer)
    notes = db.Column(db.String(500))

    user = db.relationship("User", back_populates="customer_profile")
    preferred_location = db.relationship("Location")


class Location(db.Model):
    __tablename__ = "locations"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    city = db.Column(db.String(80), nullable=False)
    state = db.Column(db.String(80))
    country = db.Column(db.String(2), nullable=False, default="IN")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    __table_args__ = (db.UniqueConstraint("name", "city", name="uq_locations_name_city"),)

    pickup_points = db.relationship("PickupPoint", back_populates="location", order_by="PickupPoint.name")

    @property
    def label(self):
        return f"{self.name}, {self.city}"


class PickupPoint(db.Model):
    __tablename__ = "pickup_points"
    id = db.Column(db.Integer, primary_key=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    address_text = db.Column(db.String(300), nullable=False)
    instructions = db.Column(db.String(300))
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    location = db.relationship("Location", back_populates="pickup_points")


class Address(TimestampMixin, db.Model):
    __tablename__ = "addresses"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    label = db.Column(db.String(40), nullable=False, default="Home")
    recipient_name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(32), nullable=False)
    line1 = db.Column(db.String(200), nullable=False)
    line2 = db.Column(db.String(200))
    city = db.Column(db.String(80), nullable=False)
    state = db.Column(db.String(80))
    postal_code = db.Column(db.String(16), nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"))
    is_default = db.Column(db.Boolean, nullable=False, default=False)

    user = db.relationship("User", back_populates="addresses")
    location = db.relationship("Location")

    def as_text(self):
        parts = [self.recipient_name, self.line1, self.line2, self.city, self.state, self.postal_code,
                 f"Phone: {self.phone}"]
        return ", ".join(p for p in parts if p)


vendor_service_areas = db.Table(
    "vendor_service_areas",
    db.Column("vendor_id", db.Integer, db.ForeignKey("vendor_profiles.id", ondelete="CASCADE"), primary_key=True),
    db.Column("location_id", db.Integer, db.ForeignKey("locations.id", ondelete="CASCADE"), primary_key=True),
)


class VendorProfile(TimestampMixin, db.Model):
    __tablename__ = "vendor_profiles"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    business_name = db.Column(db.String(160), nullable=False)
    legal_name = db.Column(db.String(160))
    registration_number = db.Column(db.String(80))
    tax_id = db.Column(db.String(40))
    food_licence_number = db.Column(db.String(60))
    contact_phone = db.Column(db.String(32), nullable=False)
    business_address = db.Column(db.String(300), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.String(16), nullable=False, default=VendorStatus.PENDING, index=True)
    status_reason = db.Column(db.String(500))
    # True for the founder-operated vendor. Shown to customers on every offer.
    is_platform_owned = db.Column(db.Boolean, nullable=False, default=False)
    # Maximum total quantity (in product units) the vendor can supply per group order.
    capacity_per_order = db.Column(QTY)
    commission_rate_override = db.Column(MONEY_PCT)
    approved_at = db.Column(db.DateTime)
    approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    user = db.relationship("User", back_populates="vendor_profile", foreign_keys=[user_id])
    documents = db.relationship("VendorDocument", back_populates="vendor", order_by="VendorDocument.id.desc()")
    service_areas = db.relationship("Location", secondary=vendor_service_areas, lazy="selectin")
    products = db.relationship("VendorProduct", back_populates="vendor")

    __table_args__ = (
        db.CheckConstraint("capacity_per_order IS NULL OR capacity_per_order >= 0", name="capacity_nonneg"),
    )

    @property
    def is_approved(self):
        return self.status == VendorStatus.APPROVED

    def serves(self, location_id):
        return not self.service_areas or any(l.id == location_id for l in self.service_areas)


class VendorDocument(db.Model):
    __tablename__ = "vendor_documents"
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    doc_type = db.Column(db.String(40), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False, unique=True)
    content_type = db.Column(db.String(80), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(16), nullable=False, default="pending")   # pending/verified/rejected
    review_notes = db.Column(db.String(500))
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    vendor = db.relationship("VendorProfile", back_populates="documents")
