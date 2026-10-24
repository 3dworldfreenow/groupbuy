"""Vendor bidding: eligibility, sealed bids, transparent scoring and awards."""
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from ..constants import (BidStatus, BiddingMode, COStatus, GOStatus, PaymentStatus, Roles, SelectionMode,
                         SubstitutionPolicy, VendorStatus, Verification)
from ..extensions import db
from ..models import (AwardDecision, Complaint, CustomerOrder, Delivery, GroupOrder, OfferVote,
                      ProductGrade, QualityInspection, Review, VendorBid, VendorBidItem)
from ..utils import ZERO, json_dumps, money, qty, utcnow
from . import settings
from .core import ConcurrencyError, DomainError, PermissionDenied, audit, notify, transition_customer_order, \
    transition_group_order
from .groups import is_organiser
from .pricing import apply_pricing, bid_customer_cost, clear_pricing, parse_decimal, price_customer_orders

NEUTRAL = Decimal("0.75")    # prior used when a vendor has no history yet
FOUR = Decimal("0.0001")


def commission_rate_for(vendor):
    if vendor.commission_rate_override is not None:
        return Decimal(vendor.commission_rate_override)
    return settings.get("commission_rate_percent")


# -------------------------------------------------------------- eligibility

def vendor_eligibility(vendor, go, now=None):
    """Return (ok, reason) for whether ``vendor`` may bid on ``go`` right now."""
    now = now or utcnow()
    if vendor is None or vendor.status != VendorStatus.APPROVED:
        return False, "Your vendor account must be approved before bidding."
    if not vendor.user.is_active:
        return False, "Your account is inactive."
    if go.status != GOStatus.BIDDING:
        return False, "This order is not open for offers."
    if go.bidding_deadline is None or now >= go.bidding_deadline:
        return False, "The bidding deadline has passed."
    if go.bidding_mode != BiddingMode.MULTI_VENDOR and not vendor.is_platform_owned:
        return False, "Multi-vendor bidding is not enabled for this order."
    if not vendor.serves(go.group.location_id):
        return False, "This order is outside your service area."
    if not vendor.capacity_per_order:
        return False, "Declare your capacity per order in your business profile before bidding."
    return True, ""


def open_opportunities(vendor):
    now = utcnow()
    orders = (db.session.query(GroupOrder)
              .filter(GroupOrder.status == GOStatus.BIDDING, GroupOrder.bidding_deadline > now)
              .order_by(GroupOrder.bidding_deadline).all())
    return [go for go in orders if vendor_eligibility(vendor, go, now)[0]]


def bid_for(vendor, go):
    return db.session.query(VendorBid).filter_by(group_order_id=go.id, vendor_id=vendor.id).first()


# --------------------------------------------------------------- submission

