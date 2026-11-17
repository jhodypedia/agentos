# Deployment

A single host running Docker is the supported deployment target: a VPS, a bare
metal box, or a homelab machine. The stack is the same Compose project you run in
development, plus a Caddy reverse proxy that terminates TLS and closes the host
ports the API and dashboard would otherwise expose.

**Services on the host**

| Service | Role | Exposed |
|---|---|---|
| `proxy` | Caddy edge: TLS, routing, SSE, security headers | 80, 443 |
| `api` | FastAPI, owns database migrations | internal only |
| `web` | Next.js dashboard | internal only |
| `worker` | Claims and executes tasks from the durable queue | internal only |
| `bot` | Delivers Telegram notifications | internal only |
| `postgres` | PostgreSQL 16 with pgvector | internal only |
| `redis` | Event bus, rate limits, queue wakeups | internal only |

Only the proxy accepts connections. Everything else is reachable on the Compose
network by service name, which is why the API needs no CORS configuration and
`NEXT_PUBLIC_API_URL` is empty in a deployment — the dashboard calls its own
origin and the proxy routes `/api/*` to the API.

**Requirements**

- 2 vCPU / 4 GiB RAM minimum (4 / 8 with the sandbox image and research tooling).
  20 GiB of disk gives room for images, the database, workspaces and backups.
- Docker Engine 24+ with the Compose plugin.
- A DNS `A`/`AAAA` record pointing at the host if you want automatic HTTPS.
- Outbound HTTPS for model providers and Telegram.

## First deployment

```bash
git clone <repository> agentos && cd agentos
cp .env.example .env
```

Edit `.env` and set at minimum:

```bash
SECRET_KEY=$(openssl rand -hex 32)          # signs JWTs
ENCRYPTION_KEY=$(openssl rand -base64 32)   # encrypts stored provider keys
POSTGRES_PASSWORD=$(openssl rand -base64 24)
PUBLIC_HOST=agentos.example.com             # or the host's IP for plain HTTP
TLS_AUTO=1                                  # 0 for plain HTTP behind another proxy
ACME_EMAIL=you@example.com                  # required when TLS_AUTO=1
```

`ENCRYPTION_KEY` must be a Fernet key: 32 random bytes, base64-encoded. It is the
key that protects model-provider credentials in the database. Losing it means
re-entering every provider key; leaking it means rotating them. Store a copy in a
password manager before the first start.

Then:

```bash
scripts/deploy.sh preflight   # validates .env, Docker and disk space
scripts/deploy.sh up          # build and start everything
scripts/deploy.sh status      # health of every service
```

Create the owner account. Registration is closed by default, so the first user is
created out of band:

```bash
docker compose exec api agentos create-admin --email you@example.com
```

The command prints a generated password once. Sign in, change it, then seed the
platform's built-in content and connect a model provider in **Providers**.

```bash
docker compose exec api agentos seed
docker compose exec api agentos seed-skills
```

Open `https://<PUBLIC_HOST>` and check that the dashboard loads, the health badge
is green, and `/ops/status` shows real numbers.

## TLS

`TLS_AUTO=1` uses Caddy's automatic HTTPS: it obtains and renews certificates from
Let's Encrypt for `PUBLIC_HOST`, redirects HTTP to HTTPS, and sets HSTS. Nothing
else is needed as long as DNS points at the host and ports 80 and 443 are open —
Caddy needs port 80 for the ACME HTTP challenge.

Set `TLS_AUTO=0` when something in front of the host already terminates TLS: a
cloud load balancer, a corporate proxy, or Cloudflare in full mode. The stack then
serves plain HTTP on `PROXY_PORT` and expects `X-Forwarded-*` headers, which Caddy
passes through to the API.

`PROXY_CONFIG` selects the Caddyfile. Leave it alone unless you are customising
routing: the deploy script picks `deploy/Caddyfile` for plain HTTP and
`deploy/Caddyfile.tls` when `TLS_AUTO=1`.

## Updating

```bash
git pull
scripts/deploy.sh preflight
scripts/deploy.sh update
```

