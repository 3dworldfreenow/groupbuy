# FreshCircle — group-buying marketplace for fresh produce

FreshCircle is a Flask web application. Neighbours form **buying groups**, pool their vegetable and fruit orders, and vendors make **sealed, transparently scored offers** to fulfil the combined demand. The platform records quality inspections, deliveries, complaints, refunds, commissions and profit.

It supports two business stages without a rewrite:

| Stage | Who supplies orders | How to switch |
|---|---|---|
| **1 — Founder-operated** | One platform-owned vendor account (the founder). It is always disclosed as "Platform-owned". | Default (`multi_vendor_enabled = off`) |
| **2 — Multi-vendor** | Any vendor approved by an administrator competes with sealed bids. | Admin → Settings → *Multi-vendor bidding* |

The founder's vendor account follows exactly the same rules as other vendors: same bid form, validation, commission, inspections and scoring. Conflict-of-interest controls:

* Administrators **cannot see bid values while bidding is open** — only the bid count.
* Administrators **cannot hand-pick a winner**. The organiser selects (organiser policy), members vote, or the highest published score wins. Admins can only *apply* the group's published rule.
* Every award stores an explanation, a score snapshot and the platform-owner disclosure, shown to members.
* Bid audit-log entries never contain prices.

---

## Contents

