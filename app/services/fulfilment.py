"""Customer confirmation, quality inspections, packing, delivery and closing."""
from datetime import timedelta
from decimal import Decimal

from ..constants import (INSPECTION_CHECKLIST, COStatus, ComplaintStatus, DeliveryStatus, GOStatus,
                         InspectionResult, Roles, Verification)
from ..extensions import db
from ..models import (Complaint, Delivery, InspectionItem, InventoryBatch, Product, QualityInspection)
from ..utils import ZERO, json_dumps, money, qty, utcnow
from . import settings
from .bidding import require_awarded_vendor
from .core import DomainError, PermissionDenied, audit, notify, transition_customer_order, \
    transition_delivery, transition_group_order
from .payments import request_payment
from .pricing import parse_decimal

CONFIRMABLE_GO = (GOStatus.AWARDED, GOStatus.SOURCING, GOStatus.QUALITY_CHECK, GOStatus.ON_HOLD)


# ------------------------------------------------- delivery provider interface

class DeliveryProvider:
    """Interface for external delivery/courier integrations."""
    name = "base"

    def create_shipment(self, delivery):
        """Book the delivery; return a provider reference or None."""
        raise NotImplementedError

    def cancel_shipment(self, delivery):
        raise NotImplementedError


class ManualDeliveryProvider(DeliveryProvider):
    """Vendor or platform staff coordinate deliveries themselves."""
    name = "manual"

    def create_shipment(self, delivery):
        return None

    def cancel_shipment(self, delivery):
        return None


DELIVERY_PROVIDERS = {"manual": ManualDeliveryProvider}


def delivery_provider(name="manual"):
    return DELIVERY_PROVIDERS.get(name, ManualDeliveryProvider)()


# ------------------------------------------------------ customer confirmation

def confirm_customer_order(user, co, fulfilment_method=None, address=None):
    if co.user_id != user.id:
        raise PermissionDenied("This is not your order.")
    go = co.group_order
    if co.status != COStatus.AWAITING_CONFIRMATION:
        raise DomainError("This order is not awaiting confirmation.")
    if go.status not in CONFIRMABLE_GO:
        raise DomainError("This order can no longer be confirmed.")
    method = fulfilment_method or co.fulfilment_method
    if go.fulfilment_options != "both" and method != go.fulfilment_options:
        raise DomainError("That fulfilment method is not available for this order.")
    if method == "home":
        address = address or co.address
        if address is None or address.user_id != user.id:
            raise DomainError("Choose a delivery address.")
        co.address_id = address.id
        co.delivery_address_snapshot = address.as_text()[:600]
    else:
        pp = go.pickup_point
        if pp is None:
            raise DomainError("This order has no pickup point.")
        co.address_id = None
        co.delivery_address_snapshot = f"Pickup: {pp.name}, {pp.address_text}"[:600]
    if method != co.fulfilment_method:
        bid = go.awarded_bid
        co.fulfilment_method = method
        co.home_delivery_fee = money(bid.home_delivery_fee) if method == "home" else money(0)
        co.total = money(co.subtotal + co.delivery_fee + co.home_delivery_fee + co.service_fee)
    if co.total <= 0:
        raise DomainError("Nothing was allocated to your order by the selected offer.")
    transition_customer_order(co, COStatus.CONFIRMED)
    co.confirmed_at = utcnow()
    db.session.flush()
    request_payment(co)
    return co


def decline_customer_order(user, co, reason=None):
    if co.user_id != user.id:
        raise PermissionDenied("This is not your order.")
    if co.status != COStatus.AWAITING_CONFIRMATION:
        raise DomainError("Only orders awaiting confirmation can be declined.")
    transition_customer_order(co, COStatus.CANCELLED)
    co.cancelled_at = utcnow()
    co.cancel_reason = (reason or "Declined by customer").strip()[:300]
    return co


# ------------------------------------------------------------ batches & QA

