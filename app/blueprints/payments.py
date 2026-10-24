"""Payment gateway integration points.

* ``/payments/webhook/<provider>``: server-to-server notifications. Only a
  verified signature can change a payment's status.
* ``/payments/return``: where a gateway sends the customer's browser. It never
  changes payment status — the order page shows the verified state.
"""
from flask import Blueprint, abort, jsonify, render_template, request

from ..extensions import csrf, db
from ..services.core import DomainError
from ..services.payments import apply_gateway_event, get_provider

bp = Blueprint("payments", __name__)


@bp.route("/webhook/<provider>", methods=["POST"])
@csrf.exempt
def webhook(provider):
    gateway = get_provider(provider)
    if gateway is None or provider == "manual":
        abort(404)
    payload = request.get_data(cache=False, as_text=False)
    try:
        event = gateway.verify_webhook(payload, request.headers)
        apply_gateway_event(event, gateway.name)
        db.session.commit()
    except DomainError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


@bp.route("/return")
def browser_return():
    return render_template("public/payment_return.html")
