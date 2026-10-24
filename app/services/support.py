"""Accounts, vendor verification, complaints, reviews and deadline processing."""
from datetime import timedelta

from flask import current_app

from ..constants import (COMPLAINT_RESOLUTIONS, COMPLAINT_TYPES, VENDOR_TRANSITIONS, COStatus, ComplaintStatus,
                         GOStatus, Roles, SelectionMode, VendorStatus)
from ..extensions import db
from ..models import (Complaint, CustomerProfile, GroupOrder, Review, Role, User, VendorDocument, VendorProfile)
from ..utils import utcnow
from .core import DomainError, PermissionDenied, audit, notify
from .pricing import parse_decimal

PASSWORD_MIN = 10


# ----------------------------------------------------------------- accounts

def validate_password(password):
    if not password or len(password) < PASSWORD_MIN:
        raise DomainError(f"Password must be at least {PASSWORD_MIN} characters.")
    if password.isdigit() or password.isalpha():
        raise DomainError("Password must mix letters with numbers or symbols.")
    if len(password) > 128:
        raise DomainError("Password is too long.")


def get_role(name):
    role = db.session.query(Role).filter_by(name=name).first()
    if role is None:
        role = Role(name=name, description=name.capitalize())
        db.session.add(role)
        db.session.flush()
    return role


def create_user(*, email, password, full_name, phone=None, roles=(Roles.CUSTOMER,), granted_by=None):
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise DomainError("Enter a valid email address.")
    if db.session.query(User.id).filter(User.email == email).first():
        raise DomainError("An account with this email already exists.")
    validate_password(password)
    name = (full_name or "").strip()
    if not 2 <= len(name) <= 120:
        raise DomainError("Enter your full name.")
    user = User(email=email, full_name=name, phone=(phone or "").strip()[:32] or None)
    user.set_password(password)
    for r in roles:
        user.add_role(get_role(r), granted_by)
    if Roles.CUSTOMER in roles:
        user.customer_profile = CustomerProfile()
    db.session.add(user)
    db.session.flush()
    return user


def register_failed_login(user):
    cfg = current_app.config
    user.failed_logins = (user.failed_logins or 0) + 1
    if user.failed_logins >= cfg["LOGIN_MAX_FAILURES"]:
        user.locked_until = utcnow() + timedelta(minutes=cfg["LOGIN_LOCK_MINUTES"])
        user.failed_logins = 0
        audit(user, "auth.locked", user, {"minutes": cfg["LOGIN_LOCK_MINUTES"]})


def is_locked(user):
    return user.locked_until is not None and user.locked_until > utcnow()


def create_vendor_profile(user, *, business_name, contact_phone, business_address, legal_name=None,
                          registration_number=None, tax_id=None, food_licence_number=None, description=None,
                          capacity_per_order=None, platform_owned=False, approved_by=None):
    if user.vendor_profile is not None:
        raise DomainError("This account already has a vendor profile.")
    vp = VendorProfile(user_id=user.id, business_name=business_name.strip()[:160],
                       contact_phone=contact_phone.strip()[:32], business_address=business_address.strip()[:300],
                       legal_name=(legal_name or "").strip() or None,
                       registration_number=(registration_number or "").strip() or None,
                       tax_id=(tax_id or "").strip() or None,
                       food_licence_number=(food_licence_number or "").strip() or None,
                       description=(description or "").strip() or None,
                       capacity_per_order=parse_decimal(capacity_per_order, "Capacity", 0, 10000000, 3, required=False),
                       is_platform_owned=platform_owned, status=VendorStatus.PENDING)
    user.add_role(get_role(Roles.VENDOR), approved_by)
    db.session.add(vp)
    db.session.flush()
    if platform_owned:
        set_vendor_status(vp, approved_by, VendorStatus.APPROVED, "Founder-operated vendor account")
    return vp


def set_vendor_status(vendor, admin, new_status, reason=None):
    if admin is not None and not admin.has_role(Roles.ADMIN):
        raise PermissionDenied("Only administrators manage vendor status.")
    if new_status not in VENDOR_TRANSITIONS.get(vendor.status, set()):
        raise DomainError(f"Cannot change vendor status from {vendor.status} to {new_status}.")
    if new_status in (VendorStatus.REJECTED, VendorStatus.SUSPENDED) and not (reason or "").strip():
        raise DomainError("A reason is required.")
    old = vendor.status
    vendor.status = new_status
    vendor.status_reason = (reason or "").strip()[:500] or None
    if new_status == VendorStatus.APPROVED:
        vendor.approved_at = utcnow()
        vendor.approved_by_id = getattr(admin, "id", None)
    audit(admin, "vendor.status", vendor, {"from": old, "to": new_status, "reason": reason})
    notify(vendor.user_id, f"Your vendor account status is now: {new_status}."
           + (f" Reason: {vendor.status_reason}" if vendor.status_reason else ""), "/vendor/verification")
    return vendor


def review_document(doc, admin, status, notes=None):
    if status not in ("verified", "rejected"):
        raise DomainError("Unknown document status.")
    if status == "rejected" and not (notes or "").strip():
        raise DomainError("Explain why the document was rejected.")
    doc.status = status
    doc.review_notes = (notes or "").strip()[:500] or None
    doc.reviewed_by_id = admin.id
    doc.reviewed_at = utcnow()
    audit(admin, "vendor.document_reviewed", doc, {"vendor_id": doc.vendor_id, "status": status})
    return doc


def verified_documents(vendor):
    return [d for d in vendor.documents if d.status == "verified"]


def has_verified_organic(vendor):
    return any(d.doc_type == "organic_certificate" for d in verified_documents(vendor))


# --------------------------------------------------------------- complaints

