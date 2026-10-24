"""Product catalogue: categories, products, grades and vendor listings."""
from ..extensions import db
from ..utils import utcnow
from .base import MONEY, MONEY_PCT, QTY, TimestampMixin


class ProductCategory(db.Model):
    __tablename__ = "product_categories"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    slug = db.Column(db.String(80), unique=True, nullable=False)
    description = db.Column(db.String(300))
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    products = db.relationship("Product", back_populates="category", order_by="Product.name")


class ProductGrade(db.Model):
    """Platform-wide quality grade with a written specification."""
    __tablename__ = "product_grades"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(16), unique=True, nullable=False)
    name = db.Column(db.String(60), nullable=False)
    rank = db.Column(db.Integer, nullable=False, default=1)   # higher is better
    specification = db.Column(db.Text, nullable=False)
    max_defect_percent = db.Column(MONEY_PCT, nullable=False, default=5)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    @property
    def label(self):
        return f"{self.name} ({self.code})"


class Product(TimestampMixin, db.Model):
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("product_categories.id"), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    slug = db.Column(db.String(80), unique=True, nullable=False)
    unit = db.Column(db.String(16), nullable=False, default="kg")
    emoji = db.Column(db.String(8))
    description = db.Column(db.String(500))
    min_customer_qty = db.Column(QTY, nullable=False, default=1)
    qty_step = db.Column(QTY, nullable=False, default=0.5)
    max_customer_qty = db.Column(QTY)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    category = db.relationship("ProductCategory", back_populates="products")

    __table_args__ = (
        db.CheckConstraint("min_customer_qty > 0", name="min_qty_pos"),
        db.CheckConstraint("qty_step > 0", name="qty_step_pos"),
    )


class VendorProduct(TimestampMixin, db.Model):
    """A vendor's catalogue listing. Origin/organic data is vendor-declared."""
    __tablename__ = "vendor_products"
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    grade_id = db.Column(db.Integer, db.ForeignKey("product_grades.id"), nullable=False)
    price_per_unit = db.Column(MONEY, nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    stock_qty = db.Column(QTY, nullable=False, default=0)
    is_available = db.Column(db.Boolean, nullable=False, default=True)
    origin = db.Column(db.String(120))
    declared_organic = db.Column(db.Boolean, nullable=False, default=False)
    notes = db.Column(db.String(300))

    vendor = db.relationship("VendorProfile", back_populates="products")
    product = db.relationship("Product")
    grade = db.relationship("ProductGrade")
    tiers = db.relationship("PriceTier", back_populates="vendor_product", cascade="all, delete-orphan",
                            order_by="PriceTier.min_qty")

    __table_args__ = (
        db.UniqueConstraint("vendor_id", "product_id", "grade_id", name="uq_vendor_product_grade"),
        db.CheckConstraint("price_per_unit > 0", name="price_pos"),
        db.CheckConstraint("stock_qty >= 0", name="stock_nonneg"),
    )


class PriceTier(db.Model):
    """Quantity-based price: applies when the aggregate quantity >= min_qty."""
    __tablename__ = "price_tiers"
    id = db.Column(db.Integer, primary_key=True)
    vendor_product_id = db.Column(db.Integer, db.ForeignKey("vendor_products.id", ondelete="CASCADE"),
                                  nullable=False, index=True)
    min_qty = db.Column(QTY, nullable=False)
    price_per_unit = db.Column(MONEY, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    vendor_product = db.relationship("VendorProduct", back_populates="tiers")

    __table_args__ = (
        db.UniqueConstraint("vendor_product_id", "min_qty", name="uq_price_tier_min"),
        db.CheckConstraint("min_qty > 0", name="tier_min_pos"),
        db.CheckConstraint("price_per_unit > 0", name="tier_price_pos"),
    )
