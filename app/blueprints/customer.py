"""Customer pages: dashboard, groups, group orders, orders, complaints, profile."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..constants import (COStatus, DeliveryStatus, FulfilmentOption, GOStatus, Roles, SelectionMode)
from ..extensions import db
from ..forms import AddressForm, GroupForm, PasswordChangeForm, ProfileForm
from ..models import (Address, BuyingGroup, Complaint, CustomerOrder, GroupMembership, GroupOrder,
                      GroupOrderItem, Location, Notification, PickupPoint, Product, ProductGrade, Review)
from ..security import customer_required, rate_limit, run_action
from ..services import bidding as bid_svc
from ..services import fulfilment as ful_svc
from ..services import groups as group_svc
from ..services import payments as pay_svc
from ..services import settings as settings_svc
from ..services import support as support_svc
from ..services.core import DomainError
from ..services.uploads import attach_files, attachments_for
from ..utils import parse_local_datetime, utcnow
from .public import refresh_deadlines

bp = Blueprint("customer", __name__)


# ------------------------------------------------------------------ helpers

def _go_or_404(go_id):
    go = db.session.get(GroupOrder, go_id) or abort(404)
    refresh_deadlines(go)
    return go


def _require_member(go):
    """Members of the group (or admins) may view a group order's private details."""
    if current_user.has_role(Roles.ADMIN):
        return None
    m = go.group.membership_for(current_user)
    if m is None:
        abort(403)
    return m


def _own_order(order_id):
    co = db.session.get(CustomerOrder, order_id) or abort(404)
    if co.user_id != current_user.id:
        abort(404)   # do not reveal other customers' orders
    return co


def _locations():
    return db.session.query(Location).filter_by(is_active=True).order_by(Location.city, Location.name).all()


def _pickup_choices():
    rows = (db.session.query(PickupPoint).filter_by(is_active=True).order_by(PickupPoint.name).all())
    return [(0, "— None —")] + [(p.id, f"{p.name} ({p.location.label})") for p in rows]


# ---------------------------------------------------------------- dashboard

@bp.route("/dashboard")
@login_required
def dashboard():
    if not current_user.has_role(Roles.CUSTOMER):
        from .auth import home_for
        return redirect(home_for(current_user))
    memberships = (db.session.query(GroupMembership)
                   .filter_by(user_id=current_user.id, status="active").all())
    active = []
    for m in memberships:
        go = m.group.current_order
        refresh_deadlines(go)
        go = m.group.current_order
        if go:
            active.append((m.group, go, group_svc.progress(go), group_svc.customer_order_for(go, current_user)))
    orders = (db.session.query(CustomerOrder).filter_by(user_id=current_user.id)
              .order_by(CustomerOrder.id.desc()).all())
    pending = [o for o in orders if o.status in (COStatus.LOCKED, COStatus.AWAITING_CONFIRMATION, COStatus.CONFIRMED)]
    upcoming = [o for o in orders if o.delivery and o.delivery.status in (DeliveryStatus.SCHEDULED,
                                                                         DeliveryStatus.DISPATCHED)]
    recent = [o for o in orders if o.status in (COStatus.DELIVERED, COStatus.COMPLETED)][:5]
    return render_template("customer/dashboard.html", active=active, pending=pending, upcoming=upcoming,
                           recent=recent, group_count=len(memberships))


# ------------------------------------------------------------------- groups

@bp.route("/my-groups")
@customer_required
def my_groups():
    memberships = (db.session.query(GroupMembership).filter_by(user_id=current_user.id, status="active")
                   .order_by(GroupMembership.joined_at.desc()).all())
    return render_template("customer/my_groups.html", memberships=memberships)


