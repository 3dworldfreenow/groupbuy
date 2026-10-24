"""Status values, roles and allowed state transitions.

Status fields are stored as short strings; the backend enforces every
transition through the dictionaries below (see ``services.state``).
"""


class Roles:
    CUSTOMER = "customer"
    VENDOR = "vendor"
    ADMIN = "admin"
    ALL = (CUSTOMER, VENDOR, ADMIN)


class VendorStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUSPENDED = "suspended"
    ALL = (PENDING, APPROVED, REJECTED, SUSPENDED)


VENDOR_TRANSITIONS = {
    VendorStatus.PENDING: {VendorStatus.APPROVED, VendorStatus.REJECTED},
    VendorStatus.REJECTED: {VendorStatus.APPROVED, VendorStatus.PENDING},
    VendorStatus.APPROVED: {VendorStatus.SUSPENDED},
    VendorStatus.SUSPENDED: {VendorStatus.APPROVED},
}


class GOStatus:
    """Group order lifecycle."""
    DRAFT = "draft"
    COLLECTING = "collecting"
    FROZEN = "frozen"
    BIDDING = "bidding"
    SELECTION = "selection"
    AWARDED = "awarded"
    SOURCING = "sourcing"
    QUALITY_CHECK = "quality_check"
    ON_HOLD = "on_hold"
    PACKED = "packed"
    DISPATCHED = "dispatched"
    DELIVERED = "delivered"
    CLOSED = "closed"
    CANCELLED = "cancelled"

    ACTIVE = (DRAFT, COLLECTING, FROZEN, BIDDING, SELECTION, AWARDED, SOURCING,
              QUALITY_CHECK, ON_HOLD, PACKED, DISPATCHED, DELIVERED)
    IN_FULFILMENT = (AWARDED, SOURCING, QUALITY_CHECK, ON_HOLD, PACKED, DISPATCHED, DELIVERED)
    TERMINAL = (CLOSED, CANCELLED)

    LABELS = {
        DRAFT: "Draft",
        COLLECTING: "Collecting orders",
        FROZEN: "Frozen",
        BIDDING: "Receiving offers",
        SELECTION: "Comparing offers",
        AWARDED: "Offer selected",
        SOURCING: "Sourcing",
        QUALITY_CHECK: "Quality check",
        ON_HOLD: "On hold",
        PACKED: "Packed",
        DISPATCHED: "Out for delivery",
        DELIVERED: "Delivered",
        CLOSED: "Closed",
        CANCELLED: "Cancelled",
    }


GO_TRANSITIONS = {
    GOStatus.DRAFT: {GOStatus.COLLECTING, GOStatus.CANCELLED},
    GOStatus.COLLECTING: {GOStatus.FROZEN, GOStatus.CANCELLED},
    GOStatus.FROZEN: {GOStatus.BIDDING, GOStatus.CANCELLED},
    GOStatus.BIDDING: {GOStatus.SELECTION, GOStatus.CANCELLED},
    GOStatus.SELECTION: {GOStatus.AWARDED, GOStatus.BIDDING, GOStatus.CANCELLED},
    GOStatus.AWARDED: {GOStatus.SOURCING, GOStatus.SELECTION, GOStatus.CANCELLED},
    GOStatus.SOURCING: {GOStatus.QUALITY_CHECK, GOStatus.ON_HOLD, GOStatus.CANCELLED},
    GOStatus.QUALITY_CHECK: {GOStatus.PACKED, GOStatus.ON_HOLD, GOStatus.SOURCING, GOStatus.CANCELLED},
    GOStatus.ON_HOLD: {GOStatus.SOURCING, GOStatus.QUALITY_CHECK, GOStatus.PACKED,
                       GOStatus.DISPATCHED, GOStatus.DELIVERED, GOStatus.CANCELLED},
    GOStatus.PACKED: {GOStatus.DISPATCHED, GOStatus.ON_HOLD, GOStatus.CANCELLED},
    GOStatus.DISPATCHED: {GOStatus.DELIVERED, GOStatus.ON_HOLD},
    GOStatus.DELIVERED: {GOStatus.CLOSED, GOStatus.ON_HOLD},
    GOStatus.CLOSED: set(),
    GOStatus.CANCELLED: set(),
}


