# AgentOS — Autonomous Bounty AI Platform

Self-hosted, operator-controlled multi-agent AI platform for bounty research,
Make.fun competitions, software development and **authorized** security
research.

> **Status: Phase 9 dashboard delivered.** The foundation, the agent runtime, the
> orchestrator, the skill/memory systems, the coding work surface and the
> make.fun bounty integration are implemented and verified: repository layout,
> typed configuration, security primitives, the full relational schema (33
> tables) with migrations, an async FastAPI service with authentication/API
> keys, health probes, a Redis event bus, Docker packaging, eight model-provider
> adapters, a permission-checked tool layer with 21 built-ins, the
> provider-agnostic agent loop with budgets and cancellation, task execution that
> persists events, tokens and cost, a multi-agent orchestrator with LLM planning,
> a validated execution DAG, parallel scheduling with retries, 15 seeded
> specialists, child-task delegation with handoffs, resumable runs and human
> approvals, SKILL.md packages loaded on demand, a six-layer memory system with
> hybrid retrieval, deduplication, versioning and pgvector search, a confined
> project workspace with a command sandbox and coding engines, and a make.fun
> integration that mirrors the competition board, reads its published
> requirements and gates submission on a recorded human approval. The security
> research workflow adds scopes, evidence and independent review, Telegram adds a
> bot surface with an approval gate, and a Next.js dashboard now exposes every
> one of those surfaces, with a live event feed — see
> [docs/PHASES.md](docs/PHASES.md).

---

## What exists today

| Area | State |
| --- | --- |
| Repository structure, tooling, env management | Implemented |
| PostgreSQL schema + Alembic migration + indexes + FKs | Implemented (34 tables) |
| Async SQLAlchemy session layer | Implemented |
| Config validation + production hardening | Implemented |
| Structured JSON/console logging with request IDs | Implemented |
| Argon2 passwords, JWT, encrypted provider secrets, hashed API keys | Implemented |
| Login, current user, API key create/list/revoke | Implemented |
| RBAC roles + API-key scopes | Implemented |
| Health/readiness probes, service metadata | Implemented |
| Redis event bus (Stream + Pub/Sub) | Implemented |
| Docker image, Compose stack (api/postgres/redis), health checks | Implemented |
| Model provider adapters (OpenAI, Anthropic, Gemini, OpenRouter, local, compatible, Responses API) | Implemented |
| Agent runtime loop with budgets, cancellation and tool broker | Implemented |
| Tool layer with validation, permissions, risk gating and SSRF guard | Implemented |
| Provider, agent, tool and task APIs (run, cancel, events) | Implemented |
| Orchestrator, LLM planning, DAG scheduling, approvals | Implemented |
| SKILL.md loader, builtin skills, on-demand injection, assignment | Implemented |
| Memory layers, hybrid retrieval, dedup, versioning, pgvector search | Implemented |
| Project workspaces with path confinement and file tools | Implemented |
| Command sandbox (Docker image + host fallback with scrubbed env) | Implemented |
| Coding engines (OpenCode adapter, shell) and session history | Implemented |
| Human approval gate for high-risk tools, with resume | Implemented |
| Make.fun board sync, requirements, approval-gated submission | Implemented |
| Unit, integration and end-to-end tests | Implemented (full suite: 562 passed, 1 opt-in skip) |
| Local security scopes, hypotheses, evidence, review and reports | Implemented |
| Telegram bot, webhook, notifications and inline approvals | Implemented (live delivery unverified) |
| Next.js dashboard: 14 routes, role-aware, live SSE event feed | Implemented |
| Dashboard aggregates, usage series, audit feed, admin settings API | Implemented |
| Prometheus `/metrics`, request-ID/trace propagation, security headers | Implemented |
| Rate limiting (Redis shared, memory fallback), slow-request logging | Implemented |
| Sandbox limits: max timeout, read-only root, tmpfs, memory/CPU/PID caps | Implemented |
| Workspace disk quota enforced on writes and before/after commands | Implemented |
| `ops/status` + `ops/logs` APIs and the dashboard Logs page | Implemented |
| Key rotation (`ENCRYPTION_KEY_PREVIOUS`), secret scanner, retention prune | Implemented |
| Full-stack backup/restore scripts and a read-API load test | Implemented |
| Durable task queue with leases, heartbeats and crash recovery | Implemented |
| Background worker (`agentos worker`) and Telegram loop (`agentos bot`) | Implemented |
| Single-host deployment: Caddy TLS edge, deploy script, runbook | Implemented |

