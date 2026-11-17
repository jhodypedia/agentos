# Architecture

AgentOS is a layered, event-driven system. Phase 1 implemented the platform
kernel — configuration, persistence, identity, observability and the service
shell. Phase 2 added the execution core: provider adapters, the tool layer, the
agent runtime loop and task execution that persists events, tokens and cost.
Phase 5 adds the work surface: a confined project workspace, a command sandbox,
coding engines, coding tools and the human-approval gate that guards every
high-risk action. Phase 6 adds the first external bounty integration: the
make.fun competition board, its published requirements, and an entry workflow
whose submission step cannot be taken without a recorded human approval. Phase 9
adds the operator-facing control plane: a Next.js dashboard for every surface
built so far, reading the same versioned API and following the event bus live.

```
                    ┌─────────────────────────────────────────────┐
                    │  Dashboard (Next.js, Phase 9)  Telegram (P8)│
                    └───────────────┬─────────────────────────────┘
                                    │ HTTPS / WS
                    ┌───────────────▼─────────────────────────────┐
                    │            FastAPI service (P1)             │
                    │  auth · RBAC · health · versioned API        │
                    │  error envelope · request logging            │
                    └───────┬───────────────────┬─────────────────┘
                            │                   │
              ┌─────────────▼──────┐   ┌────────▼─────────────┐
              │ Orchestrator (P3)  │   │ Agent runtime (P2)    │
              │ plans, delegates,  │   │ LLM loop, tools,      │
              │ budgets, approvals │   │ memory, checkpoints   │
              └───────┬────────────┘   └────────┬─────────────┘
                      │                         │
     ┌────────────────▼───────────┐   ┌─────────▼──────────────┐
     │ Tool registry + sandbox(P5)│   │ Provider abstraction(P2)│
     │ coding engine, workspace   │   │ OpenAI/Anthropic/...   │
     └────────────────┬───────────┘   └─────────┬──────────────┘
                      │                         │
              ┌───────▼─────────────────────────▼───────┐
              │   PostgreSQL (state)  ·  Redis (bus)     │
              └───────────────┬─────────────────────────┘
                              │  approval-gated
                    ┌─────────▼──────────┐
                    │  make.fun API (P6) │
                    └────────────────────┘
```

## Layers

### 1. API layer (`app/api`)
- `/health/live`, `/health/ready` — Kubernetes-compatible probes.
- `/api/v1/*` — versioned business API: `meta`, `auth`, `providers`, `agents`,
  `tools`, `tasks` (create, list, get, run, cancel, events), `orchestration`,
  `approvals`, `skills`, `memory`, `projects`, `bounties`, `research`,
  `telegram`, `telegram/notifications`, `dashboard` and `settings`.
- Dependencies in `app/api/deps.py` provide the DB session, the authenticated
  `Principal`, and role/scope checks. Every later router reuses them, so
  authorization stays consistent.

### 2. Domain services (`app/services`)
Business operations that are independent of HTTP: `users`, `audit`,
`providers`, `agents`, `tasks`, `execution`, `orchestration`, `approvals`,
`messages`, `skills`, `memory`, `bounty`, `agent_context`, `research`,
`telegram` and `dashboard`. Routers stay thin; services own transactions.

### 3. Persistence (`app/db`)
- Async SQLAlchemy 2 with `asyncpg`.
- Declarative base with a deterministic constraint naming convention, UUID
  primary keys and timezone-aware timestamps.
- Enums are VARCHAR + CHECK constraints (not native PG enums) so adding a value
  is a normal migration rather than a type rewrite.
- 34 tables; see `docs/DATABASE.md`.
- Alembic runs asynchronously and is verified with `alembic check`.

### 4. Core (`app/core`)
- **config** — Pydantic settings, validation, production hardening.
- **security** — Argon2id, JWT, Fernet encryption, API key generation/hashing.
- **events** — typed domain events published to a Redis Stream (durable) and
  Pub/Sub (live fan-out). Event types match the specification.
- **middleware** — request ID propagation, access logs, security headers.
- **exceptions** — typed error hierarchy rendered as a stable JSON envelope.