class SelectionMode:
    ORGANISER = "organiser"
    GROUP_VOTE = "group_vote"
    AUTO_SCORE = "auto_score"
    ALL = (ORGANISER, GROUP_VOTE, AUTO_SCORE)
    LABELS = {
        ORGANISER: "Organiser selects from eligible offers",
        GROUP_VOTE: "Members vote; most votes wins (ties broken by score)",
        AUTO_SCORE: "Highest transparent score wins automatically",
    }


class BiddingMode:
    SINGLE_VENDOR = "single_vendor"   # Stage 1: founder-operated vendor only
    MULTI_VENDOR = "multi_vendor"     # Stage 2: approved external vendors may bid


class FulfilmentOption:
    PICKUP = "pickup"
    HOME = "home"
    BOTH = "both"
    LABELS = {PICKUP: "Community pickup point", HOME: "Home delivery", BOTH: "Pickup or home delivery"}


class COStatus:
    """Customer order lifecycle (one per member per group order)."""
    OPEN = "open"                         # adding quantities while collecting
    LOCKED = "locked"                     # group order frozen, awaiting offers
    AWAITING_CONFIRMATION = "awaiting_confirmation"   # priced, customer must confirm
    CONFIRMED = "confirmed"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    LIVE = (OPEN, LOCKED, AWAITING_CONFIRMATION, CONFIRMED, DELIVERED)
    LABELS = {
        OPEN: "Open",
        LOCKED: "Locked",
        AWAITING_CONFIRMATION: "Awaiting your confirmation",
        CONFIRMED: "Confirmed",
        DELIVERED: "Delivered",
        COMPLETED: "Completed",
        CANCELLED: "Cancelled",
    }


CO_TRANSITIONS = {
    COStatus.OPEN: {COStatus.LOCKED, COStatus.CANCELLED},
    COStatus.LOCKED: {COStatus.AWAITING_CONFIRMATION, COStatus.CANCELLED},
    COStatus.AWAITING_CONFIRMATION: {COStatus.CONFIRMED, COStatus.LOCKED, COStatus.CANCELLED},
    COStatus.CONFIRMED: {COStatus.DELIVERED, COStatus.LOCKED, COStatus.CANCELLED},
    COStatus.DELIVERED: {COStatus.COMPLETED},
    COStatus.COMPLETED: set(),
    COStatus.CANCELLED: set(),
}


class PaymentStatus:
    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    INITIATED = "initiated"
    PAID = "paid"
    FAILED = "failed"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    LABELS = {
        NOT_REQUESTED: "Not requested",
        PENDING: "Pending",
        INITIATED: "Payment initiated",
        PAID: "Paid",
        FAILED: "Failed",
        PARTIALLY_REFUNDED: "Partially refunded",
        REFUNDED: "Refunded",
    }


# Transitions for an individual PaymentRecord.
PAYMENT_TRANSITIONS = {
    PaymentStatus.PENDING: {PaymentStatus.INITIATED, PaymentStatus.PAID, PaymentStatus.FAILED},
    PaymentStatus.INITIATED: {PaymentStatus.PAID, PaymentStatus.FAILED},
    PaymentStatus.FAILED: {PaymentStatus.INITIATED},
    PaymentStatus.PAID: set(),
}


class BidStatus:
    SUBMITTED = "submitted"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"
    AWARDED = "awarded"
    NOT_SELECTED = "not_selected"
    DECLINED = "declined"        # vendor declined an award
    DISQUALIFIED = "disqualified"
    LABELS = {
        SUBMITTED: "Submitted",
        WITHDRAWN: "Withdrawn",
        EXPIRED: "Expired",
        AWARDED: "Awarded",
        NOT_SELECTED: "Not selected",
        DECLINED: "Declined by vendor",
        DISQUALIFIED: "Disqualified",
    }


