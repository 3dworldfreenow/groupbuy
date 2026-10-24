"""Shared column types and mixins."""
from ..extensions import db
from ..utils import utcnow

# Money: 12 digits, 2 decimal places. Quantities: 3 decimal places (grams).
MONEY = db.Numeric(12, 2, asdecimal=True)
QTY = db.Numeric(12, 3, asdecimal=True)
MONEY_PCT = db.Numeric(5, 2, asdecimal=True)
SCORE = db.Numeric(7, 4, asdecimal=True)


class TimestampMixin:
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
