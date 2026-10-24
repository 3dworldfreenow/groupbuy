"""Buying groups, group orders, customer quantities and demand aggregation."""
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func

from ..constants import (BidStatus, BiddingMode, COStatus, FulfilmentOption, GOStatus, Roles, SelectionMode,
                         VendorStatus)
from ..extensions import db
from ..models import (BuyingGroup, CustomerOrder, CustomerOrderItem, GroupMembership, GroupOrder,
                      GroupOrderItem, PickupPoint, Product, ProductGrade, VendorProfile)
from ..utils import ZERO, qty, random_code, utcnow
from . import settings
from .core import DomainError, PermissionDenied, audit, notify, transition_customer_order, \
    transition_group_order
from .pricing import parse_decimal


# ---------------------------------------------------------------- groups

def _new_invite_code():
    for _ in range(20):
        code = random_code(8)
        if not db.session.query(BuyingGroup.id).filter_by(invite_code=code).first():
            return code
    raise DomainError("Could not generate an invite code; please retry.")


def create_group(organiser, *, name, description, location, pickup_point=None, is_public=True):
    name = (name or "").strip()
    if not 3 <= len(name) <= 120:
        raise DomainError("Group name must be 3–120 characters.")
    if location is None or not location.is_active:
        raise DomainError("Choose an active location.")
    if pickup_point is not None and pickup_point.location_id != location.id:
        raise DomainError("The pickup point must be in the group's location.")
    group = BuyingGroup(name=name, description=(description or "").strip()[:1000], organiser_id=organiser.id,
                        location_id=location.id, pickup_point_id=pickup_point.id if pickup_point else None,
                        invite_code=_new_invite_code(), is_public=bool(is_public))
    db.session.add(group)
    group.memberships.append(GroupMembership(user_id=organiser.id, role="organiser"))
    db.session.flush()
    audit(organiser, "group.created", group, {"name": name})
    return group


def is_organiser(group, user):
    m = group.membership_for(user)
    return m is not None and m.role == "organiser"


def require_organiser_or_admin(group, user):
    if not (is_organiser(group, user) or (user and user.has_role(Roles.ADMIN))):
        raise PermissionDenied("Only the group organiser or an administrator can do this.")


def join_group(user, group):
    if not group.is_active:
        raise DomainError("This group is no longer active.")
    if not user.has_role(Roles.CUSTOMER):
        raise PermissionDenied("Only customer accounts can join buying groups.")
    existing = db.session.query(GroupMembership).filter_by(group_id=group.id, user_id=user.id).first()
    if existing:
        if existing.status == "active":
            return existing
        if existing.status == "removed":
            raise PermissionDenied("You were removed from this group by the organiser.")
        existing.status = "active"
        existing.joined_at = utcnow()
        return existing
    m = GroupMembership(group_id=group.id, user_id=user.id, role="member")
    db.session.add(m)
    notify(group.organiser_id, f"{user.full_name} joined {group.name}.")
    return m


def leave_group(user, group):
    m = group.membership_for(user)
    if m is None:
        raise DomainError("You are not a member of this group.")
    if m.role == "organiser":
        raise DomainError("The organiser cannot leave the group.")
    for go in group.orders:
        co = customer_order_for(go, user)
        if co is None or co.status == COStatus.CANCELLED:
            continue
        if co.status == COStatus.OPEN:
            transition_customer_order(co, COStatus.CANCELLED)
            co.cancel_reason = "Left the group"
            co.cancelled_at = utcnow()
        else:
            raise DomainError("You have an order in progress in this group and cannot leave until it completes.")
    m.status = "left"
    return m


# ---------------------------------------------------------- group orders

def _validate_schedule(order_deadline, window_start, window_end, now=None):
    now = now or utcnow()
    if order_deadline is None or window_start is None or window_end is None:
        raise DomainError("Order deadline and delivery window are required.")
    if order_deadline <= now:
        raise DomainError("The order deadline must be in the future.")
    if window_start <= order_deadline:
        raise DomainError("The delivery window must start after the order deadline.")
    if window_end < window_start:
        raise DomainError("The delivery window must end after it starts.")
    if window_end - window_start > timedelta(days=7):
        raise DomainError("The delivery window cannot be longer than 7 days.")