### 5. Providers (`app/providers`)
`BaseProvider` owns transport concerns: HTTP client reuse, retries with
exponential backoff, timeout mapping and a single error taxonomy
(`ProviderAuthError`, `ProviderRateLimitError`, `ProviderTimeoutError`, …).
Concrete adapters (`OpenAICompatibleProvider` covering OpenAI/OpenRouter/local
servers, `AnthropicProvider`, `GoogleProvider`) translate the neutral
`LLMRequest`/`LLMResponse` pair to and from each wire format, including tool
calls. `registry.build_provider` resolves a `ProviderType` + record into a
ready adapter; `pricing` attaches a per-call cost estimate from a model price
catalogue.

### 6. Tools (`app/tools`)
`Tool` declares a slug, JSON-schema parameters, required permissions and a risk
level. `Tool.run` validates arguments, checks permissions, refuses `high`/`critical`
risk tools unless they appear in the caller's approval set, and enforces a
per-tool timeout. `ToolRegistry.allowed_for()` filters tools by what the caller
may use; the runtime passes `include_approval_gated=True` so a tool the agent is
granted but which still needs sign-off is **advertised** — the model can then ask
for it and the request becomes an approval record instead of dying silently.
Permission failures are never advertised.
Built-ins (`echo`, `now`, `calculate`, `http_get`, `load_skill`, `remember`,
`recall`) plus the coding set (`read_file`, `list_files`, `search_code`,
`write_file`, `delete_file`, `run_command`, `git`) and the four make.fun tools
(`makefun_competitions`, `makefun_competition_details`, `makefun_prepare_entry`,
`makefun_submit_entry`) are declared in code and upserted into `tool_registry`
at startup;
`http_get` blocks non-HTTP schemes, credentials in URLs and private/loopback/
link-local targets (SSRF guard). The three memory tools reach their services
through `ToolContext.resources`, which the runtime fills from `AgentSpec`, so a
tool never opens its own database session or re-derives the caller's identity.

### 7. Runtime (`app/runtime`)
`run_agent` is the provider-agnostic ReAct loop: it assembles the system prompt
and history, asks the provider for a completion, brokers any requested tool
calls through the registry, feeds results back as tool messages, and stops on a
plain completion, an iteration/token/cost/budget limit, a deadline or a
cancellation check. A `RuntimeListener` receives iteration, tool and completion
hooks so persistence never leaks into the loop.

When a tool raises `ApprovalRequiredError`, the loop records the blocked call in
`RuntimeResult.pending_approvals`, invokes an optional `approval_callback` (which
persists an `approvals` row in its own session) and returns a tool result stating
plainly that nothing ran. The callback is best-effort: a database failure is
logged and the run continues, because losing an approval row must not corrupt a
conversation. On the next run, `AgentSpec.approvals` — populated from the task's
approved requests — is what lets the same tool execute.

### 8. Execution service (`app/services/execution.py`)
`execute_task` binds runtime to persistence: it validates the agent and
provider, walks the task state machine (`pending → queued → running → terminal`),
streams events through `TaskProgressListener` (own session, so a long run never
holds the request transaction), then records output, token/cost totals and a
`model_usage` row. A run that ends with `pending_approvals` parks the task in
`waiting_approval` instead of `completed`, and the task may only be re-run from
that state once its approvals are decided. For a task bound to a project it also
resolves the workspace and injects `workspace`/`command_runner`/`sandbox` into
`AgentSpec.resources`, so coding tools receive a confined runner without
importing the workspace layer. On success it writes an episodic memory inside a savepoint:
the memory write is best-effort and can never roll back a completed task. It accepts an `extra_context`, a `model_name` override and a
`provider_override`/`provider_id` so the orchestrator can steer a step without
mutating the agent row. The `prepare`/`run` split keeps queue dispatch local.

### 9. Orchestrator (`app/orchestrator`)
Three cooperating pieces, each testable without the database:

- `plan.py` — `PlanStep`/`ExecutionPlan` plus `validate()` (key pattern, step
  cap, unique keys, no self-dependency, known agents, defined dependencies,
  acyclicity) and `levels()`, which returns the topological waves the scheduler
  runs in parallel.
- `planner.py` — `LlmPlanner` asks the orchestrator model for a `submit_plan`
  tool call given the objective, the agent catalogue and optional context. Every
  failure mode (provider error, unparseable output, invalid plan) degrades to a
  deterministic single-step plan with `metadata["fallback_reason"]`, so planning
  cannot block a run. `render_dependency_context()` assembles the per-step
  context from completed dependency outputs.
- `scheduler.py` — `run_plan()` executes the plan level by level, bounding each
  wave with a semaphore, applying `max_attempts` with backoff, honouring a
  deadline and a `cancel_check`, containing step exceptions, and skipping the
  dependants of failed steps. `StepRunner` is a protocol, so the scheduler is
  independent of tasks, agents and the database.
