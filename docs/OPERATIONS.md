# Operations runbook

Day-two procedures for a running AgentOS deployment. Everything here uses
commands that exist in this repository; where a step is manual, it says so.

For installing, upgrading and configuring a host, see
[DEPLOYMENT.md](DEPLOYMENT.md). This file assumes the stack is already running.

## 1. Health and status

```bash
curl -s localhost:8000/health/live     # process is up
curl -s localhost:8000/health/ready    # Postgres and Redis reachable
```

`/health/ready` returns `{"status":"ok","components":{"database":"ok","redis":"ok"}}`
or a non-200 with the failing component named. Liveness deliberately checks only
the process: a restart loop caused by a transient database outage is worse than
serving 503s.

For a full picture without shell access to the host:

```bash
curl -s -H "Authorization: Bearer $TOKEN" localhost:8000/api/v1/ops/status | jq
```

It reports the resolved sandbox mode, workspace bytes against quota, filesystem
free/total, task and approval counts, 24-hour model calls/tokens/cost, and
database/Redis health. The dashboard's **Logs** page renders the same data.

## 2. Metrics

`GET /metrics` is Prometheus text format 0.0.4. It is not under `/api/v1` and
does not take a JWT (a scraper cannot hold one). Either set `METRICS_TOKEN` and
present it as a bearer token, or keep the port off the public network. `ops/status`
reports which mode is in effect as `metrics_token_required`.

```yaml
scrape_configs:
  - job_name: agentos
    authorization:
      credentials: <METRICS_TOKEN>
    static_configs:
      - targets: ["agentos.internal:8000"]
```

Series worth alerting on:

| Series | Meaning |
| --- | --- |
| `agentos_http_requests_total{status=~"5.."}` | server errors by route |
| `agentos_http_request_duration_seconds` (histogram) | latency; p95 per route |
| `agentos_http_requests_in_flight` | concurrency; a flat high value is a stall |
| `agentos_sandbox_fallbacks_total{reason}` | commands running outside the sandbox |
| `agentos_workspace_quota_rejected_total` | an agent hit its disk ceiling |
| `agentos_tool_runs_total{outcome="refused"}` | tool permission denials |
| `agentos_approvals_pending` | queue depth needing a human |
| `agentos_tokens_total` / `agentos_cost_total_usd` | spend by model |
| `agentos_background_jobs_total{outcome="error"}` | failing maintenance work |

State gauges are recomputed from the database at scrape time, so a restart cannot
leave a stale number behind.

## 3. Logs

Structured, one line per request, with `request_id` on every record. `trace_id`
is set when the caller sends a `traceparent` header; both are echoed on the
response as `X-Request-ID` and `X-Trace-ID`, so a user-reported failure can be
found by id.

Requests slower than `SLOW_REQUEST_MS` are logged at WARNING with their duration,
which is usually enough to find a regression without a profiler.

For application-level history (who did what, which task failed and why) use
`/api/v1/ops/logs` or the dashboard **Logs** page: one merged timeline over task
events and the audit log, filterable by level, source, task, agent and time.

## 4. Rate limiting

Defaults: 240 requests/minute per identity, 10 per 5 minutes for
`/auth/login` and `/auth/password/reset`. Counters live in Redis so they are
shared across API replicas.

- A client hitting a limit gets `429 rate_limited` with `Retry-After`.
- If Redis is unreachable, the limiter falls back to a per-process memory counter
  and logs `rate_limiter_degraded`. Limits still apply, just per replica. Treat
  that log line as an alert: it means Redis is down and limits are weaker than
  configured.
- Behind a reverse proxy, set `TRUST_FORWARDED_FOR=true` **only** if that proxy
  overwrites the header. Otherwise every client looks like the proxy, or a client
  can choose its own bucket.

Identity is the bearer-token subject when present, else the client IP, so one
user's abuse does not throttle another user behind the same NAT.

## 5. Sandbox state

`ops/status` → `sandbox.mode` is the answer to "are commands sandboxed?":

- `docker` — Docker is available and the image exists; commands run with
  `--network=none`, a read-only root, a bounded `/tmp` tmpfs and memory/CPU/PID
  limits.
- `host` — Docker or the image is unavailable and host execution is allowed.
  Commands run in the API process with a scrubbed environment. This is the
  Compose default, because the API container has no Docker socket.

The fallback is metered (`agentos_sandbox_fallbacks_total{reason}`), never
silent. Set `SANDBOX_ALLOW_HOST_EXECUTION=false` to refuse command execution
instead of falling back. Every command is clamped to
`SANDBOX_MAX_TIMEOUT_SECONDS`.

To actually use the sandbox in Compose, mount a Docker socket into the `api`
service or run command execution in a dedicated worker. Mounting the socket
widens the blast radius of an API compromise to the host daemon — that is why it
is not the default.

## 6. Backup

```bash
make backup                     # or: scripts/backup.sh /mnt/backups
```

Writes `backups/agentos-<UTC stamp>.tar.gz` containing:

- `postgres.sql.gz` — `pg_dump --clean --if-exists`
- `redis-data.tar.gz` — the Redis dataset after `BGSAVE`
- `workspaces.tar.gz` — the agent workspace volume
- `manifest.env` — timestamp, database name, version, Alembic head

All three are needed. A database-only dump loses task events (Redis Streams) and
every agent's files.

**Keep `ENCRYPTION_KEY` outside the backup.** Provider API keys in the dump are
encrypted with it: without the key the dump cannot restore them, and with the key
the dump is a full credential disclosure. Store it in a secret manager.

The tarball is not encrypted. Encrypt it before it leaves the host
(`age`, `gpg`, or the storage layer's own encryption).

## 7. Restore

```bash
make restore f=backups/agentos-20261117T020000Z.tar.gz
```

The script prints the archive contents and the manifest, then requires typing
`restore`. It stops the API and web, stops Redis before replacing its data,
restores all three pieces, restarts, and runs `alembic upgrade head` so a backup
taken before a later migration still lands on the current schema.

Test this on a throwaway stack before you need it. A backup that has never been
restored is a hypothesis, not a backup.

## 8. Key rotation

Provider credentials are encrypted with `ENCRYPTION_KEY`. To change it without
losing them:

```bash
# 1. generate a new key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 2. .env: put the NEW key in ENCRYPTION_KEY, the OLD one in ENCRYPTION_KEY_PREVIOUS
# 3. restart the API (both keys are now readable)

# 4. re-encrypt everything under the new key
make rotate-keys             # or: uv run agentos rotate-encryption-key --dry-run first

# 5. verify a provider call still authenticates, then remove ENCRYPTION_KEY_PREVIOUS
```

The command reports `rotated` / `unchanged` / `failed`. `failed` means no
configured key could decrypt that row — the row is left untouched rather than
overwritten, so put the correct old key back and re-run. Re-running is safe:
"already current" is decided by asking the primary key to decrypt, not by
comparing ciphertexts (Fernet output is randomised).

## 9. Retention

```bash
make prune dry=1     # report what would be deleted
make prune           # delete
```

Windows come from `RETENTION_TASK_EVENTS_DAYS` (30), `RETENTION_AUDIT_DAYS`
(180) and `RETENTION_MODEL_USAGE_DAYS` (365); `0` keeps a table forever. Nothing
prunes automatically — schedule it with the platform's automation mechanism, not
an in-process timer that stops with the container.

Take a backup before the first real prune.

## 10. Secret scan

```bash
make scan-secrets
```

Matches 13 credential shapes (AWS, GitHub, Slack, Telegram, OpenAI/Anthropic,
Stripe, Google, JWT, private-key blocks, database URLs with inline passwords, and
a generic `password = "…"` heuristic) across the source tree, skipping `.git`,
`node_modules`, virtualenvs and binaries. Matches are printed redacted, so the
output is safe to paste into a ticket. Use `--fail-on-findings` in CI.

## 11. Load testing

```bash
make loadtest email=admin@example.com password='...'
# or: uv run python scripts/loadtest.py --requests 400 --concurrency 20
```

Reports error rate, mean, p50/p95/p99 and max latency plus a status histogram,
computed from the responses actually received. It only issues GETs — a load test
that creates tasks spends real model budget.

Run it before and after a change; compare p95 and error rate, not just the mean.

## 12. Queue and worker

```bash
docker compose ps worker                       # is a worker running?
docker compose exec api agentos queue          # depth by status
docker compose exec api agentos queue --reap   # recover tasks whose worker died
```

A task is claimed with a lease (`WORKER_LEASE_SECONDS`, 300 by default) and the
worker renews it while it runs. A worker that is killed mid-task stops renewing;
once the lease expires the task can be reclaimed by any worker, or immediately by
`--reap`. A task stuck in `running` with a future lease is being worked on — check
`docker compose logs worker` before intervening.

Tasks can also be executed inline with `POST /api/v1/tasks/{id}/run`, which is
the development path; in a deployment let the worker do it so the request does not
hold a connection for the length of a model run.

The worker exposes its own metrics on `WORKER_METRICS_PORT` (9101):

```bash
docker compose exec worker python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9101/metrics').read().decode())"
```

`agentos_background_jobs_total{kind="task",worker=...}` counts executions and
`agentos_task_duration_seconds` records how long they took.

## 13. Incident checklist

1. `/health/ready` — which component is down?
2. `/api/v1/ops/status` — sandbox mode, disk headroom, queue depth.
3. `/metrics` — error rate by route, latency p95, in-flight requests.
4. Logs by `request_id` from the failing request, or `trace_id` if the caller
   propagates one.
5. If Redis is down: expect `rate_limiter_degraded` and weaker limits; the event
   bus and SSE stream stop. Restore Redis before tuning anything else.
6. If the workspace filesystem is full: `workspace_filesystem_free_bytes` in
   `ops/status`. Prune workspaces of finished tasks, or raise the volume — the
   quota protects against one agent, not against many.
7. If commands are unexpectedly on the host: `agentos_sandbox_fallbacks_total`.
   Either fix Docker access or set `SANDBOX_ALLOW_HOST_EXECUTION=false` to fail
   loudly instead.