## Technology

- **Backend:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (async), Alembic
- **Database:** PostgreSQL 16 with pgvector (HNSW cosine index on memories)
- **Cache/queue:** Redis 7
- **Frontend:** Next.js 16 (App Router), React 19, TypeScript, Tailwind CSS v4
- **Execution:** Docker sandbox image with a scrubbed-environment host fallback
- **Packaging:** uv, Docker Compose

## Quick start (Docker)

```bash
cp .env.example .env
# Generate the two required secrets:
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(48))"
python3 -c "from cryptography.fernet import Fernet; print('ENCRYPTION_KEY=' + Fernet.generate_key().decode())"
# paste both into .env, set POSTGRES_PASSWORD, then:
docker compose up -d --build

# create the owner account (password is read from BOOTSTRAP_ADMIN_PASSWORD or prompted)
docker compose exec api agentos create-admin
docker compose exec api agentos seed

curl http://localhost:8000/health/ready
```

The stack includes four long-running application services: `api`, `web`,
`worker` (executes queued tasks) and `bot` (Telegram notifications; it idles
until a token is configured). Tasks created through the API run on the worker;
`POST /tasks/{id}/run` executes one inline and is meant for development.

API docs: <http://localhost:8000/docs> (disabled when `ENVIRONMENT=production`).
Dashboard: <http://localhost:3000> — sign in with the owner account.

### Deploying to a server

For a real host, use the production overlay: one Caddy edge on 80/443, every
other port closed, automatic HTTPS when `TLS_AUTO=1`.

```bash
scripts/deploy.sh preflight   # check .env, Docker and disk
scripts/deploy.sh up          # build and start everything
scripts/deploy.sh status
```

See `docs/DEPLOYMENT.md` for TLS, updates, backup/restore and troubleshooting.

The dashboard's API origin is compiled into the bundle, so when it is not
`http://localhost:8000` rebuild it:

```bash
NEXT_PUBLIC_API_URL=https://api.example.com docker compose build web
```

## Quick start (local development)

```bash
make install                 # uv sync --extra dev
docker compose up -d postgres redis
make migrate                 # alembic upgrade head
make seed
make admin email=you@example.com
make dev                     # http://localhost:8000
make test                    # unit and integration tests
```

The backend reads `.env` from the repository root (symlinked into `backend/`).
Local defaults in this repo use ports 5432/6379; adjust `DATABASE_URL` and
`REDIS_URL` if yours differ.

## Authentication

```bash
# Login (JWT)
curl -sX POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"..."}'

# Use the token
curl http://localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN"

# Create an API key (returned exactly once)
curl -sX POST http://localhost:8000/api/v1/auth/api-keys \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"ci","scopes":["read"]}'

# API-key auth
curl http://localhost:8000/api/v1/auth/me -H "X-API-Key: aos_..."
```

## Running an agent task

```bash
# 1. Register a model provider (the API key is encrypted at rest)
curl -sX POST http://localhost:8000/api/v1/providers -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"slug":"openai","name":"OpenAI","provider_type":"openai",
       "api_key":"sk-...","default_model":"gpt-4o-mini","enabled":true}'

# 2. Check credentials without spending tokens
curl -sX POST http://localhost:8000/api/v1/providers/$PROVIDER_ID/health \
  -H "Authorization: Bearer $TOKEN"

# 3. Create an agent and let it use a built-in tool
curl -sX POST http://localhost:8000/api/v1/agents -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"slug":"analyst","name":"Analyst","system_prompt":"You analyse code.",
       "model_provider_id":"'$PROVIDER_ID'","permissions":{"tools":["tool:read"]}}'

curl -sX POST http://localhost:8000/api/v1/agents/$AGENT_ID/tools \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"tool_slug":"calculate"}'

# 4. Create a task and execute it
curl -sX POST http://localhost:8000/api/v1/tasks -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"title":"Estimate effort","objective":"Multiply 6 by 7.",
       "assigned_agent_id":"'$AGENT_ID'","budget_usd":"0.50"}'

curl -sX POST http://localhost:8000/api/v1/tasks/$TASK_ID/run \
  -H "Authorization: Bearer $TOKEN"

# 5. Inspect the trace (iterations, tool calls, token/cost per step)
curl -s http://localhost:8000/api/v1/tasks/$TASK_ID/events -H "Authorization: Bearer $TOKEN"
```

