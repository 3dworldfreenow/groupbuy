"""Small shared helpers: time handling, decimals and pagination."""
import json
import secrets
import string
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from flask import current_app

MONEY_Q = Decimal("0.01")
QTY_Q = Decimal("0.001")
ZERO = Decimal("0")


def utcnow():
    """Naive UTC timestamp. All datetimes are stored as naive UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def app_tz():
    return ZoneInfo(current_app.config.get("APP_TIMEZONE", "UTC"))


def to_local(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(app_tz())


def parse_local_datetime(value):
    """Parse an HTML ``datetime-local`` value in the app timezone into naive UTC."""
    if not value:
        return None
    if isinstance(value, datetime):
        local = value
    else:
        try:
            local = datetime.strptime(value.strip()[:16], "%Y-%m-%dT%H:%M")
        except ValueError:
            return None
    if local.tzinfo is None:
        local = local.replace(tzinfo=app_tz())
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def to_decimal(value, default=None):
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    try:
        # str() avoids binary float artefacts (e.g. SQLite aggregate results).
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return default


def money(value):
    return (to_decimal(value, ZERO) or ZERO).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def qty(value):
    return (to_decimal(value, ZERO) or ZERO).quantize(QTY_Q, rounding=ROUND_HALF_UP)


def dsum(values):
    total = ZERO
    for v in values:
        if v is not None:
            total += to_decimal(v, ZERO)
    return total


def fmt_qty(value):
    d = qty(value).normalize()
    # normalize() may produce scientific notation for round numbers (1E+2)
    return f"{d:f}"


def random_code(length=8):
    alphabet = string.ascii_uppercase + string.digits
    alphabet = alphabet.translate(str.maketrans("", "", "O0I1"))
    return "".join(secrets.choice(alphabet) for _ in range(length))


def json_dumps(data):
    return json.dumps(data, default=str, sort_keys=True)


def json_loads(text, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default
