"""Vendor pages: verification, catalogue, opportunities, bids, fulfilment, finance."""
from datetime import timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..constants import (INSPECTION_CHECKLIST, VENDOR_DOCUMENT_TYPES, BidStatus, ComplaintStatus,
                         DeliveryStatus, GOStatus, SubstitutionPolicy)
from ..extensions import db
from ..forms import VendorProfileForm
from ..models import (Complaint, Delivery, GroupOrder, InventoryBatch, PriceTier, Product, ProductGrade,
                      QualityInspection, VendorBid, VendorDocument, VendorProduct)
from ..security import approved_vendor_required, rate_limit, run_action, vendor_required
from ..services import bidding as bid_svc
from ..services import finance as fin_svc
from ..services import fulfilment as ful_svc
from ..services import groups as group_svc
from ..services import settings as settings_svc
from ..services.core import DomainError, audit
from ..services.pricing import parse_decimal, tier_price
from ..services.uploads import DOCUMENT_TYPES, attach_files, attachments_for, save_upload
from ..utils import parse_local_datetime, utcnow
from .public import refresh_deadlines

bp = Blueprint("vendor", __name__)


def _vendor():
    return current_user.vendor_profile


def _parse_date(value):
    from datetime import datetime
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise DomainError("Enter dates as YYYY-MM-DD.")


@bp.route("/dashboard")
@vendor_required
def dashboard():
    v = _vendor()
    opportunities = bid_svc.open_opportunities(v) if v.is_approved else []
    bids = db.session.query(VendorBid).filter_by(vendor_id=v.id).all()
    active_bids = [b for b in bids if b.status == BidStatus.SUBMITTED and b.group_order.status == GOStatus.BIDDING]
    awarded = [b.group_order for b in bids if b.status == BidStatus.AWARDED
               and b.group_order.status in GOStatus.IN_FULFILMENT]
    history = bid_svc.vendor_history(v)
    return render_template("vendor/dashboard.html", vendor=v, opportunities=opportunities, active_bids=active_bids,
                           awarded=sorted(awarded, key=lambda g: g.delivery_window_end),
                           bid_ids={b.group_order_id for b in bids},
                           history=history, quality_score=bid_svc.quality_score(v, history),
                           finance=fin_svc.vendor_finance(v),
                           multi_vendor=settings_svc.get("multi_vendor_enabled"))


# ------------------------------------------------------------- verification

@bp.route("/verification", methods=["GET", "POST"])
@vendor_required
def verification():
    v = _vendor()
    form = VendorProfileForm(obj=v)
    if form.validate_on_submit():
        for field in ("business_name", "legal_name", "registration_number", "tax_id", "food_licence_number",
                      "contact_phone", "business_address", "description"):
            value = getattr(form, field).data
            setattr(v, field, value.strip() if isinstance(value, str) and value.strip() else
                    (None if field not in ("business_name", "contact_phone", "business_address") else value))
        v.capacity_per_order = form.capacity_per_order.data
        audit(current_user, "vendor.profile_updated", v, {})
        db.session.commit()
        flash("Business profile saved.", "success")
        return redirect(url_for("vendor.verification"))
    return render_template("vendor/verification.html", vendor=v, form=form, doc_types=VENDOR_DOCUMENT_TYPES)


@bp.route("/documents", methods=["POST"])
@vendor_required
@rate_limit(20, 600)
def upload_document():
    v = _vendor()
    doc_type = request.form.get("doc_type")

    def save():
        if doc_type not in VENDOR_DOCUMENT_TYPES:
            raise DomainError("Choose a document type.")
        meta = save_upload(request.files.get("document"), DOCUMENT_TYPES)
        doc = VendorDocument(vendor_id=v.id, doc_type=doc_type, **meta)
        db.session.add(doc)
        db.session.flush()
        audit(current_user, "vendor.document_uploaded", doc, {"doc_type": doc_type})
        return doc

    run_action(save, success="Document uploaded. An administrator will review it.")
    return redirect(url_for("vendor.verification"))


# ---------------------------------------------------------------- catalogue

