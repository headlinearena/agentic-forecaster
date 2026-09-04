#!/bin/sh
set -eu

ENV_FILE="${1:?usage: pg_backup.sh <.env.dev|.env.prod> <backup-dir>}"
BACKUP_DIR="${2:?usage: pg_backup.sh <.env.dev|.env.prod> <backup-dir>}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_FILE="$BACKUP_DIR/postgres-${STAMP}.sql.gz"
TMP_FILE="$BACKUP_DIR/.postgres-${STAMP}.sql.tmp"

if ! docker compose --env-file "$ENV_FILE" exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$TMP_FILE"; then
  rm -f "$TMP_FILE"
  echo "pg_backup.sh: pg_dump failed for $ENV_FILE; no backup written" >&2
  exit 1
fi

gzip < "$TMP_FILE" > "$OUT_FILE"
rm -f "$TMP_FILE"

echo "Wrote $OUT_FILE"

find "$BACKUP_DIR" -type f -name 'postgres-*.sql.gz' -mtime +30 -delete