def submit_bid(vendor, go, *, items, grade_id, origin=None, substitution_policy=SubstitutionPolicy.NONE,
               delivery_fee="0", home_delivery_fee="0", estimated_delivery_at=None, expires_at=None,
               notes=None, ip=None, user_agent=None, actor=None):
    """Create or revise the vendor's sealed bid.

    ``items`` maps GroupOrderItem id -> dict(offered_qty, unit_price, origin).
    """
    now = utcnow()
    ok, reason = vendor_eligibility(vendor, go, now)
    if not ok:
        raise PermissionDenied(reason)
    grade = db.session.get(ProductGrade, int(grade_id)) if grade_id else None
    if grade is None or not grade.is_active:
        raise DomainError("Choose the quality grade you are offering.")
    if substitution_policy not in SubstitutionPolicy.LABELS:
        raise DomainError("Choose a substitution policy.")
    d_fee = parse_decimal(delivery_fee or "0", "Delivery fee", ZERO, Decimal("100000"))
    h_fee = parse_decimal(home_delivery_fee or "0", "Home delivery fee", ZERO, Decimal("10000"))
    if estimated_delivery_at is None:
        raise DomainError("Estimated delivery date and time is required.")
    if estimated_delivery_at < go.bidding_deadline or estimated_delivery_at > go.delivery_window_end:
        raise DomainError("Estimated delivery must be after the bidding deadline and within the delivery window.")
    min_expiry = go.bidding_deadline + timedelta(hours=settings.get("min_bid_validity_hours"))
    if expires_at is None or expires_at < min_expiry:
        raise DomainError("The bid must remain valid at least "
                          f"{settings.get('min_bid_validity_hours')} hours after the bidding deadline.")

    lines = []
    total_offered = ZERO
    subtotal = ZERO
    for goi in go.items:
        if goi.dropped:
            continue
        data = items.get(goi.id) or items.get(str(goi.id)) or {}
        p = goi.product
        offered = parse_decimal(data.get("offered_qty") or "0", f"Quantity for {p.name}", ZERO,
                                Decimal("100000"), places=3)
        price = parse_decimal(data.get("unit_price") or "0", f"Price for {p.name}", ZERO, Decimal("100000"))
        required = qty(goi.frozen_qty or 0)
        if offered > required:
            raise DomainError(f"You cannot offer more {p.name} than the group ordered ({required} {p.unit}).")
        if not go.allow_partial and offered != required:
            raise DomainError(f"This order requires the full quantity of {p.name} ({required} {p.unit}).")
        if go.allow_partial and required > 0:
            coverage = offered / required * 100
            if coverage < go.min_fulfilment_percent:
                raise DomainError(f"Offer at least {go.min_fulfilment_percent}% of the {p.name} quantity.")
        if offered > 0 and price <= 0:
            raise DomainError(f"Enter a unit price for {p.name}.")
        if goi.min_grade is not None and grade.rank < goi.min_grade.rank:
            raise DomainError(f"{p.name} requires grade {goi.min_grade.label} or better.")
        lt = money(offered * price)
        subtotal += lt
        total_offered += offered
        lines.append(VendorBidItem(group_order_item_id=goi.id, product_id=p.id, offered_qty=offered,
                                   unit_price=price, line_total=lt,
                                   origin=(data.get("origin") or "").strip()[:120] or None))
    if total_offered <= 0:
        raise DomainError("Your offer must include at least one product.")
    if total_offered > vendor.capacity_per_order:
        raise DomainError(f"This offer ({total_offered} units) exceeds your declared capacity per order "
                          f"({vendor.capacity_per_order}).")

    bid = bid_for(vendor, go)
    action = "bid.submitted"
    if bid is None:
        bid = VendorBid(group_order_id=go.id, vendor_id=vendor.id, revision=1)
        db.session.add(bid)
    else:
        if bid.status not in (BidStatus.SUBMITTED, BidStatus.WITHDRAWN):
            raise DomainError("This bid can no longer be changed.")
        bid.revision = (bid.revision or 1) + 1
        bid.items.clear()
        db.session.flush()
        action = "bid.revised"
    bid.status = BidStatus.SUBMITTED
    bid.grade_id = grade.id
    bid.origin = (origin or "").strip()[:160] or None
    bid.substitution_policy = substitution_policy
    bid.currency = go.currency
    bid.delivery_fee = d_fee
    bid.home_delivery_fee = h_fee
    bid.estimated_delivery_at = estimated_delivery_at
    bid.expires_at = expires_at
    bid.submitted_at = now
    bid.commission_rate = commission_rate_for(vendor)
    bid.is_platform_owned = bool(vendor.is_platform_owned)
    bid.notes = (notes or "").strip()[:1000] or None
    bid.subtotal = money(subtotal)
    bid.total_offered_qty = total_offered
    bid.submitted_ip = (ip or "")[:64] or None
    bid.submitted_user_agent = (user_agent or "")[:255] or None
    bid.score = None
    bid.eligible = None
    bid.items.extend(lines)
    db.session.flush()
    # Sealed bidding: the audit entry deliberately omits prices and quantities.
    audit(actor or vendor.user, action, bid, {"group_order_id": go.id, "revision": bid.revision})
    return bid


def withdraw_bid(vendor, bid, actor=None):
    if bid.vendor_id != vendor.id:
        raise PermissionDenied("This is not your bid.")
    go = bid.group_order
    if bid.status != BidStatus.SUBMITTED:
        raise DomainError("Only submitted bids can be withdrawn.")
    if go.status != GOStatus.BIDDING or utcnow() >= go.bidding_deadline:
        raise DomainError("Bids can only be withdrawn before the bidding deadline.")
    bid.status = BidStatus.WITHDRAWN
    audit(actor or vendor.user, "bid.withdrawn", bid, {"group_order_id": go.id})
    return bid


def bids_visible_to_customers(go):
    """Sealed until bidding closes: nothing is revealed while bidding is open."""
    return go.status not in (GOStatus.DRAFT, GOStatus.COLLECTING, GOStatus.FROZEN, GOStatus.BIDDING)


