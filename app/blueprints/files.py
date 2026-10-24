"""Authorization-checked access to private uploads (never served from /static)."""
from flask import Blueprint, abort, send_file
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Attachment, Complaint, Delivery, InventoryBatch, QualityInspection, VendorDocument
from ..services.uploads import file_path

bp = Blueprint("files", __name__)


def _go_party(go):
    """Admins, the awarded vendor and members with an order in the group order."""
    if go is None:
        return False
    if current_user.is_admin:
        return True
    bid = go.awarded_bid
    vp = current_user.vendor_profile
    if bid and vp and bid.vendor_id == vp.id:
        return True
    return any(co.user_id == current_user.id and co.status != "cancelled" for co in go.customer_orders)


def can_view(att):
    if current_user.is_admin:
        return True
    vp = current_user.vendor_profile
    if att.entity_type == "complaint":
        c = db.session.get(Complaint, att.entity_id)
        return c is not None and (c.user_id == current_user.id or (vp is not None and c.vendor_id == vp.id))
    if att.entity_type == "delivery":
        d = db.session.get(Delivery, att.entity_id)
        return d is not None and (d.customer_order.user_id == current_user.id or (vp is not None and d.vendor_id == vp.id))
    if att.entity_type == "inspection":
        i = db.session.get(QualityInspection, att.entity_id)
        return i is not None and _go_party(i.group_order)
    if att.entity_type == "batch":
        b = db.session.get(InventoryBatch, att.entity_id)
        if b is None:
            return False
        if vp is not None and b.vendor_id == vp.id:
            return True
        return b.group_order is not None and _go_party(b.group_order)
    return False


def _send(stored, content_type, original):
    try:
        path = file_path(stored)
        inline = content_type.startswith("image/")
        return send_file(path, mimetype=content_type, as_attachment=not inline, download_name=original,
                         max_age=0)
    except (FileNotFoundError, OSError):
        abort(404)


@bp.route("/attachment/<int:attachment_id>")
@login_required
def attachment(attachment_id):
    att = db.session.get(Attachment, attachment_id) or abort(404)
    if not can_view(att):
        abort(404)
    return _send(att.stored_filename, att.content_type, att.original_filename)


@bp.route("/vendor-document/<int:doc_id>")
@login_required
def vendor_document(doc_id):
    doc = db.session.get(VendorDocument, doc_id) or abort(404)
    vp = current_user.vendor_profile
    if not (current_user.is_admin or (vp is not None and vp.id == doc.vendor_id)):
        abort(404)
    return _send(doc.stored_filename, doc.content_type, doc.original_filename)