@bp.route("/catalogue", methods=["GET", "POST"])
@vendor_required
def catalogue():
    v = _vendor()
    if request.method == "POST":
        f = request.form

        def add():
            product = db.session.get(Product, int(f.get("product_id") or 0))
            grade = db.session.get(ProductGrade, int(f.get("grade_id") or 0))
            if product is None or grade is None:
                raise DomainError("Choose a product and grade.")
            if db.session.query(VendorProduct).filter_by(vendor_id=v.id, product_id=product.id,
                                                         grade_id=grade.id).first():
                raise DomainError("You already list this product in that grade. Edit the existing listing.")
            vp = VendorProduct(vendor_id=v.id, product_id=product.id, grade_id=grade.id,
                               price_per_unit=parse_decimal(f.get("price_per_unit"), "Price", 0.01, 100000),
                               currency=settings_svc.currency(),
                               stock_qty=parse_decimal(f.get("stock_qty") or "0", "Stock", 0, 10000000, 3),
                               origin=(f.get("origin") or "").strip()[:120] or None,
                               declared_organic=f.get("declared_organic") == "1",
                               notes=(f.get("notes") or "").strip()[:300] or None)
            db.session.add(vp)
            return vp

        run_action(add, success="Listing added.")
        return redirect(url_for("vendor.catalogue"))
    listings = (db.session.query(VendorProduct).filter_by(vendor_id=v.id).join(Product)
                .order_by(Product.name).all())
    return render_template("vendor/catalogue.html", vendor=v, listings=listings,
                           products=group_svc.products_for_selection(), grades=group_svc.grades())


def _own_listing(listing_id):
    vp = db.session.get(VendorProduct, listing_id) or abort(404)
    if vp.vendor_id != _vendor().id:
        abort(404)
    return vp


@bp.route("/catalogue/<int:listing_id>", methods=["POST"])
@vendor_required
def update_listing(listing_id):
    vp = _own_listing(listing_id)
    f = request.form

    def update():
        vp.price_per_unit = parse_decimal(f.get("price_per_unit"), "Price", 0.01, 100000)
        vp.stock_qty = parse_decimal(f.get("stock_qty") or "0", "Stock", 0, 10000000, 3)
        vp.is_available = f.get("is_available") == "1"
        vp.origin = (f.get("origin") or "").strip()[:120] or None
        vp.declared_organic = f.get("declared_organic") == "1"

    run_action(update, success="Listing updated.")
    return redirect(url_for("vendor.catalogue"))


@bp.route("/catalogue/<int:listing_id>/tiers", methods=["POST"])
@vendor_required
def add_tier(listing_id):
    vp = _own_listing(listing_id)

    def add():
        min_qty = parse_decimal(request.form.get("min_qty"), "Minimum quantity", 0.001, 1000000, 3)
        price = parse_decimal(request.form.get("price_per_unit"), "Tier price", 0.01, 100000)
        if price >= vp.price_per_unit:
            raise DomainError("A quantity tier price should be lower than the base price.")
        if any(t.min_qty == min_qty for t in vp.tiers):
            raise DomainError("A tier with that minimum already exists.")
        vp.tiers.append(PriceTier(min_qty=min_qty, price_per_unit=price))

    run_action(add, success="Price tier added.")
    return redirect(url_for("vendor.catalogue"))


@bp.route("/tiers/<int:tier_id>/delete", methods=["POST"])
@vendor_required
def delete_tier(tier_id):
    tier = db.session.get(PriceTier, tier_id) or abort(404)
    if tier.vendor_product.vendor_id != _vendor().id:
        abort(404)
    db.session.delete(tier)
    db.session.commit()
    flash("Tier removed.", "success")
    return redirect(url_for("vendor.catalogue"))


# ------------------------------------------------------------ opportunities

@bp.route("/opportunities")
@approved_vendor_required
def opportunities():
    v = _vendor()
    for go in db.session.query(GroupOrder).filter_by(status=GOStatus.BIDDING).all():
        refresh_deadlines(go)
    rows = [(go, bid_svc.bid_for(v, go)) for go in bid_svc.open_opportunities(v)]
    return render_template("vendor/opportunities.html", rows=rows, vendor=v,
                           multi_vendor=settings_svc.get("multi_vendor_enabled"))


