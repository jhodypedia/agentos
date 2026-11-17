#!/usr/bin/env bash
# Restore an AgentOS backup produced by scripts/backup.sh.
#
# Usage: scripts/restore.sh <backup.tar.gz>
#
# This overwrites the database, Redis and the workspace volume, so it asks for
# confirmation by name and refuses to run without it. The old schema is not
# dropped first: the dump is taken with --clean --if-exists so it replaces what
# it contains, and `alembic upgrade head` afterwards covers a backup taken before
# a later migration.
set -euo pipefail

FILE="${1:?usage: scripts/restore.sh <backup.tar.gz>}"
if [[ ! -f "$FILE" ]]; then
  echo "No such file: $FILE" >&2
  exit 1
fi

echo "Backup contents:"
tar -tzf "$FILE"

read -r -p "This overwrites the current database, Redis and workspaces. Type 'restore' to continue: " answer
[[ "$answer" == "restore" ]] || { echo "Aborted"; exit 1; }

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
tar -C "$WORK_DIR" -xzf "$FILE"

if [[ -f "$WORK_DIR/manifest.env" ]]; then
  echo "Backup manifest:"
  sed 's/^/  /' "$WORK_DIR/manifest.env"
fi

POSTGRES_USER="${POSTGRES_USER:-agentos}"
POSTGRES_DB="${POSTGRES_DB:-agentos}"

echo "Stopping API and web..."
docker compose stop api web

echo "Stopping Redis before replacing its data..."
docker compose stop redis

echo "Restoring PostgreSQL..."
gunzip -c "$WORK_DIR/postgres.sql.gz" | docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"

echo "Restoring Redis data..."
docker compose run --rm --no-deps -T --entrypoint sh redis \
  -c 'rm -rf /data/* && tar -C /data -xzf -' < "$WORK_DIR/redis-data.tar.gz"

echo "Restoring workspaces..."
docker compose run --rm --no-deps -T --entrypoint sh api \
  -c 'rm -rf /data/workspaces/* && tar -C /data/workspaces -xzf -' < "$WORK_DIR/workspaces.tar.gz"

echo "Starting services and applying migrations..."
docker compose start redis
docker compose start api web
docker compose exec -T api alembic upgrade head

echo "Restore complete. Check /health/ready before sending traffic back."