def add_batch(vendor, go, *, product_id, lot_code, quantity, grade_id=None, source_name=None, origin=None,
              harvest_date=None, sourced_on=None, notes=None):
    if go is not None:
        require_awarded_vendor(go, vendor)
        if go.status not in (GOStatus.SOURCING, GOStatus.QUALITY_CHECK, GOStatus.ON_HOLD):
            raise DomainError("Batches can be recorded while the order is being sourced or checked.")
        if int(product_id) not in {i.product_id for i in go.items if not i.dropped}:
            raise DomainError("That product is not part of this order.")
    product = db.session.get(Product, int(product_id)) if product_id else None
    if product is None:
        raise DomainError("Choose a product.")
    lot_code = (lot_code or "").strip()
    if not lot_code or len(lot_code) > 60:
        raise DomainError("A lot code (max 60 characters) is required.")
    if db.session.query(InventoryBatch.id).filter_by(vendor_id=vendor.id, lot_code=lot_code).first():
        raise DomainError("You already used that lot code.")
    q = parse_decimal(quantity, "Batch quantity", Decimal("0.001"), Decimal("1000000"), places=3)
    today = utcnow().date()
    if harvest_date and harvest_date > today:
        raise DomainError("Harvest date cannot be in the future.")
    if sourced_on and sourced_on > today:
        raise DomainError("Sourcing date cannot be in the future.")
    batch = InventoryBatch(vendor_id=vendor.id, group_order_id=go.id if go else None, product_id=product.id,
                           grade_id=int(grade_id) if grade_id else None, lot_code=lot_code, quantity=q,
                           source_name=(source_name or "").strip()[:160] or None,
                           origin=(origin or "").strip()[:160] or None, harvest_date=harvest_date,
                           sourced_on=sourced_on or today, notes=(notes or "").strip()[:500] or None,
                           created_by_id=vendor.user_id)
    db.session.add(batch)
    db.session.flush()
    audit(vendor.user, "batch.recorded", batch, {"group_order_id": batch.group_order_id, "lot": lot_code})
    return batch


def start_quality_check(vendor, go):
    require_awarded_vendor(go, vendor)
    transition_group_order(go, GOStatus.QUALITY_CHECK, vendor.user, "Ready for quality check")
    from .groups import active_admin_ids
    for uid in active_admin_ids():
        notify(uid, f"Group order #{go.id} is ready for quality inspection.", f"/admin/quality/{go.id}")


def record_inspection(go, inspector, *, result, items, checklist=None, stage="pre_dispatch", defect_notes=None,
                      spoilage_qty="0", delivery_condition=None, place_on_hold=False, batch_id=None):
    """Record an inspection. ``items``: list of dicts with product_id, expected_qty,
    received_qty, accepted_qty, rejected_qty, notes."""
    is_admin = inspector.has_role(Roles.ADMIN)
    if is_admin:
        verification = Verification.PLATFORM_VERIFIED
    else:
        require_awarded_vendor(go, inspector.vendor_profile)
        verification = Verification.VENDOR_DECLARED
        if place_on_hold:
            raise PermissionDenied("Only an administrator can place an order on hold.")
    if result not in InspectionResult.ALL:
        raise DomainError("Choose an inspection result.")
    allowed = {"pre_dispatch": (GOStatus.SOURCING, GOStatus.QUALITY_CHECK, GOStatus.ON_HOLD),
               "on_delivery": (GOStatus.DISPATCHED, GOStatus.DELIVERED, GOStatus.ON_HOLD)}
    if stage not in allowed:
        raise DomainError("Unknown inspection stage.")
    if go.status not in allowed[stage]:
        raise DomainError("Inspections of this type cannot be recorded at the order's current status.")
    order_products = {i.product_id for i in go.items if not i.dropped}
    insp = QualityInspection(group_order_id=go.id, inspector_id=inspector.id, stage=stage,
                             verification=verification, result=result,
                             batch_id=int(batch_id) if batch_id else None,
                             checklist=json_dumps({k: bool((checklist or {}).get(k)) for k, _ in INSPECTION_CHECKLIST}),
                             defect_notes=(defect_notes or "").strip()[:5000] or None,
                             spoilage_qty=parse_decimal(spoilage_qty or "0", "Spoilage", ZERO, Decimal("100000"), 3),
                             delivery_condition=(delivery_condition or "").strip()[:300] or None)
    for row in items:
        pid = int(row["product_id"])
        if pid not in order_products:
            raise DomainError("Inspection includes a product that is not in this order.")
        vals = {k: parse_decimal(row.get(k) or "0", k.replace("_", " ").capitalize(), ZERO, Decimal("100000"), 3)
                for k in ("expected_qty", "received_qty", "accepted_qty", "rejected_qty")}
        if vals["accepted_qty"] + vals["rejected_qty"] > vals["received_qty"]:
            raise DomainError("Accepted plus rejected quantity cannot exceed the received quantity.")
        insp.items.append(InspectionItem(product_id=pid, notes=(row.get("notes") or "").strip()[:300] or None,
                                         **vals))
    if result == InspectionResult.FAIL and not insp.defect_notes:
        raise DomainError("Describe the defects for a failed inspection.")
    db.session.add(insp)
    if result == InspectionResult.FAIL and place_on_hold and go.status != GOStatus.ON_HOLD:
        place_order_on_hold(go, inspector, f"Failed inspection: {insp.defect_notes[:200]}")
        insp.placed_on_hold = True
    db.session.flush()
    audit(inspector, "inspection.recorded", insp, {"group_order_id": go.id, "result": result,
                                                    "verification": verification, "hold": insp.placed_on_hold})
    if result == InspectionResult.FAIL:
        bid = go.awarded_bid
        if bid and is_admin:
            notify(bid.vendor.user_id, f"Order #{go.id} failed quality inspection.", f"/vendor/awarded/{go.id}")
    return insp