@bp.route("/opportunities/<int:go_id>/bid", methods=["GET", "POST"])
@approved_vendor_required
@rate_limit(30, 60)
def bid(go_id):
    v = _vendor()
    go = db.session.get(GroupOrder, go_id) or abort(404)
    refresh_deadlines(go)
    ok, reason = bid_svc.vendor_eligibility(v, go)
    existing = bid_svc.bid_for(v, go)
    if not ok and request.method == "GET":
        if existing:
            flash(reason, "info")
            return redirect(url_for("vendor.bids"))
        flash(reason, "warning")
        return redirect(url_for("vendor.opportunities"))
    if request.method == "POST":
        f = request.form
        items = {i.id: {"offered_qty": f.get(f"qty_{i.id}"), "unit_price": f.get(f"price_{i.id}"),
                        "origin": f.get(f"origin_{i.id}")} for i in go.items if not i.dropped}
        result = run_action(
            bid_svc.submit_bid, v, go, items=items, grade_id=f.get("grade_id"), origin=f.get("origin"),
            substitution_policy=f.get("substitution_policy"), delivery_fee=f.get("delivery_fee"),
            home_delivery_fee=f.get("home_delivery_fee"),
            estimated_delivery_at=parse_local_datetime(f.get("estimated_delivery_at")),
            expires_at=parse_local_datetime(f.get("expires_at")), notes=f.get("notes"),
            ip=request.remote_addr, user_agent=request.headers.get("User-Agent"), actor=current_user,
            success="Your sealed offer was submitted. You can revise or withdraw it until the deadline.")
        if result:
            return redirect(url_for("vendor.bids"))
    # Prefill from the vendor's catalogue tiers at the aggregate quantity.
    listings = {}
    for vp in db.session.query(VendorProduct).filter_by(vendor_id=v.id, is_available=True).all():
        listings.setdefault(vp.product_id, vp)
    suggestions = {}
    for i in go.items:
        vp = listings.get(i.product_id)
        if vp:
            suggestions[i.id] = tier_price(vp, i.frozen_qty or 0)
    home_orders = sum(1 for co in go.live_customer_orders if co.fulfilment_method == "home")
    min_expiry = go.bidding_deadline + timedelta(hours=settings_svc.get("min_bid_validity_hours"))
    return render_template("vendor/bid_form.html", go=go, vendor=v, bid=existing, suggestions=suggestions,
                           grades=group_svc.grades(), home_orders=home_orders, min_expiry=min_expiry,
                           policies=SubstitutionPolicy.LABELS,
                           existing_items={bi.group_order_item_id: bi for bi in existing.items} if existing else {},
                           disclosure=settings_svc.get("platform_owner_disclosure"))


@bp.route("/bids")
@vendor_required
def bids():
    v = _vendor()
    rows = db.session.query(VendorBid).filter_by(vendor_id=v.id).order_by(VendorBid.id.desc()).all()
    return render_template("vendor/bids.html", bids=rows, now=utcnow())


@bp.route("/bids/<int:bid_id>/withdraw", methods=["POST"])
@vendor_required
def withdraw(bid_id):
    b = db.session.get(VendorBid, bid_id) or abort(404)
    if b.vendor_id != _vendor().id:
        abort(404)
    run_action(bid_svc.withdraw_bid, _vendor(), b, current_user, success="Bid withdrawn.")
    return redirect(url_for("vendor.bids"))


# ---------------------------------------------------------------- awarded

def _awarded_go(go_id):
    go = db.session.get(GroupOrder, go_id) or abort(404)
    bid = go.awarded_bid
    if bid is None or bid.vendor_id != _vendor().id:
        abort(404)
    return go


@bp.route("/awarded")
@vendor_required
def awarded():
    v = _vendor()
    rows = (db.session.query(VendorBid).filter_by(vendor_id=v.id, status=BidStatus.AWARDED)
            .order_by(VendorBid.id.desc()).all())
    return render_template("vendor/awarded.html", rows=rows)


