"""Blueprint registration."""


def register_blueprints(app):
    from .admin import bp as admin_bp
    from .auth import bp as auth_bp
    from .customer import bp as customer_bp
    from .files import bp as files_bp
    from .payments import bp as payments_bp
    from .public import bp as public_bp
    from .vendor import bp as vendor_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(customer_bp)
    app.register_blueprint(vendor_bp, url_prefix="/vendor")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(files_bp, url_prefix="/files")
    app.register_blueprint(payments_bp, url_prefix="/payments")
