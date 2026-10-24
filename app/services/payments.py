"""Payment abstraction, manual payment recording and refunds.

No payment gateway is required for the first release. Payments are recorded
manually by an administrator after they verify receipt. A gateway adapter can
be added by subclassing :class:`PaymentProvider`; it must confirm payments
server-side (signed webhook or API lookup) — a browser redirect is never
treated as proof of payment.
"""
import hashlib
import hmac
import json
from decimal import Decimal

from flask import current_app

from ..constants import PAYMENT_TRANSITIONS, COStatus, PaymentStatus
from ..extensions import db
from ..models import PaymentRecord, Refund
from ..utils import ZERO, dsum, money, utcnow
from .core import DomainError, audit, notify
from .pricing import parse_decimal

PAYMENT_METHODS = {
    "cash": "Cash",
    "upi": "UPI",
    "bank_transfer": "Bank transfer",
    "card": "Card (external terminal)",
}


class PaymentProvider:
    """Interface for payment providers."""
    name = "base"

    def create_payment_request(self, order):
        raise NotImplementedError

    def verify_webhook(self, payload: bytes, headers) -> dict:
        """Verify authenticity and return a normalised event dict, or raise DomainError."""
        raise NotImplementedError


class ManualPaymentProvider(PaymentProvider):
    """Customer pays offline; an administrator records the verified payment."""
    name = "manual"

    def create_payment_request(self, order):
        outstanding = amount_outstanding(order)
        if outstanding <= 0:
            return None
        record = PaymentRecord(customer_order_id=order.id, amount=outstanding, currency=order.currency,
                               provider=self.name, status=PaymentStatus.PENDING)
        db.session.add(record)
        order.payments.append(record)
        return record

    def verify_webhook(self, payload, headers):
        raise DomainError("Manual payments do not use webhooks.")


class HmacWebhookProvider(PaymentProvider):
    """Template for a gateway that signs webhooks with HMAC-SHA256.

    Expected header ``X-Signature: <hex digest of the raw body>``. Body JSON:
    ``{"payment_id": <PaymentRecord.id>, "status": "paid"|"failed",
    "amount": "123.45", "currency": "INR", "provider_ref": "..."}``.
    """
    name = "gateway"

    def __init__(self, secret):
        self.secret = secret

    def create_payment_request(self, order):
        raise DomainError("Online payments are not enabled.")

    def verify_webhook(self, payload, headers):
        signature = headers.get("X-Signature", "")
        if not self.secret or not verify_signature(self.secret, payload, signature):
            raise DomainError("Invalid webhook signature.")
        try:
            event = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise DomainError("Malformed webhook payload.")
        if event.get("status") not in ("paid", "failed") or "payment_id" not in event:
            raise DomainError("Unsupported webhook event.")
        return event


def verify_signature(secret, payload: bytes, signature: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (signature or "").strip().lower())


def get_provider(name="manual"):
    if name == "manual":
        return ManualPaymentProvider()
    if name == "gateway":
        secret = current_app.config.get("PAYMENT_WEBHOOK_SECRET")
        if not secret:
            return None
        return HmacWebhookProvider(secret)
    return None


# ------------------------------------------------------------------ amounts

def amount_paid(order):
    return money(dsum(p.amount for p in order.payments if p.status == PaymentStatus.PAID))


def amount_refunded(order):
    return money(dsum(r.amount for r in order.refunds))


def amount_outstanding(order):
    return money(max(money(order.total) - amount_paid(order), ZERO))


def refresh_payment_status(order):
    paid = amount_paid(order)
    refunded = amount_refunded(order)
    statuses = {p.status for p in order.payments}
    if refunded > 0:
        status = PaymentStatus.REFUNDED if refunded >= paid else PaymentStatus.PARTIALLY_REFUNDED
    elif paid > 0 and paid >= money(order.total):
        status = PaymentStatus.PAID
    elif PaymentStatus.INITIATED in statuses:
        status = PaymentStatus.INITIATED
    elif PaymentStatus.PENDING in statuses or paid > 0:
        status = PaymentStatus.PENDING
    elif PaymentStatus.FAILED in statuses:
        status = PaymentStatus.FAILED
    else:
        status = PaymentStatus.NOT_REQUESTED
    order.payment_status = status
    return status


def _transition(record, new):
    if new not in PAYMENT_TRANSITIONS.get(record.status, set()):
        raise DomainError(f"Invalid payment status change: {record.status} → {new}.")
    record.status = new


def request_payment(order):
    if any(p.status in (PaymentStatus.PENDING, PaymentStatus.INITIATED) for p in order.payments):
        return None
    record = get_provider("manual").create_payment_request(order)
    db.session.flush()
    refresh_payment_status(order)
    return record


