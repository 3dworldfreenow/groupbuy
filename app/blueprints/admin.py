"""Administrator pages. Every route requires the admin role (server-side)."""
import csv
import io
from datetime import datetime

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..constants import (COMPLAINT_RESOLUTIONS, INSPECTION_CHECKLIST, PRODUCT_UNITS, VENDOR_DOCUMENT_TYPES,
                         VENDOR_TRANSITIONS,
                         BidStatus, ComplaintStatus, CostCategory, DeliveryStatus, GOStatus, Roles, VendorStatus)
from ..extensions import db
from ..forms import CategoryForm, GradeForm, LocationForm, PickupPointForm, ProductForm
from ..models import (AuditLog, BuyingGroup, CommissionRecord, Complaint, ContactMessage, CostRecord,
                      CustomerOrder, Delivery, GroupOrder, Location, PaymentRecord, PickupPoint, Product,
                      ProductCategory, ProductGrade, QualityInspection, Refund, User, VendorDocument, VendorProfile,
                      VendorSettlementRecord)
from ..security import admin_required, run_action
from ..services import bidding as bid_svc
from ..services import finance as fin_svc
from ..services import fulfilment as ful_svc
from ..services import groups as group_svc
from ..services import payments as pay_svc
from ..services import settings as settings_svc
from ..services import support as support_svc
from ..services.core import DomainError, audit
from ..services.uploads import attach_files, attachments_for
from ..utils import parse_local_datetime, utcnow
from .public import refresh_deadlines
from .vendor import inspection_items_from_form

bp = Blueprint("admin", __name__)


@bp.before_request
@admin_required
def _guard():
    """All admin routes require the admin role."""
    return None


# ---------------------------------------------------------------- dashboard

@bp.route("/")
def dashboard():
    metrics = fin_svc.admin_metrics()
    attention = {
        "pending_vendors": db.session.query(VendorProfile).filter_by(status=VendorStatus.PENDING).count(),
        "pending_docs": db.session.query(VendorDocument).filter_by(status="pending").count(),
        "quality_check": db.session.query(GroupOrder).filter_by(status=GOStatus.QUALITY_CHECK).count(),
        "on_hold": db.session.query(GroupOrder).filter_by(status=GOStatus.ON_HOLD).count(),
        "open_complaints": db.session.query(Complaint).filter(
            Complaint.status.in_(ComplaintStatus.UNRESOLVED)).count(),
        "to_close": db.session.query(GroupOrder).filter_by(status=GOStatus.DELIVERED).count(),
        "selection_no_bids": [go for go in db.session.query(GroupOrder).filter_by(status=GOStatus.SELECTION)
                              if not bid_svc.eligible_bids(go)],
        "unpaid": db.session.query(CustomerOrder).filter(
            CustomerOrder.status.in_(("confirmed", "delivered")),
            CustomerOrder.payment_status.in_(("pending", "initiated", "failed"))).count(),
        "messages": db.session.query(ContactMessage).filter_by(is_handled=False).count(),
    }
    recent = db.session.query(GroupOrder).order_by(GroupOrder.updated_at.desc()).limit(8).all()
    return render_template("admin/dashboard.html", m=metrics, attention=attention, recent=recent,
                           multi_vendor=settings_svc.get("multi_vendor_enabled"))


# -------------------------------------------------------------------- users

@bp.route("/users")
def users():
    q = (request.args.get("q") or "").strip()[:80]
    role = request.args.get("role")
    query = db.session.query(User)
    if q:
        query = query.filter((User.email.ilike(f"%{q}%")) | (User.full_name.ilike(f"%{q}%")))
    rows = query.order_by(User.id.desc()).limit(300).all()
    if role in Roles.ALL:
        rows = [u for u in rows if u.has_role(role)]
    return render_template("admin/users.html", users=rows, q=q, role=role, roles=Roles.ALL)