# ------------------------------------------------------------------ history

def vendor_history(vendor):
    delivered = (db.session.query(Delivery)
                 .filter(Delivery.vendor_id == vendor.id, Delivery.status == "delivered").all())
    on_time = [d for d in delivered if d.on_time]
    awards = db.session.query(AwardDecision).filter_by(vendor_id=vendor.id).count()
    cancellations = (db.session.query(VendorBid)
                     .filter(VendorBid.vendor_id == vendor.id, VendorBid.status == BidStatus.DECLINED).count())
    avg_rating, rating_count = (db.session.query(func.avg(Review.rating), func.count(Review.id))
                                .filter(Review.vendor_id == vendor.id).one())
    inspections = (db.session.query(QualityInspection)
                   .join(GroupOrder, GroupOrder.id == QualityInspection.group_order_id)
                   .join(AwardDecision, AwardDecision.group_order_id == GroupOrder.id)
                   .filter(AwardDecision.vendor_id == vendor.id, AwardDecision.status == "active",
                           QualityInspection.verification == Verification.PLATFORM_VERIFIED).all())
    passed = [i for i in inspections if i.result != "fail"]
    complaints = db.session.query(Complaint).filter_by(vendor_id=vendor.id).count()
    return {
        "deliveries": len(delivered),
        "on_time_rate": Decimal(len(on_time)) / len(delivered) if delivered else None,
        "awards": awards,
        "cancellations": cancellations,
        "cancellation_rate": Decimal(cancellations) / awards if awards else None,
        "avg_rating": Decimal(str(avg_rating)).quantize(Decimal("0.01")) if avg_rating is not None else None,
        "rating_count": rating_count or 0,
        "inspections": len(inspections),
        "inspection_pass_rate": Decimal(len(passed)) / len(inspections) if inspections else None,
        "complaints": complaints,
    }


def quality_score(vendor, history=None):
    """0–100 score from platform inspections, complaints and ratings."""
    h = history or vendor_history(vendor)
    insp = h["inspection_pass_rate"] if h["inspection_pass_rate"] is not None else NEUTRAL
    rating = h["avg_rating"] / 5 if h["avg_rating"] is not None else NEUTRAL
    complaint_ratio = (Decimal(h["complaints"]) / h["deliveries"]) if h["deliveries"] else Decimal("0.25")
    complaint_component = max(Decimal(1) - complaint_ratio, ZERO)
    score = (insp * Decimal("0.5") + rating * Decimal("0.3") + complaint_component * Decimal("0.2")) * 100
    return score.quantize(Decimal("0.1"))


# ------------------------------------------------------------------ scoring

def evaluate_bids(go, now=None):
    """Score every submitted bid. Returns evaluations sorted best first."""
    now = now or utcnow()
    weights = settings.score_weights()
    max_rank = db.session.query(func.max(ProductGrade.rank)).scalar() or 1
    required_total = sum((qty(i.frozen_qty or 0) for i in go.items if not i.dropped), ZERO)
    evaluations = []
    for bid in go.bids:
        if bid.status != BidStatus.SUBMITTED:
            continue
        reasons = []
        if bid.expires_at <= now:
            reasons.append("Bid expired")
        if bid.vendor.status != VendorStatus.APPROVED:
            reasons.append("Vendor is not approved")
        if go.bidding_mode != BiddingMode.MULTI_VENDOR and not bid.vendor.is_platform_owned:
            reasons.append("Multi-vendor bidding not enabled")
        cost = bid_customer_cost(go, bid)
        coverage = (bid.total_offered_qty / required_total) if required_total else ZERO
        evaluations.append({"bid": bid, "eligible": not reasons, "reasons": reasons, "cost": cost,
                            "coverage": coverage,
                            "adjusted_cost": (cost / coverage) if coverage else None})
    eligible = [e for e in evaluations if e["eligible"] and e["adjusted_cost"]]
    best = min((e["adjusted_cost"] for e in eligible), default=None)
    for e in evaluations:
        bid = e["bid"]
        h = vendor_history(bid.vendor)
        grade_part = Decimal(bid.grade.rank) / Decimal(max_rank)
        insp = h["inspection_pass_rate"] if h["inspection_pass_rate"] is not None else NEUTRAL
        comps = {
            "price": (best / e["adjusted_cost"]) if (best and e["adjusted_cost"]) else ZERO,
            "quality": (grade_part + insp) / 2,
            "on_time": h["on_time_rate"] if h["on_time_rate"] is not None else NEUTRAL,
            "cancellation": (Decimal(1) - h["cancellation_rate"]) if h["cancellation_rate"] is not None else NEUTRAL,
            "rating": (h["avg_rating"] / 5) if h["avg_rating"] is not None else NEUTRAL,
        }
        score = sum((weights.get(k, ZERO) * v for k, v in comps.items()), ZERO) * 100
        e["score"] = score.quantize(Decimal("0.01")) if e["eligible"] else ZERO
        e["components"] = {k: str(v.quantize(FOUR)) for k, v in comps.items()}
        e["weights"] = {k: str(v.quantize(FOUR)) for k, v in weights.items()}
        e["history"] = {
            "deliveries": h["deliveries"], "on_time_rate": str(h["on_time_rate"]) if h["on_time_rate"] is not None else None,
            "awards": h["awards"], "cancellations": h["cancellations"],
            "avg_rating": str(h["avg_rating"]) if h["avg_rating"] is not None else None,
            "rating_count": h["rating_count"], "inspections": h["inspections"],
        }
    evaluations.sort(key=lambda e: (not e["eligible"], -e["score"], e["cost"], e["bid"].submitted_at))
    return evaluations


