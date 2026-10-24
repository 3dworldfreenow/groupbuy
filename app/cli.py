"""Flask CLI commands: ``flask --app run.py <command>``."""
import os
import secrets

import click
from flask import current_app

from .constants import Roles
from .extensions import db


def _password(option_value, env_name, label):
    if option_value:
        return option_value
    if os.environ.get(env_name):
        return os.environ[env_name]
    return click.prompt(f"{label} password", hide_input=True, confirmation_prompt=True)


def register_cli(app):
    @app.cli.command("seed-reference")
    def seed_reference_cmd():
        """Create roles, categories, grades, products and locations (idempotent)."""
        from .seed import seed_reference
        seed_reference()
        click.echo("Reference data ready.")

    @app.cli.command("create-admin")
    @click.option("--email", required=True)
    @click.option("--name", default="Platform Administrator")
    @click.option("--password", default=None, help="Omit to be prompted (recommended).")
    def create_admin(email, name, password):
        """Create an administrator account (password is prompted, never echoed)."""
        from .models import User
        from .seed import seed_reference
        from .services.core import DomainError, audit
        from .services.support import create_user, get_role
        seed_reference()
        existing = db.session.query(User).filter_by(email=email.strip().lower()).first()
        if existing:
            existing.add_role(get_role(Roles.ADMIN))
            audit(None, "admin.role_granted_cli", existing, {})
            db.session.commit()
            click.echo(f"Granted admin role to existing user {existing.email}.")
            return
        pw = _password(password, "ADMIN_PASSWORD", "Administrator")
        try:
            user = create_user(email=email, password=pw, full_name=name, roles=(Roles.ADMIN,))
        except DomainError as exc:
            raise click.ClickException(str(exc))
        audit(None, "admin.created_cli", user, {})
        db.session.commit()
        click.echo(f"Administrator {user.email} created.")

    @app.cli.command("create-founder-vendor")
    @click.option("--email", required=True)
    @click.option("--name", default="Founder")
    @click.option("--business-name", required=True)
    @click.option("--phone", default="+91 00000 00000")
    @click.option("--address", default="Update in the vendor profile")
    @click.option("--capacity", default="1000", help="Max total quantity per group order")
    @click.option("--password", default=None, help="Omit to be prompted (recommended).")
    def create_founder_vendor(email, name, business_name, phone, address, capacity, password):
        """Create the platform-owned (founder) vendor account, pre-approved and disclosed."""
        from .models import User
        from .seed import seed_reference
        from .services.core import DomainError, audit
        from .services.support import create_user, create_vendor_profile
        seed_reference()
        try:
            user = db.session.query(User).filter_by(email=email.strip().lower()).first()
            if user is None:
                pw = _password(password, "FOUNDER_PASSWORD", "Founder vendor")
                user = create_user(email=email, password=pw, full_name=name, roles=())
            vp = create_vendor_profile(user, business_name=business_name, contact_phone=phone,
                                       business_address=address, capacity_per_order=capacity, platform_owned=True)
        except DomainError as exc:
            db.session.rollback()
            raise click.ClickException(str(exc))
        audit(None, "vendor.founder_created_cli", vp, {})
        db.session.commit()
        click.echo(f"Founder vendor '{vp.business_name}' created for {user.email} (platform-owned, approved).")

    @app.cli.command("seed-demo")
    @click.option("--password", default=None, help="Password for all demo accounts (default: random, printed).")
    @click.option("--force", is_flag=True, help="Allow running outside development.")
    def seed_demo_cmd(password, force):
        """Create demo accounts and group orders covering the full workflow."""
        if current_app.config.get("ENV_NAME") == "production" and not force:
            raise click.ClickException("Refusing to load demo data in production (use --force if you are sure).")
        from .seed import seed_demo
        pw = password or os.environ.get("DEMO_PASSWORD") or ("Demo-" + secrets.token_urlsafe(9))
        from .services.support import validate_password
        validate_password(pw)
        seed_demo(pw)
        click.echo("Demo data loaded. Accounts (all share one password):")
        for email in ("admin@example.com", "founder@example.com", "vendor2@example.com", "vendor3@example.com",
                      "alice@example.com", "bob@example.com", "carol@example.com"):
            click.echo(f"  {email}")
        if not password and not os.environ.get("DEMO_PASSWORD"):
            click.echo(f"Demo password: {pw}")

    @app.cli.command("process-deadlines")
    def process_deadlines_cmd():
        """Freeze orders and close bidding whose deadlines have passed (run from cron/systemd timer)."""
        from .services.support import process_deadlines
        n = process_deadlines()
        db.session.commit()
        click.echo(f"Processed {n} group order(s).")