@bp.route("/users/<int:user_id>", methods=["GET", "POST"])
def user_detail(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        if user.id == current_user.id and action in ("deactivate", "revoke_admin"):
            flash("You cannot remove your own access.", "warning")
        elif action == "deactivate":
            user.active = False
            audit(current_user, "user.deactivated", user)
        elif action == "activate":
            user.active = True
            user.locked_until = None
            audit(current_user, "user.activated", user)
        elif action == "unlock":
            user.locked_until = None
            user.failed_logins = 0
            audit(current_user, "user.unlocked", user)
        elif action in ("grant_admin", "grant_customer"):
            role = Roles.ADMIN if action == "grant_admin" else Roles.CUSTOMER
            user.add_role(support_svc.get_role(role), current_user)
            if role == Roles.CUSTOMER and user.customer_profile is None:
                from ..models import CustomerProfile
                user.customer_profile = CustomerProfile()
            audit(current_user, "user.role_granted", user, {"role": role})
        elif action in ("revoke_admin", "revoke_customer"):
            role = Roles.ADMIN if action == "revoke_admin" else Roles.CUSTOMER
            user.remove_role(role)
            audit(current_user, "user.role_revoked", user, {"role": role})
        else:
            abort(400)
        db.session.commit()
        flash("User updated.", "success")
        return redirect(url_for("admin.user_detail", user_id=user.id))
    orders = db.session.query(CustomerOrder).filter_by(user_id=user.id).order_by(CustomerOrder.id.desc()).limit(20)
    return render_template("admin/user_detail.html", user=user, orders=orders.all())


# ------------------------------------------------------------------ vendors

@bp.route("/vendors")
def vendors():
    status = request.args.get("status")
    q = db.session.query(VendorProfile)
    if status in VendorStatus.ALL:
        q = q.filter_by(status=status)
    rows = q.order_by(VendorProfile.status, VendorProfile.id.desc()).all()
    return render_template("admin/vendors.html", vendors=rows, status=status, statuses=VendorStatus.ALL)


@bp.route("/vendors/<int:vendor_id>", methods=["GET", "POST"])
def vendor_detail(vendor_id):
    v = db.session.get(VendorProfile, vendor_id) or abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "status":
            run_action(support_svc.set_vendor_status, v, current_user, request.form.get("status"),
                       request.form.get("reason"), success="Vendor status updated.")
        elif action == "settings":
            def save():
                rate = request.form.get("commission_rate_override")
                from ..services.pricing import parse_decimal
                v.commission_rate_override = parse_decimal(rate, "Commission", 0, 50, required=False)
                ids = [int(x) for x in request.form.getlist("service_areas")]
                v.service_areas = db.session.query(Location).filter(Location.id.in_(ids or [0])).all()
                audit(current_user, "vendor.settings", v, {"commission_override": rate, "areas": ids})
            run_action(save, success="Vendor settings saved.")
        else:
            abort(400)
        return redirect(url_for("admin.vendor_detail", vendor_id=v.id))
    history = bid_svc.vendor_history(v)
    return render_template("admin/vendor_detail.html", v=v, history=history,
                           score=bid_svc.quality_score(v, history), doc_types=VENDOR_DOCUMENT_TYPES,
                           locations=db.session.query(Location).order_by(Location.name).all(),
                           transitions=VENDOR_TRANSITIONS.get(v.status, set()))


@bp.route("/documents/<int:doc_id>/review", methods=["POST"])
def review_document(doc_id):
    doc = db.session.get(VendorDocument, doc_id) or abort(404)
    run_action(support_svc.review_document, doc, current_user, request.form.get("status"),
               request.form.get("notes"), success="Document reviewed.")
    return redirect(url_for("admin.vendor_detail", vendor_id=doc.vendor_id))


# ------------------------------------------------------------------ catalog

@bp.route("/catalog")
def catalog():
    return render_template("admin/catalog.html",
                           products=db.session.query(Product).order_by(Product.name).all(),
                           categories=db.session.query(ProductCategory).order_by(ProductCategory.sort_order).all(),
                           grades=db.session.query(ProductGrade).order_by(ProductGrade.rank.desc()).all())


def _crud(model, form_cls, template, entity_id, setup=None, label="Item"):
    obj = db.session.get(model, entity_id) if entity_id else None
    if entity_id and obj is None:
        abort(404)
    form = form_cls(obj=obj)
    if setup:
        setup(form)
    if form.validate_on_submit():
        is_new = obj is None
        obj = obj or model()
        form.populate_obj(obj)
        if hasattr(obj, "location_id") and getattr(obj, "location_id", None) == 0:
            obj.location_id = None
        db.session.add(obj)
        try:
            db.session.flush()
            audit(current_user, f"catalog.{model.__tablename__}.{'created' if is_new else 'updated'}", obj, {})
            db.session.commit()
        except Exception:  # unique constraint etc.
            db.session.rollback()
            flash(f"{label} could not be saved — the name/code/slug may already be in use.", "danger")
        else:
            flash(f"{label} saved.", "success")
            return None, None
    return form, obj


@bp.route("/products/new", methods=["GET", "POST"])
@bp.route("/products/<int:entity_id>", methods=["GET", "POST"])
def product_form(entity_id=None):
    def setup(form):
        form.category_id.choices = [(c.id, c.name) for c in db.session.query(ProductCategory).order_by(
            ProductCategory.name)]
        form.unit.choices = [(u, u) for u in PRODUCT_UNITS]
    form, obj = _crud(Product, ProductForm, None, entity_id, setup, "Product")
    if form is None:
        return redirect(url_for("admin.catalog"))
    return render_template("admin/simple_form.html", form=form, title=("Edit" if obj else "New") + " product",
                           back=url_for("admin.catalog"))


@bp.route("/categories/new", methods=["GET", "POST"])
@bp.route("/categories/<int:entity_id>", methods=["GET", "POST"])
def category_form(entity_id=None):
    form, obj = _crud(ProductCategory, CategoryForm, None, entity_id, label="Category")
    if form is None:
        return redirect(url_for("admin.catalog"))
    return render_template("admin/simple_form.html", form=form, title=("Edit" if obj else "New") + " category",
                           back=url_for("admin.catalog"))


@bp.route("/grades/new", methods=["GET", "POST"])
@bp.route("/grades/<int:entity_id>", methods=["GET", "POST"])
def grade_form(entity_id=None):
    form, obj = _crud(ProductGrade, GradeForm, None, entity_id, label="Grade")
    if form is None:
        return redirect(url_for("admin.catalog"))
    return render_template("admin/simple_form.html", form=form, title=("Edit" if obj else "New") + " grade",
                           back=url_for("admin.catalog"))


@bp.route("/locations")
def locations():
    return render_template("admin/locations.html",
                           locations=db.session.query(Location).order_by(Location.city, Location.name).all())


@bp.route("/locations/new", methods=["GET", "POST"])
@bp.route("/locations/<int:entity_id>", methods=["GET", "POST"])
def location_form(entity_id=None):
    form, obj = _crud(Location, LocationForm, None, entity_id, label="Location")
    if form is None:
        return redirect(url_for("admin.locations"))
    return render_template("admin/simple_form.html", form=form, title=("Edit" if obj else "New") + " location",
                           back=url_for("admin.locations"))


@bp.route("/pickup-points/new", methods=["GET", "POST"])
@bp.route("/pickup-points/<int:entity_id>", methods=["GET", "POST"])
def pickup_form(entity_id=None):
    def setup(form):
        form.location_id.choices = [(l.id, l.label) for l in db.session.query(Location).order_by(Location.name)]
    form, obj = _crud(PickupPoint, PickupPointForm, None, entity_id, setup, "Pickup point")
    if form is None:
        return redirect(url_for("admin.locations"))
    return render_template("admin/simple_form.html", form=form, title=("Edit" if obj else "New") + " pickup point",
                           back=url_for("admin.locations"))


# ------------------------------------------------------------- group orders

@bp.route("/groups")
def groups():
    rows = db.session.query(BuyingGroup).order_by(BuyingGroup.id.desc()).limit(300).all()
    return render_template("admin/groups.html", groups=rows)


@bp.route("/groups/<int:group_id>/toggle", methods=["POST"])
def toggle_group(group_id):
    g = db.session.get(BuyingGroup, group_id) or abort(404)
    g.is_active = not g.is_active
    audit(current_user, "group.active" if g.is_active else "group.deactivated", g)
    db.session.commit()
    flash("Group updated.", "success")
    return redirect(url_for("admin.groups"))


@bp.route("/group-orders")
def group_orders():
    status = request.args.get("status")
    q = db.session.query(GroupOrder)
    if status in GOStatus.LABELS:
        q = q.filter_by(status=status)
    rows = q.order_by(GroupOrder.id.desc()).limit(300).all()
    return render_template("admin/group_orders.html", orders=rows, status=status, statuses=GOStatus.LABELS)


@bp.route("/group-orders/<int:go_id>")
def group_order(go_id):
    go = db.session.get(GroupOrder, go_id) or abort(404)
    refresh_deadlines(go)
    sealed = go.status == GOStatus.BIDDING
    return render_template(
        "admin/group_order.html", go=go, progress=group_svc.progress(go), sealed=sealed,
        bids=sorted(go.bids, key=lambda b: (-(b.score or 0))), eligible=bid_svc.eligible_bids(go),
        tally=bid_svc.vote_tally(go), inspections=ful_svc.inspections_for(go),
        deliveries=ful_svc.deliveries_for(go), fin=fin_svc.order_financials(go),
        close_ready=ful_svc.close_readiness(go), clearance=ful_svc.pre_dispatch_clearance(go),
        payment_methods=pay_svc.PAYMENT_METHODS, cost_categories=CostCategory.LABELS,
        founder=db.session.query(VendorProfile).filter_by(is_platform_owned=True).first(),
        outstanding={co.id: pay_svc.amount_outstanding(co) for co in go.customer_orders},
        refundable={co.id: pay_svc.amount_paid(co) - pay_svc.amount_refunded(co) for co in go.customer_orders},
    )


@bp.route("/group-orders/<int:go_id>/<action>", methods=["POST"])
def group_order_action(go_id, action):
    go = db.session.get(GroupOrder, go_id) or abort(404)
    f = request.form
    if action == "freeze":
        run_action(group_svc.freeze, go, current_user, f.get("reason") or "Frozen by administrator",
                   success="Orders frozen.")
    elif action == "reopen-bidding":
        run_action(group_svc.open_bidding, go, current_user, parse_local_datetime(f.get("bidding_deadline")),
                   success="Bidding reopened.")
    elif action == "close-bidding":
        run_action(bid_svc.close_bidding, go, current_user, f.get("reason"), success="Bidding closed and offers scored.")
    elif action == "apply-award-rule":
        run_action(bid_svc.award, go, current_user, None, success="Award rule applied.")
    elif action == "revoke-award":
        run_action(bid_svc.revoke_award, go, current_user, f.get("reason"), success="Award revoked.")
    elif action == "hold":
        run_action(ful_svc.place_order_on_hold, go, current_user, f.get("reason"), success="Order placed on hold.")
    elif action == "resume":
        run_action(ful_svc.resume_order, go, current_user, f.get("target") or None, success="Hold released.")
    elif action == "cancel":
        run_action(group_svc.cancel_group_order, go, current_user, f.get("reason"), success="Order cancelled.")
    elif action == "close":
        run_action(ful_svc.close_group_order, go, current_user, success="Order closed and settlement created.")
    elif action == "cost":
        founder = db.session.query(VendorProfile).filter_by(is_platform_owned=True).first()
        run_action(fin_svc.add_cost, current_user, category=f.get("category"), amount=f.get("amount"),
                   description=f.get("description"), group_order=go,
                   vendor=founder if f.get("founder") == "1" else None, success="Cost recorded.")
    else:
        abort(404)
    return redirect(url_for("admin.group_order", go_id=go.id))


@bp.route("/customer-orders/<int:order_id>/<action>", methods=["POST"])
def customer_order_action(order_id, action):
    co = db.session.get(CustomerOrder, order_id) or abort(404)
    f = request.form
    if action == "payment":
        run_action(pay_svc.record_manual_payment, co, current_user, amount=f.get("amount"), method=f.get("method"),
                   reference=f.get("reference"), notes=f.get("notes"), success="Payment recorded.")
    elif action == "payment-failed":
        rec = db.session.get(PaymentRecord, int(f.get("payment_id") or 0))
        if rec is None or rec.customer_order_id != co.id:
            abort(404)
        run_action(pay_svc.mark_payment_failed, rec, current_user, f.get("reason"), success="Payment marked failed.")
    elif action == "request-payment":
        run_action(pay_svc.request_payment, co, success="Payment request created.")
    elif action == "refund":
        run_action(pay_svc.issue_refund, co, current_user, amount=f.get("amount"), reason=f.get("reason"),
                   charged_to=f.get("charged_to", "vendor"), reference=f.get("reference"),
                   success="Refund recorded.")
    else:
        abort(404)
    return redirect(request.referrer or url_for("admin.group_order", go_id=co.group_order_id))


@bp.route("/bidding")
def bidding():
    rows = (db.session.query(GroupOrder)
            .filter(GroupOrder.status.in_((GOStatus.BIDDING, GOStatus.SELECTION, GOStatus.AWARDED)))
            .order_by(GroupOrder.bidding_deadline).all())
    counts = {go.id: len([b for b in go.bids if b.status != BidStatus.WITHDRAWN]) for go in rows}
    decisions = (db.session.query(GroupOrder).filter(GroupOrder.awarded_at.isnot(None))
                 .order_by(GroupOrder.awarded_at.desc()).limit(20).all())
    return render_template("admin/bidding.html", rows=rows, counts=counts, decisions=decisions,
                           multi_vendor=settings_svc.get("multi_vendor_enabled"),
                           weights=settings_svc.score_weights())


# ------------------------------------------------------------------ quality

@bp.route("/quality")
def quality():
    waiting = (db.session.query(GroupOrder)
               .filter(GroupOrder.status.in_((GOStatus.SOURCING, GOStatus.QUALITY_CHECK, GOStatus.ON_HOLD)))
               .order_by(GroupOrder.delivery_window_start).all())
    recent = db.session.query(QualityInspection).order_by(QualityInspection.id.desc()).limit(50).all()
    return render_template("admin/quality.html", waiting=waiting, recent=recent)


@bp.route("/quality/<int:go_id>", methods=["GET", "POST"])
def inspect(go_id):
    go = db.session.get(GroupOrder, go_id) or abort(404)
    if request.method == "POST":
        f = request.form

        def create():
            insp = ful_svc.record_inspection(
                go, current_user, result=f.get("result"), items=inspection_items_from_form(go, f),
                checklist={k: f.get(f"check_{k}") == "1" for k, _ in INSPECTION_CHECKLIST}, stage=f.get("stage"),
                defect_notes=f.get("defect_notes"), spoilage_qty=f.get("spoilage_qty"),
                delivery_condition=f.get("delivery_condition"), place_on_hold=f.get("place_on_hold") == "1",
                batch_id=f.get("batch_id") or None)
            attach_files(request.files.getlist("photos"), "inspection", insp.id, current_user)
            return insp

        if run_action(create, success="Inspection recorded (platform-verified)."):
            return redirect(url_for("admin.inspect", go_id=go.id))
    from ..models import InventoryBatch
    inspections = ful_svc.inspections_for(go)
    return render_template("admin/inspect.html", go=go, checklist=INSPECTION_CHECKLIST, inspections=inspections,
                           photos={i.id: attachments_for("inspection", i.id) for i in inspections},
                           batches=db.session.query(InventoryBatch).filter_by(group_order_id=go.id).all(),
                           packing={r["product"].id: r["qty"] for r in ful_svc.packing_list(go)},
                           clearance=ful_svc.pre_dispatch_clearance(go))


# --------------------------------------------------------------- deliveries

@bp.route("/deliveries")
def deliveries():
    status = request.args.get("status")
    q = db.session.query(Delivery)
    if status in DeliveryStatus.LABELS:
        q = q.filter_by(status=status)
    return render_template("admin/deliveries.html", deliveries=q.order_by(Delivery.id.desc()).limit(300).all(),
                           status=status, statuses=DeliveryStatus.LABELS)


@bp.route("/deliveries/<int:delivery_id>/<action>", methods=["POST"])
def delivery_action(delivery_id, action):
    d = db.session.get(Delivery, delivery_id) or abort(404)
    f = request.form
    if action == "delivered":
        run_action(ful_svc.mark_delivered, current_user, d, received_by=f.get("received_by"),
                   proof_notes=f.get("proof_notes"), condition_notes=f.get("condition_notes"),
                   success="Delivery marked delivered.")
    elif action == "failed":
        run_action(ful_svc.mark_failed, current_user, d, f.get("reason"), success="Failed attempt recorded.")
    elif action == "reschedule":
        run_action(ful_svc.reschedule, current_user, d, f.get("assigned_to"), success="Delivery rescheduled.")
    elif action == "returned":
        run_action(ful_svc.mark_returned, current_user, d, f.get("reason"), success="Marked not delivered.")
    else:
        abort(404)
    return redirect(url_for("admin.deliveries"))


# --------------------------------------------------------------- complaints

@bp.route("/complaints")
def complaints():
    status = request.args.get("status", "unresolved")
    q = db.session.query(Complaint)
    if status == "unresolved":
        q = q.filter(Complaint.status.in_(ComplaintStatus.UNRESOLVED))
    elif status in ComplaintStatus.LABELS:
        q = q.filter_by(status=status)
    refunds = db.session.query(Refund).order_by(Refund.id.desc()).limit(50).all()
    return render_template("admin/complaints.html", complaints=q.order_by(Complaint.id.desc()).all(),
                           status=status, refunds=refunds)


@bp.route("/complaints/<int:complaint_id>", methods=["GET", "POST"])
def complaint_detail(complaint_id):
    c = db.session.get(Complaint, complaint_id) or abort(404)
    if request.method == "POST":
        f = request.form
        run_action(support_svc.resolve_complaint, c, current_user, resolution=f.get("resolution"),
                   notes=f.get("notes"), refund_amount=f.get("refund_amount"),
                   charged_to=f.get("charged_to", "vendor"), reject=f.get("reject") == "1",
                   success="Complaint updated.")
        return redirect(url_for("admin.complaint_detail", complaint_id=c.id))
    co = c.customer_order
    return render_template("admin/complaint_detail.html", c=c, co=co, photos=attachments_for("complaint", c.id),
                           resolutions=COMPLAINT_RESOLUTIONS,
                           refundable=pay_svc.amount_paid(co) - pay_svc.amount_refunded(co))


# ------------------------------------------------------------------ finance

@bp.route("/finance", methods=["GET", "POST"])
def finance():
    if request.method == "POST":
        f = request.form
        founder = db.session.query(VendorProfile).filter_by(is_platform_owned=True).first()
        incurred = None
        if f.get("incurred_on"):
            try:
                incurred = datetime.strptime(f["incurred_on"], "%Y-%m-%d").date()
            except ValueError:
                flash("Enter the date as YYYY-MM-DD.", "danger")
                return redirect(url_for("admin.finance"))
        go = db.session.get(GroupOrder, int(f["group_order_id"])) if f.get("group_order_id") else None
        run_action(fin_svc.add_cost, current_user, category=f.get("category"), amount=f.get("amount"),
                   description=f.get("description"), incurred_on=incurred, group_order=go,
                   vendor=founder if f.get("founder") == "1" else None, success="Cost recorded.")
        return redirect(url_for("admin.finance"))
    return render_template(
        "admin/finance.html", fin=fin_svc.financial_summary(), cost_categories=CostCategory.LABELS,
        settlements=db.session.query(VendorSettlementRecord).order_by(VendorSettlementRecord.id.desc()).limit(50).all(),
        costs=db.session.query(CostRecord).order_by(CostRecord.id.desc()).limit(50).all(),
        commissions=db.session.query(CommissionRecord).order_by(CommissionRecord.id.desc()).limit(50).all(),
        reports=REPORTS)


@bp.route("/settlements/<int:settlement_id>/paid", methods=["POST"])
def settlement_paid(settlement_id):
    s = db.session.get(VendorSettlementRecord, settlement_id) or abort(404)
    run_action(fin_svc.mark_settlement_paid, s, current_user, request.form.get("reference"),
               success="Settlement marked as paid.")
    return redirect(url_for("admin.finance"))


def _report_orders():
    yield ["group_order_id", "group", "status", "currency", "created", "awarded_vendor", "platform_owned_vendor",
           "customer_orders", "order_value", "service_fees", "paid", "refunds", "commission", "vendor_payable",
           "costs_recorded"]
    for go in db.session.query(GroupOrder).order_by(GroupOrder.id):
        fin = fin_svc.order_financials(go)
        bid = go.awarded_bid
        yield [go.id, go.group.name, go.status, go.currency, go.created_at.isoformat(),
               bid.vendor.business_name if bid else "", bid.is_platform_owned if bid else "",
               len(go.live_customer_orders), fin["order_value"], fin["service_fees"], fin["paid"], fin["refunds"],
               fin["commission"].amount if fin["commission"] else "",
               fin["settlement"].net_payable if fin["settlement"] else "", fin["cost_total"]]


def _report_customer_orders():
    yield ["reference", "group_order_id", "customer_email", "status", "fulfilment", "subtotal", "delivery_fee",
           "home_delivery_fee", "service_fee", "total", "payment_status", "confirmed_at"]
    for co in db.session.query(CustomerOrder).order_by(CustomerOrder.id):
        yield [co.reference, co.group_order_id, co.user.email, co.status, co.fulfilment_method, co.subtotal,
               co.delivery_fee, co.home_delivery_fee, co.service_fee, co.total, co.payment_status,
               co.confirmed_at.isoformat() if co.confirmed_at else ""]


def _report_payments():
    yield ["payment_id", "order_reference", "amount", "currency", "provider", "method", "status",
           "verified_server_side", "provider_ref", "confirmed_at"]
    for p in db.session.query(PaymentRecord).order_by(PaymentRecord.id):
        yield [p.id, p.customer_order.reference, p.amount, p.currency, p.provider, p.method or "", p.status,
               p.verified_server_side, p.provider_ref or "", p.confirmed_at.isoformat() if p.confirmed_at else ""]


def _report_refunds():
    yield ["refund_id", "order_reference", "amount", "currency", "charged_to", "reason", "complaint_id", "processed_at"]
    for r in db.session.query(Refund).order_by(Refund.id):
        yield [r.id, r.customer_order.reference, r.amount, r.currency, r.charged_to, r.reason, r.complaint_id or "",
               r.processed_at.isoformat()]


def _report_settlements():
    yield ["settlement_id", "vendor", "platform_owned", "group_order_id", "goods", "delivery_fees",
           "refunds_deducted", "commission", "net_payable", "status", "reference", "paid_at"]
    for s in db.session.query(VendorSettlementRecord).order_by(VendorSettlementRecord.id):
        yield [s.id, s.vendor.business_name, s.vendor.is_platform_owned, s.group_order_id, s.goods_amount,
               s.delivery_fees, s.refunds_deducted, s.commission_amount, s.net_payable, s.status, s.reference or "",
               s.paid_at.isoformat() if s.paid_at else ""]


def _report_costs():
    yield ["cost_id", "category", "amount", "currency", "group_order_id", "founder_vendor", "description", "incurred_on"]
    for c in db.session.query(CostRecord).order_by(CostRecord.id):
        yield [c.id, c.category, c.amount, c.currency, c.group_order_id or "", bool(c.vendor_id), c.description,
               c.incurred_on.isoformat()]


def _report_complaints():
    yield ["complaint_id", "order_reference", "type", "status", "resolution", "vendor", "created_at", "resolved_at"]
    for c in db.session.query(Complaint).order_by(Complaint.id):
        yield [c.id, c.customer_order.reference, c.complaint_type, c.status, c.resolution or "",
               c.vendor.business_name if c.vendor else "", c.created_at.isoformat(),
               c.resolved_at.isoformat() if c.resolved_at else ""]


REPORTS = {
    "group-orders": ("Group orders & financials", _report_orders),
    "customer-orders": ("Customer orders", _report_customer_orders),
    "payments": ("Payments", _report_payments),
    "refunds": ("Refunds", _report_refunds),
    "settlements": ("Vendor settlements", _report_settlements),
    "costs": ("Cost records", _report_costs),
    "complaints": ("Complaints", _report_complaints),
}


def _csv_safe(value):
    """Neutralise spreadsheet formula injection in exported text."""
    s = "" if value is None else str(value)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r") and not s.lstrip("-").replace(".", "", 1).isdigit():
        return "'" + s
    return s


@bp.route("/reports/<name>.csv")
def export_csv(name):
    if name not in REPORTS:
        abort(404)
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in REPORTS[name][1]():
        writer.writerow([_csv_safe(v) for v in row])
    audit(current_user, "report.exported", entity_type="report", details={"report": name})
    db.session.commit()
    stamp = utcnow().strftime("%Y%m%d-%H%M")
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}-{stamp}.csv"})