- `specialists.py` — the 15 built-in specialist definitions (system prompts,
  tools, permissions, limits) and an idempotent `sync_specialists()` that only
  assigns tools present in the registry. Overrides load from YAML/JSON.

### 10. Skills (`app/skills`, `app/services/skills.py`)
A skill is Markdown plus YAML frontmatter — instructions, not code. The loader
parses and validates one file (size cap, UTF-8, required `name`/`description`,
semver `version`, normalised slug) and the service manages the lifecycle:
install from a directory or git/URL, keep a version history, enable/disable,
assign to agents, and record every load in `skill_executions`.

Two decisions shape this layer. First, skills are injected **on demand**: the
system prompt carries only a catalogue of the agent's assigned skills, and the
body arrives when the agent calls `load_skill`. A skill's instructions are the
expensive part of the prompt, and most tasks do not need most skills. Second,
builtin skills are re-synced at startup by content hash, so shipping an updated
prompt is a deploy rather than a migration, while a locally edited builtin is
left alone if its hash still matches nothing forced.

An agent can only load a skill assigned to it — assignment is the permission
boundary, and the API enforces it rather than trusting the model to pick a valid
slug.

### 11. Memory (`app/memory`, `app/services/memory.py`)
Retrieval is hybrid: lexical overlap, vector cosine, recency and usage are
scored independently and combined by fixed weights in `app/memory/scoring.py`.
Lexical alone misses paraphrases; vector alone cannot tell that an exact term
matters more than a nearby one; recency alone would drown the durable facts that
memory exists to keep. Usage (`access_count`) is log-scaled so a frequently read
memory ranks higher without a single hot row dominating.

The embedder is resolved per request from the configured provider
(`resolve_embedder`). If none is configured, or the embedding call fails, the
path degrades to lexical retrieval with a warning — retrieval never fails a
request because an optional component is unavailable, which is also why the
live deployment works today with no embedding model at all.

Isolation is a single `visibility_filter` clause used by every read path
(hybrid, vector, listing, context). Any scope tag recorded on a row is required
again on read; unset means shared. Putting the rule in SQL rather than in the
callers is what makes it impossible to forget.

`agent_context.py` assembles what an agent sees: the assigned-skill catalogue,
the memory context block, and the `resources` the memory tools need. It honours
`agent.memory_config` (disable memory, or restrict layers), so a per-agent
configuration change takes effect without touching the runtime.

### 12. Orchestration service (`app/services/orchestration.py`)
`orchestrate_task()` is the seam between the DAG and the persistence model: it
plans, persists the plan on the parent (`task.output["plan"]`) and mirrors it to
`task_steps`, creates one child `Task` per step with `task_dependencies` edges,
and runs each step through `_ChildStepRunner` — which owns a session per step,
injects the dependency context, records an `agent_messages` handoff, and reuses
a completed child's output on resume. A step flagged `requires_approval` pauses
the parent into `waiting_approval`; `resume_orchestration()` rebuilds the plan,
refuses to run with an approval pending, and continues from the first
incomplete step. Totals come from the real child rows, never estimates.

### 13. Workspace, sandbox and coding engines (`app/workspace`, `app/coding`)
`Workspace` is the only way a tool touches a project's files. Every path is
validated against a project slug before it is built, then resolved (symlinks
followed) and re-checked for containment, so neither `../..`, an absolute path
nor a symlink planted inside the tree can escape. Filesystem work runs in a
thread because a large tree walk is blocking work, not CPU work.

`resolve_runner()` decides how a command executes: inside `SANDBOX_IMAGE` under
Docker when sandboxing is on and the image exists, otherwise on the host with a
pared environment. It returns the runner **and** the reason, and both are logged
— "sandboxing is off because the image is missing" is a fact an operator needs,
not a silent downgrade.

The two coding engines sit behind one interface. `OpencodeEngine` drives an
external agentic CLI and parses its JSON event stream into a transcript, a
summary and a list of changed files; `ShellEngine` runs a single command with no
model involved. `CodingEngineRegistry` reports availability by probing for the
binary, so an uninstalled engine shows up as unavailable rather than failing at
call time.