@bp.route("/groups/new", methods=["GET", "POST"])
@customer_required
def create_group():
    form = GroupForm()
    form.location_id.choices = [(l.id, l.label) for l in _locations()]
    form.pickup_point_id.choices = _pickup_choices()
    if form.validate_on_submit():
        location = db.session.get(Location, form.location_id.data)
        pickup = db.session.get(PickupPoint, form.pickup_point_id.data) if form.pickup_point_id.data else None
        group = run_action(group_svc.create_group, current_user, name=form.name.data,
                           description=form.description.data, location=location, pickup_point=pickup,
                           is_public=form.is_public.data)
        if group:
            go = _create_round(group)
            if go:
                return redirect(url_for("customer.group_order", go_id=go.id))
            flash("Group created. Fix the order round details below.", "warning")
            return redirect(url_for("customer.new_round", group_id=group.id))
    return render_template("customer/create_group.html", form=form, defaults=_round_defaults(),
                           SelectionMode=SelectionMode, FulfilmentOption=FulfilmentOption)


def _round_defaults():
    from datetime import timedelta
    now = utcnow()
    return {"order_deadline": now + timedelta(days=2), "window_start": now + timedelta(days=3, hours=2),
            "window_end": now + timedelta(days=3, hours=6),
            "selection_mode": settings_svc.get("default_selection_mode")}


def _create_round(group):
    f = request.form
    return run_action(
        group_svc.create_group_order, group, current_user,
        title=f.get("title") or f"{group.name} order",
        order_deadline=parse_local_datetime(f.get("order_deadline")),
        delivery_window_start=parse_local_datetime(f.get("delivery_window_start")),
        delivery_window_end=parse_local_datetime(f.get("delivery_window_end")),
        fulfilment_options=f.get("fulfilment_options", FulfilmentOption.BOTH),
        pickup_point_id=group.pickup_point_id, allow_partial=f.get("allow_partial") == "1",
        min_fulfilment_percent=f.get("min_fulfilment_percent") or "100",
        selection_mode=f.get("selection_mode"), notes=f.get("notes"),
        success="Order round created. Add products and target quantities, then open it for orders.",
    )


@bp.route("/groups/<int:group_id>/rounds/new", methods=["GET", "POST"])
@customer_required
def new_round(group_id):
    group = db.session.get(BuyingGroup, group_id) or abort(404)
    if not group_svc.is_organiser(group, current_user):
        abort(403)
    if request.method == "POST":
        go = _create_round(group)
        if go:
            return redirect(url_for("customer.group_order", go_id=go.id))
    return render_template("customer/new_round.html", group=group, defaults=_round_defaults(),
                           SelectionMode=SelectionMode, FulfilmentOption=FulfilmentOption)


@bp.route("/groups/join", methods=["GET", "POST"])
@customer_required
@rate_limit(20, 60)
def join_by_code():
    if request.method == "POST":
        code = (request.form.get("code") or "").strip().upper()
        group = db.session.query(BuyingGroup).filter_by(invite_code=code).first()
        if group is None:
            flash("No group matches that invitation code.", "danger")
        else:
            return redirect(url_for("customer.join_link", code=code))
    return render_template("customer/join.html")


@bp.route("/join/<code>", methods=["GET", "POST"])
@login_required
def join_link(code):
    group = db.session.query(BuyingGroup).filter_by(invite_code=code.strip().upper()).first() or abort(404)
    if request.method == "POST":
        if run_action(group_svc.join_group, current_user, group, success=f"You joined {group.name}."):
            go = group.current_order
            if go:
                return redirect(url_for("customer.group_order", go_id=go.id))
            return redirect(url_for("public.group_detail", group_id=group.id))
    return render_template("customer/join_confirm.html", group=group,
                           member=group.membership_for(current_user))


@bp.route("/groups/<int:group_id>/join", methods=["POST"])
@customer_required
def join_public(group_id):
    group = db.session.get(BuyingGroup, group_id) or abort(404)
    if not group.is_public:
        abort(403)
    run_action(group_svc.join_group, current_user, group, success=f"You joined {group.name}.")
    return redirect(url_for("public.group_detail", group_id=group.id))