def inspections_for(go):
    return (db.session.query(QualityInspection).filter_by(group_order_id=go.id)
            .order_by(QualityInspection.created_at.desc(), QualityInspection.id.desc()).all())


def pre_dispatch_clearance(go):
    """Return (ok, message) for whether the order passed quality checks."""
    need_platform = settings.get("require_platform_inspection")
    rows = [i for i in inspections_for(go) if i.stage == "pre_dispatch"
            and (i.verification == Verification.PLATFORM_VERIFIED or not need_platform)]
    if not rows:
        return False, ("A passing platform inspection is required before packing."
                       if need_platform else "Record a passing quality check before packing.")
    latest = rows[0]
    if latest.result == InspectionResult.FAIL:
        return False, "The latest inspection failed. Re-source or correct the issue and re-inspect."
    return True, ""


def place_order_on_hold(go, actor, reason):
    if not actor.has_role(Roles.ADMIN):
        raise PermissionDenied("Only an administrator can place an order on hold.")
    if not reason or not reason.strip():
        raise DomainError("A reason is required.")
    previous = go.status
    transition_group_order(go, GOStatus.ON_HOLD, actor, reason)
    go.hold_from_status = previous
    go.hold_reason = reason.strip()[:500]
    bid = go.awarded_bid
    if bid:
        notify(bid.vendor.user_id, f"Order #{go.id} was placed on hold: {go.hold_reason}", f"/vendor/awarded/{go.id}")


def resume_order(go, actor, target=None):
    if not actor.has_role(Roles.ADMIN):
        raise PermissionDenied("Only an administrator can release a hold.")
    if go.status != GOStatus.ON_HOLD:
        raise DomainError("This order is not on hold.")
    target = target or go.hold_from_status
    allowed = {go.hold_from_status, GOStatus.SOURCING, GOStatus.QUALITY_CHECK}
    if target not in allowed:
        raise DomainError("The order can only resume to its previous status, sourcing or quality check.")
    if go.hold_from_status in (GOStatus.SOURCING, GOStatus.QUALITY_CHECK) and target == GOStatus.PACKED:
        raise DomainError("The order must pass inspection before packing.")
    transition_group_order(go, target, actor, "Hold released")
    go.hold_reason = None
    go.hold_from_status = None


# ---------------------------------------------------------------- packing

