#!/usr/bin/env bash
# Back up AgentOS: PostgreSQL, Redis and the agent workspace volume.
#
# Usage: scripts/backup.sh [output-dir]
#
# A database-only dump is an incomplete restore: task events live in Redis
# Streams and every agent's files live in the workspace volume. The tarball this
# writes contains all three, so scripts/restore.sh can put a machine back the way
# it was.
set -euo pipefail

OUT_DIR="${1:-$(pwd)/backups}"
mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

POSTGRES_USER="${POSTGRES_USER:-agentos}"
POSTGRES_DB="${POSTGRES_DB:-agentos}"

echo "Dumping PostgreSQL..."
docker compose exec -T postgres \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists \
  | gzip -9 > "$WORK_DIR/postgres.sql.gz"

echo "Snapshotting Redis..."
# BGSAVE then copy the persistence files: copying a live AOF can capture a
# half-written tail, and Redis truncates that on load rather than repairing it.
docker compose exec -T redis redis-cli BGSAVE >/dev/null
for _ in $(seq 1 30); do
  if docker compose exec -T redis redis-cli LASTSAVE | grep -qE '^[0-9]+$'; then
    break
  fi
  sleep 1
done
docker compose exec -T redis sh -c 'tar -C /data -czf - .' > "$WORK_DIR/redis-data.tar.gz"

echo "Archiving workspace volume..."
docker compose run --rm --no-deps --entrypoint sh -T api \
  -c 'tar -C /data/workspaces -czf - .' > "$WORK_DIR/workspaces.tar.gz"

echo "Writing manifest..."
{
  echo "created_at=$STAMP"
  echo "postgres_db=$POSTGRES_DB"
  echo "agentos_version=$(docker compose exec -T api agentos version 2>/dev/null | tail -1 || echo unknown)"
  echo "alembic_head=$(docker compose exec -T api alembic current 2>/dev/null | tail -1 || echo unknown)"
} > "$WORK_DIR/manifest.env"

FILE="$OUT_DIR/agentos-$STAMP.tar.gz"
tar -C "$WORK_DIR" -czf "$FILE" .
echo "Backup written: $FILE"

cat <<'EOF'

Restore with: scripts/restore.sh <file>
Keep ENCRYPTION_KEY somewhere separate and durable: provider API keys in the
database are encrypted with it, and a backup without the key cannot recover them.
EOF