@bp.route("/groups/<int:group_id>/leave", methods=["POST"])
@customer_required
def leave_group(group_id):
    group = db.session.get(BuyingGroup, group_id) or abort(404)
    if run_action(group_svc.leave_group, current_user, group, success=f"You left {group.name}."):
        return redirect(url_for("customer.my_groups"))
    return redirect(url_for("public.group_detail", group_id=group.id))


# ------------------------------------------------------------- group order

@bp.route("/group-orders/<int:go_id>")
@login_required
def group_order(go_id):
    go = _go_or_404(go_id)
    member = _require_member(go)
    is_org = group_svc.is_organiser(go.group, current_user)
    co = group_svc.customer_order_for(go, current_user)
    products = group_svc.products_for_selection() if is_org else []
    used = {i.product_id for i in go.items}
    return render_template(
        "customer/group_order.html", go=go, member=member, is_organiser=is_org, co=co,
        progress=group_svc.progress(go), products=[p for p in products if p.id not in used],
        grades=group_svc.grades(), award=go.active_award, offers_visible=bid_svc.bids_visible_to_customers(go),
        bid_count=len([b for b in go.bids if b.status != "withdrawn"]),
        members=go.group.active_members,
    )


def _organiser_go(go_id):
    go = _go_or_404(go_id)
    if not group_svc.is_organiser(go.group, current_user):
        abort(403)
    return go


@bp.route("/group-orders/<int:go_id>/items", methods=["POST"])
@customer_required
def save_item(go_id):
    go = _organiser_go(go_id)
    f = request.form
    product = db.session.get(Product, int(f.get("product_id") or 0))
    grade = db.session.get(ProductGrade, int(f.get("min_grade_id") or 0)) if f.get("min_grade_id") else None
    run_action(group_svc.set_item, go, current_user, product=product, target_qty=f.get("target_qty"),
               min_qty=f.get("min_qty"), max_qty=f.get("max_qty"), per_customer_max=f.get("per_customer_max"),
               min_grade=grade, success="Product saved.")
    return redirect(url_for("customer.group_order", go_id=go.id) + "#manage")


@bp.route("/group-orders/<int:go_id>/items/<int:item_id>/remove", methods=["POST"])
@customer_required
def remove_item(go_id, item_id):
    go = _organiser_go(go_id)
    item = db.session.get(GroupOrderItem, item_id)
    if item is None or item.group_order_id != go.id:
        abort(404)
    run_action(group_svc.remove_item, go, current_user, item, success="Product removed.")
    return redirect(url_for("customer.group_order", go_id=go.id) + "#manage")


@bp.route("/group-orders/<int:go_id>/open", methods=["POST"])
@customer_required
def open_order(go_id):
    go = _organiser_go(go_id)
    run_action(group_svc.open_for_orders, go, current_user,
               success="The order is open. Share the invite link so neighbours can add their quantities.")
    return redirect(url_for("customer.group_order", go_id=go.id))


@bp.route("/group-orders/<int:go_id>/schedule", methods=["POST"])
@customer_required
def update_schedule(go_id):
    go = _organiser_go(go_id)
    f = request.form
    run_action(group_svc.update_schedule, go, current_user,
               order_deadline=parse_local_datetime(f.get("order_deadline")),
               delivery_window_start=parse_local_datetime(f.get("delivery_window_start")),
               delivery_window_end=parse_local_datetime(f.get("delivery_window_end")),
               success="Schedule updated.")
    return redirect(url_for("customer.group_order", go_id=go.id) + "#manage")


@bp.route("/group-orders/<int:go_id>/close-early", methods=["POST"])
@customer_required
def close_early(go_id):
    go = _organiser_go(go_id)
    if go.status != GOStatus.COLLECTING:
        flash("Only an order that is collecting quantities can be closed.", "warning")
    else:
        run_action(group_svc.freeze, go, current_user, "Closed early by organiser",
                   success="Orders are frozen and the group order moved to offers.")
    return redirect(url_for("customer.group_order", go_id=go.id))


