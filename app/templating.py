"""Jinja filters and context processors."""
from flask import current_app
from flask_login import current_user

from .constants import (COMPLAINT_TYPES, BidStatus, COStatus, ComplaintStatus, DeliveryStatus, GOStatus,
                        PaymentStatus, SelectionMode, SubstitutionPolicy, Verification)
from .extensions import db
from .utils import fmt_qty, money, to_local, utcnow

BADGE_COLOURS = {
    # group order
    "draft": "secondary", "collecting": "success", "frozen": "info", "bidding": "primary",
    "selection": "warning", "awarded": "info", "sourcing": "info", "quality_check": "warning",
    "on_hold": "danger", "packed": "info", "dispatched": "primary", "delivered": "success",
    "closed": "dark", "cancelled": "secondary",
    # customer order
    "open": "success", "locked": "info", "awaiting_confirmation": "warning", "confirmed": "primary",
    "completed": "dark",
    # vendor
    "pending": "warning", "approved": "success", "rejected": "danger", "suspended": "danger",
    "verified": "success",
    # payment
    "not_requested": "secondary", "initiated": "info", "paid": "success", "failed": "danger",
    "partially_refunded": "warning", "refunded": "secondary",
    # bids
    "submitted": "primary", "withdrawn": "secondary", "expired": "secondary", "not_selected": "secondary",
    "declined": "danger", "disqualified": "danger",
    # delivery
    "scheduled": "info", "returned": "danger",
    # inspection / complaint
    "pass": "success", "conditional": "warning", "fail": "danger", "vendor_responded": "info",
    "resolved": "success",
}

ALL_LABELS = {}
for mapping in (GOStatus.LABELS, COStatus.LABELS, PaymentStatus.LABELS, BidStatus.LABELS, DeliveryStatus.LABELS,
                ComplaintStatus.LABELS):
    ALL_LABELS.update(mapping)
ALL_LABELS.update({"pending": "Pending", "approved": "Approved", "rejected": "Rejected", "suspended": "Suspended",
                   "verified": "Verified", "pass": "Passed", "fail": "Failed", "conditional": "Conditional pass"})


def register_template_helpers(app):
    @app.template_filter("money")
    def money_filter(value, currency=None):
        symbol = current_app.config.get("CURRENCY_SYMBOL", "")
        if currency and currency != current_app.config.get("DEFAULT_CURRENCY"):
            symbol = currency + " "
        amount = money(value or 0)
        return f"{symbol}{amount:,.2f}"

    @app.template_filter("qty")
    def qty_filter(value):
        return fmt_qty(value or 0)

    @app.template_filter("localdt")
    def localdt(value, fmt="%d %b %Y, %H:%M"):
        if value is None:
            return "—"
        return to_local(value).strftime(fmt)

    @app.template_filter("dtinput")
    def dtinput(value):
        if value is None:
            return ""
        return to_local(value).strftime("%Y-%m-%dT%H:%M")

    @app.template_filter("pct")
    def pct(value, places=0):
        if value is None:
            return "—"
        return f"{float(value):.{places}f}%"

    @app.template_filter("status_label")
    def status_label(value):
        return ALL_LABELS.get(value, (value or "").replace("_", " ").capitalize())

    @app.template_filter("badge")
    def badge(value):
        return BADGE_COLOURS.get(value, "secondary")

    @app.template_filter("timeleft")
    def timeleft(value):
        if value is None:
            return "—"
        seconds = int((value - utcnow()).total_seconds())
        if seconds <= 0:
            return "passed"
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days:
            return f"{days}d {hours}h left"
        if hours:
            return f"{hours}h {minutes}m left"
        return f"{minutes}m left"

    @app.context_processor
    def inject():
        unread = 0
        if current_user.is_authenticated:
            from .models import Notification
            unread = (db.session.query(Notification)
                      .filter_by(user_id=current_user.id, is_read=False).count())
        from .services import settings as platform_settings
        return {
            "app_name": current_app.config["APP_NAME"],
            "currency_symbol": current_app.config["CURRENCY_SYMBOL"],
            "unread_notifications": unread,
            "now_utc": utcnow(),
            "GOStatus": GOStatus,
            "COStatus": COStatus,
            "SelectionMode": SelectionMode,
            "SubstitutionPolicy": SubstitutionPolicy,
            "Verification": Verification,
            "COMPLAINT_TYPES": COMPLAINT_TYPES,
            "platform_setting": platform_settings.get,
        }
