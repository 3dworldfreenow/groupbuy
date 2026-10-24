"""Commissions, settlements, cost records and financial metrics.

Definitions (see README "Financial model"):
* Order value (GMV): what customers pay. NOT platform revenue.
* Platform revenue: commissions + customer service fees − refunds the platform bears.
* Vendor payable: goods + delivery fees − vendor-borne refunds − commission.
* Founder-vendor margin: founder vendor payable − founder purchase costs − spoilage.
* Estimated gross margin: platform revenue + founder-vendor margin.
* Estimated operating profit: gross margin − delivery, packaging and other operating costs.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import func

from ..constants import COStatus, CostCategory, DeliveryStatus, GOStatus, Roles, SettlementStatus, VendorStatus
from ..extensions import db
from ..models import (BuyingGroup, CommissionRecord, Complaint, CostRecord, CustomerOrder, CustomerOrderItem,
                      Delivery, GroupOrder, Product, Refund, Role, RoleAssignment, User, VendorProfile,
                      VendorSettlementRecord)
from ..utils import ZERO, dsum, money, qty, utcnow
from .core import DomainError, PermissionDenied, audit, notify
from .pricing import parse_decimal, percent_of

DELIVERED_STATES = (COStatus.DELIVERED, COStatus.COMPLETED)


def record_close(go, admin):
    bid = go.awarded_bid
    if bid is None:
        raise DomainError("This order has no awarded offer.")
    if db.session.query(CommissionRecord.id).filter_by(group_order_id=go.id).first():
        raise DomainError("Financial records already exist for this order.")
    delivered = [co for co in go.customer_orders if co.status in DELIVERED_STATES]
    goods = money(dsum(co.subtotal for co in delivered))
    delivery_fees = money(dsum(co.delivery_fee + co.home_delivery_fee for co in delivered))
    vendor_refunds = money(dsum(r.amount for co in go.customer_orders for r in co.refunds
                                if r.charged_to == "vendor"))
    base = money(max(goods - vendor_refunds, ZERO))
    commission = percent_of(base, bid.commission_rate)
    net = money(goods + delivery_fees - vendor_refunds - commission)
    c = CommissionRecord(group_order_id=go.id, vendor_id=bid.vendor_id, bid_id=bid.id, base_amount=base,
                         rate_percent=bid.commission_rate, amount=commission, currency=go.currency)
    s = VendorSettlementRecord(vendor_id=bid.vendor_id, group_order_id=go.id, goods_amount=goods,
                               delivery_fees=delivery_fees, refunds_deducted=vendor_refunds,
                               commission_amount=commission, net_payable=net, currency=go.currency,
                               status=SettlementStatus.PENDING)
    db.session.add_all([c, s])
    db.session.flush()
    audit(admin, "finance.order_closed", go, {"goods": str(goods), "commission": str(commission),
                                              "vendor_payable": str(net), "vendor_refunds": str(vendor_refunds)})
    notify(bid.vendor.user_id, f"Settlement for order #{go.id}: {net} {go.currency} payable.", "/vendor/finance")
    return c, s


def mark_settlement_paid(settlement, admin, reference):
    if settlement.status == SettlementStatus.PAID:
        raise DomainError("This settlement is already marked as paid.")
    if not reference or not reference.strip():
        raise DomainError("A payment reference is required.")
    settlement.status = SettlementStatus.PAID
    settlement.paid_at = utcnow()
    settlement.reference = reference.strip()[:120]
    audit(admin, "finance.settlement_paid", settlement, {"amount": str(settlement.net_payable),
                                                         "reference": settlement.reference})
    notify(settlement.vendor.user_id, f"Settlement for order #{settlement.group_order_id} was paid.", "/vendor/finance")


def add_cost(admin, *, category, amount, description, incurred_on=None, group_order=None, vendor=None):
    if not admin.has_role(Roles.ADMIN):
        raise PermissionDenied("Only administrators record costs.")
    if category not in CostCategory.LABELS:
        raise DomainError("Choose a cost category.")
    amt = parse_decimal(amount, "Amount", Decimal("0.01"), Decimal("100000000"))
    if not description or not description.strip():
        raise DomainError("A description is required.")
    if vendor is not None and not vendor.is_platform_owned:
        raise DomainError("Costs can only be recorded for the platform or its own (founder) vendor.")
    if incurred_on and incurred_on > date.today():
        raise DomainError("The cost date cannot be in the future.")
    cost = CostRecord(category=category, amount=amt, currency=group_order.currency if group_order else None,
                      group_order_id=group_order.id if group_order else None,
                      vendor_id=vendor.id if vendor else None, description=description.strip()[:300],
                      incurred_on=incurred_on or date.today(), recorded_by_id=admin.id)
    from .settings import currency
    cost.currency = cost.currency or currency()
    db.session.add(cost)
    db.session.flush()
    audit(admin, "finance.cost_recorded", cost, {"category": category, "amount": str(amt)})
    return cost


def _sum(query):
    return money(query.scalar() or 0)


def costs_by_category():
    rows = db.session.query(CostRecord.category, func.sum(CostRecord.amount)).group_by(CostRecord.category).all()
    out = {k: money(0) for k in CostCategory.LABELS}
    for cat, total in rows:
        out[cat] = money(total)
    return out


def financial_summary():
    completed = db.session.query(CustomerOrder).filter(CustomerOrder.status == COStatus.COMPLETED)
    gmv = money(dsum(co.total for co in completed))
    service_fees = money(dsum(co.service_fee for co in completed))
    commissions = _sum(db.session.query(func.sum(CommissionRecord.amount)))
    refunds_total = _sum(db.session.query(func.sum(Refund.amount)))
    refunds_platform = _sum(db.session.query(func.sum(Refund.amount)).filter(Refund.charged_to == "platform"))
    refunds_vendor = money(refunds_total - refunds_platform)
    platform_revenue = money(commissions + service_fees - refunds_platform)
    costs = costs_by_category()
    founder_ids = [v.id for v in db.session.query(VendorProfile).filter_by(is_platform_owned=True)]
    founder_payable = _sum(db.session.query(func.sum(VendorSettlementRecord.net_payable))
                           .filter(VendorSettlementRecord.vendor_id.in_(founder_ids or [0])))
    founder_margin = money(founder_payable - costs[CostCategory.PURCHASE] - costs[CostCategory.SPOILAGE])
    gross_margin = money(platform_revenue + founder_margin)
    operating_profit = money(gross_margin - costs[CostCategory.DELIVERY] - costs[CostCategory.PACKAGING]
                             - costs[CostCategory.OPERATIONAL])
    payable_total = _sum(db.session.query(func.sum(VendorSettlementRecord.net_payable)))
    payable_pending = _sum(db.session.query(func.sum(VendorSettlementRecord.net_payable))
                           .filter(VendorSettlementRecord.status == SettlementStatus.PENDING))
    return {
        "gmv": gmv, "service_fees": service_fees, "commissions": commissions, "platform_revenue": platform_revenue,
        "refunds_total": refunds_total, "refunds_platform": refunds_platform, "refunds_vendor": refunds_vendor,
        "costs": costs, "founder_payable": founder_payable, "founder_margin": founder_margin,
        "gross_margin": gross_margin, "operating_profit": operating_profit,
        "vendor_payable_total": payable_total, "vendor_payable_pending": payable_pending,
    }


def admin_metrics():
    def role_count(name):
        return (db.session.query(func.count(RoleAssignment.user_id))
                .join(Role, Role.id == RoleAssignment.role_id).filter(Role.name == name).scalar() or 0)

    active_groups = (db.session.query(func.count(func.distinct(GroupOrder.group_id)))
                     .join(BuyingGroup, BuyingGroup.id == GroupOrder.group_id)
                     .filter(GroupOrder.status.in_(GOStatus.ACTIVE), BuyingGroup.is_active.is_(True)).scalar() or 0)
    demand_rows = (db.session.query(Product.unit, func.sum(CustomerOrderItem.quantity))
                   .join(CustomerOrderItem, CustomerOrderItem.product_id == Product.id)
                   .join(CustomerOrder, CustomerOrder.id == CustomerOrderItem.customer_order_id)
                   .join(GroupOrder, GroupOrder.id == CustomerOrder.group_order_id)
                   .filter(GroupOrder.status.in_((GOStatus.COLLECTING, GOStatus.FROZEN, GOStatus.BIDDING,
                                                  GOStatus.SELECTION)),
                           CustomerOrder.status != COStatus.CANCELLED)
                   .group_by(Product.unit).all())
    delivered = db.session.query(Delivery).filter(Delivery.status == DeliveryStatus.DELIVERED).all()
    on_time = [d for d in delivered if d.on_time]
    per_customer = (db.session.query(CustomerOrder.user_id, func.count(CustomerOrder.id))
                    .filter(CustomerOrder.status.in_(DELIVERED_STATES)).group_by(CustomerOrder.user_id).all())
    buyers = len(per_customer)
    repeat = len([1 for _, n in per_customer if n >= 2])
    delivered_orders = sum(n for _, n in per_customer)
    complaints = db.session.query(func.count(Complaint.id)).scalar() or 0
    fin = financial_summary()
    return {
        "customers": role_count(Roles.CUSTOMER),
        "vendors_approved": db.session.query(VendorProfile).filter_by(status=VendorStatus.APPROVED).count(),
        "vendors_pending": db.session.query(VendorProfile).filter_by(status=VendorStatus.PENDING).count(),
        "active_groups": active_groups,
        "aggregate_demand": {unit: qty(total) for unit, total in demand_rows},
        "on_time_rate": (Decimal(len(on_time)) / len(delivered) * 100).quantize(Decimal("0.1")) if delivered else None,
        "repeat_rate": (Decimal(repeat) / buyers * 100).quantize(Decimal("0.1")) if buyers else None,
        "complaint_rate": ((Decimal(complaints) / delivered_orders * 100).quantize(Decimal("0.1"))
                           if delivered_orders else None),
        "complaints": complaints,
        "delivered_orders": delivered_orders,
        "fin": fin,
    }


def vendor_finance(vendor):
    settlements = (db.session.query(VendorSettlementRecord).filter_by(vendor_id=vendor.id)
                   .order_by(VendorSettlementRecord.id.desc()).all())
    commissions = (db.session.query(CommissionRecord).filter_by(vendor_id=vendor.id)
                   .order_by(CommissionRecord.id.desc()).all())
    return {
        "settlements": settlements,
        "commissions": commissions,
        "completed_value": money(dsum(s.goods_amount + s.delivery_fees for s in settlements)),
        "commission_total": money(dsum(c.amount for c in commissions)),
        "payable_pending": money(dsum(s.net_payable for s in settlements if s.status == SettlementStatus.PENDING)),
        "paid_total": money(dsum(s.net_payable for s in settlements if s.status == SettlementStatus.PAID)),
    }


def order_financials(go):
    """Per group order financial view used in reports and the admin order page."""
    s = db.session.query(VendorSettlementRecord).filter_by(group_order_id=go.id).first()
    c = db.session.query(CommissionRecord).filter_by(group_order_id=go.id).first()
    costs = db.session.query(CostRecord).filter_by(group_order_id=go.id).all()
    live = [co for co in go.customer_orders if co.status != COStatus.CANCELLED]
    return {
        "order_value": money(dsum(co.total for co in live)),
        "service_fees": money(dsum(co.service_fee for co in live)),
        "paid": money(dsum(p.amount for co in go.customer_orders for p in co.payments if p.status == "paid")),
        "refunds": money(dsum(r.amount for co in go.customer_orders for r in co.refunds)),
        "settlement": s, "commission": c, "costs": costs,
        "cost_total": money(dsum(x.amount for x in costs)),
    }


def customer_count():
    return (db.session.query(func.count(User.id)).join(RoleAssignment, RoleAssignment.user_id == User.id)
            .join(Role, Role.id == RoleAssignment.role_id).filter(Role.name == Roles.CUSTOMER).scalar() or 0)