def record_manual_payment(order, admin, *, amount, method, reference=None, notes=None):
    if order.status not in (COStatus.CONFIRMED, COStatus.DELIVERED, COStatus.COMPLETED):
        raise DomainError("Payments can only be recorded for confirmed orders.")
    if method not in PAYMENT_METHODS:
        raise DomainError("Choose a payment method.")
    amt = parse_decimal(amount, "Amount", Decimal("0.01"), Decimal("10000000"))
    outstanding = amount_outstanding(order)
    if amt > outstanding:
        raise DomainError(f"Amount exceeds the outstanding balance ({outstanding}).")
    now = utcnow()
    record = next((p for p in order.payments if p.status in (PaymentStatus.PENDING, PaymentStatus.INITIATED)),
                  None)
    if record is None:
        record = PaymentRecord(customer_order_id=order.id, currency=order.currency, provider="manual",
                               status=PaymentStatus.PENDING, amount=amt)
        db.session.add(record)
        order.payments.append(record)
    _transition(record, PaymentStatus.PAID)
    record.amount = amt
    record.method = method
    record.provider_ref = (reference or "").strip()[:120] or None
    record.notes = (notes or "").strip()[:500] or None
    record.verified_server_side = True
    record.recorded_by_id = admin.id
    record.confirmed_at = now
    record.initiated_at = record.initiated_at or now
    db.session.flush()
    if amount_outstanding(order) > 0:
        get_provider("manual").create_payment_request(order)
        db.session.flush()
    db.session.refresh(order)
    refresh_payment_status(order)
    audit(admin, "payment.recorded", record, {"order": order.reference, "amount": str(amt), "method": method,
                                              "reference": record.provider_ref})
    notify(order.user_id, f"Payment of {amt} {order.currency} received for {order.reference}.",
           f"/orders/{order.id}")
    return record


def mark_payment_failed(record, admin, reason):
    _transition(record, PaymentStatus.FAILED)
    record.failure_reason = (reason or "Marked failed").strip()[:300]
    refresh_payment_status(record.customer_order)
    audit(admin, "payment.failed", record, {"reason": record.failure_reason})
    return record


def apply_gateway_event(event, provider_name):
    """Apply a *verified* gateway event. Amount and currency must match."""
    record = db.session.get(PaymentRecord, int(event["payment_id"]))
    if record is None or record.provider != provider_name:
        raise DomainError("Unknown payment.")
    if event["status"] == "paid":
        if money(event.get("amount")) != money(record.amount) or event.get("currency") != record.currency:
            raise DomainError("Amount or currency mismatch.")
        if record.status == PaymentStatus.PAID:
            return record   # idempotent
        _transition(record, PaymentStatus.PAID)
        record.verified_server_side = True
        record.confirmed_at = utcnow()
    else:
        if record.status == PaymentStatus.FAILED:
            return record
        _transition(record, PaymentStatus.FAILED)
    record.provider_ref = (event.get("provider_ref") or record.provider_ref or "")[:120] or None
    refresh_payment_status(record.customer_order)
    audit(None, "payment.webhook", record, {"status": event["status"]})
    return record


def issue_refund(order, admin, *, amount, reason, charged_to="vendor", complaint=None, reference=None):
    amt = parse_decimal(amount, "Refund amount", Decimal("0.01"), Decimal("10000000"))
    if not reason or not reason.strip():
        raise DomainError("A refund reason is required.")
    if charged_to not in ("vendor", "platform"):
        raise DomainError("Choose who bears the refund.")
    refundable = amount_paid(order) - amount_refunded(order)
    if amt > refundable:
        raise DomainError(f"Refund exceeds the refundable amount ({money(refundable)}).")
    refund = Refund(customer_order_id=order.id, amount=amt, currency=order.currency, reason=reason.strip()[:500],
                    charged_to=charged_to, complaint_id=complaint.id if complaint else None,
                    reference=(reference or "").strip()[:120] or None, processed_by_id=admin.id)
    paid = [p for p in order.payments if p.status == PaymentStatus.PAID]
    refund.payment_id = paid[-1].id if paid else None
    db.session.add(refund)
    order.refunds.append(refund)
    db.session.flush()
    refresh_payment_status(order)
    audit(admin, "refund.processed", refund, {"order": order.reference, "amount": str(amt),
                                              "charged_to": charged_to})
    notify(order.user_id, f"A refund of {amt} {order.currency} was processed for {order.reference}.",
           f"/orders/{order.id}")
    return refund
