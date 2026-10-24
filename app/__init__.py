"""Group-buying marketplace application factory."""
import logging
import os
import warnings

from flask import Flask, render_template, request
from sqlalchemy import exc as sa_exc

from .config import get_config
from .extensions import csrf, db, login_manager, migrate


def create_app(config_name=None, **overrides):
    app = Flask(__name__, instance_relative_config=True)
    config = get_config(config_name)
    config.validate()
    app.config.from_object(config)
    app.config.update(overrides)
    app.config["MAX_CONTENT_LENGTH"] = app.config["MAX_UPLOAD_MB"] * 1024 * 1024 * 4

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        # SQLite stores NUMERIC as REAL; values are re-quantised to their scale
        # on load. PostgreSQL (production) stores exact NUMERIC values.
        warnings.filterwarnings("ignore", category=sa_exc.SAWarning, message=".*does \\*not\\* support Decimal.*")

    if app.config.get("BEHIND_PROXY"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True, compare_type=True)
    login_manager.init_app(app)
    csrf.init_app(app)

    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        _enable_sqlite_foreign_keys(app)

    from . import models  # noqa: F401  (register models)
    from .models import User

    @login_manager.user_loader
    def load_user(user_id):
        user = db.session.get(User, int(user_id))
        return user if user and user.is_active else None

    from .blueprints import register_blueprints
    register_blueprints(app)

    from .templating import register_template_helpers
    register_template_helpers(app)

    from .cli import register_cli
    register_cli(app)

    _register_error_handlers(app)
    _register_security_headers(app)

    if not app.debug and not app.testing:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return app


def _enable_sqlite_foreign_keys(app):
    from sqlalchemy import event

    with app.app_context():
        engine = db.engine

        @event.listens_for(engine, "connect")
        def _fk_pragma(dbapi_connection, _record):
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()


def _register_error_handlers(app):
    def render(code, title, message):
        return render_template("errors/error.html", code=code, title=title, message=message), code

    @app.errorhandler(400)
    def bad_request(e):
        return render(400, "Bad request", "The request could not be processed. If you submitted a form, "
                                          "reload the page and try again.")

    @app.errorhandler(403)
    def forbidden(e):
        return render(403, "Access denied", "You do not have permission to view this page.")

    @app.errorhandler(404)
    def not_found(e):
        return render(404, "Page not found", "The page you were looking for does not exist.")

    @app.errorhandler(413)
    def too_large(e):
        return render(413, "File too large", f"Uploads are limited to {app.config['MAX_UPLOAD_MB']} MB per file.")

    @app.errorhandler(429)
    def too_many(e):
        return render(429, "Too many requests", "Please wait a minute before trying again.")

    @app.errorhandler(500)
    def server_error(e):
        db.session.rollback()
        app.logger.exception("Unhandled error on %s", request.path)
        return render(500, "Something went wrong", "An unexpected error occurred. The team has been notified.")

    from flask_wtf.csrf import CSRFError

    @app.errorhandler(CSRFError)
    def csrf_error(e):
        return render(400, "Session expired", "Your form session expired. Reload the page and try again.")


def _register_security_headers(app):
    @app.after_request
    def headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'",
        )
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.endpoint and not request.endpoint.startswith("static"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response