def create_group_order(group, actor, *, title, order_deadline, delivery_window_start, delivery_window_end,
                       fulfilment_options=FulfilmentOption.BOTH, pickup_point_id=None, allow_partial=False,
                       min_fulfilment_percent="100", selection_mode=None, bidding_deadline=None, notes=None):
    require_organiser_or_admin(group, actor)
    if group.current_order is not None:
        raise DomainError("This group already has an active order round. Finish or cancel it first.")
    _validate_schedule(order_deadline, delivery_window_start, delivery_window_end)
    if fulfilment_options not in FulfilmentOption.LABELS:
        raise DomainError("Choose a fulfilment option.")
    selection_mode = selection_mode or settings.get("default_selection_mode")
    if selection_mode not in SelectionMode.ALL:
        raise DomainError("Choose an award policy.")
    pickup_point = None
    if pickup_point_id:
        pickup_point = db.session.get(PickupPoint, int(pickup_point_id))
    pickup_point = pickup_point or group.pickup_point
    if fulfilment_options in (FulfilmentOption.PICKUP, FulfilmentOption.BOTH):
        if pickup_point is None or not pickup_point.is_active:
            raise DomainError("Pickup requires an active pickup point.")
        if pickup_point.location_id != group.location_id:
            raise DomainError("The pickup point must be in the group's location.")
    if bidding_deadline is not None:
        if bidding_deadline <= order_deadline or bidding_deadline >= delivery_window_end:
            raise DomainError("The bidding deadline must fall between the order deadline and the end of the delivery window.")
    pct = parse_decimal(min_fulfilment_percent, "Minimum fulfilment", Decimal("0"), Decimal("100"))
    go = GroupOrder(
        group_id=group.id, title=(title or "").strip()[:160] or f"{group.name} order",
        status=GOStatus.DRAFT, currency=settings.currency(),
        order_deadline=order_deadline, bidding_deadline=bidding_deadline,
        delivery_window_start=delivery_window_start, delivery_window_end=delivery_window_end,
        fulfilment_options=fulfilment_options, pickup_point_id=pickup_point.id if pickup_point else None,
        allow_partial=bool(allow_partial), min_fulfilment_percent=pct if allow_partial else Decimal("100"),
        selection_mode=selection_mode, service_fee_percent=settings.get("service_fee_percent"),
        notes=(notes or "").strip()[:1000] or None, created_by_id=actor.id,
    )
    db.session.add(go)
    db.session.flush()
    audit(actor, "group_order.created", go, {"group_id": group.id, "selection_mode": selection_mode})
    return go


def update_schedule(go, actor, *, order_deadline, delivery_window_start, delivery_window_end):
    require_organiser_or_admin(go.group, actor)
    if go.status not in (GOStatus.DRAFT, GOStatus.COLLECTING):
        raise DomainError("The schedule can only be changed before the order deadline.")
    _validate_schedule(order_deadline, delivery_window_start, delivery_window_end)
    go.order_deadline = order_deadline
    go.delivery_window_start = delivery_window_start
    go.delivery_window_end = delivery_window_end
    audit(actor, "group_order.schedule", go, {"order_deadline": order_deadline})


def set_item(go, actor, *, product, target_qty, min_qty=None, max_qty=None, per_customer_max=None,
             min_grade=None):
    require_organiser_or_admin(go.group, actor)
    if go.status not in (GOStatus.DRAFT, GOStatus.COLLECTING):
        raise DomainError("Products can only be changed before the order deadline.")
    if product is None or not product.is_active:
        raise DomainError("Choose an active product.")
    target = parse_decimal(target_qty, "Target quantity", Decimal("0.001"), Decimal("100000"), places=3)
    minimum = parse_decimal(min_qty, "Minimum quantity", ZERO, Decimal("100000"), places=3, required=False)
    if minimum is None:
        minimum = min(settings.get("default_item_min_qty"), target)
    maximum = parse_decimal(max_qty, "Maximum quantity", Decimal("0.001"), Decimal("100000"), places=3,
                            required=False)
    pcm = parse_decimal(per_customer_max, "Per-customer limit", Decimal("0.001"), Decimal("10000"), places=3,
                        required=False)
    if maximum is not None and maximum < minimum:
        raise DomainError("Maximum quantity cannot be below the minimum.")
    if maximum is not None and maximum < target:
        raise DomainError("Maximum quantity cannot be below the target.")
    item = next((i for i in go.items if i.product_id == product.id), None)
    if item is not None and maximum is not None:
        current = aggregate_demand(go).get(item.id, ZERO)
        if maximum < current:
            raise DomainError(f"Maximum cannot be below the {current} {product.unit} already ordered.")
    if item is None:
        item = GroupOrderItem(product_id=product.id)
        go.items.append(item)
    item.target_qty, item.min_qty, item.max_qty, item.per_customer_max = target, minimum, maximum, pcm
    item.min_grade_id = min_grade.id if min_grade else None
    db.session.flush()
    return item