@bp.route("/group-orders/<int:go_id>/cancel", methods=["POST"])
@customer_required
def cancel_order(go_id):
    go = _organiser_go(go_id)
    run_action(group_svc.cancel_group_order, go, current_user, request.form.get("reason", ""),
               success="The group order was cancelled.")
    return redirect(url_for("customer.group_order", go_id=go.id))


@bp.route("/group-orders/<int:go_id>/my-order", methods=["GET", "POST"])
@customer_required
def my_order(go_id):
    go = _go_or_404(go_id)
    _require_member(go)
    co = group_svc.customer_order_for(go, current_user)
    if request.method == "POST":
        quantities = {i.id: request.form.get(f"qty_{i.id}", "0") for i in go.items}
        address = None
        if request.form.get("address_id"):
            address = db.session.get(Address, int(request.form["address_id"]))
        if run_action(group_svc.update_customer_quantities, go, current_user, quantities,
                      request.form.get("fulfilment_method"), address,
                      success="Your quantities were saved. You can change them until the deadline."):
            return redirect(url_for("customer.group_order", go_id=go.id))
        co = group_svc.customer_order_for(go, current_user)
    agg = group_svc.aggregate_demand(go)
    mine = {it.group_order_item_id: it.quantity for it in co.items} if co else {}
    from ..services.pricing import indicative_prices
    return render_template("customer/my_order.html", go=go, co=co, mine=mine, agg=agg,
                           can_edit=group_svc.ordering_open(go), addresses=current_user.addresses,
                           prices=indicative_prices([i.product_id for i in go.items]))


@bp.route("/group-orders/<int:go_id>/offers")
@login_required
def offers(go_id):
    go = _go_or_404(go_id)
    _require_member(go)
    if not bid_svc.bids_visible_to_customers(go):
        flash("Offers are sealed until the bidding deadline.", "info")
        return redirect(url_for("customer.group_order", go_id=go.id))
    from ..services.pricing import bid_customer_cost
    bids = bid_svc.eligible_bids(go)
    co = group_svc.customer_order_for(go, current_user)
    ineligible = [b for b in go.bids if b not in bids and b.status not in ("withdrawn",)]
    histories = {b.id: bid_svc.vendor_history(b.vendor) for b in bids}
    return render_template(
        "customer/offers.html", go=go, bids=bids, ineligible=ineligible, costs={b.id: bid_customer_cost(go, b) for b in bids},
        histories=histories, tally=bid_svc.vote_tally(go), my_vote=bid_svc.user_vote(go, current_user),
        is_organiser=group_svc.is_organiser(go.group, current_user), co=co, award=go.active_award,
        disclosure=settings_svc.get("platform_owner_disclosure"),
        verified_organic={b.id: support_svc.has_verified_organic(b.vendor) for b in bids},
    )


@bp.route("/group-orders/<int:go_id>/vote", methods=["POST"])
@customer_required
def vote(go_id):
    go = _go_or_404(go_id)
    _require_member(go)
    from ..models import VendorBid
    bid = db.session.get(VendorBid, int(request.form.get("bid_id") or 0)) or abort(404)
    run_action(bid_svc.cast_vote, current_user, go, bid, success="Your vote was recorded.")
    return redirect(url_for("customer.offers", go_id=go.id))


@bp.route("/group-orders/<int:go_id>/award", methods=["POST"])
@customer_required
def award(go_id):
    go = _go_or_404(go_id)
    _require_member(go)
    from ..models import VendorBid
    bid = None
    if request.form.get("bid_id"):
        bid = db.session.get(VendorBid, int(request.form["bid_id"])) or abort(404)
    run_action(bid_svc.award, go, current_user, bid,
               success="Offer selected. Members can now confirm their orders.")
    return redirect(url_for("customer.offers", go_id=go.id))


# ------------------------------------------------------------------- orders

@bp.route("/orders")
@customer_required
def orders():
    rows = (db.session.query(CustomerOrder).filter_by(user_id=current_user.id)
            .order_by(CustomerOrder.id.desc()).all())
    return render_template("customer/orders.html", orders=rows)


