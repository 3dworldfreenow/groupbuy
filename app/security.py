"""Authorization decorators, rate limiting and the action helper used by views."""
import threading
import time
from collections import defaultdict
from functools import wraps

from flask import abort, current_app, flash, request
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from .constants import Roles, VendorStatus
from .extensions import db
from .services.core import DomainError


def roles_required(*roles):
    """Require login and at least one of ``roles``. Enforced server-side."""
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not current_user.has_role(*roles):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


customer_required = roles_required(Roles.CUSTOMER)
admin_required = roles_required(Roles.ADMIN)


def vendor_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.has_role(Roles.VENDOR) or current_user.vendor_profile is None:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def approved_vendor_required(view):
    @wraps(view)
    @vendor_required
    def wrapped(*args, **kwargs):
        if current_user.vendor_profile.status != VendorStatus.APPROVED:
            flash("Your vendor account must be approved before you can use this feature.", "warning")
            abort(403)
        return view(*args, **kwargs)
    return wrapped


class RateLimiter:
    """Small fixed-window, in-process rate limiter.

    Suitable for a single server. With several Gunicorn workers each worker
    keeps its own counters; account lockout (stored in the database) still
    protects logins globally. For larger deployments put limits in the
    reverse proxy as well (see docs/DEPLOYMENT.md).
    """

    def __init__(self):
        self._hits = defaultdict(list)
        self._lock = threading.Lock()

    def hit(self, key, limit, window):
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits[key] if now - t < window]
            allowed = len(hits) < limit
            if allowed:
                hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 50000:  # bound memory
                self._hits.clear()
            return allowed

    def reset(self):
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def rate_limit(limit, window=60, methods=("POST",)):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if current_app.config.get("RATELIMIT_ENABLED", True) and request.method in methods:
                key = f"{request.endpoint}:{request.remote_addr}"
                if not limiter.hit(key, limit, window):
                    abort(429)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def run_action(fn, *args, success=None, **kwargs):
    """Run a service call, commit on success, roll back and flash on failure.

    Returns the service result, or ``None`` if a business rule rejected it.
    """
    try:
        result = fn(*args, **kwargs)
        db.session.commit()
    except DomainError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return None
    except (StaleDataError, IntegrityError):
        db.session.rollback()
        flash("This record was changed by someone else at the same time. Please review and try again.", "danger")
        return None
    if success:
        flash(success, "success")
    return result if result is not None else True