def mark_packed(vendor, go):
    require_awarded_vendor(go, vendor)
    if go.status != GOStatus.QUALITY_CHECK:
        raise DomainError("The order must be in quality check before packing.")
    ok, msg = pre_dispatch_clearance(go)
    if not ok:
        raise DomainError(msg)
    confirmed = [co for co in go.customer_orders if co.status == COStatus.CONFIRMED]
    if not confirmed:
        raise DomainError("No customer has confirmed this order yet.")
    for co in go.customer_orders:
        if co.status == COStatus.AWAITING_CONFIRMATION:
            transition_customer_order(co, COStatus.CANCELLED)
            co.cancelled_at = utcnow()
            co.cancel_reason = "Not confirmed before packing"
            notify(co.user_id, f"Your order in {go.group.name} was not confirmed in time and was cancelled.",
                   f"/orders/{co.id}")
    transition_group_order(go, GOStatus.PACKED, vendor.user, "Packed")
    for co in confirmed:
        if co.delivery is None:
            db.session.add(Delivery(
                group_order_id=go.id, customer_order_id=co.id, vendor_id=vendor.id, method=co.fulfilment_method,
                pickup_point_id=go.pickup_point_id if co.fulfilment_method == "pickup" else None,
                address_text=co.delivery_address_snapshot, window_start=go.delivery_window_start,
                window_end=go.delivery_window_end, fee=money(co.delivery_fee + co.home_delivery_fee),
                status=DeliveryStatus.SCHEDULED,
            ))
    db.session.flush()


def dispatch(vendor, go, assigned_to=None):
    require_awarded_vendor(go, vendor)
    if go.status != GOStatus.PACKED:
        raise DomainError("Only packed orders can be dispatched.")
    transition_group_order(go, GOStatus.DISPATCHED, vendor.user, "Dispatched")
    go.dispatched_at = utcnow()
    for d in deliveries_for(go):
        if d.status == DeliveryStatus.SCHEDULED:
            _dispatch_one(d, assigned_to)
            notify(d.customer_order.user_id,
                   "Your order is out for delivery." if d.method == "home"
                   else "Your order is on its way to the pickup point.", f"/orders/{d.customer_order_id}/tracking")


def _dispatch_one(d, assigned_to=None):
    transition_delivery(d, DeliveryStatus.DISPATCHED)
    d.dispatched_at = utcnow()
    d.attempts = (d.attempts or 0) + 1
    d.assigned_to = (assigned_to or d.assigned_to or "").strip()[:120] or None
    d.provider_ref = delivery_provider(d.provider).create_shipment(d) or d.provider_ref


def deliveries_for(go):
    return db.session.query(Delivery).filter_by(group_order_id=go.id).order_by(Delivery.id).all()


def _require_delivery_actor(actor, delivery):
    if actor.has_role(Roles.ADMIN):
        return
    vp = actor.vendor_profile
    if vp is None or vp.id != delivery.vendor_id:
        raise PermissionDenied("You cannot update this delivery.")


def mark_delivered(actor, delivery, *, received_by=None, proof_notes=None, condition_notes=None):
    _require_delivery_actor(actor, delivery)
    transition_delivery(delivery, DeliveryStatus.DELIVERED)
    delivery.delivered_at = utcnow()
    delivery.received_by = (received_by or "").strip()[:120] or None
    delivery.proof_notes = (proof_notes or "").strip()[:500] or None
    delivery.condition_notes = (condition_notes or "").strip()[:500] or None
    co = delivery.customer_order
    for it in co.items:
        it.delivered_qty = it.allocated_qty
    transition_customer_order(co, COStatus.DELIVERED)
    co.delivered_at = delivery.delivered_at
    notify(co.user_id, f"Order {co.reference} was delivered. Please confirm receipt or report a problem.",
           f"/orders/{co.id}")
    audit(actor, "delivery.delivered", delivery, {"order": co.reference})
    _maybe_complete(delivery.group_order, actor)


def mark_failed(actor, delivery, reason):
    _require_delivery_actor(actor, delivery)
    if not reason or not reason.strip():
        raise DomainError("Give a reason for the failed delivery.")
    transition_delivery(delivery, DeliveryStatus.FAILED)
    delivery.failure_reason = reason.strip()[:300]
    notify(delivery.customer_order.user_id, f"Delivery attempt failed: {delivery.failure_reason}",
           f"/orders/{delivery.customer_order_id}/tracking")
    audit(actor, "delivery.failed", delivery, {"reason": delivery.failure_reason})