@bp.route("/orders/<int:order_id>")
@customer_required
def order_detail(order_id):
    co = _own_order(order_id)
    refresh_deadlines(co.group_order)
    review = db.session.query(Review).filter_by(customer_order_id=co.id).first()
    complaints = db.session.query(Complaint).filter_by(customer_order_id=co.id).order_by(Complaint.id.desc()).all()
    return render_template("customer/order_detail.html", co=co, go=co.group_order, review=review,
                           complaints=complaints, paid=pay_svc.amount_paid(co),
                           refunded=pay_svc.amount_refunded(co), outstanding=pay_svc.amount_outstanding(co),
                           instructions=settings_svc.get("payment_instructions"),
                           addresses=current_user.addresses, award=co.group_order.active_award)


@bp.route("/orders/<int:order_id>/confirm", methods=["POST"])
@customer_required
def confirm_order(order_id):
    co = _own_order(order_id)
    address = None
    if request.form.get("address_id"):
        address = db.session.get(Address, int(request.form["address_id"]))
    run_action(ful_svc.confirm_customer_order, current_user, co, request.form.get("fulfilment_method"), address,
               success="Order confirmed. Payment instructions are shown below.")
    return redirect(url_for("customer.order_detail", order_id=co.id))


@bp.route("/orders/<int:order_id>/decline", methods=["POST"])
@customer_required
def decline_order(order_id):
    co = _own_order(order_id)
    run_action(ful_svc.decline_customer_order, current_user, co, request.form.get("reason"),
               success="You declined this order.")
    return redirect(url_for("customer.order_detail", order_id=co.id))


@bp.route("/orders/<int:order_id>/tracking")
@customer_required
def tracking(order_id):
    co = _own_order(order_id)
    proofs = attachments_for("delivery", co.delivery.id) if co.delivery else []
    inspections = [i for i in ful_svc.inspections_for(co.group_order)]
    return render_template("customer/tracking.html", co=co, go=co.group_order, delivery=co.delivery,
                           proofs=proofs, inspections=inspections)


@bp.route("/orders/<int:order_id>/received", methods=["POST"])
@customer_required
def confirm_received(order_id):
    co = _own_order(order_id)
    if co.delivery is None:
        abort(404)
    run_action(ful_svc.customer_confirm_receipt, current_user, co.delivery,
               success="Thanks for confirming receipt.")
    return redirect(url_for("customer.tracking", order_id=co.id))


@bp.route("/orders/<int:order_id>/receipt")
@customer_required
def receipt(order_id):
    co = _own_order(order_id)
    return render_template("customer/receipt.html", co=co, go=co.group_order, paid=pay_svc.amount_paid(co),
                           refunded=pay_svc.amount_refunded(co), outstanding=pay_svc.amount_outstanding(co))


@bp.route("/orders/<int:order_id>/complaint", methods=["GET", "POST"])
@customer_required
@rate_limit(10, 600)
def new_complaint(order_id):
    co = _own_order(order_id)
    if request.method == "POST":
        f = request.form

        def create():
            c = support_svc.create_complaint(current_user, co, complaint_type=f.get("complaint_type"),
                                             description=f.get("description"), product_id=f.get("product_id"),
                                             quantity_affected=f.get("quantity_affected") or None)
            attach_files(request.files.getlist("photos"), "complaint", c.id, current_user)
            return c

        if run_action(create, success="Complaint submitted. We will review it and keep you updated."):
            return redirect(url_for("customer.complaints"))
    return render_template("customer/complaint_form.html", co=co)


@bp.route("/orders/<int:order_id>/review", methods=["POST"])
@customer_required
def review(order_id):
    co = _own_order(order_id)
    run_action(support_svc.create_review, current_user, co, request.form.get("rating"), request.form.get("comment"),
               success="Thanks for rating this order.")
    return redirect(url_for("customer.order_detail", order_id=co.id))


