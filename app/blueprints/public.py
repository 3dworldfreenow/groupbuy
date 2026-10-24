"""Public pages: home, catalogue, groups, information pages."""
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import or_

from ..constants import GOStatus
from ..extensions import db
from ..forms import ContactForm
from ..models import BuyingGroup, ContactMessage, GroupOrder, Product, ProductCategory, ProductGrade
from ..security import rate_limit
from ..services import groups as group_svc
from ..services.pricing import indicative_prices
from ..services.support import process_group_order

bp = Blueprint("public", __name__)


def refresh_deadlines(go):
    """Lazily apply expired deadlines when a page is viewed (cron also does this)."""
    if go is not None and process_group_order(go):
        db.session.commit()


@bp.route("/")
def index():
    products = (db.session.query(Product).filter_by(is_active=True).order_by(Product.id).limit(8).all())
    prices = indicative_prices([p.id for p in products])
    open_groups = (db.session.query(GroupOrder).join(BuyingGroup)
                   .filter(GroupOrder.status == GOStatus.COLLECTING, BuyingGroup.is_public.is_(True),
                           BuyingGroup.is_active.is_(True))
                   .order_by(GroupOrder.order_deadline).limit(3).all())
    return render_template("public/index.html", products=products, prices=prices,
                           open_groups=[(go, group_svc.progress(go)) for go in open_groups])


@bp.route("/how-it-works")
def how_it_works():
    return render_template("public/how_it_works.html")


@bp.route("/products")
def products():
    cat_slug = request.args.get("category", "")
    q = (request.args.get("q") or "").strip()[:60]
    query = db.session.query(Product).join(ProductCategory).filter(Product.is_active.is_(True),
                                                                   ProductCategory.is_active.is_(True))
    category = None
    if cat_slug:
        category = db.session.query(ProductCategory).filter_by(slug=cat_slug).first()
        if category:
            query = query.filter(Product.category_id == category.id)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Product.name.ilike(like), Product.description.ilike(like)))
    items = query.order_by(ProductCategory.sort_order, Product.name).all()
    categories = db.session.query(ProductCategory).filter_by(is_active=True).order_by(ProductCategory.sort_order).all()
    grades = db.session.query(ProductGrade).filter_by(is_active=True).order_by(ProductGrade.rank.desc()).all()
    return render_template("public/products.html", products=items, categories=categories, category=category,
                           q=q, prices=indicative_prices([p.id for p in items]), grades=grades)


@bp.route("/groups")
def groups():
    q = (request.args.get("q") or "").strip()[:60]
    query = db.session.query(BuyingGroup).filter(BuyingGroup.is_public.is_(True), BuyingGroup.is_active.is_(True))
    if q:
        query = query.filter(BuyingGroup.name.ilike(f"%{q}%"))
    rows = []
    for g in query.order_by(BuyingGroup.created_at.desc()).limit(60).all():
        go = g.current_order
        refresh_deadlines(go)
        go = g.current_order
        rows.append((g, go, group_svc.progress(go) if go else None))
    return render_template("public/groups.html", rows=rows, q=q)


@bp.route("/groups/<int:group_id>")
def group_detail(group_id):
    group = db.session.get(BuyingGroup, group_id) or abort(404)
    member = group.membership_for(current_user) if current_user.is_authenticated else None
    is_admin = current_user.is_authenticated and current_user.is_admin
    if not group.is_public and not member and not is_admin:
        abort(404)
    go = group.current_order
    refresh_deadlines(go)
    go = group.current_order
    return render_template("public/group_detail.html", group=group, go=go, member=member,
                           progress=group_svc.progress(go) if go else None,
                           past_orders=[o for o in group.orders if o.status in GOStatus.TERMINAL][:5])


@bp.route("/about")
def about():
    return render_template("public/about.html")


@bp.route("/contact", methods=["GET", "POST"])
@rate_limit(5, 300)
def contact():
    form = ContactForm()
    if current_user.is_authenticated and request.method == "GET":
        form.name.data = current_user.full_name
        form.email.data = current_user.email
    if form.validate_on_submit():
        db.session.add(ContactMessage(name=form.name.data.strip(), email=form.email.data.strip().lower(),
                                      subject=form.subject.data.strip(), message=form.message.data.strip()))
        db.session.commit()
        flash("Thanks — your message was received. We usually reply within one working day.", "success")
        return redirect(url_for("public.contact"))
    return render_template("public/contact.html", form=form, contact_email=current_app.config["CONTACT_EMAIL"])


@bp.route("/privacy")
def privacy():
    return render_template("public/privacy.html")


@bp.route("/terms")
def terms():
    return render_template("public/terms.html")
