#!/usr/bin/env bash
# Ежедневный бэкап SQLite. Храним только 7 последних копий.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/funpay-helper}"
DATA_DIR="${DATA_DIR:-$APP_DIR/data}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
DB_PATH="${DB_PATH:-$DATA_DIR/funpay.db}"

mkdir -p "$BACKUP_DIR"
if [[ ! -f "$DB_PATH" ]]; then
  echo "База $DB_PATH не найдена — бэкап пропущен"
  exit 0
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="$BACKUP_DIR/funpay-$STAMP.db"

# sqlite3 .backup консистентен даже при активной записи ботом.
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$DB_PATH" ".backup '$TARGET'"
else
  python3 - "$DB_PATH" "$TARGET" <<'PY'
import sqlite3
import sys

source, target = sys.argv[1], sys.argv[2]
with sqlite3.connect(source) as conn:
    with sqlite3.connect(target) as backup:
        conn.backup(backup)
PY
fi

find "$BACKUP_DIR" -maxdepth 1 -name 'funpay-*.db' -type f -printf '%T@ %p\n' \
  | sort -nr \
  | tail -n +8 \
  | cut -d' ' -f2- \
  | xargs -r rm -f

echo "Бэкап создан: $TARGET"