Tools available to agents today (21): `echo`, `now`, `calculate`, `http_get`
(SSRF-guarded); the skill/memory tools `load_skill`, `recall`, `remember`; the
workspace tools `read_file`, `list_files`, `search_code`, `write_file`,
`delete_file`, `run_command`, `git`; and the make.fun tools
`makefun_competitions`, `makefun_competition_details`,
`makefun_prepare_entry`, `makefun_submit_entry`; plus `research_scopes`,
`research_hypothesis`, `research_report`. `GET /api/v1/tools` lists them
with their schemas and risk levels; `POST /api/v1/tools/{slug}/execute` runs one
directly for debugging.

## Running a multi-agent orchestration

```bash
# 1. Seed the 15 built-in specialists onto a provider
docker compose exec api agentos seed-agents \
  --provider $PROVIDER_ID --model gpt-4o-mini

# 2. Preview the plan the orchestrator would produce (no task is created)
curl -sX POST http://localhost:8000/api/v1/orchestration/plan \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"objective":"Write a Fibonacci function and have a reviewer check it."}'

# 3. Orchestrate a task: plan, delegate to specialists, aggregate the report
curl -sX POST http://localhost:8000/api/v1/orchestration/tasks/$TASK_ID/run \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"max_parallel":3,"max_step_attempts":2}'

# 4. Inspect the children and the agent-to-agent handoffs
curl -s http://localhost:8000/api/v1/orchestration/tasks/$TASK_ID/children \
  -H "Authorization: Bearer $TOKEN"
curl -s http://localhost:8000/api/v1/orchestration/tasks/$TASK_ID/messages \
  -H "Authorization: Bearer $TOKEN"

# 5. Resume a run that paused for approval, once it has been decided
curl -sX POST http://localhost:8000/api/v1/orchestration/tasks/$TASK_ID/resume \
  -H "Authorization: Bearer $TOKEN"
```

Each step becomes a real child task (so budgets, retries and events apply
unchanged), a failed step's dependants are skipped rather than left pending, and
a step marked `requires_approval` pauses the run into `waiting_approval` until
`POST /api/v1/approvals/{id}/decide`.

Roles: `viewer` < `operator` < `admin` < `owner`. API keys can additionally be
restricted by scopes (`"*"` grants all).

## Skills

A skill is a `SKILL.md` file — YAML frontmatter plus Markdown instructions. Five
builtin skills (`code-review`, `bug-repro`, `test-authoring`, `security-audit`,
`report-writing`) are installed at startup. Skills are *not* pasted into every
prompt: the system prompt carries only a catalogue of the agent's assigned
skills, and the body is loaded on demand with the `load_skill` tool.

```bash
# List installed skills (builtin and custom)
curl -s http://localhost:8000/api/v1/skills -H "Authorization: Bearer $TOKEN"

# Validate a SKILL.md before installing it
curl -sX POST http://localhost:8000/api/v1/skills/validate -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"content":"---\nname: My Skill\ndescription: Does a thing\n---\n\nSteps..."}'

# Install from a git URL or a directory inside the API container
curl -sX POST http://localhost:8000/api/v1/skills -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"source":"https://github.com/org/skills","git_ref":"main"}'

# Grant a skill to an agent — this is the permission boundary
curl -sX POST http://localhost:8000/api/v1/skills/$SKILL_ID/assign/$AGENT_ID \
  -H "Authorization: Bearer $TOKEN"

# See how often (and by whom) a skill was actually loaded
curl -s http://localhost:8000/api/v1/skills/$SKILL_ID/executions \
  -H "Authorization: Bearer $TOKEN"
```

## Memory

Six layers — `working`, `conversation`, `episodic`, `semantic`, `project`,
`agent` — with deduplication, versioning and provenance. Retrieval is hybrid:
lexical overlap, vector similarity, recency and usage are combined into one
score. A completed task automatically writes an episodic memory of its outcome.

```bash
# Store a fact (secrets are rejected, not stored)
curl -sX POST http://localhost:8000/api/v1/memory -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"content":"Staging runs PostgreSQL 16 with pgvector.","layer":"semantic",
       "key":"infra/staging-db","importance":0.8}'

# Hybrid search, and a ready-to-inject context block
curl -sX POST http://localhost:8000/api/v1/memory/search -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"query":"staging database"}'
curl -sX POST http://localhost:8000/api/v1/memory/context -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"query":"staging database"}'

# Delete what has outlived its TTL (admin only)
curl -sX POST http://localhost:8000/api/v1/memory/purge-expired \
  -H "Authorization: Bearer $TOKEN"
```