def remove_item(go, actor, item):
    require_organiser_or_admin(go.group, actor)
    if go.status not in (GOStatus.DRAFT, GOStatus.COLLECTING):
        raise DomainError("Products can only be changed before the order deadline.")
    if aggregate_demand(go).get(item.id, ZERO) > 0:
        raise DomainError("Members have already ordered this product; it cannot be removed.")
    go.items.remove(item)


def open_for_orders(go, actor):
    require_organiser_or_admin(go.group, actor)
    if not go.items:
        raise DomainError("Add at least one product before opening the order.")
    if go.order_deadline <= utcnow():
        raise DomainError("The order deadline has passed; update the schedule first.")
    transition_group_order(go, GOStatus.COLLECTING, actor)
    for m in go.group.active_members:
        if m.user_id != actor.id:
            notify(m.user_id, f"{go.group.name} is collecting orders until the deadline.",
                   f"/group-orders/{go.id}")


# --------------------------------------------------------- customer orders

def customer_order_for(go, user):
    return db.session.query(CustomerOrder).filter_by(group_order_id=go.id, user_id=user.id).first()


def aggregate_demand(go, exclude_user_id=None):
    """Total requested quantity per GroupOrderItem across live customer orders."""
    q = (db.session.query(CustomerOrderItem.group_order_item_id, func.sum(CustomerOrderItem.quantity))
         .join(CustomerOrder, CustomerOrder.id == CustomerOrderItem.customer_order_id)
         .filter(CustomerOrder.group_order_id == go.id, CustomerOrder.status != COStatus.CANCELLED))
    if exclude_user_id is not None:
        q = q.filter(CustomerOrder.user_id != exclude_user_id)
    rows = q.group_by(CustomerOrderItem.group_order_item_id).all()
    return {item_id: qty(total) for item_id, total in rows}


def progress(go):
    agg = aggregate_demand(go)
    buyers = (db.session.query(func.count(func.distinct(CustomerOrder.id)))
              .join(CustomerOrderItem, CustomerOrderItem.customer_order_id == CustomerOrder.id)
              .filter(CustomerOrder.group_order_id == go.id, CustomerOrder.status != COStatus.CANCELLED)
              .scalar() or 0)
    rows = []
    for item in go.items:
        total = item.frozen_qty if item.frozen_qty is not None else agg.get(item.id, ZERO)
        target = item.target_qty or Decimal(1)
        pct = min(int((total / target) * 100), 100) if target else 0
        remaining = (item.max_qty - total) if item.max_qty is not None else None
        rows.append({
            "item": item, "product": item.product, "aggregate": qty(total), "target": item.target_qty,
            "min": item.min_qty, "max": item.max_qty, "percent": pct,
            "remaining": qty(remaining) if remaining is not None else None,
            "meets_min": total >= item.min_qty and total > 0,
        })
    total_qty = sum((r["aggregate"] for r in rows), ZERO)
    total_target = sum((r["target"] for r in rows), ZERO)
    overall = min(int(total_qty / total_target * 100), 100) if total_target else 0
    return {"rows": rows, "buyers": buyers, "total_qty": qty(total_qty), "total_target": qty(total_target),
            "percent": overall, "units": sorted({r["product"].unit for r in rows})}


def ordering_open(go, now=None):
    return go.status == GOStatus.COLLECTING and (now or utcnow()) < go.order_deadline