1. [Features](#features)
2. [Quick start (Ubuntu)](#quick-start-ubuntu)
3. [Manual installation, step by step](#manual-installation-step-by-step)
4. [Demo accounts and walkthrough](#demo-accounts-and-walkthrough)
5. [Running the tests](#running-the-tests)
6. [Production deployment](#production-deployment)
7. [Backups](#backups)
8. [Architecture](#architecture)
9. [Business rules](#business-rules)
10. [Financial model](#financial-model)
11. [Security](#security)
12. [Known limitations](#known-limitations)

---

## Features

**Customers:** register, log in and manage their profile and addresses; browse produce; create groups or join them by invite link or code; add quantities, with live aggregate progress; compare scored offers; vote or select; confirm their price breakdown; view payment instructions and receipts; track delivery; confirm receipt; report problems with photos; rate vendors.

**Vendors:** apply and upload verification documents (PDF or images); manage the catalogue with quantity-based price tiers; see eligible open orders; submit, revise or withdraw sealed bids; accept or decline awards; record batches or lots and self-check inspections; run packing, dispatch and delivery with proof of delivery; answer complaints; see settlements and commission statements.

**Administrators:** dashboard metrics calculated from real records; user and role management; vendor approval, rejection, suspension and reactivation; document review; products, categories, grades, locations and pickup points; group-order management (freeze, reopen or close bidding, apply the award rule, revoke an award, hold or resume, cancel, close); platform-verified quality inspections; delivery management; complaints and refunds; manual payment recording; cost records; settlements; CSV exports; platform settings; audit log; contact messages.

**Lifecycle (enforced server-side):**

`draft → collecting → frozen → bidding → selection → awarded → sourcing → quality_check → packed → dispatched → delivered → closed`, plus `on_hold` and `cancelled`.

Allowed transitions are defined in `app/constants.py` and checked on every change. `GroupOrder` uses optimistic version locking, and a partial unique index allows only one active award per order.

---

## Quick start (Ubuntu)

```bash
git clone <your-repo-url> groupbuy && cd groupbuy
./scripts/install_ubuntu.sh            # add --postgres to also install and configure PostgreSQL
. .venv/bin/activate
flask --app run.py create-admin --email you@example.com --name "Your Name"      # prompts for password
flask --app run.py create-founder-vendor --email founder@example.com --business-name "Your Produce Co"
flask --app run.py seed-demo           # optional demo data (prints a random password)
flask --app run.py run --debug         # open http://127.0.0.1:5000
```

The install script is safe to run again. It never overwrites an existing `.env`, database or uploads, and it never prints secrets.

---

## Manual installation, step by step

### 1. System packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip sqlite3
sudo apt install -y postgresql postgresql-client     # production database (optional for development)
```

You need Python 3.11 or newer. Ubuntu 22.04 ships 3.10, so use the deadsnakes PPA (`python3.11 python3.11-venv`) or Ubuntu 24.04.

### 2. Virtual environment

```bash
python3 -m venv .venv
. .venv/bin/activate
```

### 3. Dependencies

```bash
pip install -r requirements.txt          # pinned runtime dependencies
pip install -r requirements-dev.txt      # + pytest
```

### 4. Environment variables

```bash
cp .env.example .env && chmod 600 .env
python -c "import secrets; print(secrets.token_hex(32))"   # paste the output into SECRET_KEY
```

| Variable | Purpose |
|---|---|
| `FLASK_ENV` | `development`, `production` or `testing` |
| `SECRET_KEY` | Required in production (at least 32 characters) |
| `DATABASE_URL` | Empty means SQLite at `instance/groupbuy.db`. Production example: `postgresql://groupbuy:PASSWORD@localhost:5432/groupbuy` |
| `APP_TIMEZONE`, `DEFAULT_CURRENCY`, `CURRENCY_SYMBOL` | Market settings. Development defaults: `Asia/Kolkata`, `INR`, `₹` |
| `UPLOAD_FOLDER`, `MAX_UPLOAD_MB` | Private upload storage, outside `static/`, and the size limit per file |
| `BEHIND_PROXY` | Set to `1` behind Nginx or Caddy so `X-Forwarded-*` headers are trusted |
| `SESSION_COOKIE_SECURE` | `1` in production (HTTPS only) |
| `PAYMENT_WEBHOOK_SECRET` | Optional HMAC secret that enables the gateway webhook |

To use PostgreSQL:

```bash
sudo -u postgres createuser --pwprompt groupbuy
sudo -u postgres createdb -O groupbuy groupbuy
# then set DATABASE_URL in .env
```

### 5–6. Initialise the database and apply migrations

```bash
export FLASK_APP=run.py
flask db upgrade          # creates or upgrades the schema (Alembic migrations in migrations/)
flask seed-reference      # roles, grades, products, locations — idempotent
```

### 7. Create the first administrator

```bash
flask create-admin --email you@example.com --name "Your Name"
```

The password is prompted for with hidden input and confirmation. You can set `ADMIN_PASSWORD` for automation, but don't leave it in your shell history. Passwords must be at least 10 characters and mix letters with digits or symbols.

### 8. Create the founder (platform-owned) vendor

```bash
flask create-founder-vendor --email founder@example.com --business-name "Your Produce Co" --capacity 1000
```

The account is approved immediately, marked `is_platform_owned`, and disclosed on every offer it makes.

### 9. Sample products and orders

`flask seed-reference` loads the product catalogue. `flask seed-demo` additionally creates demo users, vendors and three group orders (see below). Demo data is refused in production unless you pass `--force`.

### 10. Development server

```bash
flask --app run.py run --debug       # or: python run.py
```

On Windows you can use Waitress: `waitress-serve --listen=127.0.0.1:8000 wsgi:app`.

### 11. Tests

```bash
python -m pytest
```

### 12–14. Production, HTTPS and backups

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

---

## Demo accounts and walkthrough

`flask seed-demo --password 'Choose-a-demo-pass1'` creates these accounts, all with the password you choose:

| Email | Role |
|---|---|
| `admin@example.com` | Administrator |
| `founder@example.com` | Founder vendor (platform-owned, approved) |
| `vendor2@example.com` | External vendor (approved) — bids once multi-vendor is enabled |
| `vendor3@example.com` | External vendor (pending approval) |
| `alice@example.com`, `bob@example.com`, `carol@example.com` | Customers |

It also creates three group orders:

1. **Lakeview weekly vegetables**: a *closed* order that went through the full Stage 1 workflow, so dashboards show real numbers.
2. **Green Park weekend vegetables**: *collecting*, with the sample demand of potatoes 100 kg + onions 60 kg + tomatoes 40 kg = **200 kg** from three households.
3. **Riverside fortnightly order**: *receiving offers* (member-vote policy) with the founder's sealed bid already submitted.

Sample prices are illustrative demo values only, not market prices.

Suggested walkthrough:

1. Log in as Alice, open Green Park and edit your quantities. As organiser, click **Close orders now**.
2. Log in as the founder and open **Available group orders** to submit an offer.
3. Log in as the admin and open **Group orders → #2 → Close bidding** (an early close requires a reason and is audit-logged).
4. As Alice, open **Compare offers** and select the offer; then confirm your order.
5. Log in as the founder: **Accept**, then **Ready for quality check**.
6. Log in as the admin: **Quality → Record inspection** (pass, or fail to see the hold).
7. As the founder: **Packed**, then **Dispatch**, then mark the deliveries delivered.
8. As the admin: record payments, resolve any complaint, then **Close** the order. The settlement and commission are created automatically.

To try Stage 2, turn on **Admin → Settings → Multi-vendor bidding** and bid as `vendor2@example.com`.

Deadlines are applied by `flask process-deadlines`, which you run every minute through the systemd timer or cron. Pages that show an order also apply expired deadlines when they are viewed.

---

## Running the tests

```bash
python -m pytest                                             # SQLite in-memory
TEST_DATABASE_URL=postgresql://user:pass@localhost/test_db python -m pytest   # against PostgreSQL
```

There are 53 tests. They cover:

* registration and login
* lockout
* open-redirect protection
* CSRF
* role permissions
* privacy of other customers' orders
* vendor approval
* group creation and membership
* aggregation (100 + 60 + 40 = 200 kg)
* per-customer limits and quantity steps
* over-ordering
* order cutoff
* freeze and minimums
* invalid status transitions
* price tiers and exact allocation
* bid submission, revision and withdrawal
* bid validation (full or partial quantities, capacity, expiry)
* sealed-bid privacy (customers, competing vendors, admins, audit log)
* scoring beyond price
* expired bids
* conflict-of-interest award rules
* duplicate awards
* a **real two-thread concurrent award race**
* group voting
* vendor declining an award
* failed inspection and hold
* the vendor self-check limits
* delivery success, failure and reschedule
* payment states
* refunds
* webhook signature validation
* browser-return safety
* commission and settlement maths
* upload validation and private file access
* CSV export authorization
* two **end-to-end HTTP workflows**: the Stage 1 single vendor, and Stage 2 multi-vendor auto-award

---

## Production deployment

Summary (details in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)):

```bash
sudo cp deploy/groupbuy.service deploy/groupbuy-deadlines.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now groupbuy groupbuy-deadlines.timer
```

Gunicorn listens on `127.0.0.1:8000` (see `deploy/gunicorn.conf.py`). Put a TLS-terminating reverse proxy in front of it. Example configurations for Nginx and Caddy are in `deploy/`.

## Backups

```bash
./scripts/backup.sh                 # pg_dump (or SQLite online backup) + uploads archive, 14-day retention
./scripts/restore.sh backups/db-YYYYmmdd-HHMMSS.dump backups/uploads-YYYYmmdd-HHMMSS.tar.gz
```

`restore.sh` asks you to confirm and takes a safety backup before it replaces anything.

---

## Architecture

```
app/
  __init__.py        application factory, security headers, error handlers
  config.py          Development / Testing / Production config from environment variables
  constants.py       roles, statuses, allowed state transitions
  models/            SQLAlchemy models (users, catalog, groups, bidding, operations, finance, support)
  services/          business logic — no Flask request code
    groups.py        groups, rounds, quantities, aggregation, freeze/validation
    pricing.py       Decimal pricing engine, tiers, proportional allocation
    bidding.py       eligibility, sealed bids, scoring, voting, award (transactional), revocation
    fulfilment.py    confirmation, batches, inspections, holds, packing, delivery (+ provider interface)
    payments.py      payment provider abstraction, manual payments, webhook verification, refunds
    finance.py       commissions, settlements, costs, metrics
    support.py       accounts, vendor verification, complaints, reviews, deadline processing
    settings.py      typed admin-configurable platform settings
    uploads.py       validated private uploads
  blueprints/        public, auth, customer, vendor, admin, files (authorized downloads), payments
  templates/         Jinja2 + Bootstrap 5 (vendored locally; strict CSP, no inline scripts)
  static/            css, js, vendored Bootstrap and Bootstrap Icons
migrations/          Alembic migrations (tested on SQLite and PostgreSQL 16)
scripts/             install_ubuntu.sh, backup.sh, restore.sh
deploy/              gunicorn.conf.py, systemd units/timer, Nginx and Caddy examples
tests/               pytest suite
```

Views stay thin. They call services, which raise `DomainError` with a message that is safe to show to the user. `run_action()` commits on success and rolls back and flashes on failure.

## Business rules

* **Ordering.** Members can change their quantities only while the order is collecting *and* before the deadline. Each change is checked against:
  * the minimum, the quantity step and the per-customer maximum
  * the group maximum. Item rows are locked with `SELECT … FOR UPDATE`.
* **Freeze.** When the deadline passes, or the organiser or an admin closes early:
  * items below their minimum are dropped and members are notified
  * orders left with no items are cancelled
  * if nothing qualifies, the round is cancelled.
* **Bidding.**
  * Only approved vendors can bid. In Stage 1 only the platform-owned vendor can bid.
  * The vendor must serve the group's location and declare a capacity, and the total offered quantity must fit that capacity.
  * A vendor cannot offer more than was ordered, and must offer the full quantity unless the round allows partial fulfilment, in which case the minimum fulfilment % applies.
  * The offered grade must meet each item's minimum grade.
  * Estimated delivery must fall within the delivery window, and the bid must stay valid long enough past the deadline.
* **Scoring.** The score is a weighted sum (admin-configurable; price can't be the only criterion) of:
  * total customer cost (adjusted for partial coverage)
  * quality (offered grade plus the vendor's pass rate in platform inspections)
  * on-time delivery history
  * cancellation history
  * verified ratings
  
  Vendors without history get a neutral 0.75.
* **Award policies.**
  * *Organiser selects*: the organiser picks any eligible offer.
  * *Member vote*: most votes wins; ties go to the higher score.
  * *Auto score*: the highest score wins at the deadline.
  
  Expired bids can never be awarded.
* **Confirmation.** After the award, each order is priced (allocation, a proportional share of the delivery fee, home-delivery fee, service fee) and the customer confirms it, which creates a payment request. Orders not confirmed by packing time are cancelled.
* **Quality.**
  * A passing **platform-verified** inspection is required before packing (configurable). Vendor self-checks are labelled *vendor-declared*.
  * A failed admin inspection can place the order on hold.
  * "Organic" is never shown unless the vendor has a *verified* organic certificate.
* **Closing.** An order can be closed only when it is delivered, every complaint is resolved, and either all customers have confirmed receipt or the complaint window has passed. Closing creates the commission and settlement records.

## Financial model

| Term | Definition |
|---|---|
| Completed order value (GMV) | What customers paid for completed orders. **Not revenue.** |
| Platform revenue | Commissions + customer service fees − refunds borne by the platform |
| Vendor payable | Goods + delivery fees − refunds charged to the vendor − commission |
| Founder-vendor margin | Founder vendor payable − founder purchase costs − spoilage |
| Estimated gross margin | Platform revenue + founder-vendor margin |
| Estimated operating profit | Gross margin − delivery, packaging and other operating costs |

All money uses `Decimal` and `NUMERIC(12,2)`; quantities use `NUMERIC(12,3)`. Currency is stored on every financial record. `DEFAULT_CURRENCY` sets the market (INR by default). Every financial change is written to the audit log.

## Security

* Passwords are hashed with Werkzeug scrypt. After 5 failed logins the account is locked for 15 minutes. Login errors are the same whether or not the account exists, so they don't reveal which accounts exist.
* Flask-WTF CSRF protection covers every POST. The only exception is the signature-verified payment webhook.
* Authorization is checked server-side on every route: role decorators plus object-level ownership checks. Other users' records return 404.
* Uploads are checked by extension *and* file signature, given random names, stored outside `static/`, and served only through `/files/...` after an authorization check.
* Security headers: strict CSP (`script-src 'self'`, with Bootstrap vendored locally), `X-Frame-Options: DENY`, nosniff, Referrer-Policy, HSTS when secure cookies are on, and `Cache-Control: no-store` on dynamic pages.
* An in-process rate limiter protects login, registration, contact, complaints, uploads and bids. Production configuration refuses to start without a strong `SECRET_KEY` and a `DATABASE_URL`.
* CSV exports neutralise spreadsheet-formula injection. The audit log records privileged actions with actor, IP and details.

## Known limitations

* **Payments.** No live payment gateway is integrated. Payments are recorded manually by an admin after verification. `HmacWebhookProvider` is a tested template for a gateway that signs its webhooks, but it must be adapted to a real provider's API, and the applicable payment regulations reviewed, before it handles real money. No wallet holds customer funds.
* **Notifications.** Notifications are in-app only (no email or SMS yet).
* **Delivery.** Delivery is coordinated manually. `DeliveryProvider` is the extension point for courier APIs. There is no mapping or routing.
* **Rate limiting.** Limits are counted per process. With several Gunicorn workers, also apply proxy-level limits (see the Nginx example). Account lockout is stored in the database, so it applies across all workers.
* **Deadlines.** Deadline processing runs every minute (systemd timer) and also when a page is viewed. It is not real-time to the second.
* **Legal text.** The Privacy Policy and Terms are templates and need legal review before launch.
* **Product images.** Product imagery uses emoji icons. Product photo uploads are not implemented.