def close_bidding(go, actor=None, early_reason=None):
    now = utcnow()
    if go.status != GOStatus.BIDDING:
        raise DomainError("Bidding is not open for this order.")
    if now < go.bidding_deadline:
        if actor is None or not actor.has_role(Roles.ADMIN):
            raise PermissionDenied("Bidding can only be closed early by an administrator.")
        if not early_reason or not early_reason.strip():
            raise DomainError("A reason is required to close bidding before the deadline.")
        audit(actor, "bidding.closed_early", go, {"reason": early_reason.strip()[:300],
                                                  "deadline": go.bidding_deadline})
    transition_group_order(go, GOStatus.SELECTION, actor, early_reason or "Bidding deadline reached")
    go.selection_started_at = now
    evaluations = evaluate_bids(go, now)
    for e in evaluations:
        bid = e["bid"]
        if "Bid expired" in e["reasons"]:
            bid.status = BidStatus.EXPIRED
        bid.eligible = e["eligible"]
        bid.eligibility_notes = "; ".join(e["reasons"]) or None
        bid.score = e["score"]
        bid.score_breakdown = json_dumps({
            "components": e["components"], "weights": e["weights"], "history": e["history"],
            "customer_cost": str(e["cost"]), "coverage": str(e["coverage"].quantize(FOUR)),
        })
    eligible = [e for e in evaluations if e["eligible"]]
    for co in go.customer_orders:
        if co.status == COStatus.LOCKED:
            msg = (f"{len(eligible)} offer(s) received for {go.group.name}. Compare them now."
                   if eligible else f"No eligible offers were received for {go.group.name} yet.")
            notify(co.user_id, msg, f"/group-orders/{go.id}/offers")
    if not eligible:
        from .groups import active_admin_ids
        for uid in active_admin_ids():
            notify(uid, f"No eligible offers for group order #{go.id}. Reopen bidding or cancel.",
                   f"/admin/group-orders/{go.id}")
    db.session.flush()
    return evaluations


def eligible_bids(go):
    bids = [b for b in go.bids if b.status in (BidStatus.SUBMITTED, BidStatus.AWARDED, BidStatus.NOT_SELECTED)
            and b.eligible]
    return sorted(bids, key=lambda b: (-(b.score or 0), b.subtotal, b.submitted_at))


# ------------------------------------------------------------------- voting

def cast_vote(user, go, bid):
    if go.status != GOStatus.SELECTION:
        raise DomainError("Voting is only open while offers are being compared.")
    if go.selection_mode != SelectionMode.GROUP_VOTE:
        raise DomainError("This group order does not use member voting.")
    co = db.session.query(CustomerOrder).filter_by(group_order_id=go.id, user_id=user.id).first()
    if co is None or co.status == COStatus.CANCELLED:
        raise PermissionDenied("Only members with an order in this round can vote.")
    if bid.group_order_id != go.id or bid not in eligible_bids(go):
        raise DomainError("That offer is not eligible.")
    vote = db.session.query(OfferVote).filter_by(group_order_id=go.id, user_id=user.id).first()
    if vote is None:
        vote = OfferVote(group_order_id=go.id, user_id=user.id, bid_id=bid.id)
        db.session.add(vote)
    else:
        vote.bid_id = bid.id
    return vote


