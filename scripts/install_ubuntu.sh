#!/usr/bin/env bash
# Install FreshCircle on Ubuntu (22.04/24.04).
#
#   ./scripts/install_ubuntu.sh                 # local development with SQLite
#   ./scripts/install_ubuntu.sh --postgres      # also install PostgreSQL and create a database
#   ./scripts/install_ubuntu.sh --no-apt        # skip system packages (already installed)
#
# Safe to re-run: it never overwrites an existing .env, database or uploads,
# and never prints secrets.
set -euo pipefail

cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"
WITH_POSTGRES=0
USE_APT=1
for arg in "$@"; do
  case "$arg" in
    --postgres) WITH_POSTGRES=1 ;;
    --no-apt) USE_APT=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

log() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

if [ "$USE_APT" -eq 1 ]; then
  log "Installing system packages"
  $SUDO apt-get update -y
  $SUDO apt-get install -y python3 python3-venv python3-pip sqlite3 ca-certificates
  if [ "$WITH_POSTGRES" -eq 1 ]; then
    $SUDO apt-get install -y postgresql postgresql-client
  fi
fi

PY_OK=$(python3 -c 'import sys; print(int(sys.version_info >= (3, 11)))')
if [ "$PY_OK" != "1" ]; then
  echo "Python 3.11 or newer is required (found $(python3 --version))." >&2
  exit 1
fi

log "Creating virtual environment (.venv)"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

mkdir -p instance backups
chmod 750 instance backups

ENV_CREATED=0
if [ ! -f .env ]; then
  log "Creating .env with a new random SECRET_KEY"
  umask 077
  cp .env.example .env
  SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET}|" .env
  ENV_CREATED=1
  chmod 600 .env
else
  log ".env already exists — leaving it unchanged"
fi

if [ "$WITH_POSTGRES" -eq 1 ]; then
  log "Configuring PostgreSQL"
  $SUDO systemctl enable --now postgresql >/dev/null 2>&1 || $SUDO service postgresql start || true
  DB_EXISTS=$($SUDO -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='groupbuy'" || true)
  if [ "$DB_EXISTS" != "1" ]; then
    if [ "$ENV_CREATED" -ne 1 ] && grep -qE '^DATABASE_URL=.+' .env; then
      echo "DATABASE_URL is already set in .env; not creating a new database." >&2
    else
      DB_PASS=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
      $SUDO -u postgres psql -qc "CREATE ROLE groupbuy LOGIN PASSWORD '${DB_PASS}'" 2>/dev/null || \
        $SUDO -u postgres psql -qc "ALTER ROLE groupbuy LOGIN PASSWORD '${DB_PASS}'"
      $SUDO -u postgres createdb -O groupbuy groupbuy
      sed -i "s|^DATABASE_URL=.*|DATABASE_URL=postgresql://groupbuy:${DB_PASS}@localhost:5432/groupbuy|" .env
      echo "Database 'groupbuy' created; credentials were written to .env (not displayed)."
    fi
  else
    echo "Database 'groupbuy' already exists — leaving it unchanged."
  fi
fi

log "Applying database migrations (non-destructive)"
export FLASK_APP=run.py
flask db upgrade

log "Loading reference data (idempotent: roles, grades, products, locations)"
flask seed-reference

cat <<EOF

Installation complete.

Next steps:
  . .venv/bin/activate
  flask --app run.py create-admin --email you@yourdomain.com --name "Your Name"
  flask --app run.py create-founder-vendor --email founder@yourdomain.com --business-name "Your Produce Co"
  flask --app run.py seed-demo            # optional demo data (development only)
  flask --app run.py run --debug          # http://127.0.0.1:5000
  python -m pytest                        # run the automated tests (pip install -r requirements-dev.txt)

Production: see docs/DEPLOYMENT.md (Gunicorn + systemd + HTTPS).
EOF
