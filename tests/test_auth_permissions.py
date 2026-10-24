"""Registration, login, role permissions, privacy of records, CSV export authorization."""
import pytest

from app import create_app
from app.extensions import db as _db
from app.models import User
from tests.conftest import PASSWORD, login


def test_registration_creates_customer_with_hashed_password(client, db):
    r = client.post("/register", data={"full_name": "New Person", "email": "New@Example.com",
                                       "phone": "+91 99999 88888", "password": "Sup3r-secret!",
                                       "confirm": "Sup3r-secret!", "accept_terms": "y"})
    assert r.status_code == 302 and r.location.endswith("/dashboard")
    u = db.session.query(User).filter_by(email="new@example.com").one()
    assert u.has_role("customer") and not u.has_role("admin")
    assert u.password_hash != "Sup3r-secret!" and u.check_password("Sup3r-secret!")


def test_registration_rejects_weak_password_and_duplicates(client, f):
    f.customer()
    r = client.post("/register", data={"full_name": "X Y", "email": "user1@example.com", "phone": "+91 99999 88888",
                                       "password": "Sup3r-secret!", "confirm": "Sup3r-secret!", "accept_terms": "y"},
                    follow_redirects=True)
    assert b"already exists" in r.data
    r = client.post("/register", data={"full_name": "X Y", "email": "z@example.com", "phone": "+91 99999 88888",
                                       "password": "aaaaaaaaaaaa", "confirm": "aaaaaaaaaaaa", "accept_terms": "y"},
                    follow_redirects=True)
    assert b"mix letters" in r.data


def test_login_logout_and_lockout(client, f, db):
    u = f.customer()
    assert login(client, u.email, "wrong-password").status_code == 200
    r = login(client, u.email)
    assert r.status_code == 302
    assert client.get("/dashboard").status_code == 200
    client.post("/logout")
    assert client.get("/dashboard").status_code == 302
    for _ in range(5):
        login(client, u.email, "wrong-password")
    r = client.post("/login", data={"email": u.email, "password": PASSWORD}, follow_redirects=True)
    assert b"temporarily locked" in r.data


def test_login_rejects_open_redirect(client, f):
    u = f.customer()
    r = client.post("/login?next=https://evil.example.com/x", data={"email": u.email, "password": PASSWORD})
    assert "evil.example.com" not in r.location


def test_role_permissions(client, f):
    customer, admin = f.customer(), f.admin()
    vendor = f.vendor()
    assert client.get("/admin/").status_code == 302          # anonymous → login
    login(client, customer.email)
    assert client.get("/admin/").status_code == 403
    assert client.get("/vendor/dashboard").status_code == 403
    assert client.post("/admin/settings", data={}).status_code == 403
    client.post("/logout")
    login(client, vendor.user.email)
    assert client.get("/vendor/dashboard").status_code == 200
    assert client.get("/admin/users").status_code == 403
    assert client.get("/groups/new").status_code == 403        # vendors are not customers
    client.post("/logout")
    login(client, admin.email)
    assert client.get("/admin/").status_code == 200


def test_customers_cannot_see_other_customers_orders(client, scenario, db):
    bob_order = [co for co in scenario["go"].customer_orders if co.user_id == scenario["bob"].id][0]
    login(client, scenario["alice"].email)
    assert client.get(f"/orders/{bob_order.id}").status_code == 404
    assert client.get(f"/orders/{bob_order.id}/receipt").status_code == 404
    r = client.get(f"/group-orders/{scenario['go'].id}")
    assert r.status_code == 200
    assert b"Bob" in r.data                      # member names are visible ...
    assert b"Flat" not in r.data                 # ... but never addresses


def test_non_members_cannot_view_group_order(client, scenario, f):
    outsider = f.customer()
    login(client, outsider.email)
    assert client.get(f"/group-orders/{scenario['go'].id}").status_code == 403
    assert client.get(f"/group-orders/{scenario['go'].id}/my-order").status_code == 403


def test_csv_export_authorization(client, f):
    customer, admin = f.customer(), f.admin()
    vendor = f.vendor()
    assert client.get("/admin/reports/payments.csv").status_code == 302
    login(client, customer.email)
    assert client.get("/admin/reports/payments.csv").status_code == 403
    client.post("/logout")
    login(client, vendor.user.email)
    assert client.get("/admin/reports/settlements.csv").status_code == 403
    client.post("/logout")
    login(client, admin.email)
    r = client.get("/admin/reports/payments.csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert client.get("/admin/reports/unknown.csv").status_code == 404


def test_csrf_protection_enabled_outside_tests(tmp_path):
    app = create_app("testing", WTF_CSRF_ENABLED=True, UPLOAD_FOLDER=str(tmp_path))
    with app.app_context():
        _db.create_all()
        r = app.test_client().post("/login", data={"email": "a@b.co", "password": "x"})
        assert r.status_code == 400
        _db.drop_all()


def test_production_config_requires_secrets(monkeypatch):
    from app.config import ProductionConfig
    monkeypatch.setattr(ProductionConfig, "SECRET_KEY", None)
    with pytest.raises(RuntimeError):
        ProductionConfig.validate()


def test_security_headers(client):
    r = client.get("/")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