### 14. Human approval for high-risk actions (`app/services/approvals.py`)
Approval is a record, not a boolean. A request carries the action type, the
task, the requesting agent, the exact tool and arguments, and an expiry; a
decision is one-way (approving an already-decided or expired request is
rejected). Deciding writes a task event and publishes `approval.decided`.

The runtime consumes approvals, rather than the API injecting an allow-list:
`approved_tool_slugs()` reads the approved `run_high_risk_tool` rows for the
task and hands them to the spec. That means the authority for running a
dangerous tool lives in the database, is auditable, and cannot be forged by the
model's own output.

### 15. Bounties: make.fun (`app/bounty`, `app/services/bounty.py`)
The integration keeps three things apart, because merging them is how a bounty
integration starts lying to its operator:

1. **What the platform published.** `sync_competitions` stores a listing exactly
   as the API returned it, keyed by `(program_id, external_id)`, and returns what
   it created and updated. A stale row keeps its original data; staleness is
   visible rather than smoothed over.
2. **What the rules require.** The requirements document is fetched per
   competition and stored verbatim. `requirements_summary` derives the
   deliverables, the checks and their fixes, the deadlines, the quota, the payout
   curve and whether transcripts are mandatory — from the document, never from
   an assumption.
3. **What we did.** `competition_entries` holds attempts: repo and commit, the
   transcript paths, the approval that authorised it and the platform's verdict.

Status is never overstated. An `open` listing past its deadline is stored
`closed`, because the API stops accepting entries then.

Submission is a three-step handshake, and each step is enforced somewhere the
caller does not control:

```
POST /competitions/{id}/entries   prepare, write entry + approvals row, no network
POST /approvals/{id}/decide       a human approves (action_type=submit_competition)
POST /entries/{id}/submit         re-reads the approval, then calls make.fun
```

`MakeFunClient` also refuses to follow a redirect: this endpoint decides whether
a submission is accepted, so a `3xx` is an error, not a detour. Failures are
split by meaning — `401`/`403` and `5xx` leave the entry untouched (a lapsed key
is a deployment problem, not a rejected entry), while a `4xx` problem document
is stored on the entry with its per-check verdicts before it is re-raised.

## Orchestration flow

Phase 7 adds `app/security_research/scanners.py`, `app/services/research.py`,
project-bound research tool resources and `/api/v1/research`. The service owns
scope checks, evidence hashing, lifecycle transitions and independent reviews.
Scanner execution uses a dedicated image (`SECURITY_SANDBOX_IMAGE`) and requires
Docker with network disabled. Normalized scanner leads are imported as
hypotheses; confirmation requires separate validation evidence and review.

```
POST /orchestration/tasks/{id}/run
        │
        ├─ plan          LLM planner (submit_plan) ──fallback──> single step
        ├─ persist       task.output.plan + task_steps mirror
        ├─ delegate      one child Task + task_dependencies per step
        └─ schedule      run_plan(): level → semaphore → StepRunner
                            │
                            ├─ execute_task(child)  → runtime → provider → tools
                            ├─ handoff message       agent_messages
                            └─ approval gate        waiting_approval → resume
        → aggregate tokens/cost/iterations → terminal status + report
```

Each run is idempotent against the persisted children map, so re-running a
completed task is rejected and resuming a failed one only re-executes the steps
that did not succeed. Failures are contained at the step level: a failed step is
retried up to `max_attempts`, then its dependants are skipped and their child
tasks cancelled rather than left `pending`.

## Dashboard and live events

`app/api/v1/dashboard.py` is read-only aggregation. Every figure is a query
against persisted rows (`agents`, `tasks`, `tasks.status`, `approvals`,
`competitions`, `findings`, `model_usage`, `audit_logs`) — nothing is simulated,
and costs come from recorded usage rather than a price estimate.

The live feed is the Pub/Sub half of the event bus. `/dashboard/stream` opens
one pubsub subscription per browser tab, forwards each `Event` as a named SSE
frame (`id`, `event`, `data`), sends `retry: 3000` and a keepalive comment every
20 seconds, and unsubscribes when the client goes away. `EventSource` cannot set
request headers, so this single route also accepts the JWT as `?token=`; it is
verified with the same `principal_from_token` path as a bearer token and still
requires operator or higher. The durable Stream half is exposed as
`/dashboard/events` for the initial backfill.

The dashboard itself (`frontend/`) is a client-side Next.js SPA. It follows the
same RBAC: each page calls a route the API already gates, and a role mismatch
produces the API's `403` rather than a rendered page. The nav filters itself
only to avoid advertising routes the user cannot use.