# ----------------------------------------------------------------- settings

@bp.route("/settings", methods=["GET", "POST"])
def settings():
    defs = settings_svc.DEFINITIONS
    if request.method == "POST":
        def save():
            for key, (kind, *_rest) in defs.items():
                if kind == "bool":
                    settings_svc.set_value(key, request.form.get(key) == "1", current_user)
                elif key in request.form:
                    settings_svc.set_value(key, request.form.get(key), current_user)
            weights = {k: settings_svc.get(k) for k in settings_svc.SCORE_WEIGHT_KEYS}
            if sum(v for k, v in weights.items() if k != "score_weight_price") <= 0:
                raise DomainError("Price cannot be the only award criterion; give another criterion a weight.")
        run_action(save, success="Settings saved.")
        return redirect(url_for("admin.settings"))
    return render_template("admin/settings.html", defs=defs, values={k: settings_svc.get(k) for k in defs})


# -------------------------------------------------------------- audit & misc

@bp.route("/audit")
def audit_log():
    page = max(int(request.args.get("page", 1) or 1), 1)
    action = (request.args.get("action") or "").strip()[:60]
    q = db.session.query(AuditLog)
    if action:
        q = q.filter(AuditLog.action.ilike(f"{action}%"))
    rows = q.order_by(AuditLog.id.desc()).offset((page - 1) * 50).limit(51).all()
    return render_template("admin/audit.html", rows=rows[:50], has_next=len(rows) > 50, page=page, action=action)


@bp.route("/messages", methods=["GET", "POST"])
def messages():
    if request.method == "POST":
        m = db.session.get(ContactMessage, int(request.form.get("id") or 0)) or abort(404)
        m.is_handled = True
        db.session.commit()
        flash("Marked as handled.", "success")
        return redirect(url_for("admin.messages"))
    rows = db.session.query(ContactMessage).order_by(ContactMessage.is_handled, ContactMessage.id.desc()).limit(200)
    return render_template("admin/messages.html", messages=rows.all())
