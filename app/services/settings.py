"""Typed, admin-configurable platform settings stored in PlatformSetting."""
from decimal import Decimal

from flask import current_app, g, has_app_context

from ..constants import SelectionMode
from ..extensions import db
from ..models import PlatformSetting
from ..utils import to_decimal
from .core import DomainError, audit

# key: (type, default, label, help)
DEFINITIONS = {
    "multi_vendor_enabled": ("bool", False, "Multi-vendor bidding (Stage 2)",
                             "Allow approved external vendors to bid. When off, only the platform-owned "
                             "(founder) vendor can publish offers."),
    "commission_rate_percent": ("decimal", "5.00", "Platform commission (%)",
                                "Charged on goods value of completed orders. Applies equally to the founder vendor."),
    "service_fee_percent": ("decimal", "0.00", "Customer service fee (%)",
                            "Optional platform fee added to each customer order subtotal."),
    "default_bidding_hours": ("int", 24, "Bidding window (hours)",
                              "Default time vendors have to submit sealed offers after orders freeze."),
    "min_bid_validity_hours": ("int", 24, "Minimum bid validity after deadline (hours)",
                               "Bids must remain valid at least this long after the bidding deadline."),
    "default_selection_mode": ("choice", SelectionMode.ORGANISER, "Default award policy",
                               "Applied to new group orders. Disclosed to members before they order."),
    "default_item_min_qty": ("decimal", "10", "Default minimum aggregate quantity per item",
                             "Items below their minimum at the deadline are removed from the order."),
    "score_weight_price": ("int", 50, "Score weight: total customer cost", ""),
    "score_weight_quality": ("int", 20, "Score weight: quality compliance", ""),
    "score_weight_on_time": ("int", 15, "Score weight: on-time delivery history", ""),
    "score_weight_cancellation": ("int", 5, "Score weight: low cancellation history", ""),
    "score_weight_rating": ("int", 10, "Score weight: verified customer ratings", ""),
    "require_platform_inspection": ("bool", True, "Require platform inspection before packing",
                                    "When on, a passing platform-verified inspection is required before an "
                                    "order can be packed. Vendor self-checks alone are not enough."),
    "complaint_window_hours": ("int", 48, "Complaint window after delivery (hours)",
                               "Orders can be closed after this window if no complaints remain open."),
    "payment_instructions": ("text",
                             "Pay the amount shown by UPI, bank transfer or cash to the platform team. "
                             "Quote your order reference. Payments are marked as paid only after an "
                             "administrator verifies them.",
                             "Manual payment instructions", "Shown to customers after they confirm an order."),
    "platform_owner_disclosure": ("text",
                                  "This vendor is operated by the platform owner. It must follow the same "
                                  "published product, pricing, quality and award rules as every other vendor. "
                                  "Platform administrators cannot see competing bid values before the bidding "
                                  "deadline and cannot hand-pick the winning offer.",
                                  "Platform-owned vendor disclosure", "Shown wherever the founder vendor makes an offer."),
}

SCORE_WEIGHT_KEYS = ["score_weight_price", "score_weight_quality", "score_weight_on_time",
                     "score_weight_cancellation", "score_weight_rating"]


def _convert(kind, raw):
    if kind == "bool":
        return raw in (True, "1", "true", "True", "on", "yes")
    if kind == "int":
        return int(raw)
    if kind == "decimal":
        return to_decimal(raw, Decimal("0"))
    return raw


def _cache():
    if not has_app_context():
        return {}
    if "_settings_cache" not in g:
        g._settings_cache = {s.key: s.value for s in db.session.query(PlatformSetting).all()}
    return g._settings_cache


def get(key):
    kind, default, *_ = DEFINITIONS[key]
    raw = _cache().get(key)
    if raw is None:
        return _convert(kind, default) if kind != "bool" else bool(default)
    return _convert(kind, raw)


def currency():
    return current_app.config.get("DEFAULT_CURRENCY", "INR")


def validate_value(key, value):
    kind, *_ = DEFINITIONS[key]
    if kind == "bool":
        return "1" if value in (True, "1", "true", "on", "yes") else "0"
    if kind == "int":
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise DomainError(f"{DEFINITIONS[key][2]} must be a whole number.")
        if v < 0 or v > 10000:
            raise DomainError(f"{DEFINITIONS[key][2]} is out of range.")
        return str(v)
    if kind == "decimal":
        d = to_decimal(value)
        if d is None or d < 0 or d > 100000:
            raise DomainError(f"{DEFINITIONS[key][2]} must be a non-negative number.")
        if key.endswith("_percent") and d > 50:
            raise DomainError(f"{DEFINITIONS[key][2]} cannot exceed 50%.")
        return str(d)
    if kind == "choice":
        if value not in SelectionMode.ALL:
            raise DomainError("Unknown award policy.")
        return value
    text = (value or "").strip()
    if not text or len(text) > 2000:
        raise DomainError(f"{DEFINITIONS[key][2]} must be 1–2000 characters.")
    return text


def set_value(key, value, actor=None):
    if key not in DEFINITIONS:
        raise DomainError("Unknown setting.")
    stored = validate_value(key, value)
    row = db.session.query(PlatformSetting).filter_by(key=key).first()
    old = row.value if row else None
    if row is None:
        row = PlatformSetting(key=key, value=stored)
        db.session.add(row)
    row.value = stored
    row.updated_by_id = getattr(actor, "id", None)
    if old != stored:
        audit(actor, "setting.changed", row, {"key": key, "from": old, "to": stored})
    if has_app_context():
        g.pop("_settings_cache", None)
    return row


def score_weights():
    weights = {k.replace("score_weight_", ""): Decimal(get(k)) for k in SCORE_WEIGHT_KEYS}
    total = sum(weights.values())
    if total <= 0:
        weights = {"price": Decimal(1)}
        total = Decimal(1)
    return {k: v / total for k, v in weights.items()}