## Request lifecycle

Middleware is added in this order, which by Starlette's semantics makes the last
one added the outermost:

```
InFlight  →  RateLimit  →  CORS  →  RequestContext   (outermost)
```

1. `RequestContextMiddleware` (outermost) assigns or propagates `X-Request-ID`,
   parses an inbound `traceparent` into `X-Trace-ID`, and starts the request
   timer. Being outermost means a `429` from the limiter still carries an id and
   a log line.
2. CORS is evaluated against the allowlist. It has to be outside the limiter:
   with CORS innermost, a `429` reached the browser without
   `Access-Control-Allow-Origin`, so the dashboard reported a network error
   instead of "slow down". `Retry-After` and the `X-RateLimit-*` headers are in
   `expose_headers` for the same reason.
3. `RateLimitMiddleware` resolves the client identity and checks the shared
   Redis counter (memory fallback if Redis is down); a rejection short-circuits
   with `429 rate_limited` + `Retry-After`.
4. `InFlightMiddleware` maintains the in-flight gauge around the handler.
5. The route dependency resolves the async DB session and authenticates via
   Bearer JWT or `X-API-Key`.
6. The service layer executes the operation inside a transaction and writes
   audit entries in the same transaction.
7. Errors are rendered as `{"error": {"code", "message", "details",
   "request_id"}}` with the matching HTTP status.
8. On the way out: request-duration histogram, rate-limit headers, the request
   id and security headers, plus a slow-request warning above
   `SLOW_REQUEST_MS`.

## Observability

`app/core/metrics.py` is a dependency-free Prometheus text-format registry
(counters, gauges, histograms, label sets, a renderer). Instrumentation is at the
edges — middleware for HTTP, the runtime loop for tools, `_persist_outcome` for
tokens and cost — rather than scattered through the services.

State gauges (`tasks`, `agents`, `approvals_pending`) are recomputed from the
database on each scrape by `refresh_state_gauges()`. Caching them would make a
restart or a crash show a number that never was true; a scrape is a cheap query
and monitoring asks "what is true now", not "what was true at boot".

`/metrics` sits outside `API_PREFIX`, is exempt from rate limiting (a scraper is
a legitimate high-frequency client) and is gated by `METRICS_TOKEN` when
configured.

`app/api/v1/ops.py` exposes the operational view over HTTP: `/ops/status`
(configuration, sandbox mode, disk and quota, component health) and `/ops/logs`
(one merged timeline over `task_events` and `audit_logs`). Both read what is
stored; neither keeps a parallel copy of the truth.

## Sandbox resolution

```
resolve_runner(settings, ...)
    sandbox disabled            → host runner (or refuse)
    docker available + image    → DockerRunner(--read-only, --tmpfs, limits, --network=none)
    docker unavailable          → SANDBOX_FALLBACK{reason=docker_unavailable} → host runner
    image missing               → SANDBOX_FALLBACK{reason=image_missing}       → host runner
    host execution disallowed   → refusal
```

Resource limits (memory, CPU, PIDs, max timeout) come from settings, with the
database able to narrow them further (`app/services/runtime_settings.py`).
Workspace quota is enforced independently of the runner, so it applies to host
execution too.

## Design decisions

| Decision | Rationale |
| --- | --- |
| Async SQLAlchemy + asyncpg | Agent workloads are I/O bound; matches FastAPI. |
| VARCHAR-backed enums | Painless forward migrations, portable. |
| JSONB for flexible configuration | Agent/tool config evolves faster than schema. |
| Encrypted provider keys at rest | DB leaks must not expose model credentials. |
| Hashed API keys | Same reason; plaintext is shown once. |
| UUID keys | Safe for distributed workers and external references. |
| Event bus in Redis Streams | Replay/recovery without adding Kafka to the stack. |
| Plans as validated data, not prompt text | A bad plan is caught before any task or token is spent. |
| Planner always falls back | A provider outage degrades to a single-step plan instead of a failed task. |
| Steps as real child tasks | Ordinary task APIs, events, budgets and retries apply unchanged. |
| Scheduler behind a `StepRunner` protocol | The DAG logic is testable without a database or an LLM. |
| No ORM lazy loading in API | Prevents accidental N+1 and async surprises. |
| Dashboard as a client-side SPA | One static bundle, no BFF to operate, and no proxy sitting in front of the SSE stream. |
| Settings writes limited to an allowlist | The settings table is not a remote config shell; a typo cannot create a key the code never reads. |
| Fail-fast config validation | Misconfiguration is caught at boot, not at first use. |