`update` rebuilds and recreates the API first, waits for it to report healthy, and
then recreates the worker, dashboard, bot and proxy. The API's entrypoint runs
`alembic upgrade head` before it serves traffic, so migrations are applied by the
one process that owns them; the worker and bot run with
`AGENTOS_RUN_MIGRATIONS=false` so two containers never race on the schema.

That ordering means a schema-changing release briefly serves API traffic on the
new schema while the old dashboard bundle is still cached in a browser. The API is
additive within a release; a destructive change (dropping a column) belongs in a
separate release after the code that reads it is gone.

Before updating, take a backup:

```bash
scripts/backup.sh          # writes backups/agentos-<timestamp>.tar.gz
```

## Backup and restore

```bash
scripts/backup.sh                                  # database, Redis, workspaces
scripts/restore.sh backups/agentos-2026-11-17.tar.gz
```

The backup manifest records the Alembic revision and the application version, so
a restore into a different release fails loudly instead of producing a database
the code cannot read. Restore into a fresh stack rather than over a running one.

Copy backups off the host. A backup on the same disk as the database does not
survive the disk.

## Operating

```bash
scripts/deploy.sh logs            # everything
scripts/deploy.sh logs api        # one service
docker compose exec api agentos queue            # queue depth by status
docker compose exec api agentos queue --reap     # recover tasks whose worker died
docker compose exec api agentos prune            # apply retention windows
docker compose exec api agentos rotate-encryption-key
```

The worker heartbeats every task it runs and renews its lease. A worker that dies
mid-task stops renewing; the lease expires after `WORKER_LEASE_SECONDS` (300 by
default) and `queue --reap` (or any running worker's periodic reap) puts the task
back on the queue. Run more than one worker for throughput — the claim query uses
`FOR UPDATE SKIP LOCKED`, so workers never take the same task.

**Metrics.** The API serves `/metrics` on its own port; the worker serves its own
on `WORKER_METRICS_PORT` (9101). The proxy returns 404 for `/metrics`, so metrics
are not public. Scrape them over the Compose network or from the host:

```bash
docker compose exec worker python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9101/metrics').read().decode())"
```

## Hardening

- Registration is closed by default (`ALLOW_REGISTRATION=false`); keep it closed
  and create accounts with `agentos create-admin`.
- Set `SANDBOX_ALLOW_HOST_EXECUTION=false` to refuse command execution outright
  when no container runtime is reachable, instead of falling back to the host.
  The Compose `api` has no Docker socket, so on this stack the honest choice is
  either to mount one deliberately or leave the fallback enabled and metered.
- Keep `METRICS_TOKEN` set if `/metrics` is reachable by anything but the scraper.
- The rate limiter trusts `X-Forwarded-For` from the proxy. Do not expose the API
  port directly, or a client can spoof its address and bypass the limit.
- Backups contain encrypted provider credentials and password hashes. Treat them
  as secrets; the encryption key is separate on purpose, so a stolen backup alone
  does not yield usable provider keys.
- `docker compose exec api agentos scan-secrets --path app` before publishing a
  fork or a snapshot of the repository.

## Troubleshooting

**The dashboard loads but every page is empty.** The browser is calling an API
origin it cannot reach. In a deployment `NEXT_PUBLIC_API_URL` must be empty so
requests go to the same origin; rebuild the web image if you changed it, because
the value is baked in at build time.

**`502` from the proxy.** The API is not up or not healthy:
`scripts/deploy.sh logs api`, then `curl localhost:8000/health/ready` inside the
network.

**Health checks fail on `bot`.** The bot idles when `TELEGRAM_BOT_TOKEN` is unset.
That is expected and it reports healthy; it only delivers once a token is
configured.

**Tasks stay `pending`.** No worker is running, or all of them are wedged. Check
`docker compose ps worker` and `agentos queue`; a task stuck in `running` with an
expired lease is recovered by `agentos queue --reap`.

**Migrations did not run.** `AGENTOS_RUN_MIGRATIONS=false` is set on the service
that should have run them. The API is the only service that should have it
unset.

**The certificate never issues.** DNS does not point at the host, or port 80 is
blocked. `docker compose logs proxy` shows the ACME error. Set `TLS_AUTO=0` and
put TLS in front of the host if the environment cannot pass the challenge.