def reschedule(actor, delivery, assigned_to=None):
    _require_delivery_actor(actor, delivery)
    transition_delivery(delivery, DeliveryStatus.SCHEDULED)
    if delivery.group_order.status in (GOStatus.DISPATCHED, GOStatus.ON_HOLD):
        _dispatch_one(delivery, assigned_to)


def mark_returned(actor, delivery, reason):
    _require_delivery_actor(actor, delivery)
    transition_delivery(delivery, DeliveryStatus.RETURNED)
    delivery.failure_reason = (reason or delivery.failure_reason or "Not delivered").strip()[:300]
    co = delivery.customer_order
    transition_customer_order(co, COStatus.CANCELLED)
    co.cancelled_at = utcnow()
    co.cancel_reason = "Delivery could not be completed: " + delivery.failure_reason
    notify(co.user_id, f"Your order {co.reference} could not be delivered. Any payment will be refunded.",
           f"/orders/{co.id}")
    audit(actor, "delivery.returned", delivery, {"reason": delivery.failure_reason})
    _maybe_complete(delivery.group_order, actor)


def _maybe_complete(go, actor):
    if go.status != GOStatus.DISPATCHED:
        return
    ds = deliveries_for(go)
    if ds and all(d.status in (DeliveryStatus.DELIVERED, DeliveryStatus.RETURNED) for d in ds):
        transition_group_order(go, GOStatus.DELIVERED, actor, "All deliveries completed")
        go.delivered_at = utcnow()


def customer_confirm_receipt(user, delivery):
    co = delivery.customer_order
    if co.user_id != user.id:
        raise PermissionDenied("This is not your order.")
    if delivery.status == DeliveryStatus.DISPATCHED:
        # Customer confirmation counts as proof of delivery.
        transition_delivery(delivery, DeliveryStatus.DELIVERED)
        delivery.delivered_at = utcnow()
        delivery.received_by = user.full_name
        delivery.proof_notes = "Confirmed by customer"
        for it in co.items:
            it.delivered_qty = it.allocated_qty
        transition_customer_order(co, COStatus.DELIVERED)
        co.delivered_at = delivery.delivered_at
        _maybe_complete(delivery.group_order, user)
    elif delivery.status != DeliveryStatus.DELIVERED:
        raise DomainError("This order has not been dispatched yet.")
    delivery.customer_confirmed_at = utcnow()


# ---------------------------------------------------------------- closing

def close_readiness(go):
    if go.status != GOStatus.DELIVERED:
        return False, "The order must be delivered before it can be closed."
    open_complaints = (db.session.query(Complaint)
                       .filter(Complaint.group_order_id == go.id,
                               Complaint.status.in_(ComplaintStatus.UNRESOLVED)).count())
    if open_complaints:
        return False, f"{open_complaints} complaint(s) must be resolved first."
    window = timedelta(hours=settings.get("complaint_window_hours"))
    ds = [d for d in deliveries_for(go) if d.status == DeliveryStatus.DELIVERED]
    all_confirmed = all(d.customer_confirmed_at for d in ds)
    if not all_confirmed and go.delivered_at and utcnow() < go.delivered_at + window:
        return False, "Waiting for customers to confirm receipt or for the complaint window to end."
    return True, ""


def close_group_order(go, admin):
    if not admin.has_role(Roles.ADMIN):
        raise PermissionDenied("Only an administrator can close an order.")
    ok, msg = close_readiness(go)
    if not ok:
        raise DomainError(msg)
    from .finance import record_close
    record_close(go, admin)
    for co in go.customer_orders:
        if co.status == COStatus.DELIVERED:
            transition_customer_order(co, COStatus.COMPLETED)
    transition_group_order(go, GOStatus.CLOSED, admin, "Closed")
    go.closed_at = utcnow()
    db.session.flush()


def packing_list(go):
    """Confirmed customer quantities per product (for vendors)."""
    rows = {}
    for co in go.customer_orders:
        if co.status not in (COStatus.CONFIRMED, COStatus.DELIVERED, COStatus.COMPLETED):
            continue
        for it in co.items:
            r = rows.setdefault(it.product_id, {"product": it.product, "qty": ZERO})
            r["qty"] += qty(it.allocated_qty or 0)
    return list(rows.values())