class SubstitutionPolicy:
    NONE = "none"
    SAME_GRADE = "same_grade"
    CONTACT = "contact_customer"
    LABELS = {
        NONE: "No substitutions (short items are refunded)",
        SAME_GRADE: "Substitute with same product of equal or better grade",
        CONTACT: "Contact the organiser before substituting",
    }


class DeliveryStatus:
    SCHEDULED = "scheduled"
    DISPATCHED = "dispatched"
    DELIVERED = "delivered"
    FAILED = "failed"
    RETURNED = "returned"
    OPEN = (SCHEDULED, DISPATCHED, FAILED)
    LABELS = {
        SCHEDULED: "Scheduled",
        DISPATCHED: "Out for delivery",
        DELIVERED: "Delivered",
        FAILED: "Delivery failed",
        RETURNED: "Returned / not delivered",
    }


DELIVERY_TRANSITIONS = {
    DeliveryStatus.SCHEDULED: {DeliveryStatus.DISPATCHED},
    DeliveryStatus.DISPATCHED: {DeliveryStatus.DELIVERED, DeliveryStatus.FAILED},
    DeliveryStatus.FAILED: {DeliveryStatus.SCHEDULED, DeliveryStatus.RETURNED},
    DeliveryStatus.DELIVERED: set(),
    DeliveryStatus.RETURNED: set(),
}


class InspectionResult:
    PASS = "pass"
    CONDITIONAL = "conditional"
    FAIL = "fail"
    ALL = (PASS, CONDITIONAL, FAIL)


class Verification:
    VENDOR_DECLARED = "vendor_declared"
    PLATFORM_VERIFIED = "platform_verified"
    LABELS = {
        VENDOR_DECLARED: "Vendor-declared (not independently verified)",
        PLATFORM_VERIFIED: "Verified by platform inspector",
    }


INSPECTION_CHECKLIST = [
    ("freshness", "Freshness (firm, no wilting)"),
    ("size_uniformity", "Size and uniformity match the grade"),
    ("colour", "Colour typical of the variety"),
    ("damage", "Free of bruising, cuts and pest damage"),
    ("spoilage", "No rot, mould or sprouting"),
    ("foreign_matter", "Free of soil clumps and foreign matter"),
    ("packaging", "Packaging clean, labelled and intact"),
    ("weight", "Weight matches the packing list"),
]


class ComplaintStatus:
    OPEN = "open"
    VENDOR_RESPONDED = "vendor_responded"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    UNRESOLVED = (OPEN, VENDOR_RESPONDED)
    LABELS = {
        OPEN: "Open",
        VENDOR_RESPONDED: "Vendor responded",
        RESOLVED: "Resolved",
        REJECTED: "Rejected",
    }


COMPLAINT_TYPES = {
    "shortage": "Shortage / missing quantity",
    "damage": "Damaged items",
    "quality": "Quality below the offered grade",
    "late": "Late or missed delivery",
    "other": "Other",
}

COMPLAINT_RESOLUTIONS = {
    "refund": "Refund",
    "replacement": "Replacement",
    "no_action": "No action required",
}


class RefundStatus:
    PROCESSED = "processed"


class CostCategory:
    PURCHASE = "purchase"
    DELIVERY = "delivery"
    PACKAGING = "packaging"
    SPOILAGE = "spoilage"
    OPERATIONAL = "operational"
    LABELS = {
        PURCHASE: "Vendor purchase cost (produce)",
        DELIVERY: "Delivery expense",
        PACKAGING: "Packaging",
        SPOILAGE: "Spoilage / wastage",
        OPERATIONAL: "Other operating cost",
    }


class SettlementStatus:
    PENDING = "pending"
    PAID = "paid"


PRODUCT_UNITS = ("kg", "dozen", "piece", "bunch", "litre")

VENDOR_DOCUMENT_TYPES = {
    "business_registration": "Business registration certificate",
    "tax_registration": "Tax registration (e.g. GST)",
    "food_safety_licence": "Food safety licence/registration",
    "identity_proof": "Owner identity proof",
    "bank_proof": "Bank account proof",
    "organic_certificate": "Organic certification (if claimed)",
    "other": "Other",
}