@bp.route("/awarded/<int:go_id>")
@vendor_required
def awarded_detail(go_id):
    go = _awarded_go(go_id)
    batches = (db.session.query(InventoryBatch).filter_by(group_order_id=go.id)
               .order_by(InventoryBatch.id).all())
    inspections = ful_svc.inspections_for(go)
    photos = {i.id: attachments_for("inspection", i.id) for i in inspections}
    batch_photos = {b.id: attachments_for("batch", b.id) for b in batches}
    clearance = ful_svc.pre_dispatch_clearance(go)
    confirmed = [co for co in go.customer_orders if co.status in ("confirmed", "delivered", "completed")]
    pending = [co for co in go.customer_orders if co.status == "awaiting_confirmation"]
    return render_template("vendor/awarded_detail.html", go=go, bid=go.awarded_bid, batches=batches,
                           inspections=inspections, photos=photos, batch_photos=batch_photos,
                           clearance=clearance, packing=ful_svc.packing_list(go), confirmed=confirmed,
                           pending=pending, deliveries=ful_svc.deliveries_for(go), checklist=INSPECTION_CHECKLIST,
                           grades=group_svc.grades(), award=go.active_award)


@bp.route("/awarded/<int:go_id>/<action>", methods=["POST"])
@vendor_required
def awarded_action(go_id, action):
    go = _awarded_go(go_id)
    v = _vendor()
    f = request.form
    actions = {
        "accept": (bid_svc.vendor_accept_award, (v, go), "Award accepted. Start sourcing."),
        "decline": (bid_svc.vendor_decline_award, (v, go, f.get("reason")), "You declined the award."),
        "start-qc": (ful_svc.start_quality_check, (v, go), "Marked ready for quality check."),
        "packed": (ful_svc.mark_packed, (v, go), "Order packed. Deliveries scheduled."),
        "dispatch": (ful_svc.dispatch, (v, go, f.get("assigned_to")), "Order dispatched."),
    }
    if action not in actions:
        abort(404)
    fn, args, msg = actions[action]
    run_action(fn, *args, success=msg)
    if action == "decline":
        return redirect(url_for("vendor.awarded"))
    return redirect(url_for("vendor.awarded_detail", go_id=go.id))


@bp.route("/awarded/<int:go_id>/batches", methods=["POST"])
@vendor_required
def add_batch(go_id):
    go = _awarded_go(go_id)
    f = request.form

    def create():
        b = ful_svc.add_batch(_vendor(), go, product_id=f.get("product_id"), lot_code=f.get("lot_code"),
                              quantity=f.get("quantity"), grade_id=f.get("grade_id") or None,
                              source_name=f.get("source_name"), origin=f.get("origin"),
                              harvest_date=_parse_date(f.get("harvest_date")),
                              sourced_on=_parse_date(f.get("sourced_on")), notes=f.get("notes"))
        attach_files(request.files.getlist("photos"), "batch", b.id, current_user)
        return b

    run_action(create, success="Batch recorded.")
    return redirect(url_for("vendor.awarded_detail", go_id=go.id) + "#quality")


def inspection_items_from_form(go, form):
    rows = []
    for i in go.items:
        if i.dropped:
            continue
        rows.append({"product_id": i.product_id, "expected_qty": form.get(f"expected_{i.id}"),
                     "received_qty": form.get(f"received_{i.id}"), "accepted_qty": form.get(f"accepted_{i.id}"),
                     "rejected_qty": form.get(f"rejected_{i.id}"), "notes": form.get(f"notes_{i.id}")})
    return rows


@bp.route("/awarded/<int:go_id>/inspection", methods=["POST"])
@vendor_required
def self_inspection(go_id):
    go = _awarded_go(go_id)
    f = request.form

    def create():
        insp = ful_svc.record_inspection(go, current_user, result=f.get("result"),
                                         items=inspection_items_from_form(go, f),
                                         checklist={k: f.get(f"check_{k}") == "1" for k, _ in INSPECTION_CHECKLIST},
                                         defect_notes=f.get("defect_notes"), spoilage_qty=f.get("spoilage_qty"),
                                         batch_id=f.get("batch_id") or None)
        attach_files(request.files.getlist("photos"), "inspection", insp.id, current_user)
        return insp

    run_action(create, success="Self-check recorded (vendor-declared).")
    return redirect(url_for("vendor.awarded_detail", go_id=go.id) + "#quality")