Agents read and write memory through the `recall` and `remember` tools. A memory
carries optional `agent_id`/`project_id`/`task_id`/`user_id` scope tags; any tag
recorded on a row is required again on read, so agent A cannot see agent B's
memories and a project-scoped memory stays inside that project.

Vector search needs an embedding model. Set `MEMORY_EMBEDDING_PROVIDER` to a
registered provider slug and `MEMORY_EMBEDDING_MODEL` to its embedding model;
without them memory runs lexically and the vector column simply stays empty.

## Projects, workspaces and coding runs

A project owns a directory under `WORKSPACE_ROOT`. Files are read and written
through tools that refuse absolute paths, `../..` and symlinks pointing outside
the tree.

```bash
# Create a project (the workspace directory is created for you)
curl -sX POST http://localhost:8000/api/v1/projects \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"slug":"demo","name":"Demo project"}'

# Write and read a file
curl -sX PUT http://localhost:8000/api/v1/projects/$PROJECT_ID/file \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"path":"src/app.py","content":"print(1)\n"}'
curl -s "http://localhost:8000/api/v1/projects/$PROJECT_ID/file?path=src/app.py" \
  -H "Authorization: Bearer $TOKEN"

# Run a command in the workspace (admin; sandboxed when the image exists)
curl -sX POST http://localhost:8000/api/v1/projects/$PROJECT_ID/commands \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"command":"python3 --version"}'

# Which coding engines are usable on this host/machine?
curl -s http://localhost:8000/api/v1/projects/engines -H "Authorization: Bearer $TOKEN"
```

Build the sandbox image with `make sandbox-image` (or
`docker compose --profile sandbox build sandbox`). The `api` container
deliberately has **no Docker socket**, so command runs from the API fall back to
the host with a scrubbed environment and a logged `sandbox_fallback` reason.

### High-risk tools need approval

`write_file`, `delete_file`, `run_command` and `git` are `high` risk. When an
agent asks for one, nothing runs: an approval row is created, the task moves to
`waiting_approval`, and a human decides.

```bash
# See what is waiting
curl -s "http://localhost:8000/api/v1/approvals?status=pending" -H "Authorization: Bearer $TOKEN"

# Approve it, then re-run the task: only approved tools can execute
curl -sX POST http://localhost:8000/api/v1/approvals/$APPROVAL_ID/decide \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"approved":true,"note":"reviewed the command"}'
curl -sX POST http://localhost:8000/api/v1/tasks/$TASK_ID/run -H "Authorization: Bearer $TOKEN"
```

## Bounties (make.fun)

The platform mirrors the make.fun competition board and runs entry workflow
against it. Every route used is taken from make.fun's published OpenAPI
document; reads work without a key, writes need `MAKEFUN_API_KEY`.

```bash
# Sync the board into `bounty_programs` + `competitions`
curl -sX POST http://localhost:8000/api/v1/bounties/competitions/sync \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"limit":50}'

# Live competitions first, with rewards and deadlines
curl -s "http://localhost:8000/api/v1/bounties/competitions?status=open&limit=20" \
  -H "Authorization: Bearer $TOKEN"

# What does this competition actually require? (52 checks, quota, deliverables)
curl -sX POST http://localhost:8000/api/v1/bounties/competitions/$COMPETITION_ID/requirements/refresh \
  -H "Authorization: Bearer $TOKEN"

# Prepare an entry: writes the entry, opens an approval, calls nothing
curl -sX POST http://localhost:8000/api/v1/bounties/competitions/$COMPETITION_ID/entries \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"repo_url":"https://github.com/you/chess","commit_sha":"'"$(printf 'a%.0s' {1..40})"'","title":"Tiny chess"}'

# A human approves, then the platform submits
curl -sX POST http://localhost:8000/api/v1/approvals/$APPROVAL_ID/decide \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"approved":true}'
curl -sX POST http://localhost:8000/api/v1/bounties/entries/$ENTRY_ID/submit \
  -H "Authorization: Bearer $TOKEN"
```

