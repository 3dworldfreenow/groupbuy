"""Cross-cutting service helpers: errors, audit log, notifications, transitions."""
from flask import has_request_context, request

from ..constants import CO_TRANSITIONS, DELIVERY_TRANSITIONS, GO_TRANSITIONS
from ..extensions import db
from ..models import AuditLog, Notification
from ..utils import json_dumps


class DomainError(Exception):
    """A business-rule violation with a message safe to show to the user."""


class PermissionDenied(DomainError):
    pass


class ConcurrencyError(DomainError):
    pass


def audit(actor, action, entity=None, details=None, entity_type=None, entity_id=None):
    """Record a privileged or sensitive action. Added to the current transaction."""
    ip = None
    if has_request_context():
        ip = request.remote_addr
    if entity is not None:
        entity_type = entity_type or entity.__class__.__name__
        entity_id = entity_id or getattr(entity, "id", None)
    log = AuditLog(
        actor_id=getattr(actor, "id", None),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=json_dumps(details or {}),
        ip_address=ip,
    )
    db.session.add(log)
    return log


def notify(user_or_id, message, link=None):
    user_id = getattr(user_or_id, "id", user_or_id)
    if user_id is None:
        return None
    n = Notification(user_id=user_id, message=message[:500], link=link)
    db.session.add(n)
    return n


def _check(transitions, current, new, label):
    if new not in transitions.get(current, set()):
        raise DomainError(f"Invalid {label} status change: {current} → {new}.")


def transition_group_order(go, new_status, actor=None, reason=None):
    old = go.status
    _check(GO_TRANSITIONS, old, new_status, "group order")
    go.status = new_status
    audit(actor, "group_order.status", go, {"from": old, "to": new_status, "reason": reason})
    return go


def transition_customer_order(co, new_status):
    _check(CO_TRANSITIONS, co.status, new_status, "customer order")
    co.status = new_status
    return co


def transition_delivery(delivery, new_status):
    _check(DELIVERY_TRANSITIONS, delivery.status, new_status, "delivery")
    delivery.status = new_status
    return delivery