def update_customer_quantities(go, user, quantities, fulfilment_method=None, address=None):
    """Set a member's requested quantities. ``quantities`` maps GroupOrderItem id to a value."""
    if go.status != GOStatus.COLLECTING:
        raise DomainError("This group order is not accepting orders.")
    if utcnow() >= go.order_deadline:
        raise DomainError("The order deadline has passed; quantities are frozen.")
    if go.group.membership_for(user) is None:
        raise PermissionDenied("Join the group before ordering.")
    method = fulfilment_method or ("pickup" if go.fulfilment_options != FulfilmentOption.HOME else "home")
    if method not in ("pickup", "home"):
        raise DomainError("Choose pickup or home delivery.")
    if go.fulfilment_options != FulfilmentOption.BOTH and method != go.fulfilment_options:
        raise DomainError(f"This order only supports {FulfilmentOption.LABELS[go.fulfilment_options].lower()}.")
    if method == "home":
        if address is None or address.user_id != user.id:
            raise DomainError("Choose one of your delivery addresses for home delivery.")

    # Lock the item rows (PostgreSQL: SELECT ... FOR UPDATE) so concurrent
    # members cannot jointly exceed the maximum quantity.
    items = {i.id: i for i in db.session.query(GroupOrderItem)
             .filter(GroupOrderItem.group_order_id == go.id).with_for_update().all()}
    others = aggregate_demand(go, exclude_user_id=user.id)

    co = customer_order_for(go, user)
    if co is None:
        co = CustomerOrder(group_order_id=go.id, user_id=user.id, currency=go.currency, status=COStatus.OPEN)
        db.session.add(co)
    elif co.status == COStatus.CANCELLED:
        co.status = COStatus.OPEN
        co.cancelled_at = None
        co.cancel_reason = None
    co.fulfilment_method = method
    co.address_id = address.id if (method == "home" and address) else None

    existing = {it.group_order_item_id: it for it in co.items}
    for item_id, raw in quantities.items():
        item = items.get(int(item_id))
        if item is None:
            raise DomainError("Unknown product in this order.")
        p = item.product
        q = parse_decimal(raw if raw not in (None, "") else "0", f"Quantity for {p.name}", ZERO,
                          Decimal("100000"), places=3)
        if q > 0:
            if q < p.min_customer_qty:
                raise DomainError(f"Minimum order for {p.name} is {p.min_customer_qty.normalize():f} {p.unit}.")
            if (q / p.qty_step) % 1 != 0:
                raise DomainError(f"{p.name} is sold in steps of {p.qty_step.normalize():f} {p.unit}.")
            limit = min([x for x in (p.max_customer_qty, item.per_customer_max) if x is not None], default=None)
            if limit is not None and q > limit:
                raise DomainError(f"The per-customer limit for {p.name} is {limit.normalize():f} {p.unit}.")
            if item.max_qty is not None:
                available = item.max_qty - others.get(item.id, ZERO)
                if q > available:
                    raise DomainError(
                        f"Only {max(available, ZERO).normalize():f} {p.unit} of {p.name} remain before the "
                        f"group maximum is reached.")
        line = existing.get(item.id)
        if q == 0:
            if line is not None:
                co.items.remove(line)
        elif line is None:
            co.items.append(CustomerOrderItem(group_order_item_id=item.id, product_id=p.id, quantity=q))
        else:
            line.quantity = q
    db.session.flush()
    return co


# ------------------------------------------------------ freeze / validate

def freeze(go, actor=None, reason="Order deadline reached"):
    """Freeze quantities, validate minimums and move the order to offers."""
    if actor is not None and utcnow() < go.order_deadline:
        require_organiser_or_admin(go.group, actor)
    transition_group_order(go, GOStatus.FROZEN, actor, reason)
    go.frozen_at = utcnow()
    agg = aggregate_demand(go)
    dropped_names = []
    for item in go.items:
        total = agg.get(item.id, ZERO)
        item.frozen_qty = total
        if total <= 0 or total < item.min_qty:
            item.dropped = True
            dropped_names.append(item.product.name)
    live = [co for co in go.customer_orders if co.status == COStatus.OPEN]
    for co in live:
        for line in list(co.items):
            if line.group_order_item.dropped:
                co.items.remove(line)
        if not co.items:
            transition_customer_order(co, COStatus.CANCELLED)
            co.cancelled_at = utcnow()
            co.cancel_reason = "No products reached the group minimum" if dropped_names else "No items ordered"
        else:
            transition_customer_order(co, COStatus.LOCKED)
        if dropped_names:
            notify(co.user_id, f"{', '.join(dropped_names)} did not reach the group minimum in "
                               f"{go.group.name} and were removed.", f"/group-orders/{go.id}")
    remaining = [i for i in go.items if not i.dropped]
    if not remaining or not any(co.status == COStatus.LOCKED for co in live):
        cancel_group_order(go, actor, "Minimum group quantities were not reached by the deadline.",
                           _system=True)
        return go
    db.session.flush()
    open_bidding(go, actor)
    return go