COMPLAINABLE = (COStatus.CONFIRMED, COStatus.DELIVERED)


def create_complaint(user, co, *, complaint_type, description, product_id=None, quantity_affected=None):
    if co.user_id != user.id:
        raise PermissionDenied("This is not your order.")
    go = co.group_order
    if co.status not in COMPLAINABLE or go.status not in (GOStatus.DISPATCHED, GOStatus.DELIVERED,
                                                           GOStatus.ON_HOLD):
        raise DomainError("Complaints can be submitted after dispatch and before the order is closed.")
    if complaint_type not in COMPLAINT_TYPES:
        raise DomainError("Choose a complaint type.")
    description = (description or "").strip()
    if len(description) < 10:
        raise DomainError("Please describe the problem (at least 10 characters).")
    pid = int(product_id) if product_id else None
    if pid and pid not in {it.product_id for it in co.items}:
        raise DomainError("That product is not in your order.")
    q = parse_decimal(quantity_affected, "Quantity affected", 0, 100000, 3, required=False)
    bid = go.awarded_bid
    c = Complaint(customer_order_id=co.id, group_order_id=go.id, user_id=user.id,
                  vendor_id=bid.vendor_id if bid else None, product_id=pid, complaint_type=complaint_type,
                  quantity_affected=q, description=description[:5000])
    db.session.add(c)
    db.session.flush()
    if bid:
        notify(bid.vendor.user_id, f"New complaint on order #{go.id}.", "/vendor/complaints")
    from .groups import active_admin_ids
    for uid in active_admin_ids():
        notify(uid, f"New {complaint_type} complaint on order #{go.id}.", f"/admin/complaints/{c.id}")
    return c


def vendor_respond(vendor, complaint, response):
    if complaint.vendor_id != vendor.id:
        raise PermissionDenied("This complaint is not about your order.")
    if complaint.status not in ComplaintStatus.UNRESOLVED:
        raise DomainError("This complaint is already closed.")
    response = (response or "").strip()
    if len(response) < 5:
        raise DomainError("Enter a response.")
    complaint.vendor_response = response[:5000]
    complaint.vendor_responded_at = utcnow()
    complaint.status = ComplaintStatus.VENDOR_RESPONDED
    notify(complaint.user_id, "The vendor responded to your complaint.", "/complaints")
    return complaint


def resolve_complaint(complaint, admin, *, resolution, notes, refund_amount=None, charged_to="vendor",
                      reject=False):
    if complaint.status not in ComplaintStatus.UNRESOLVED:
        raise DomainError("This complaint is already closed.")
    if not (notes or "").strip():
        raise DomainError("Resolution notes are required.")
    if reject:
        complaint.status = ComplaintStatus.REJECTED
        complaint.resolution = "no_action"
    else:
        if resolution not in COMPLAINT_RESOLUTIONS:
            raise DomainError("Choose a resolution.")
        complaint.status = ComplaintStatus.RESOLVED
        complaint.resolution = resolution
        if resolution == "refund":
            from .payments import issue_refund
            issue_refund(complaint.customer_order, admin, amount=refund_amount,
                         reason=f"Complaint #{complaint.id}: {complaint.complaint_type}", charged_to=charged_to,
                         complaint=complaint)
    complaint.resolution_notes = notes.strip()[:5000]
    complaint.resolved_by_id = admin.id
    complaint.resolved_at = utcnow()
    audit(admin, "complaint.resolved", complaint, {"status": complaint.status, "resolution": complaint.resolution})
    notify(complaint.user_id, f"Your complaint #{complaint.id} was {complaint.status}.", "/complaints")
    return complaint


def create_review(user, co, rating, comment=None):
    if co.user_id != user.id:
        raise PermissionDenied("This is not your order.")
    if co.status not in (COStatus.DELIVERED, COStatus.COMPLETED):
        raise DomainError("You can rate an order after it is delivered.")
    if db.session.query(Review.id).filter_by(customer_order_id=co.id).first():
        raise DomainError("You already rated this order.")
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        raise DomainError("Choose a rating from 1 to 5.")
    if not 1 <= rating <= 5:
        raise DomainError("Choose a rating from 1 to 5.")
    bid = co.group_order.awarded_bid
    r = Review(customer_order_id=co.id, user_id=user.id, vendor_id=bid.vendor_id, rating=rating,
               comment=(comment or "").strip()[:1000] or None)
    db.session.add(r)
    return r


# --------------------------------------------------------- deadline worker

def process_group_order(go, now=None):
    """Advance one group order past any deadline that has expired. Returns True if changed."""
    from .bidding import award, close_bidding
    from .groups import freeze
    now = now or utcnow()
    changed = False
    if go.status == GOStatus.COLLECTING and go.order_deadline <= now:
        freeze(go, None)
        changed = True
    if go.status == GOStatus.BIDDING and go.bidding_deadline and go.bidding_deadline <= now:
        close_bidding(go, None)
        changed = True
        if go.selection_mode == SelectionMode.AUTO_SCORE:
            try:
                award(go, None)
            except DomainError:
                pass
    return changed


def process_deadlines(now=None):
    now = now or utcnow()
    due = (db.session.query(GroupOrder)
           .filter(((GroupOrder.status == GOStatus.COLLECTING) & (GroupOrder.order_deadline <= now))
                   | ((GroupOrder.status == GOStatus.BIDDING) & (GroupOrder.bidding_deadline <= now)))
           .all())
    count = 0
    for go in due:
        if process_group_order(go, now):
            count += 1
    return count


def documents_by_vendor(vendor_id):
    return db.session.query(VendorDocument).filter_by(vendor_id=vendor_id).order_by(VendorDocument.id.desc()).all()