## Extension points

- **Model providers:** add a `ProviderType` plus an adapter subclass and
  register it in `app/providers/registry.py`; no service or API change needed.
  Two wire formats exist today: `/chat/completions`
  (`OpenAICompatibleProvider` and its vendor subclasses) and the Responses
  API (`CodexResponsesProvider`, which also sends an `originator` header for
  gateways that fingerprint the calling client).
- **Tools:** subclass `Tool`, declare a schema/risk level, and add it to
  `BUILTIN_TOOLS`; the registry row is upserted at boot.
- **Sandbox:** point `SANDBOX_IMAGE` at another image, or mount the Docker
  socket into `api` to move command execution off the host.
- **Metrics backends:** `app/core/metrics.py` renders the Prometheus text
  exposition; a second exporter can read the same registry rather than
  re-instrumenting, and `refresh_state_gauges()` is the single place that turns
  database state into gauges.
- **Coding engines:** implement the engine protocol and register it in
  `app/coding/__init__.py`; the API enumerates engines from the registry.
- **Tools:** `tool_registry` with risk level, permissions, schema and execution
  environment; disabled by default.
- **Skills:** `skills` + `agent_skills` with versioning and frontmatter.
- **Memory:** six layers, dedup keys, hashes, optional embeddings.
- **Agents:** fully database-configured; no code change to add a specialist.
- **Events:** `EventType` plus a durable stream; dashboard/Telegram subscribers
  can attach without touching publishers.
- **Specialists:** add a `SpecialistDefinition` in `app/orchestrator/specialists.py`
  (or a YAML override) and run `agentos seed-agents`; no code change per agent.
- **Planning strategy:** implement the planner protocol in
  `app/orchestrator/planner.py` — decompose, critique, or single-step — without
  touching the scheduler or the service layer.

## Task execution: queue and workers

`POST /api/v1/tasks` writes a task in `pending`. A worker process
(`agentos worker`, its own container) claims it from PostgreSQL and executes it
through the same execution service the API uses; `POST /tasks/{id}/run` is the
inline path kept for development and debugging.

The claim is a lease, not a lock held in memory: `claimed_by` names the worker and
`lease_expires_at` says when the claim lapses. A single query takes a task with
`FOR UPDATE SKIP LOCKED`, so any number of workers can run against one database
without coordinating — each either gets a different row or none. While a task
runs, its worker renews the lease every third of the lease window; if the worker
dies, renewal stops, the lease expires, and the task becomes claimable again. A
worker that fails a task decides between requeueing it (a transport or database
error might not recur) and failing it outright (a missing agent or model will
fail identically every time), so a broken configuration does not burn the queue.

Claiming and execution are separate from the API so a long model run does not
occupy a request, and so the API can be restarted or scaled without interrupting
work in flight. The worker also serves its own `/metrics` on `WORKER_METRICS_PORT`,
because the API's registry belongs to a different process.

## Non-goals so far

- No token-by-token streaming of model output; the SSE endpoint streams *domain
  events*, not partial completions.
- No prompt-cache accounting.
- The queue has no fair scheduling between tenants: claims are ordered by
  priority then age.
- Sandboxed execution exists but the Compose `api` service deliberately has no
  Docker socket, so it falls back to host execution with a logged reason.
- The dashboard has no server-side rendering of business data and no cookie
  session: it is a static client-side SPA over the JSON API (see
  `frontend/README.md`).

## Operational model

- API, worker, bot and dashboard containers plus Postgres and Redis via Compose.
  In a deployment a Caddy proxy is the only public listener.
- Migrations run in the API entrypoint before Uvicorn starts; the worker and bot
  set `AGENTOS_RUN_MIGRATIONS=false` so only one process touches the schema.
- Readiness checks both Postgres and Redis; liveness checks the process only.
- Logs are structured (JSON in production) and include request IDs.
- `/metrics` for Prometheus; `/api/v1/ops/status` and `/api/v1/ops/logs` for the
  dashboard and for an operator without shell access.
- `scripts/backup.sh` / `scripts/restore.sh` cover PostgreSQL, Redis and the
  workspace volume together, because a database-only restore would silently lose
  live events and agent files.
- `agentos prune` applies the retention windows; nothing prunes on its own.
- Secrets come from the environment/`.env`, never from source control.