def open_bidding(go, actor=None, deadline=None):
    now = utcnow()
    if go.status == GOStatus.SELECTION:
        if actor is None or not actor.has_role(Roles.ADMIN):
            raise PermissionDenied("Only an administrator can reopen bidding.")
    deadline = deadline or (go.bidding_deadline if go.bidding_deadline and go.bidding_deadline > now else None)
    deadline = deadline or now + timedelta(hours=settings.get("default_bidding_hours"))
    if deadline <= now:
        raise DomainError("The bidding deadline must be in the future.")
    if deadline >= go.delivery_window_end:
        raise DomainError("The bidding deadline must be before the end of the delivery window.")
    multi = settings.get("multi_vendor_enabled")
    go.bidding_mode = BiddingMode.MULTI_VENDOR if multi else BiddingMode.SINGLE_VENDOR
    go.bidding_deadline = deadline
    go.bidding_opened_at = now
    transition_group_order(go, GOStatus.BIDDING, actor, f"Bidding open ({go.bidding_mode})")
    vendors = db.session.query(VendorProfile).filter(VendorProfile.status == VendorStatus.APPROVED)
    if not multi:
        vendors = vendors.filter(VendorProfile.is_platform_owned.is_(True))
    for v in vendors.all():
        if v.serves(go.group.location_id):
            notify(v.user_id, f"New group order open for offers: {go.title}.", f"/vendor/opportunities/{go.id}/bid")
    for co in go.customer_orders:
        if co.status == COStatus.LOCKED:
            notify(co.user_id, f"Orders for {go.group.name} are frozen. Vendors are preparing offers.",
                   f"/group-orders/{go.id}")
    return go


def cancel_group_order(go, actor, reason, _system=False):
    if not _system:
        if actor is None or not actor.has_role(Roles.ADMIN):
            # Organisers may cancel only before any offer is accepted.
            require_organiser_or_admin(go.group, actor)
            if go.status not in (GOStatus.DRAFT, GOStatus.COLLECTING, GOStatus.BIDDING, GOStatus.SELECTION):
                raise PermissionDenied("Only an administrator can cancel an order that is in fulfilment.")
    if not reason or not reason.strip():
        raise DomainError("A cancellation reason is required.")
    transition_group_order(go, GOStatus.CANCELLED, actor, reason)
    go.cancelled_at = utcnow()
    go.cancellation_reason = reason.strip()[:500]
    for bid in go.bids:
        if bid.status in (BidStatus.SUBMITTED, BidStatus.AWARDED):
            bid.status = BidStatus.NOT_SELECTED
    award = go.active_award
    if award:
        award.status = "revoked"
        award.revoked_at = utcnow()
        award.revoked_by_id = getattr(actor, "id", None)
        award.revoke_reason = "Group order cancelled"
    for co in go.customer_orders:
        if co.status in (COStatus.CANCELLED, COStatus.COMPLETED):
            continue
        co.status = COStatus.CANCELLED
        co.cancelled_at = utcnow()
        co.cancel_reason = "Group order cancelled: " + go.cancellation_reason
        for p in co.payments:
            if p.status in ("pending", "initiated"):
                p.status = "failed"
                p.failure_reason = "Order cancelled"
        from .payments import refresh_payment_status
        refresh_payment_status(co)
        msg = f"{go.title} was cancelled: {go.cancellation_reason}"
        if co.payment_status in ("paid", "partially_refunded"):
            msg += " Any payment you made will be refunded."
        notify(co.user_id, msg, f"/orders/{co.id}")
    db.session.flush()
    return go


def active_admin_ids():
    from ..models import Role, RoleAssignment
    return [uid for (uid,) in db.session.query(RoleAssignment.user_id)
            .join(Role, Role.id == RoleAssignment.role_id).filter(Role.name == Roles.ADMIN).all()]


def products_for_selection():
    return db.session.query(Product).filter_by(is_active=True).order_by(Product.name).all()


def grades():
    return db.session.query(ProductGrade).filter_by(is_active=True).order_by(ProductGrade.rank.desc()).all()