@bp.route("/quality")
@vendor_required
def quality():
    v = _vendor()
    batches = (db.session.query(InventoryBatch).filter_by(vendor_id=v.id)
               .order_by(InventoryBatch.id.desc()).limit(100).all())
    go_ids = [b.group_order_id for b in db.session.query(VendorBid).filter_by(vendor_id=v.id,
                                                                               status=BidStatus.AWARDED)]
    inspections = (db.session.query(QualityInspection).filter(QualityInspection.group_order_id.in_(go_ids or [0]))
                   .order_by(QualityInspection.id.desc()).limit(100).all())
    history = bid_svc.vendor_history(v)
    return render_template("vendor/quality.html", batches=batches, inspections=inspections, history=history,
                           score=bid_svc.quality_score(v, history))


# -------------------------------------------------------------- deliveries

@bp.route("/deliveries")
@vendor_required
def deliveries():
    v = _vendor()
    status = request.args.get("status")
    q = db.session.query(Delivery).filter_by(vendor_id=v.id)
    if status in DeliveryStatus.LABELS:
        q = q.filter_by(status=status)
    return render_template("vendor/deliveries.html", deliveries=q.order_by(Delivery.id.desc()).limit(200).all(),
                           status=status, statuses=DeliveryStatus.LABELS)


def _own_delivery(delivery_id):
    d = db.session.get(Delivery, delivery_id) or abort(404)
    if d.vendor_id != _vendor().id:
        abort(404)
    return d


@bp.route("/deliveries/<int:delivery_id>/<action>", methods=["POST"])
@vendor_required
def delivery_action(delivery_id, action):
    d = _own_delivery(delivery_id)
    f = request.form
    if action == "delivered":
        def fn():
            ful_svc.mark_delivered(current_user, d, received_by=f.get("received_by"), proof_notes=f.get("proof_notes"),
                                   condition_notes=f.get("condition_notes"))
            attach_files(request.files.getlist("photos"), "delivery", d.id, current_user, caption="Proof of delivery")
        run_action(fn, success="Delivery marked as delivered.")
    elif action == "failed":
        run_action(ful_svc.mark_failed, current_user, d, f.get("reason"), success="Failed attempt recorded.")
    elif action == "reschedule":
        run_action(ful_svc.reschedule, current_user, d, f.get("assigned_to"), success="Delivery rescheduled.")
    elif action == "returned":
        run_action(ful_svc.mark_returned, current_user, d, f.get("reason"),
                   success="Marked as not delivered. The customer will be refunded by the platform.")
    else:
        abort(404)
    return redirect(request.referrer if request.referrer and "/vendor/" in request.referrer
                    else url_for("vendor.deliveries"))


# ---------------------------------------------------------------- finance

@bp.route("/finance")
@vendor_required
def finance():
    return render_template("vendor/finance.html", vendor=_vendor(), finance=fin_svc.vendor_finance(_vendor()),
                           default_rate=bid_svc.commission_rate_for(_vendor()))


@bp.route("/complaints")
@vendor_required
def complaints():
    v = _vendor()
    rows = db.session.query(Complaint).filter_by(vendor_id=v.id).order_by(Complaint.id.desc()).all()
    photos = {c.id: attachments_for("complaint", c.id) for c in rows}
    return render_template("vendor/complaints.html", complaints=rows, photos=photos,
                           unresolved=ComplaintStatus.UNRESOLVED)


@bp.route("/complaints/<int:complaint_id>/respond", methods=["POST"])
@vendor_required
def respond(complaint_id):
    from ..services.support import vendor_respond
    c = db.session.get(Complaint, complaint_id) or abort(404)
    if c.vendor_id != _vendor().id:
        abort(404)
    run_action(vendor_respond, _vendor(), c, request.form.get("response"), success="Response sent.")
    return redirect(url_for("vendor.complaints"))
