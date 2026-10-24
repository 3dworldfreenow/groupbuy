#!/usr/bin/env bash
# Restore a database backup (and optionally uploads) created by backup.sh.
#
#   ./scripts/restore.sh backups/db-20250101-120000.dump [backups/uploads-20250101-120000.tar.gz]
#
# Stop the application first (sudo systemctl stop groupbuy). The current
# database is backed up automatically before it is replaced.
set -euo pipefail
cd "$(dirname "$0")/.."
DB_BACKUP="${1:?Usage: restore.sh <db-backup> [uploads-archive]}"
UPLOADS_BACKUP="${2:-}"
[ -f "$DB_BACKUP" ] || { echo "Not found: $DB_BACKUP" >&2; exit 1; }
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python

read -r -p "This REPLACES the current database. Type 'restore' to continue: " ANSWER
[ "$ANSWER" = "restore" ] || { echo "Aborted."; exit 1; }

echo "Taking a safety backup of the current state first..."
./scripts/backup.sh backups/pre-restore

read -r DB_URL UPLOADS < <($PY - <<'EOF'
import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.getcwd(), ".env"))
from app.config import INSTANCE_DIR
url = os.environ.get("DATABASE_URL") or "sqlite:///" + os.path.join(INSTANCE_DIR, "groupbuy.db")
uploads = os.environ.get("UPLOAD_FOLDER") or os.path.join(INSTANCE_DIR, "uploads")
print(url.replace("postgres://", "postgresql://", 1), uploads)
EOF
)
if [ -z "${DB_URL:-}" ]; then echo "Could not read the database configuration." >&2; exit 1; fi

case "$DB_URL" in
  postgresql*)
    PG_URL=$(echo "$DB_URL" | sed -E 's#^postgresql\+[a-z0-9]+://#postgresql://#')
    pg_restore --clean --if-exists --no-owner --dbname="$PG_URL" "$DB_BACKUP"
    ;;
  sqlite*)
    DB_FILE="${DB_URL#sqlite:///}"
    $PY - "$DB_BACKUP" "$DB_FILE" <<'EOF'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
EOF
    ;;
esac
echo "Database restored from $DB_BACKUP"

if [ -n "$UPLOADS_BACKUP" ]; then
  mkdir -p "$UPLOADS"
  tar -xzf "$UPLOADS_BACKUP" -C "$UPLOADS"
  echo "Uploads restored into $UPLOADS"
fi
echo "Run 'flask --app run.py db upgrade' if the backup is from an older version, then start the app."