def vote_tally(go):
    rows = (db.session.query(OfferVote.bid_id, func.count(OfferVote.id))
            .filter(OfferVote.group_order_id == go.id).group_by(OfferVote.bid_id).all())
    return dict(rows)


def user_vote(go, user):
    return db.session.query(OfferVote).filter_by(group_order_id=go.id, user_id=user.id).first()


def rule_winner(go):
    """Winner according to the order's disclosed rule (vote or score)."""
    candidates = [b for b in eligible_bids(go) if b.status == BidStatus.SUBMITTED and not b.is_expired]
    if not candidates:
        raise DomainError("There are no eligible, unexpired offers to award.")
    if go.selection_mode == SelectionMode.GROUP_VOTE:
        tally = vote_tally(go)
        if not tally:
            raise DomainError("No member has voted yet.")
        return max(candidates, key=lambda b: (tally.get(b.id, 0), b.score or 0, -b.subtotal))
    return candidates[0]


# -------------------------------------------------------------------- award

def award(go, actor, bid=None):
    """Award the group order to a bid inside a single transaction.

    Concurrency: GroupOrder uses optimistic version locking and AwardDecision has
    a partial unique index on active awards, so a second concurrent award fails.
    """
    mode = go.selection_mode
    is_admin = actor is not None and actor.has_role(Roles.ADMIN)
    organiser = actor is not None and is_organiser(go.group, actor)
    if go.status != GOStatus.SELECTION:
        raise DomainError("This order is not awaiting an award (it may already have been awarded).")
    if mode == SelectionMode.ORGANISER:
        if not organiser:
            raise PermissionDenied("Under this order's award policy only the group organiser selects the offer. "
                                   "Platform administrators cannot hand-pick a winner.")
        if bid is None:
            raise DomainError("Choose an offer.")
    else:
        if actor is not None and not (organiser or is_admin):
            raise PermissionDenied("Only the organiser or an administrator can finalise the award.")
        chosen = rule_winner(go)
        if bid is not None and bid.id != chosen.id:
            raise DomainError("Under this order's award policy the winner is determined by the published rule.")
        bid = chosen
    if bid.group_order_id != go.id:
        raise DomainError("That offer belongs to a different order.")
    if bid.status != BidStatus.SUBMITTED or not bid.eligible:
        raise DomainError("That offer is not eligible.")
    if bid.is_expired:
        raise DomainError("That offer has expired and cannot be accepted.")
    if bid.vendor.status != VendorStatus.APPROVED:
        raise DomainError("That vendor is no longer approved.")

    ranked = eligible_bids(go)
    tally = vote_tally(go) if mode == SelectionMode.GROUP_VOTE else {}
    snapshot = [{
        "bid_id": b.id, "vendor": b.vendor.business_name, "platform_owned": b.is_platform_owned,
        "score": str(b.score), "goods_total": str(b.subtotal), "customer_cost": str(bid_customer_cost(go, b)),
        "grade": b.grade.code, "votes": tally.get(b.id, 0),
    } for b in ranked]
    method_text = {
        SelectionMode.ORGANISER: f"selected by the group organiser ({actor.full_name if actor else 'n/a'})",
        SelectionMode.GROUP_VOTE: f"chosen by member vote ({tally.get(bid.id, 0)} of {sum(tally.values())} votes)",
        SelectionMode.AUTO_SCORE: "awarded automatically to the highest transparent score",
    }[mode]
    explanation = (f"{bid.vendor.business_name} was {method_text}. Score {bid.score}/100 among "
                   f"{len(ranked)} eligible offer(s). Price weight is {settings.score_weights().get('price', 0) * 100:.0f}% "
                   "of the score; quality, delivery reliability, cancellations and verified ratings make up the rest.")
    if ranked and ranked[0].id != bid.id:
        explanation += f" The highest-scoring offer was {ranked[0].vendor.business_name} ({ranked[0].score})."
    disclosure = settings.get("platform_owner_disclosure") if bid.is_platform_owned else None

    try:
        transition_group_order(go, GOStatus.AWARDED, actor, f"Awarded to bid #{bid.id}")
        go.awarded_at = utcnow()
        decision = AwardDecision(group_order_id=go.id, bid_id=bid.id, vendor_id=bid.vendor_id, method=mode,
                                 decided_by_id=getattr(actor, "id", None), explanation=explanation,
                                 scores_snapshot=json_dumps(snapshot), platform_owned_vendor=bid.is_platform_owned,
                                 disclosure=disclosure)
        db.session.add(decision)
        bid.status = BidStatus.AWARDED
        for other in go.bids:
            if other.id != bid.id and other.status == BidStatus.SUBMITTED:
                other.status = BidStatus.NOT_SELECTED
        locked = [co for co in go.customer_orders if co.status == COStatus.LOCKED]
        for priced in price_customer_orders(go, bid, locked):
            co = priced["order"]
            apply_pricing(co, priced)
            co.priced_at = utcnow()
            transition_customer_order(co, COStatus.AWAITING_CONFIRMATION)
        db.session.flush()
    except (StaleDataError, IntegrityError):
        db.session.rollback()
        raise ConcurrencyError("This order was updated by someone else (it may already be awarded). "
                               "Please reload and try again.")
    audit(actor, "award.decided", decision, {"group_order_id": go.id, "bid_id": bid.id, "method": mode,
                                             "platform_owned": bid.is_platform_owned})
    notify(bid.vendor.user_id, f"You were awarded group order #{go.id}. Please accept it.",
           f"/vendor/awarded/{go.id}")
    for co in go.customer_orders:
        if co.status == COStatus.AWAITING_CONFIRMATION:
            notify(co.user_id, f"An offer was selected for {go.group.name}. Review your price and confirm.",
                   f"/orders/{co.id}")
    return decision