@bp.route("/complaints")
@customer_required
def complaints():
    rows = (db.session.query(Complaint).filter_by(user_id=current_user.id).order_by(Complaint.id.desc()).all())
    photos = {c.id: attachments_for("complaint", c.id) for c in rows}
    return render_template("customer/complaints.html", complaints=rows, photos=photos)


# ------------------------------------------------------------------ profile

@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    form = ProfileForm(obj=current_user)
    pw_form = PasswordChangeForm(prefix="pw")
    if form.validate_on_submit() and "full_name" in request.form:
        current_user.full_name = form.full_name.data.strip()
        current_user.phone = form.phone.data.strip()
        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("customer.profile"))
    return render_template("customer/profile.html", form=form, pw_form=pw_form)


@bp.route("/profile/password", methods=["POST"])
@login_required
@rate_limit(5, 300)
def change_password():
    pw_form = PasswordChangeForm(prefix="pw")
    if pw_form.validate_on_submit():
        if not current_user.check_password(pw_form.current_password.data):
            flash("Your current password is incorrect.", "danger")
        else:
            try:
                support_svc.validate_password(pw_form.new_password.data)
            except DomainError as exc:
                flash(str(exc), "danger")
            else:
                current_user.set_password(pw_form.new_password.data)
                from ..services.core import audit
                audit(current_user, "auth.password_changed", current_user)
                db.session.commit()
                flash("Password changed.", "success")
    else:
        for errors in pw_form.errors.values():
            for e in errors:
                flash(e, "danger")
    return redirect(url_for("customer.profile"))


def _address_form(address=None):
    form = AddressForm(obj=address)
    form.location_id.choices = [(0, "— Not listed —")] + [(l.id, l.label) for l in _locations()]
    return form


@bp.route("/addresses/new", methods=["GET", "POST"])
@bp.route("/addresses/<int:address_id>/edit", methods=["GET", "POST"])
@customer_required
def address_form(address_id=None):
    address = None
    if address_id:
        address = db.session.get(Address, address_id)
        if address is None or address.user_id != current_user.id:
            abort(404)
    form = _address_form(address)
    if form.validate_on_submit():
        if address is None:
            address = Address(user_id=current_user.id)
            db.session.add(address)
        for field in ("label", "recipient_name", "phone", "line1", "line2", "city", "state", "postal_code"):
            setattr(address, field, (getattr(form, field).data or "").strip() or None)
        address.location_id = form.location_id.data or None
        if form.is_default.data or len(current_user.addresses) <= 1:
            for a in current_user.addresses:
                a.is_default = False
            address.is_default = True
        db.session.commit()
        flash("Address saved.", "success")
        return redirect(url_for("customer.profile"))
    return render_template("customer/address_form.html", form=form, address=address)


@bp.route("/addresses/<int:address_id>/delete", methods=["POST"])
@customer_required
def delete_address(address_id):
    address = db.session.get(Address, address_id)
    if address is None or address.user_id != current_user.id:
        abort(404)
    in_use = (db.session.query(CustomerOrder)
              .filter(CustomerOrder.address_id == address.id,
                      CustomerOrder.status.in_((COStatus.OPEN, COStatus.LOCKED, COStatus.AWAITING_CONFIRMATION,
                                                COStatus.CONFIRMED))).count())
    if in_use:
        flash("This address is used by an order in progress.", "warning")
    else:
        db.session.delete(address)
        db.session.commit()
        flash("Address deleted.", "success")
    return redirect(url_for("customer.profile"))


@bp.route("/notifications")
@login_required
def notifications():
    rows = (db.session.query(Notification).filter_by(user_id=current_user.id)
            .order_by(Notification.id.desc()).limit(100).all())
    return render_template("customer/notifications.html", notifications=rows)


@bp.route("/notifications/read-all", methods=["POST"])
@login_required
def notifications_read():
    db.session.query(Notification).filter_by(user_id=current_user.id, is_read=False).update({"is_read": True})
    db.session.commit()
    return redirect(url_for("customer.notifications"))
