#!/usr/bin/env bash
# Back up the database and uploaded files.
#
#   ./scripts/backup.sh [backup_dir]       (default: ./backups)
#
# PostgreSQL: pg_dump custom format. SQLite: consistent online copy via the
# SQLite backup API. Uploads are archived alongside. Backups older than
# BACKUP_RETENTION_DAYS (default 14) are removed.
set -euo pipefail
cd "$(dirname "$0")/.."
DEST="${1:-backups}"
mkdir -p "$DEST"
chmod 700 "$DEST"
umask 077
STAMP=$(date +%Y%m%d-%H%M%S)
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python

# Resolve settings from .env without printing secrets.
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
    # pg_dump does not understand the SQLAlchemy driver suffix (+psycopg)
    PG_URL=$(echo "$DB_URL" | sed -E 's#^postgresql\+[a-z0-9]+://#postgresql://#')
    OUT="$DEST/db-$STAMP.dump"
    pg_dump --format=custom --no-owner --dbname="$PG_URL" --file="$OUT"
    ;;
  sqlite*)
    DB_FILE="${DB_URL#sqlite:///}"
    OUT="$DEST/db-$STAMP.sqlite3"
    $PY - "$DB_FILE" "$OUT" <<'EOF'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
EOF
    ;;
  *) echo "Unsupported DATABASE_URL scheme" >&2; exit 1 ;;
esac
echo "Database backup: $OUT"

if [ -d "$UPLOADS" ]; then
  tar -czf "$DEST/uploads-$STAMP.tar.gz" -C "$UPLOADS" .
  echo "Uploads backup:  $DEST/uploads-$STAMP.tar.gz"
fi

find "$DEST" -maxdepth 1 -type f \( -name 'db-*' -o -name 'uploads-*' \) \
  -mtime +"${BACKUP_RETENTION_DAYS:-14}" -delete