def revoke_award(go, actor, reason, vendor_declined=False):
    if go.status != GOStatus.AWARDED:
        raise DomainError("Only an award that has not started fulfilment can be revoked.")
    if not reason or not reason.strip():
        raise DomainError("A reason is required.")
    decision = go.active_award
    if decision is None:
        raise DomainError("No active award.")
    for co in go.customer_orders:
        if any(p.status == PaymentStatus.PAID for p in co.payments):
            raise DomainError("Payments were already recorded; cancel the order and refund instead.")
    decision.status = "revoked"
    decision.revoked_at = utcnow()
    decision.revoked_by_id = getattr(actor, "id", None)
    decision.revoke_reason = reason.strip()[:500]
    decision.bid.status = BidStatus.DECLINED if vendor_declined else BidStatus.DISQUALIFIED
    for b in go.bids:
        if b.status == BidStatus.NOT_SELECTED:
            b.status = BidStatus.SUBMITTED
    for co in go.customer_orders:
        if co.status in (COStatus.AWAITING_CONFIRMATION, COStatus.CONFIRMED):
            transition_customer_order(co, COStatus.LOCKED)
            clear_pricing(co)
            co.confirmed_at = None
            for p in co.payments:
                if p.status in (PaymentStatus.PENDING, PaymentStatus.INITIATED):
                    p.status = PaymentStatus.FAILED
                    p.failure_reason = "Award revoked"
            co.payment_status = PaymentStatus.NOT_REQUESTED
            notify(co.user_id, f"The selected offer for {go.group.name} was withdrawn. Another offer will be chosen.",
                   f"/group-orders/{go.id}/offers")
    transition_group_order(go, GOStatus.SELECTION, actor, reason)
    audit(actor, "award.revoked", decision, {"reason": reason, "vendor_declined": vendor_declined})
    db.session.flush()
    return decision


def require_awarded_vendor(go, vendor):
    bid = go.awarded_bid
    if bid is None or vendor is None or bid.vendor_id != vendor.id:
        raise PermissionDenied("This order is not awarded to you.")
    return bid


def vendor_accept_award(vendor, go):
    require_awarded_vendor(go, vendor)
    if go.status != GOStatus.AWARDED:
        raise DomainError("This award has already been accepted or is no longer active.")
    transition_group_order(go, GOStatus.SOURCING, vendor.user, "Vendor accepted the award")
    for co in go.customer_orders:
        if co.status in (COStatus.AWAITING_CONFIRMATION, COStatus.CONFIRMED):
            notify(co.user_id, f"{vendor.business_name} accepted the order for {go.group.name} and is sourcing it.",
                   f"/orders/{co.id}")


def vendor_decline_award(vendor, go, reason):
    require_awarded_vendor(go, vendor)
    return revoke_award(go, vendor.user, reason, vendor_declined=True)
