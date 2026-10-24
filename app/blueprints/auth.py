"""Registration, login and logout."""
from urllib.parse import urlsplit

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..constants import Roles
from ..extensions import db
from ..forms import LoginForm, RegisterForm, VendorRegisterForm
from ..models import User
from ..security import rate_limit
from ..services.core import DomainError, audit
from ..services.support import create_user, create_vendor_profile, is_locked, register_failed_login
from ..utils import utcnow

bp = Blueprint("auth", __name__)


def _safe_next(target):
    if not target:
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//"):
        return None
    return target


def home_for(user):
    if user.has_role(Roles.ADMIN):
        return url_for("admin.dashboard")
    if user.has_role(Roles.VENDOR) and user.vendor_profile:
        return url_for("vendor.dashboard")
    return url_for("customer.dashboard")


@bp.route("/login", methods=["GET", "POST"])
@rate_limit(10, 60)
def login():
    if current_user.is_authenticated:
        return redirect(home_for(current_user))
    form = LoginForm()
    if form.validate_on_submit():
        user = db.session.query(User).filter_by(email=form.email.data.strip().lower()).first()
        if user and is_locked(user):
            flash("Too many failed attempts. The account is temporarily locked; try again later.", "danger")
        elif user and user.check_password(form.password.data) and user.is_active:
            user.failed_logins = 0
            user.locked_until = None
            user.last_login_at = utcnow()
            db.session.commit()
            session.clear()
            login_user(user, remember=form.remember.data)
            session.permanent = True
            flash(f"Welcome back, {user.full_name}.", "success")
            return redirect(_safe_next(request.args.get("next")) or home_for(user))
        else:
            if user:
                register_failed_login(user)
                db.session.commit()
            # Same message for unknown email and wrong password (no account enumeration).
            flash("Invalid email or password.", "danger")
    return render_template("auth/login.html", form=form)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("public.index"))


@bp.route("/register", methods=["GET", "POST"])
@rate_limit(5, 300)
def register():
    if current_user.is_authenticated:
        return redirect(home_for(current_user))
    form = RegisterForm()
    if form.validate_on_submit():
        try:
            user = create_user(email=form.email.data, password=form.password.data, full_name=form.full_name.data,
                               phone=form.phone.data, roles=(Roles.CUSTOMER,))
            db.session.commit()
        except DomainError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        else:
            session.clear()
            login_user(user)
            flash("Your account was created. Create a buying group or join one with an invite code.", "success")
            return redirect(_safe_next(request.args.get("next")) or url_for("customer.dashboard"))
    return render_template("auth/register.html", form=form)


@bp.route("/vendor/register", methods=["GET", "POST"])
@rate_limit(5, 300)
def vendor_register():
    if current_user.is_authenticated and current_user.vendor_profile:
        return redirect(url_for("vendor.dashboard"))
    form = VendorRegisterForm()
    if current_user.is_authenticated:
        # Existing users only add a business profile; account fields are not needed.
        for name in ("full_name", "email", "password", "confirm"):
            del form[name]
    if form.validate_on_submit():
        try:
            if current_user.is_authenticated:
                user = current_user
            else:
                user = create_user(email=form.email.data, password=form.password.data,
                                   full_name=form.full_name.data, phone=form.phone.data, roles=())
            vp = create_vendor_profile(user, business_name=form.business_name.data, contact_phone=form.phone.data,
                                       business_address=form.business_address.data,
                                       capacity_per_order=form.capacity_per_order.data,
                                       description=form.description.data)
            audit(user, "vendor.applied", vp, {})
            db.session.commit()
        except DomainError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        else:
            if not current_user.is_authenticated:
                session.clear()
                login_user(user)
            flash("Application received. Upload your verification documents so an administrator can review them.",
                  "success")
            return redirect(url_for("vendor.verification"))
    return render_template("auth/vendor_register.html", form=form)