Failure handling is deliberate: a `4xx` problem document from make.fun is
stored on the entry (status `rejected`, with each check's verdict and `fix`) and
returned as `422`; a `401`/`403` (lapsed key) or `5xx` returns `502` and leaves
the entry untouched. Nothing is ever reported as submitted without a
`submission_id` from the platform.

Agents reach the board through four tools: `makefun_competitions`,
`makefun_competition_details`, `makefun_prepare_entry` and
`makefun_submit_entry`. The last one is `critical`, granted to no specialist,
and requires runtime approval plus the entry's own approved approval.

## Configuration

Phase 7 provides `/api/v1/research/scopes`, `/findings`, `/scans` and `/scanners`.
Register a project scope with authorization text and allowed tools, have an admin
verify it, then record hypotheses or run a local scan. Findings require
validation evidence and an independent user's review before confirmation.
`GET /research/findings/{id}/report` generates a local Markdown report.

Build the research image with `make sandbox-image && make research-image`.
`SECURITY_SANDBOX_IMAGE` defaults to `agentos/research:latest`, with Semgrep
1.99.0 installed. Scans require Docker and always disable networking. The
Compose API does not have Docker access; its inventory reports unavailable.
Other scanner adapters require their tools in a custom image. Dependency audit
execution is disabled until an advisory lookup policy exists. See the Phase 7
delivery notes in [docs/PHASES.md](docs/PHASES.md) for verified limitations.

All settings are environment variables, validated at startup
([`backend/app/core/config.py`](backend/app/core/config.py)). See
[`.env.example`](.env.example) for the full list.

| Variable | Required | Notes |
| --- | --- | --- |
| `SECRET_KEY` | yes | ≥32 chars; JWT signing |
| `ENCRYPTION_KEY` | yes | Fernet key; encrypts provider API keys at rest |
| `DATABASE_URL` | yes | must use `postgresql+asyncpg://` |
| `REDIS_URL` | yes | event bus + readiness |
| `CORS_ORIGINS` | no | CSV or JSON list; wildcard rejected in production |
| `ENVIRONMENT` | no | `development` / `testing` / `production` |
| `SKILLS_ROOT` | no | where skill packages live (`/data/skills` in Docker) |
| `WORKSPACE_ROOT` | no | where project workspaces live (`/data/workspaces` in Docker) |
| `SANDBOX_ENABLED` | no | run commands in the sandbox image when it is available |
| `SANDBOX_IMAGE` | no | image used for sandboxed commands (`agentos/sandbox:latest`) |
| `SANDBOX_ALLOW_HOST_EXECUTION` | no | allow the host fallback when Docker is unavailable |
| `SANDBOX_MAX_TIMEOUT_SECONDS` | no | hard ceiling for one command (default 900) |
| `SANDBOX_MEMORY_LIMIT` / `SANDBOX_CPU_LIMIT` / `SANDBOX_PIDS_LIMIT` | no | container limits (`1g` / `1.0` / `256`) |
| `WORKSPACE_QUOTA_MB` | no | per-workspace disk quota (default 5120) |
| `METRICS_ENABLED` / `METRICS_TOKEN` | no | expose `/metrics`; a token makes it bearer-authenticated |
| `SLOW_REQUEST_MS` | no | requests above this are logged at WARNING (default 2000) |
| `RATE_LIMIT_ENABLED` / `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` | no | global limit (default 240/min) |
| `AUTH_RATE_LIMIT_REQUESTS` / `AUTH_RATE_LIMIT_WINDOW_SECONDS` | no | login/reset limit (default 10 per 5 min) |
| `TRUST_FORWARDED_FOR` | no | honour `X-Forwarded-For`; only behind your own proxy |
| `RETENTION_TASK_EVENTS_DAYS` / `RETENTION_AUDIT_DAYS` / `RETENTION_MODEL_USAGE_DAYS` | no | windows used by `agentos prune` (0 = keep) |
| `ENCRYPTION_KEY_PREVIOUS` | no | comma-separated old Fernet keys, decryption only |
| `CODING_ENGINE` / `CODING_MAX_TURNS` | no | default coding engine and its turn cap |
| `MEMORY_EMBEDDING_PROVIDER` | no | provider slug used for embeddings; unset = lexical only |
| `MEMORY_EMBEDDING_MODEL` | no | embedding model name; must produce 1536 dimensions |
| `MAKEFUN_API_KEY` | no | make.fun `af_...` key; required to submit, expires after 30 days |
| `MAKEFUN_BASE_URL` | no | defaults to `https://make.fun` |
| `MAKEFUN_TIMEOUT_SECONDS` | no | request timeout for make.fun calls (default 30) |
| `MAKEFUN_SYNC_LIMIT` | no | competitions fetched per sync page (default 25) |
| `TELEGRAM_BOT_TOKEN` | no | Bot API token; see [Telegram setup](docs/TELEGRAM.md) |
| `TELEGRAM_ALLOWED_USER_IDS` | no | numeric user allowlist; empty denies everyone |
| `TELEGRAM_WEBHOOK_SECRET` | no | private secret header, at least 32 random characters |
| `TELEGRAM_NOTIFY_INTERVAL_SECONDS` | no | how often `agentos bot` checks for finished tasks (default 30) |
| `WORKER_POLL_INTERVAL_SECONDS` | no | idle poll interval for `agentos worker` (default 2.0) |
| `WORKER_LEASE_SECONDS` | no | how long a claim survives without a heartbeat (default 300) |
| `WORKER_METRICS_PORT` | no | the worker's own `/metrics` port (default 9101) |
| `PUBLIC_HOST` / `PROXY_PORT` / `PROXY_TLS_PORT` | yes for a deployment | name and ports the Caddy edge serves |
| `TLS_AUTO` / `ACME_EMAIL` | no | automatic HTTPS; `ACME_EMAIL` required when `TLS_AUTO=1` |
| `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD` | no | used by `agentos create-admin` |

Production refuses `DEBUG=true` and wildcard CORS; secrets are never logged.

## Commands

| Command | Purpose |
| --- | --- |
| `make dev` | API with autoreload |
| `make test` | Full test suite |
| `make test-unit` | Tests without Postgres/Redis |
| `make lint` / `make format` / `make typecheck` | Ruff / Ruff / Mypy |
| `make migrate` / `make migration m="..."` | Apply / create migrations |
| `make seed` | Upsert default system settings and built-in tools |
| `make admin email=...` | Create or promote owner |
| `make up` / `make down` / `make logs` | Compose lifecycle |
| `make verify-migration` | Detect model/migration drift |
| `make sandbox-image` | Build the `agentos/sandbox` command-sandbox image |
| `make web-check` / `make web-build` | Typecheck+lint / build the dashboard image |
| `make backup` / `make restore f=...` | Back up / restore DB + Redis + workspaces |
| `make scan-secrets` | Scan the source tree for credential patterns |
| `make rotate-keys` | Re-encrypt provider keys under the current key |
| `make prune [dry=1]` | Apply retention windows |
| `make loadtest email=... password=...` | Load test the read API |
| `make worker` / `make bot` | Run a worker or the Telegram loop in the foreground |
| `make queue [status=...] [reap=1]` | Queue depth; `reap=1` recovers dead workers' tasks |
| `make test-e2e` | End-to-end journey tests |
| `make deploy-preflight` / `make deploy` | Validate a host, then deploy |
| `make deploy-update` / `make deploy-status` | Update a deployment / check its health |
| `make docker-check` | Validate both Compose files |

## Repository layout

```
.
├── backend/
│   ├── app/
│   │   ├── api/            # health probe + versioned routers
│   │   ├── core/           # config, logging, security, events, middleware
│   │   ├── db/             # base, enums, session, model modules
│   │   ├── memory/         # embeddings, sensitivity, relevance scoring
│   │   ├── orchestrator/   # plan model, LLM planner, DAG scheduler, specialists
│   │   ├── providers/      # provider adapters, registry, pricing
│   │   ├── runtime/        # agent loop, agent spec, runtime errors
│   │   ├── schemas/        # Pydantic request/response models
│   │   ├── skills/         # SKILL.md loader + five builtin skill packages
│   │   ├── services/       # users, audit, providers, agents, tasks, execution,
│   │   │                   # orchestration, approvals, messages, skills,
│   │   │                   # memory, bounty, agent_context
│   │   ├── bounty/         # make.fun HTTP client (documented API only)
│   │   ├── coding/         # coding engine interface, OpenCode + shell engines
│   │   ├── tools/          # Tool ABC, registry, built-in + coding tools
│   │   ├── workspace/      # path confinement, local/Docker command runners
│   │   ├── worker.py       # durable-queue worker loop
│   │   ├── bot.py          # Telegram notification loop
│   │   ├── cli.py          # operator CLI
│   │   └── main.py         # app factory, lifespan, error handlers
│   ├── sandbox/            # Dockerfile for the command-sandbox image
│   ├── alembic/            # async migration environment + revisions
│   ├── tests/              # unit + integration tests
│   ├── Dockerfile
│   └── pyproject.toml
├── deploy/                 # Caddy edge (plain HTTP and TLS) + production overlay
├── docs/                   # architecture, phases, database, security, operations
├── frontend/               # Next.js dashboard (App Router, standalone image)
├── scripts/                # entrypoint, deploy, backup/restore, load test
├── docker-compose.yml
├── Makefile
└── .env.example
```

## Database

34 tables covering users, agents and their configuration, tools and skills
(with version history, assignment and execution records), tasks/steps/
dependencies/events, conversations, six memory layers with a pgvector index,
projects, workspaces and coding sessions, repositories, bounty programs,
competitions and competition entries, findings and evidence, approvals, model
providers and usage, audit logs and system settings.
Details and invariants: [docs/DATABASE.md](docs/DATABASE.md).

```bash
make migration m="add x"     # autogenerate after changing models
make migrate                 # apply
make verify-migration        # CI guard against drift
```

## Security defaults

- Argon2id password hashing; constant-time comparison for API keys.
- API keys stored only as SHA-256 hashes; plaintext returned once.
- Provider API keys encrypted at rest with Fernet (`ENCRYPTION_KEY`).
- JWT with issuer/audience/type validation and short TTL.
- RBAC + API-key scopes; audit log entries for auth events.
- Security headers, request IDs, CORS allowlist, production hardening checks.
- Containers run as non-root with `no-new-privileges`; DB/Redis bound to
  `127.0.0.1` on the host.
- Memory writes reject credentials (`memory_sensitive_content`); skill import is
  http(s)-only and confined to the skills root.
- Project files are confined to the workspace: absolute paths, traversal and
  symlink escapes are refused.
- Commands run in the sandbox image with no network, a read-only root and CPU/
  memory/pid caps when it is available; otherwise on the host with a scrubbed
  environment and a logged reason.
- High-risk tools (`write_file`, `delete_file`, `run_command`, `git`) require a
  recorded human approval per task before they can run.
- Rate limiting on every route, with a stricter bucket for login and password
  reset; the limiter degrades to an in-process counter if Redis is unavailable
  rather than failing open.
- Every command is clamped to `SANDBOX_MAX_TIMEOUT_SECONDS`, and each workspace
  cannot grow past `WORKSPACE_QUOTA_MB`.
- Submitting a bounty entry to make.fun requires an approved
  `submit_competition` approval for that entry; the preparing specialist cannot
  submit, and a missing or expired API key fails loudly instead of marking the
  entry rejected.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and
[docs/SECURITY.md](docs/SECURITY.md).

## Tests

```bash
make test          # unit + integration against local Postgres/Redis
make check         # ruff + mypy + backend tests + dashboard typecheck/lint
make web-check     # dashboard typecheck + eslint only
```

Integration tests require PostgreSQL (`agentos_test` database is created
automatically) and Redis; they skip cleanly when the services are absent.

## Deployment

Docker Compose is the supported deployment. `scripts/entrypoint.sh` waits for
PostgreSQL, applies migrations and then starts Uvicorn. The API container runs
as UID 10001 and exposes `/health/live` / `/health/ready`. The dashboard is the
`web` service (Next.js standalone, non-root) on `${WEB_PORT:-3000}`; it needs no
database of its own and talks only to the API from the operator's browser.

Backups: `make backup` writes one tarball of `pg_dump`, the Redis dataset and the
workspace volume with a manifest; `make restore f=backups/agentos-....tar.gz`
puts all three back, restarts the stack and runs `alembic upgrade head`. Restore
is destructive and asks for confirmation by name. Keep `ENCRYPTION_KEY` outside
the backup.

Operational visibility: `/metrics` (Prometheus text), `/api/v1/ops/status` and
`/api/v1/ops/logs`, and the dashboard's **Logs** page. See
[docs/OPERATIONS.md](docs/OPERATIONS.md) for the runbook.

## Roadmap

See [docs/PHASES.md](docs/PHASES.md). Phases 1–7 and 9–11 are complete. Phase 8
(Telegram) is implemented with live delivery unverified, because no bot token or
public webhook is configured. Deployment, the queue and the worker are covered by
the end-to-end suite in `backend/tests/test_e2e.py`.
