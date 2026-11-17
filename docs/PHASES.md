# Delivery phases

Status is updated only when a phase's acceptance criteria are verified.

| Phase | Scope | Status |
| --- | --- | --- |
| 1 | Architecture, repository, database, configuration | **Complete** |
| 2 | Agent runtime + model provider abstraction | **Complete** |
| 3 | Orchestrator and multi-agent coordination | **Complete** |
| 4 | Skill and memory systems | **Complete** |
| 5 | OpenCode/coding engine + tool ecosystem | **Complete** |
| 6 | Make.fun integration | **Complete** |
| 7 | Security research subsystem | **Complete (local workflow; limitations below)** |
| 8 | Telegram integration | Implemented; live verification pending |
| 9 | Web dashboard | **Complete** |
| 10 | Sandboxing, observability, production hardening | **Complete** |
| 11 | End-to-end testing and deployment | **Complete (Telegram delivery and docker sandbox unverified; see below)** |

---

## Phase 1 — Foundation (complete)

**Delivered**
- Repository structure for backend, frontend, docs, scripts, deploy.
- `pyproject.toml` (uv), pinned dependency ranges, Ruff and strict Mypy config.
- Typed settings with secret wrapping, CSV/JSON list parsing, production
  hardening (no debug, no wildcard CORS).
- Structured logging (console/JSON) with request-scoped IDs.
- error hierarchy + uniform JSON error envelope.
- Security primitives: Argon2id, JWT, Fernet encryption, API key hashing.
- 27-table PostgreSQL schema with FKs, checks, unique constraints and indexes.
- Async Alembic migration, applied and drift-checked (`alembic check`).
- Async session layer with pooling.
- FastAPI app factory, lifespan, CORS, security headers, exception handlers.
- Auth API: login, me, API-key create/list/revoke.
- RBAC roles and API-key scopes.
- Health/liveness/readiness probes and `/api/v1/meta`.
- Redis event bus with Stream + Pub/Sub and typed events.
- Operator CLI: `create-admin`, `seed`, `version`.
- Dockerfile (multi-stage, non-root), Compose stack, entrypoint with DB wait and
  migrations, health checks.
- 35 tests (unit + integration), 84% coverage.
- Documentation: README, architecture, database, security, phases.

**Verification performed**
- `uv run pytest` → 35 passed.
- `uv run ruff check` → clean; `ruff format --check` → clean.
- `uv run mypy app` → clean (strict).
- `alembic upgrade head` on a fresh PostgreSQL 16 → 27 tables, 25 enum checks.
- `alembic check` → no drift.
- Docker Compose stack builds and reports healthy; `/health/ready` returns
  `{"database":"ok","redis":"ok"}`.
- Live login, JWT `/me`, API-key creation and API-key `/me` exercised over HTTP.

**Known limitations**
- No agent runtime or orchestration yet.
- No frontend, worker process or rate limiting.
- Memory embeddings are stored as JSONB; pgvector indexing arrives with the
  memory phase.
- Integration tests need a reachable PostgreSQL/Redis; they skip otherwise.

**Acceptance criteria**
- [x] A fresh clone can configure, migrate and boot the API.
- [x] Every specified entity exists in the database with relationships.
- [x] Authentication and role checks are real and tested.
- [x] Secrets are never stored or returned in plaintext.
- [x] CI-style checks (lint, types, tests, migration drift) pass.

---

## Phase 2 — Agent runtime and provider abstraction (complete)

**Delivered**
- Provider abstraction in `app/providers/`: `BaseProvider` (retry with
  exponential backoff, timeout and error mapping), `OpenAICompatibleProvider`
  (OpenAI, OpenRouter, OpenAI-compatible and local servers), `AnthropicProvider`
  and `GoogleProvider`. Every `ProviderType` value is registered, and adapters
  translate messages/tools to each wire format and back.
- Provider-independent data model (`LLMRequest`, `LLMResponse`, `ChatMessage`,
  `ToolCall`, `ToolSpec`, `Usage`) plus a model price catalogue with
  `calculate_cost`/`apply_cost` so every call carries a cost estimate.
- Provider health checks (`health()`, `list_models()`) surfaced through
  `POST /api/v1/providers/{id}/health` and `GET /api/v1/providers/{id}/usage`.
- Tool layer in `app/tools/`: `Tool` ABC with JSON-schema argument validation,
  permission requirements, risk-level approval gating and per-tool timeouts;
  `ToolRegistry` filtering by permission/risk. Built-ins: `echo`, `now`,
  `calculate` (AST-restricted evaluator), `http_get` (SSRF and private-IP
  guard). Declarations are upserted into `tool_registry` at startup.
- Agent runtime in `app/runtime/`: `run_agent` executes an `AgentSpec` against
  any provider, brokers tool calls, enforces iteration/token/cost/deadline
  budgets, honours cancellation checks and emits life-cycle events through a
  listener interface.
- Services: provider CRUD with encrypted keys, agent CRUD with tool/skill
  assignment and versioning, task lifecycle with an explicit transition table,
  and `execute_task` persisting `task_events`, `model_usage`, output, tokens and
  cost in a separate session.
- APIs: `/providers` (CRUD, health, usage), `/agents` (CRUD, tool/skill
  assignment), `/tools` (list, direct execute) and `/tasks` (create, list, get,
  run, cancel, events), all behind role and scope checks with audit records.
- CLI `agentos seed` now also syncs the built-in tool registry.
- 75 new tests (provider payloads via `httpx.MockTransport`, pricing, tool
  validation/SSRF/timeouts/approvals, runtime loop with a fake provider, API
  flows). Total: 110 tests, 77% coverage.

**Verification performed**
- `uv run pytest` → 110 passed; `ruff check`/`format --check` clean; `mypy app`
  clean (strict); `alembic check` → no drift.
- Live Compose stack: provider health check, agent creation with a tool
  assignment, and two task runs against an OpenAI-compatible stub served outside
  the container. Events recorded `task.created → queued → running → progress →
  model.usage → tool.started → tool.completed → task.completed`; `model_usage`
  rows and the provider usage summary matched the reported tokens.

**Known limitations**
- Execution is synchronous inside the API request; queue-based dispatch with
  parallel workers lands in Phase 3.
- Streaming responses and prompt-cache accounting are not implemented yet.
- Approval-gated tools return a failed invocation rather than pausing the run
  into `waiting_approval`; the approval workflow arrives with Phase 5.
  *(Resolved in Phase 5: a blocked call records an `approvals` row, parks the
  task in `waiting_approval`, and a re-run executes only approved tools.)*
- Provider adapters are verified against mock transports, a local stub and one
  live Responses-API gateway; the remaining vendor adapters (Anthropic, Google)
  are still covered only by mock transports.

## Phase 3 — Orchestrator (complete)

Built: LLM planning with a deterministic fallback, a validated execution DAG,
level-by-level parallel scheduling with retries and deadlines, child-task
delegation with dependency handoff, resumable runs, and a human approval
workflow.

### Planning (`app/orchestrator/plan.py`, `planner.py`)

- `PlanStep` carries key, title, instruction, `agent_slug`, mode, `depends_on`,
  priority, and per-step limits (iterations, timeout, token/USD budget,
  approval requirement, free-form metadata).
- `ExecutionPlan.validate(known_agents=…)` enforces the key pattern, at most
  `MAX_STEPS = 50` steps, unique keys, no self-references, no unknown agents,
  no references to undefined steps, and acyclicity (Kahn's algorithm).
  `levels()` returns the topological waves the scheduler executes in parallel.
- `LlmPlanner` prompts the orchestrator model with a `submit_plan` tool, the
  objective, the agent catalogue and optional context. Any failure — provider
  error, unparseable output, invalid plan — degrades to a deterministic
  single-step plan and records `metadata["fallback_reason"]`, so planning never
  blocks a task. Fenced JSON and prose around the JSON object are tolerated.
- `render_dependency_context()` builds the per-step context from completed
  dependency outputs, capped at 6000 characters.

### Scheduling (`app/orchestrator/scheduler.py`)

`run_plan()` walks the plan level by level, running each wave through a
semaphore capped at `max_parallel`. Per step it applies `max_attempts` with
exponential backoff, skips non-retryable errors, enforces the overall deadline
and honours a `cancel_check`. A step exception is contained so one failure
cannot abort the wave; dependants of failed steps are skipped. `ScheduleResult`
reports outcomes, skipped/failed sets, paused step, sequential-level count,
token totals and a human summary.

### Delegation (`app/services/orchestration.py`)

`orchestrate_task()` drives `pending → planning → queued → running → terminal`:

- The plan is persisted on the parent (`task.output["plan"]`) and mirrored into
  `task_steps` so the plan is visible in the ordinary task view.
- Each step becomes a real child `Task` (`parent_task_id`, `max_attempts`) with
  `task_dependencies` edges for ordering. Child ids are stored in
  `task.output["children"]`, so a resume never double-creates them.
- `_ChildStepRunner` gives every step its own DB session, passes the rendered
  dependency context as `extra_context`, records an `agent_messages` handoff
  per step, and reuses a completed child's output on resume instead of re-running
  it (`reopen_for_retry` handles failed children).
- A step flagged `requires_approval` pauses the run into `waiting_approval`;
  `cancel_pending_for_task` clears stale approvals on resume.
- Totals are aggregated from the real child tasks (tokens, cost, iterations),
  and `FAILED` step keys land in `task.error` for the dashboard.
- `resume_orchestration()` rebuilds the plan from the parent, refuses to run
  while an approval is pending, and re-runs only what did not complete.

### Specialists (`app/orchestrator/specialists.py`)

15 built-in agent definitions with real system prompts, tool assignments,
permissions and limits: `main-orchestrator`, `bounty-discovery`, `make-fun`,
`research`, `repo-analysis`, `software-developer`, `smart-contract-auditor`,
`web-security`, `api-security`, `vuln-validation`, `testing`, `optimization`,
`report-writer`, `independent-reviewer`, `compliance`. `sync_specialists()` is
idempotent (create/update/unchanged), only assigns tools that exist in the
registry, and reports the ones it skipped. YAML/JSON overrides load by slug via
`load_overrides()`. Seeding is explicit: `agentos seed-agents`.

### Approvals and messaging (`app/services/approvals.py`, `messages.py`)

Approvals have a TTL (72h default), an explicit decision (`approved` /
`rejected`), single-decision idempotency (a second decision is a 409), expiry
checks, and both a `task_events` entry and an `approval.decided` bus event.
`agent_messages` records agent-to-agent handoffs and renders a transcript with
slug names for the API and, later, the dashboard.

### API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/v1/orchestration/plan` | Preview a plan without running it |
| POST | `/api/v1/orchestration/tasks/{id}/run` | Orchestrate a task (optional explicit plan) |
| POST | `/api/v1/orchestration/tasks/{id}/resume` | Resume a paused/failed run |
| GET | `/api/v1/orchestration/tasks/{id}/children` | Child tasks with plan keys |
| GET | `/api/v1/orchestration/tasks/{id}/messages` | Agent handoff transcript |
| GET/POST | `/api/v1/approvals` | List / create approvals |
| GET | `/api/v1/approvals/{id}` | Approval detail |
| POST | `/api/v1/approvals/{id}/decide` | Approve or reject (operator+) |

### Schema

Migration `e146a62bc18c` adds `tasks.attempts` and `tasks.max_attempts` so
retries are auditable and bounded per task.

### Provider addition — Responses API (`codex_responses`)

Integration testing surfaced a real gateway that refuses the legacy
`/chat/completions` contract and only serves the **Responses API** when an
`originator` client header is present. Rather than special-casing a vendor, the
stack gained a seventh provider type:

- `app/providers/codex_responses.py` — `CodexResponsesProvider` translates
  system messages into `instructions`, tool calls into `function_call` items and
  tool results into `function_call_output`, sends flat tool definitions, and
  normalises `output` items back into `LLMResponse`. Gateway errors returned with
  HTTP 200 (`status: "failed"`, `error.message`) are surfaced as `ProviderError`,
  and `status: "incomplete"` maps to a `length` finish reason.
- `ProviderType.CODEX_RESPONSES` plus a registry entry and migration
  `9962ec813293` that widens the `provider_type` CHECK constraint (round-trip
  tested with `alembic downgrade -1` / `upgrade head`).
- 6 new adapter tests (history/instruction translation, tool-call parsing,
  incomplete responses, gateway errors, malformed arguments, model listing).

### Tests

47 new tests: `tests/test_orchestrator_plan.py` (28 unit tests covering plan
validation, cycles, levels, planner parsing and every fallback, dependency
context rendering, and scheduler ordering/concurrency/skip/retry/cancel/pause/
deadline), `tests/test_orchestration_api.py` (15 integration tests covering plan
preview, end-to-end delegation with dependency propagation, explicit plans, step
failure with skipped dependants, retries, invalid/cyclic plans, run idempotency,
the approvals API and its events, role enforcement, resume guards and specialist
sync idempotency), and 6 adapter tests for the new provider type. Total: 159
tests.

### Verification performed

- `uv run pytest` → 159 passed; `ruff check`/`format --check` clean; `mypy app`
  clean (strict, 82 files); `alembic upgrade head` applied `e146a62bc18c` and
  `9962ec813293`, and `alembic check` reports no drift.
- Live Compose stack rebuilt: the OpenAPI document exposes all eight new routes.
  With an external OpenAI-compatible stub, `POST /orchestration/plan` returned a
  two-step LLM plan, `POST /orchestration/tasks/{id}/run` completed both steps,
  the report step's prompt contained the scan step's output, the parent's
  `used_tokens` summed to 280 across children, `task_steps` mirrored both steps,
  two `handoff` messages were recorded, and the approval created and decided
  through the API produced an `approval.decided` task event and a 409 on a
  second decision.
- Live run against a **real production model** (`gpt-6.1-sol` through the
  `codex_responses` provider): the provider health check discovered both models
  in 642ms, all 15 specialists were seeded onto it, the planner produced a real
  two-step DAG (`software-developer` → `independent-reviewer`), and the run
  completed with 2684 tokens across two steps. The reviewer genuinely criticised
  the implementer's `fibonacci` (an `int` subclass bypassing the negative check)
  and proposed a fix, which is the behaviour the specialist prompts ask for.
  Both child tasks, both `task_steps` rows, two `agent_messages` handoffs and
  the `model_usage` rows were persisted.

### Known limitations

- The Responses adapter is non-streaming; streaming is a later phase.
- Execution still happens inside the API request; the queue worker that consumes
  `Streams` events and runs orchestrations in the background lands with Phase 5's
  coding engine, where long-running containers make it necessary.
- The planner is single-attempt; a provider outage falls back to a one-step plan
  rather than retrying the planning call.
- Tool-level approval gates still fail the invocation rather than pausing the
  run; wiring tool approvals into `waiting_approval` follows with the Phase 5
  tool set. *(Resolved in Phase 5.)*
- Verification (`use_llm_planner`'s `verify` option) runs the reviewer agent but
  does not yet block completion on its verdict.

## Phase 4 — Skills and memory (complete)

**Delivered**

*Skills*
- `app/skills/loader.py`: SKILL.md parser and validator. YAML frontmatter is
  parsed with `yaml.safe_load`, the body is Markdown, the file is capped at
  256 KiB and must decode as UTF-8. Required `name`/`description`; optional
  `version`, `slug`, `allowed-tools`, `tags`, `license`, `metadata`. Slugs are
  normalised (`[a-z0-9-]`, ≤64 chars); an explicitly declared `slug` is
  validated verbatim rather than silently repaired, because a typo there is a
  packaging bug the author must see. Versions must be semver.
- Directory import (`copy_skill_directory`) copies a skill tree into the managed
  skills root, skipping `.git`/`__pycache__` and refusing to escape the root.
  Discovery (`discover_skills`) walks the root without following directory
  symlinks — an intentional boundary so a packed skill can never read outside
  the tree; a symlinked `SKILL.md` is reported as an error entry rather than
  silently loaded.
- Five builtin skills: `code-review`, `bug-repro`, `test-authoring`,
  `security-audit`, `report-writing`. They are installed idempotently at startup
  (content hash comparison) and are `is_system`, so they can be disabled but
  not deleted.
- `app/services/skills.py`: install/update/validate/enable/disable/delete,
  version history, agent assignment, execution history, directory and git/URL
  import (with an http(s)-only guard), and `sync_builtin_skills`.
- Builtin `load_skill` tool: an agent can only load a skill assigned to it, and
  every load is recorded in `skill_executions`. The skill body is injected as
  the tool result, so the model reads it on demand instead of paying for every
  catalogued skill in the system prompt.

*Memory*
- Six layers (`working`, `conversation`, `episodic`, `semantic`, `project`,
  `agent`) with versioning, deduplication by content hash, and provenance
  (`source`/`source_ref`).
- Hybrid retrieval: lexical (token overlap), vector (pgvector cosine),
  recency (exponential half-life) and usage (log-scaled `access_count`) are
  combined into one score; the weights live in `app/memory/scoring.py`. A
  configured embedder that fails degrades to lexical retrieval with a warning
  instead of failing the request.
- Sensitive-content guard: contents matching key/token/private-key patterns are
  rejected (`memory_sensitive_content`) rather than stored; redaction is
  available for log/summary paths.
- Summarisation is deterministic (`summary` derived from content, no model
  call), so `POST /memory/context` is cheap enough to run on every task.
- TTL via `expires_at`, and `POST /memory/purge-expired` (admin-only) removes
  what has lapsed.
- Isolation: any scope tag recorded on a memory (`agent_id`, `project_id`,
  `task_id`, `user_id`) is required again on read; rows with that tag unset are
  shared. The operator listing passes `agent_scope=False` so an admin can see an
  agent's memories, but user scoping is never skipped.
- `remember`/`recall` tools, plus runtime wiring: `AgentSpec.resources` carries
  the memory services into `ToolContext`, and the assembled system prompt gets a
  skill catalogue (assigned skills only) and a memory context block.
- Task outcomes write an `episodic` memory automatically. The write runs inside
  a savepoint *after* completion, so a memory failure is logged but can never
  roll back a finished task.
- Every specialist is granted `memory:write` and the three memory tools by
  `sync_specialists`; the grant is additive, so operator edits to other
  permissions survive a re-sync.

*Schema*
- Migration `df885b5d878f`: `CREATE EXTENSION vector`, `memories.embedding_vec`
  (`vector(1536)`) + `embedding_dim`, an HNSW cosine index
  (`ix_memories_embedding`), and the `skill_executions` table.
- The JSONB `embedding` column is kept alongside the pgvector column: it is the
  portable record (works without the extension, survives a database that cannot
  install it) while `embedding_vec` is the search index.

**Tests**

133 new tests, 292 total (up from 159 at the end of Phase 3).
- `tests/test_skill_loader.py` (27): frontmatter/YAML/slug/semver/size/UTF-8
  rejection, discovery, symlink handling, copy exclusions, prompt rendering.
- `tests/test_memory_core.py` (37): scoring components and weights, cosine
  similarity, sensitivity detection and redaction.
- `tests/test_skills_api.py` (15): seeding, detail/files, create + duplicate
  conflict, invalid metadata, validate (including path escape), enable/disable/
  delete, builtin delete conflict, assignment, viewer 403, sync idempotency,
  versions, execution history, unassigned load, URL import guard.
- `tests/test_memory_api.py` (19): CRUD, secret rejection, dedup/versioning,
  search ordering, agent/project/user isolation, context budget, summaries,
  updates, delete, filters, purge, TTL, `record_task_outcome`, vector
  degradation, access tracking.
- `tests/test_memory_tools.py` (13): `load_skill` injection and errors,
  `remember` payload and permission, `recall` snippets, spec completeness,
  `AgentSpec.resources` → `ToolContext`.
- `tests/test_specialists.py` (8): every specialist has the memory tools and
  grant, sync idempotency, operator grants preserved, overrides re-merge.
- `tests/test_agent_context.py` (9): assigned-skill catalogue only, memory in
  extra context, `memory_config` opt-out and layer restriction, tool resources,
  unassigned skill not found, skill load recorded, episodic write on success.
- `tests/test_memory_vector.py` (5): the pgvector path itself — vector width,
  hybrid vector score, `cosine_distance` ordering, agent isolation in
  `vector_search`, and failure degradation. These use a deterministic stub
  embedder because the configured real provider exposes no embedding model.

**Real bugs found by these tests and fixed**
- `BUILTIN_SKILLS_DIR` pointed at `app/services/builtin` instead of
  `app/skills/builtin`, so startup sync installed nothing.
- `copy_skill_directory` recursed into itself when the source was already at its
  destination.
- Explicit frontmatter slugs bypassed normalisation; a slug like `Code Review`
  was accepted verbatim and then never matched on load.
- `visibility_filter` did not require every recorded scope tag, so an agent
  could read another agent's memory when a project tag was also set.
- Memory writes started at `access_count=1`, inflating usage scores for rows
  nothing had ever read.
- `POST /memory/search` expired `updated_at` on the ORM objects it touched,
  producing `MissingGreenlet` on refresh.
- The API image created `/data/workspaces` but not `/data/skills`, and Compose
  mounted no volume there, so `SKILLS_ROOT` fell back to a directory the
  container user could not write. Startup sync logged `PermissionError` and the
  builtin skills were never installed inside Docker — invisible in the local
  test suite, which runs as a user that can write its own skills root.

**Verification performed**
- `uv run pytest` → 292 passed; `ruff check` and `ruff format --check` clean;
  `mypy app` clean (strict, 96 files).
- `alembic check` → no drift; `alembic current` → `df885b5d878f (head)`.
- Live Compose stack rebuilt and restarted **after** the Dockerfile/Compose fix:
  the `agent_skills` volume was removed and recreated from scratch, startup
  logged `builtin_skills_synced directories=5 failed=0` and
  `/data/skills` contains all five packages owned by `agentos`. Local
  `agentos seed-skills` was also verified against `SKILLS_ROOT`
  (`installed=0 unchanged=5` on a second run, i.e. idempotent).
- Live database: `pg_extension` reports `vector 0.8.7`; `memories` has
  `embedding_vec vector(1536)` and the HNSW index `ix_memories_embedding`;
  all 15 specialists hold `memory:write` and the `load_skill`/`recall`/
  `remember` tool rows.
- Live HTTP with a real owner token: `GET /skills` returned the five builtin
  skills; `POST /skills/{id}/assign/{agent}` assigned `code-review`;
  `POST /skills/sync` reported all five unchanged; `POST /skills/validate`
  returned `valid: true` with the expected warning; `POST /memory` stored a
  semantic memory; `POST /memory/search` ranked it first (score 0.549);
  `POST /memory/context` produced a one-item context block; a content string
  containing an API key was rejected with 422.
- **End-to-end against the real model** (`gpt-6.1-sol` via `codex_responses`):
  a task instructed to load `code-review` and store a fact completed in three
  iterations, calling `load_skill` (6 ms) and `remember` (5 ms). The reply
  quoted the skill's actual first checklist item, proving the skill body reached
  the model. Persisted afterwards: one `skill_executions` row linked to the task,
  one `agent`-sourced semantic memory, and the automatic `task_outcome` episodic
  memory.
- pgvector retrieval verified with a stub embedder (5 tests): stored vectors are
  1536-dimensional, `cosine_distance` ordering picks the right row, and
  agent-scoped vector search leaks nothing.

**Known limitations**
- The provider used for the live checks exposes only chat models and no
  embedding model — so the live deployment runs on lexical
  retrieval. `memory_embedding_provider`/`memory_embedding_model` are unset by
  default; point them at an embeddings endpoint and the vector path activates
  with no code change. This is why the vector path is covered by stub-embedder
  tests rather than a live one.
- Summarisation is extractive, not model-generated. Cheap and deterministic, but
  a long memory's summary is a truncated excerpt rather than a paraphrase.
- Memory context is injected as a system-prompt block; there is no
  context-window-aware eviction beyond the per-request token budget.
- Skill import accepts a git URL or a directory; there is no dependency
  resolution between skills and no signature verification of imported content.
- `skill_executions` grows without bound — no retention job yet.

## Phase 5 — Coding engine, workspace and tool ecosystem (complete)

**Delivered**

*Workspace manager* (`app/workspace/paths.py`)
- One project maps to one directory under `WORKSPACE_ROOT`. Slug validation
  (`[a-z0-9-]`, 1–64 chars) happens before any path is built, so a slug can
  never introduce a separator or a traversal segment.
- Every read and write goes through `resolve()`, which rejects absolute paths
  and Windows drive paths, resolves symlinks and then re-checks containment.
  A symlink that points outside the root — file or directory — is refused with
  `WorkspaceError`, because resolving first is the only way to see it.
- Per-file read/write size caps, a bounded tree walk (noise directories such as
  `.git`, `node_modules`, `__pycache__` skipped, entry cap enforced), and a
  regex search with a glob filter, a result cap and a large-file skip.
- All filesystem work runs in `asyncio.to_thread` so a big tree cannot block the
  event loop.

*Sandbox* (`app/workspace/sandbox.py`)
- `LocalRunner` executes in the workspace directory with a **pared environment**
  (only `PATH`, `HOME`, `LANG`, `TERM` and friends; any name starting with
  `AWS_`, `GITHUB_`, `OPENAI_`, `ANTHROPIC_`, `AGENTOS_` etc. is dropped) and a
  hard timeout that kills the process group.
- `DockerRunner` runs the same command inside `SANDBOX_IMAGE` with
  `--network=none`, `--read-only` root, a tmpfs `/tmp`, `--pids-limit`,
  memory/CPU caps, the workspace bind-mounted at `/workspace`, and
  `--user <uid>:<gid>` of the workspace owner (`run_as_owner=True`) so files the
  container writes stay usable by the host.
- `resolve_runner()` picks Docker only when sandboxing is enabled **and** the
  image actually exists (`docker image inspect`), and returns a report with the
  runner plus a human-readable reason; the four outcomes are "sandboxing
  disabled", "image is missing", "Docker unavailable" and "docker available".
  The chosen runner and reason are logged on every task that has a workspace.

*Sandbox image* (`backend/sandbox/Dockerfile`, Compose profile `sandbox`)
- `debian:bookworm-slim` plus shell, git, python3, node/npm, ripgrep, patch and
  jq, an unprivileged `sandbox` user (uid 10001) and `/workspace`. Built with
  `make sandbox-image` or `docker compose --profile sandbox build sandbox`.

*Coding engines* (`app/coding/`)
- `OpencodeEngine` shells out to `opencode run --format json` with the
  workspace as `--dir`, a model, an optional agent and a session, parses the
  event stream into a `CodingResult`, detects the files the run changed, and
  writes a transcript.
- `ShellEngine` runs a single command with no model in the loop — useful for
  scripted verification.
- `CodingEngineRegistry` resolves an engine by name, reports availability by
  probing the binary, and falls back explicitly rather than pretending an
  engine works.

*Tools* (`app/tools/coding.py`)
- `read_file`, `list_files`, `search_code` (low risk, `filesystem:read`).
- `write_file`, `delete_file`, `git` (high risk, `filesystem:write`) and
  `run_command` (high risk, `tools:execute`). High-risk tools require human
  approval; they are per-agent assignable like every other tool.

*Project API* (`app/api/v1/projects.py`)
- `GET/POST /projects`, `GET /projects/{id}`, `GET /projects/engines`.
- `GET /projects/{id}/workspace`, `GET /projects/{id}/files`,
  `GET|PUT|DELETE /projects/{id}/file`, `POST /projects/{id}/search`.
- `POST /projects/{id}/commands` (admin), `POST /projects/{id}/repositories`
  (admin), `POST /projects/{id}/coding/run` (admin),
  `GET /projects/{id}/coding/sessions`.
- Read routes need `viewer`, file writes and search need `operator`, and
  host-command execution, repository cloning and coding runs need `admin`.
  Every mutation is audited.

*Human approval for high-risk tools* (`app/runtime/loop.py`,
`app/services/execution.py`)
- Approval-gated tools are now **advertised** to the model instead of being
  silently dropped, so the model can ask for them and the request becomes a
  real record. `ToolRegistry.allowed_for(..., include_approval_gated=True)` is
  what the runtime uses; permission failures are still never advertised.
- A blocked call appends to `RuntimeResult.pending_approvals`, calls an
  `approval_callback`, and returns a tool result that says plainly it did not
  run. The callback (`ApprovalRequester`) writes an `approvals` row
  (`action_type=run_high_risk_tool`, payload carries tool, arguments and risk)
  in its own session, so the record survives a failed run and cannot roll the
  task back. A duplicate pending request for the same tool on the same task is
  suppressed.
- `pending_approvals` on the result moves the task to `waiting_approval`
  instead of `completed`.
- `spec.approvals` is populated from **approved** approvals for the task, so a
  re-run executes exactly the tools a human signed off and nothing else.
  Approving twice, or approving a rejected/expired request, is rejected by the
  approvals service.

*Persistence*
- `coding_sessions` table (`app/db/models/coding.py`): project/task/agent/user
  FKs, engine, model, status (`running`, `completed`, `failed`, `timed_out`),
  instruction, summary, JSONB changes/commands/meta, transcript path, exit
  code, duration, timestamps and three indexes. Migration `b41c7f2d9ab0`.
- Agent wiring: `resolve_workspace()` injects `workspace`, `command_runner` and
  `sandbox` into `AgentSpec.resources`, so a coding tool receives a runner
  without importing the workspace layer.
- Specialist grants: `software-developer` gets the full coding set,
  `repo-analysis`/`smart-contract-auditor`/`report-writer`/`independent-reviewer`
  get read-only coding tools, `testing`/`optimization` add `run_command`, and
  non-coding specialists get none.

**Verification performed**
- `uv run pytest` → 415 passed; `ruff check`/`ruff format --check` clean;
  `mypy app` clean (strict, 105 files); `alembic check` → no drift, head
  `b41c7f2d9ab0`.
- Sandbox image built and exercised live: a command run through `DockerRunner`
  reported `runner: docker`, `whoami` → the workspace owner uid after
  `run_as_owner`, `pwd` → `/workspace`, `git version 2.39.5`, a file written
  inside the container appeared on the host, and `curl` failed with
  `net_exit=6` (network blocked).
- Live HTTP on the Compose stack: project create, file write/read/delete, tree
  listing, search, and `POST /commands` (`exit_code 0`, sandbox `local` because
  the API container has no Docker socket). Path traversal (`../../escape.txt`)
  and an absolute read (`/etc/passwd`) were both refused with 403.
- Live agent run (`gpt-6.1-sol`): the repository-analysis agent listed the
  workspace and read `src/greet.py`, then reported the function accurately with
  file-level evidence.
- Live approval round trip: the software-developer agent asked for `write_file`
  and `run_command`; the task moved to `waiting_approval`, two `approvals` rows
  were created with the exact arguments, and both tool calls were refused. After
  approving both through `POST /approvals/{id}/decide`, a re-run executed both
  tools, created `src/util.py` inside the workspace volume, ran the verification
  command and printed `cba`, and the task completed. `audit_logs` recorded both
  decisions and the run.

**Known limitations**
- The API container has **no Docker socket mounted** (a deliberate security
  decision), so `DockerRunner` cannot be used from inside the Compose stack: a
  task with a workspace falls back to `LocalRunner` with the reason logged. To
  use the sandbox from the API, mount the socket into `api` (and accept that a
  compromised API then controls the host daemon) or run command execution in a
  dedicated worker container that has the socket.
- `opencode` is not installed in the API image, so `/projects/engines` reports
  `opencode` as unavailable there and shell runs are used instead. Install it
  in the image (or a worker) to enable agentic coding runs.
- Approval is per tool per task, not per exact argument set: approving
  `run_command` once lets that task re-run any command. Argument-level approval
  would need a hash of the call in the approval payload.
- There is no resume-from-approval endpoint that replays the paused run; the
  caller re-runs the task, which re-asks the model.
- Workspace disk quota (`execution.workspace_quota_mb`) is a setting with no
  enforcement yet, and there is no retention/cleanup job for workspaces.
- GitHub/browser/MCP tools from the original plan are not in this phase.

## Phase 6 — Make.fun (complete)

**Source of truth.** `https://make.fun/skill.md` (v1.5.182) and the public
OpenAPI document it links (`/api/openapi.public.json`, 155 paths). Every route
the platform calls is in that document; nothing was guessed. A copy of the
machine-readable document was saved while building this phase and used to check
parameter names and status codes.

**Delivered**

*Client* (`app/bounty/makefun.py`)
- `MakeFunClient` wraps the documented routes: `GET /api/competitions` (cursor
  paging, `status`/`category`/`limit`), `GET /api/competitions/{slug}`,
  `/requirements`, `/quota`, `/leaderboard`, `GET /api/agents/me`,
  `POST /api/competitions/{slug}/submissions` (multipart) and
  `GET /api/submissions/{id}/status`.
- `x-api-key` is sent only when configured and never logged. Every read route
  works unauthenticated at the lower rate limit, so discovery does not require
  the key; writes do.
- Retries: `429`/`5xx`/timeouts only, with `Retry-After` honoured and capped at
  30 s, two attempts, and an exponential fallback. A `4xx` is never retried.
- Redirects are **not** followed. This endpoint decides whether a submission is
  accepted, so a `3xx` is raised as an error rather than silently moving that
  decision to another host.
- Errors split three ways, deliberately:
  - `401`/`403` → `MakeFunError`. A lapsed 30-day key says nothing about an
    entry; recording it as a rejection would mark a good submission as failed.
  - `4xx` on submit → `MakeFunProblem` carrying the RFC 9457 document, its
    `stage`, its `upstream_status` (`402` = unpaid entry fee) and each check
    verdict with its `fix`. Our API answers `422` for it, because the caller has
    something to fix.
  - `5xx`/transport → `MakeFunError`, entry state untouched.

*Service* (`app/services/bounty.py`)
- `sync_competitions` upserts the board into `bounty_programs` (slug
  `make-fun`) and `competitions` keyed by `(program_id, external_id)`, and
  returns what it created and updated rather than a boolean.
- Status mapping never overstates: `resolved`/`closed` → `closed`, `draft` →
  `discovered`, and an `open` listing whose `deadline` has passed is stored
  `closed` — the API stops accepting entries then, so reporting it open would
  invite an entry that gets refused.
- `refresh_requirements` stores the whole document verbatim and moves the
  deadline to `deadlines.entries_close`. `requirements_summary` extracts what an
  operator acts on: required deliverables, every check with its `fix`, the
  deadlines, the entry quota, the payout curve and escrow, and
  `transcripts_required` read off the deliverables' `required` flags.
- `ensure_project` binds a `bounty-<slug>` project workspace on first use.
- `prepare_entry` writes the entry, opens an `approvals` row
  (`action_type=submit_competition`, payload carries repo, commit, title and
  transcript paths) and stops at `awaiting_approval`. It never touches the
  network.
- `submit_entry` re-reads the approval and refuses unless it is `approved`;
  then submits, stores `submission_id`, and publishes `submission.completed`.
  A `MakeFunProblem` is **stored** on the entry (status `rejected`, the check
  verdicts, the problem document) before being re-raised, because the verdicts
  are the diagnosis.
- `refresh_entry_status` maps the platform's status onto ours, and copies the
  score, rank, payout and checks.

*Persistence*
- `competition_entries` (`app/db/models/entry.py`): `competition_id` (CASCADE),
  `project_id`/`approval_id`/`created_by` (SET NULL), `submission_id`, title,
  repo URL, commit SHA, status, JSONB `transcript_paths`/`checks`/`payload`/
  `meta`, score, rank, `prize_usd` (`Numeric(18,2)`), error, `submitted_at`,
  `last_synced_at`, three indexes. Migration `021384d3b1e9`.
- `CompetitionEntryStatus`: `draft`, `prepared`, `awaiting_approval`,
  `submitted`, `rejected`, `validating`, `graded`, `placed`, `won`, `failed`,
  `withdrawn`.

*API* (`/api/v1/bounties`)
- Reads (viewer): `GET /programs`, `GET /competitions` (`status`, paging),
  `GET /competitions/{id}`, `GET /competitions/by-slug/{slug}`,
  `GET /competitions/{id}/entries`, `GET /entries`, `GET /entries/{id}`.
- Operator, audited: `POST /competitions/sync`,
  `POST /competitions/{id}/requirements/refresh`, `GET /competitions/{id}/quota`,
  `POST /competitions/{id}/entries` (prepare), `POST /entries/{id}/submit`,
  `POST /entries/{id}/refresh`.
- Live competitions sort ahead of closed ones: ordering purely by deadline put
  six dead listings on the first page.

*Tools* (`app/tools/makefun.py`, resources in `app/services/bounty_tools.py`)
- `makefun_competitions` (low) reads the local mirror and says so
  (`source: "local mirror"`); `refresh: true` performs a real sync first.
- `makefun_competition_details` (low) returns the requirements summary, fetching
  the document when it is missing or `refresh` is set.
- `makefun_prepare_entry` (medium, `bounty:write`) records the payload and opens
  the approval.
- `makefun_submit_entry` (critical, `bounty:submit`) is refused by the runtime
  unless a human approved that tool for the run, **and** it re-checks the
  entry's own approval before calling out. The two gates cannot be collapsed.
- Domain refusals (closed competition, missing approval) become tool errors the
  model can read, not unhandled exceptions.
- Grants: `bounty-discovery` gets both read tools; `make-fun` gets reads plus
  `makefun_prepare_entry` — it prepares the payload for approval but cannot
  submit.

*Config*
- `MAKEFUN_API_KEY` (optional `SecretStr`), `MAKEFUN_BASE_URL`,
  `MAKEFUN_TIMEOUT_SECONDS`, `MAKEFUN_SYNC_LIMIT`; wired into `.env.example`.

**Verified live** (2026-11-16, public routes, no key)
- `POST /bounties/competitions/sync` stored 18 listings; 12 remain actionable
  and 6 past their deadline were stored `closed`.
- `GET /bounties/competitions` returned live competitions first with real
  rewards, deadlines and `https://make.fun/bounty/<slug>` URLs.
- `POST .../requirements/refresh` on `tiny-chess-01` stored the document (52
  checks), set the deadline to `2026-11-24T17:00:00Z`, read the quota
  (`max_per_day: 5`), the 20-place payout curve and the required deliverables
  (`repo_url`, `commit_sha`, `bundle`, `LICENSE`, `main.py`), and reported
  `transcripts_required: true`.
- Preparing an entry created `bounty-tiny-chess-01` under
  `/data/workspaces/`, an entry in `awaiting_approval` and a pending
  `submit_competition` approval. Submitting before approval returned `409`.
  After approving, the submit reached make.fun and came back
  `401 {"message": "sign in required"}` → our API answered `502
  makefun_error` and the entry stayed `awaiting_approval` with no
  `submission_id`. **This is the honest end state: no API key is configured, so
  no submission was made.**

**Gate**
- `uv run pytest` → **452 passed** (37 new: 16 client, 21 API/integration).
  Integration tests drive the client through `httpx.MockTransport`, so no test
  depends on make.fun being reachable.
- `uv run ruff check app tests` and `ruff format --check` clean; `uv run mypy app`
  clean (113 files); `uv run alembic check` reports no drift.

**Fixed during verification**
- `alembic heads` (and therefore any fresh-database `alembic upgrade head`)
  crashed with `NameError: Can't invoke function 'f'` because revision
  `9962ec813293` called `op.f()` at import time, outside a migration context.
  The name is now computed inside `upgrade()`/`downgrade()`, and a full
  `upgrade head → downgrade base → upgrade head` roundtrip was run on a scratch
  database. The existing dev database was already at head, which is why the bug
  was invisible until a clean bootstrap was tried.
- Submitting after approval with no API key returned `422 makefun_entry_refused`
  and stored the entry as `rejected`. `401`/`403` were being classified as a
  problem document before authentication was checked, so a *deployment* fault
  was recorded as a *verdict* on the entry. Authentication and transport errors
  are now classified first; the same call returns `502 makefun_error` with
  `details.status_code: 401` and the entry stays `awaiting_approval` with no
  `submission_id`. Two regression tests cover both directions (401 and 502 on
  submit).

**Known limitations**
- **No make.fun API key is configured**, so authenticated routes (`/quota`,
  `POST /submissions`, `/api/agents/me`) are exercised only against a
  `MockTransport` stub. Minting a key needs a *Sign in with Solana* handshake
  with a Solana keypair (`POST /api/auth/solana/key`), which this deployment
  does not have. Submitting therefore fails with `502 makefun_error: sign in
  required` — deliberately loud, and the entry is not marked rejected.
- No entry has ever been accepted by the real platform, so the success path
  (`202` with `submission_id`/`claim_code`), grading and payouts are only
  covered by tests. Nothing in this phase claims otherwise.
- Transcripts are **recorded, not uploaded**: `transcript_paths` holds
  workspace-relative paths, and `submit_entry` sends the form fields. Sending
  the bundle needs the files read from the workspace and passed as multipart
  parts — the client supports it (`transcripts` triples), the service does not
  assemble them yet. make.fun refuses entries without authentic transcripts, so
  this must be closed before a real submission.
- Escrowed prizes require a Solana-signed `entry_signature`; the client accepts
  one but nothing produces it. An escrowed competition cannot be entered until
  an operator supplies a signature.
- `required_permissions` on the write tools use `bounty:write`/`bounty:submit`,
  which are new permission strings; only the two bounty specialists hold them.
- The MCP endpoint (`https://make.fun/mcp`) was inspected but not wired: the
  REST API covers every operation this phase needs, and a second transport would
  duplicate the approval gate.
- Sync is on demand (endpoint or tool), not scheduled; a recurring sync belongs
  with the Phase 8/10 automation work.

## Phase 7 — Security research

Delivered a local research workflow under `/api/v1/research`:

- Scope registration, admin verification, expiry and revocation. Records bind
  authorization, permitted scanner names, a project subtree and exclusions.
  External target names are records only; no remote testing endpoint exists.
- Hypothesis creation and severity/confidence prioritization; scanner leads can
  be imported idempotently as hypotheses with scanner evidence.
- Findings move from hypothesis to validating, then confirmed or false positive.
  Confirmation requires validation evidence and a review by a different user.
  Reviews hash the finding and evidence; new evidence invalidates the review.
  Evidence becomes immutable after disposition. CVSS scores and CWE identifiers
  are operator supplied, validated fields, not automatically calculated claims.
- Markdown reports include actual lifecycle status and evidence hashes. Report
  generation never marks findings reported or sends them externally.
- Fixed command adapters for Semgrep, Gitleaks, Slither, Forge and Echidna.
  All scans require Docker with network disabled; no host fallback is allowed.
  Exclusions, traversal, symlink trees and tool permissions are checked before
  execution. Malformed, truncated and timed-out reports are failed scans.
- Three project-bound agent tools: `research_scopes`, `research_hypothesis`,
  `research_report`. Agents cannot verify authorization or approve a review.
- Three new tables: `research_scopes`, `security_scans`, `finding_reviews`, plus
  nullable `findings.scope_id`. Migration head: `9bbfe701af18` (33 tables).

Verification: API integration tests cover scope gates, independent review,
stale reviews, confirmation and reports. Parser tests cover errors and secret
omission; scan import tests cover idempotency and project isolation. A real
Semgrep 1.99.0 scan in `agentos/research:latest` found the expected dynamic-eval
lead with Docker networking disabled. A fresh database completed upgrade to
head, downgrade to base, re-upgrade and a clean Alembic drift check.

Checks: full backend suite 466 passed, 1 opt-in Docker test skipped. A later
redaction regression brought the targeted research checks to 15 passed; the
opt-in real Semgrep test passed separately. Ruff and strict Mypy passed (121
application files). The API image has not been rebuilt for Phase 7.

Limitations: only Semgrep was exercised as a real scanner. Other adapters need
their binaries/compilers installed in a custom research image. pip-audit is
listed but execution is disabled because advisory lookup needs network access.
The Compose API has no Docker socket, so it reports scanners unavailable;
execute scans from a trusted host deployment with Docker access. Research scans
are synchronous and bounded to 600 seconds. Hypotheses originate from operator
or agent analysis and scanner leads; there is no autonomous attack-surface
mapper or automatic vulnerability confirmation. Validation evidence is an
operator attestation reviewed independently, not a proof generated by the API.

## Phase 8 — Telegram

Implemented a project-owned async Bot API client and secret-authenticated
webhook. Telegram IDs must be allowlisted and admin-linked to an active user.
Private chats only; platform roles apply. Commands cover help/status, agents,
task creation/listing/cancellation, synced bounties, scanner inventory, finding
reports, cost and inline approvals. Created tasks remain pending; execution uses
the task API. `telegram_updates` prevents mutation replay on webhook retries.
Notification delivery reports task status/cost/errors and pending approvals via
an admin endpoint, with an optional project scheduling script.

Verification: full suite 475 passed, 1 opt-in scanner skip; Ruff, strict Mypy and
Alembic drift checks pass. Telegram tests use mocked transport. Live delivery is
unverified: no bot token/public webhook is configured. No scheduler is registered
and the API image has not been rebuilt. Notifications are status snapshots;
`/scan` reports availability. Outbound delivery is at least once. Schema head
`e80e55457f5e`, 34 tables. Setup: `docs/TELEGRAM.md`.

## Phase 9 — Dashboard

Delivered a Next.js 16 (App Router) control plane in `frontend/` plus the API
surface it needs.

**Backend**
- `/api/v1/dashboard`: `summary` (health, agent/task status counts, pending
  approvals, open competitions, findings by severity, token and cost totals,
  recent audit activity), `usage` (per-day requests/tokens/cost), `activity`
  (paginated audit feed), `events` (recent Redis Stream entries) and `stream`
  (SSE). Every value is a query against persisted rows; an empty install
  reports zeros. Expenses come from `model_usage`, never estimates.
- `/api/v1/dashboard/stream` forwards the `agentos:events` Pub/Sub channel as
  `text/event-stream` with a named event per `EventType`, `retry: 3000`, and a
  keepalive comment every 20 s. `X-Accel-Buffering: no` stops nginx from
  buffering the stream. `EventSource` cannot send headers, so this one route
  also accepts the JWT as `?token=`; it is verified by the same code path as a
  bearer token (`principal_from_token`) and still requires operator or higher.
- `/api/v1/settings` (admin): list settings with secrets redacted, and update a
  value. Only keys in an `EDITABLE_KEYS` allowlist can be written; secret values
  are never returned and writing an unknown key is a `404`, not a silent upsert.
- Role gates are enforced per route: `summary`/`usage` viewer,
  `activity`/`events`/`stream` operator, `settings` admin, and the existing
  agent/task/project rules unchanged.

**Frontend** (`frontend/`, 4.6k lines, TypeScript strict)
- 13 routes: login, dashboard, agents + agent detail, tasks + task detail,
  bounties, research, skills, models, terminal, approvals, settings.
- Client-side SPA: JWT in `localStorage`, `Authorization: Bearer`,
  `NEXT_PUBLIC_API_URL` / `NEXT_PUBLIC_API_PREFIX`. No BFF, no cookie session —
  chosen so the build is a static bundle and nothing buffers the SSE stream.
- Live event panel on the dashboard consumes the SSE route through
  `EventSource` and keeps a bounded in-memory buffer.
- Role-aware nav (viewer < operator < admin < owner) that hides what the API
  would refuse anyway; no page treats the nav as authorization.
- Dark theme from a small `@theme` token set, hand-written primitives
  (`ui.tsx`) and hand-written SVG charts (`charts.tsx`).
- `output: "standalone"` multi-stage `node:24-alpine` image, non-root, added to
  Compose as `web` on `${WEB_PORT:-3000}`.

**Verification performed**
- `uv run pytest` → **481 passed, 1 skipped** (6 new: 4 dashboard, 2 settings).
  `ruff check`, `ruff format --check`, `mypy app` (133 files) and
  `alembic check` all clean. No migration: Phase 9 added no tables.
- `npm run typecheck` → clean; `npm run lint` → clean (ESLint flat config);
  `npm run build` → 13 routes compiled.
- Compose: `api` rebuilt with Phase 8/9 code, `/health/ready` reports
  `{"database":"ok","redis":"ok"}`; `web` built and served `/login` with `200`.
- Live smoke test in a real browser against the running stack: login, then all
  nine operator pages plus both detail pages rendered real data (agents, tasks
  with token counts and cost, 18 synced competitions, findings, one enabled
  provider, ten settings). A command typed into the terminal page ran and
  returned `exit 0 · 32ms`; saving a setting from the UI persisted and was
  confirmed through the API. No browser console errors.
- Live events verified end to end: a crafted `task.progress` event published to
  Redis reached `curl` on `/dashboard/stream` as a properly framed SSE message,
  and later events appeared in the dashboard's "Live events" panel. An
  unauthenticated stream request returns `401`.

**Deviations from the plan**
- **No shadcn/ui.** Twelve hand-written primitives on Tailwind v4 tokens
  instead of the shadcn CLI + Radix tree.
- **No charting library.** The usage chart and severity bars are inline SVG.
- **Token in `localStorage`, not an HttpOnly cookie.** Keeps the dashboard a
  static SPA and keeps SSE off a proxy. The XSS tradeoff and its mitigations are
  documented in `frontend/README.md` and `docs/SECURITY.md`.

**Known limitations**
- `NEXT_PUBLIC_API_URL` is baked in at build time; changing the API origin needs
  a rebuild, not a restart.
- The dashboard is dark-only (`ui.theme` exists but is not read).
- The terminal page returns a command's output when it finishes; stdout is not
  streamed, and an in-flight command cannot be cancelled from the UI.
- No frontend unit tests. Verification is typecheck + lint + production build +
  the browser smoke test above; the API behind every page is covered by the
  backend suite.
- The SSE panel shows events that arrive while the page is open; it does not
  backfill from the Stream on reconnect.

## Phase 10 — Hardening

Everything below runs against the same code the previous phases shipped: no new
tables, no new migrations (head stays `e80e55457f5e`) and no new dependencies —
the metrics registry and the rate limiter are written against the standard
library and Redis, so the image does not grow.

**Request pipeline**

Three middlewares wrap every route. Registration order is
`InFlight` → `RateLimit` → `CORS` → `RequestContext`, which runs
`RequestContext` outermost and `InFlight` innermost. CORS sits **outside** the
limiter on purpose: with it inside, a `429` came back without
`Access-Control-Allow-Origin` and a browser showed a CORS failure instead of the
rate-limit message — the dashboard could not tell throttling from an outage.
Reordering surfaced a second issue: `Retry-After` and the `X-RateLimit-*` headers
were not in `expose_headers` either, so the browser API could not read them.

| Middleware | Does |
| --- | --- |
| `RequestContextMiddleware` | assigns `X-Request-ID`, parses an inbound `traceparent` into `X-Trace-ID` (and returns both), logs one line per request with status and duration, warns above `SLOW_REQUEST_MS`, sets `X-Content-Type-Options` / `X-Frame-Options` / `Referrer-Policy` |
| `RateLimitMiddleware` | fixed-window counters per client identity, separate buckets for `/auth/login` and `/auth/password/reset`, `X-RateLimit-Limit` / `-Remaining` / `-Reset` on responses, `429 rate_limited` envelope with `Retry-After` |
| `InFlightMiddleware` | tracks concurrent requests for `agentos_http_requests_in_flight` |

Identity is the bearer-token subject when present, else the client IP (honouring
`X-Forwarded-For` only when `TRUST_FORWARDED_FOR=true`). Redis holds the shared
counters so limits apply across API replicas; if Redis is unreachable the limiter
falls back to a per-process memory counter and logs `rate_limiter_degraded` —
degraded rather than open, so a Redis outage cannot become an unbounded-auth
window.

**Metrics** (`GET /metrics`, Prometheus text format 0.0.4)

Counters, gauges and histograms built on a small in-house registry
(`app/core/metrics.py`): HTTP requests by route/status/method, request duration
histogram, requests in flight, tool runs by outcome (`ok`/`error`/`refused`/
`approval_required`), tool duration, tokens and cost by model, sandbox fallbacks
by reason, workspace quota rejections, background job outcomes. Status gauges
(`agentos_tasks`, `agentos_agents`, `agentos_approvals_pending`) are rebuilt from
the database at scrape time rather than cached, so a restart cannot leave a stale
number. Access is open by default and is gated by `METRICS_TOKEN` (bearer,
compared with `hmac.compare_digest`) when set; the route sits outside
`API_PREFIX` and is exempt from rate limiting so a scraper is not throttled.

**Sandbox enforcement**

`LocalRunner` and `DockerRunner` now take `max_timeout_seconds` and clamp the
requested timeout to it. The Docker path adds `--read-only`, a bounded
`--tmpfs /tmp` (`noexec,nosuid`, 256 MiB), and memory / CPU / PID limits from
settings. Resolution order is Docker → host fallback → refuse:
`resolve_runner()` increments `agentos_sandbox_fallbacks_total{reason}` with
`image_missing` or `docker_unavailable`, which is what makes the fallback visible
in monitoring instead of only in a log line. `SANDBOX_ALLOW_HOST_EXECUTION=false`
turns the fallback into a refusal. Sandbox enablement, quota and resource limits
are read through `runtime_settings`, so an admin can change them from `/settings`
and an environment variable can only narrow what the database allows, never
widen it.

**Workspace quota**

`WORKSPACE_QUOTA_MB` (default 5120) is enforced in two places: `write_bytes`
refuses a write that would cross the quota with `413 workspace_quota_exceeded`,
and `run_command` checks before spawning and re-checks afterwards (a command can
write around the tool layer), incrementing `agentos_workspace_quota_rejected_total`
and appending the overage to the command output so the agent sees it.

**Operations endpoints**

- `GET /api/v1/ops/status` (viewer): environment, version, rate-limit and metrics
  configuration, retention windows, sandbox mode and limits, workspace bytes
  against quota, filesystem free/total, task and approval counts, 24 h model
  calls/tokens/cost, component health.
- `GET /api/v1/ops/logs` (operator): one merged, newest-first timeline over
  `task_events` and `audit_logs` with `q` / `level` / `source` / `task_id` /
  `agent_id` / `since` / `until` filters and offset pagination. An unknown level
  is a `422`, not a silently empty page. Audit rows have no level column, so one
  is derived from `success` (info / warning); the threshold applies to that
  derived level too, which is why an `error` filter no longer returns successful
  audit rows.
- Dashboard `/logs` route renders both: resource cards, the filterable timeline
  with expandable payloads, and the runtime configuration table.

**Key rotation**

`ENCRYPTION_KEY_PREVIOUS` (comma-separated) feeds a `MultiFernet`, so values
written under an old key stay readable after the primary key changes.
`uv run agentos rotate-encryption-key` re-encrypts every `model_providers` row
under the primary key and reports `rotated` / `unchanged` / `failed`. "Changed"
is decided by asking the primary key to decrypt rather than by comparing
ciphertexts — Fernet is randomised, so a comparison would rewrite every row on
every run. Rows no configured key can read are counted as `failed` and left
untouched; they are never overwritten with a guess.

**Secret scanning**

`uv run agentos scan-secrets --path a --path b` walks directories (skipping
`.git`, `node_modules`, `venv`, binaries, files over 2 MB) and matches 13
credential shapes: AWS, GitHub, Slack, Telegram, OpenAI/Anthropic, Stripe,
Google, JWT, private-key blocks, database URLs with inline passwords and a
generic `password = "…"` heuristic. Findings print `path:line rule (severity)`
with the matched value redacted to its first characters, so the scan output is
not itself a leak. `--fail-on-findings` makes it usable as a CI gate; `make
scan-secrets` runs it over the backend and scripts.

**Backup, restore, retention**

`scripts/backup.sh` writes a single tarball of `pg_dump`, the Redis dataset and
the workspace volume plus a manifest (timestamp, database, version, Alembic
head). A database-only dump was not enough: task events live in Redis Streams and
agent files live on the volume, so a restore from the old script silently lost
both. `scripts/restore.sh` prints the contents, requires the word `restore`,
stops the API, replaces all three, restarts, and runs `alembic upgrade head`.
`uv run agentos prune [--dry-run]` deletes `task_events`, `audit_logs` and
`model_usage` rows past `RETENTION_*_DAYS` (0 = keep forever) and reports counts
and cutoffs.

**Load testing**

`scripts/loadtest.py` (with `make loadtest`) drives the read endpoints with a
login, N requests and C concurrent workers, then reports error rate, mean, p50,
p95, p99 and max latency plus a status-code histogram — all computed from the
responses actually received, so a run where everything fails still reports an
error rate instead of a fast success. It only issues GETs: creating tasks in a
load test would spend real model budget.

**Verification**

- `uv run pytest`: 532 passed, 1 skipped. New suites: `tests/test_observability.py`
  (20), `tests/test_ops_api.py` (8), `tests/test_maintenance.py` (18), plus 5
  quota tests added to `tests/test_workspace.py` (35 there in total).
- `uv run ruff check app tests`, `uv run ruff format --check app tests`,
  `uv run mypy app` (139 files) clean; `uv run alembic check` reports no drift.
- Frontend: `npm run typecheck`, `npm run lint`, `npm run build` clean; 14 routes.
- Live smoke on the running stack: `/metrics` renders populated series (a real
  task run produced `agentos_model_tokens_total{kind="prompt"}=825` and
  `{kind="completion"}=51`, matching the recorded 876 tokens),
  `/ops/status` returns real disk and quota numbers, `/ops/logs` returns merged
  audit and task rows, rate-limit and request-id headers appear on responses, and
  `/dashboard/stream` still emits `connected` + `keepalive` through the two added
  middleware layers, and the 11th login attempt within the auth window returns
  `429 rate_limited` with `Retry-After` and CORS headers.
- Rotation was exercised for real on the live database: a new key in
  `ENCRYPTION_KEY` with the old one in `ENCRYPTION_KEY_PREVIOUS` re-encrypted the
  stored provider credential, and a subsequent `/providers/{id}/health` call
  returned the real model list, proving the decrypted key authenticates. A
  rotation run with an unreadable key reported `failed` and left the row intact.
- `scripts/backup.sh` produced a tarball whose manifest carries the version and
  Alembic head; restoring it into a separate Compose project reproduced the
  source exactly (users 1, tasks 18, task_events 154, audit_logs 91,
  model_providers 1), Redis `XLEN agentos:events:stream` matched at 88, the
  workspace files restored, and the restored provider credential decrypted under
  the `.env` key.
- `scripts/backup.sh` and `scripts/restore.sh` pass `bash -n`; the scan found a
  deliberately planted AWS key and password in a temp directory and redacted
  both, and found nothing in the real source tree.

**Limitations**

- `/metrics` has no JWT (a scraper cannot hold one). Either set `METRICS_TOKEN`
  or keep the port off the public network; `ops/status` reports which mode is
  active.
- The Compose `api` service still has no Docker socket, so the Docker sandbox path
  is exercised on a host with a socket and falls back in Compose. The fallback is
  metered, not silent, but it is still host execution — set
  `SANDBOX_ALLOW_HOST_EXECUTION=false` to refuse instead.
- Rate limiting is a fixed window, not a sliding one: a client can burst up to
  `2 × limit` across a window boundary. Chosen for a two-command Redis
  implementation; a token bucket is the upgrade if that edge matters.
- Retention pruning runs on demand (`make prune`), not on a schedule, because
  scheduling belongs to the platform's automation mechanism rather than an
  in-process timer that stops with the container.

## Phase 11 — End-to-end testing and deployment

**Delivered**

*Durable task queue*
- `tasks` gained `claimed_by` and `lease_expires_at` plus a
  `(status, scheduled_at, lease_expires_at)` index (migration `c4a91f7b2e60`).
- `app/services/queue.py`: `claim_next` orders by priority then age and claims
  with `FOR UPDATE SKIP LOCKED`, so any number of workers run without
  coordinating; it inspects a bounded number of candidates and skips tasks whose
  dependencies are unmet, parking tasks that have exhausted `max_attempts` as
  failed instead of leaving them to be claimed forever. `heartbeat` renews a
  lease only for the worker that owns it and returns false when it has lost the
  task; `release` puts a task back or fails it; `reap_expired` recovers tasks
  whose worker died; `queue_depth` counts by status.
- `app/worker.py`: a poll loop that claims, executes through the existing
  execution service, heartbeats at a third of the lease interval, cooperatively
  cancels when the task is cancelled or the worker is shutting down, and fails
  or requeues depending on whether the error was a platform error or a crash.
  `run_worker` installs SIGINT/SIGTERM handlers and starts a metrics server.
- `app/bot.py`: the notification loop, extracted from the API so a slow Telegram
  call cannot block a request. Without a token it logs once and idles rather
  than crash-looping, and re-reads settings so a later token works without a
  redeploy.
- `app/core/metrics_server.py`: a small standalone `/metrics` + `/health` server
  for processes that have no HTTP app.
- CLI: `agentos worker`, `agentos bot`, `agentos queue [--status] [--reap]`.

*State machine fix* — the worker must be able to park a task it cannot run, so
`PENDING→FAILED` and `QUEUED→FAILED` are now legal transitions. Terminal
transitions clear the lease but keep `claimed_by`, which records which worker ran
a task.

*Deployment*
- `scripts/entrypoint.sh` applies migrations only when `AGENTOS_RUN_MIGRATIONS`
  is not `false`, so exactly one container owns the schema; the API runs them
  before serving, the worker and bot opt out.
- `docker-compose.yml` gained `worker` and `bot` services, both with a
  process-based healthcheck (the image's default probes a port these processes
  do not serve, which reported them unhealthy forever).
- `deploy/docker-compose.prod.yml`: the API and dashboard ports are unbound, a
  `caddy:2-alpine` proxy is the only public listener, and the dashboard is built
  with an empty `NEXT_PUBLIC_API_URL` so it calls the API on its own origin.
- `deploy/Caddyfile` (plain HTTP edge) and `deploy/Caddyfile.tls` (automatic
  HTTPS with HSTS): one origin routes `/api/*` and `/health/*` to the API and
  everything else to the dashboard, streams SSE without buffering via
  `flush_interval -1`, returns 404 for `/metrics`, and logs JSON.
- `scripts/deploy.sh` with `preflight`, `up`, `update`, `status` and `logs`.
  Preflight refuses to deploy with a missing, short, or example-valued secret,
  and with `TLS_AUTO=1` but no `PUBLIC_HOST`/`ACME_EMAIL`. `TLS_AUTO` selects the
  Caddyfile, because Compose cannot interpolate a variable into a mount path.
  `update` rebuilds the API and waits for it to be healthy before recreating
  everything else, so migrations land before the code that needs them.
- Makefile targets `worker`, `bot`, `queue`, `test-e2e`, `deploy`,
  `deploy-update`, `deploy-status`, `deploy-preflight`, `docker-check`.
- `docs/DEPLOYMENT.md`: host requirements, first deployment, TLS, update
  ordering, backup/restore, operating the queue, hardening, troubleshooting.

*Tests*
- `tests/test_queue.py` (13) — priority order, exclusion between two workers,
  expired-lease reclaim, reap requeue vs fail, heartbeat ownership, dependency
  blocking, future `scheduled_at`, release events, non-owner release, exhausted
  attempts, queue depth.
- `tests/test_worker.py` (4) — claim and complete, empty queue, crashed run
  requeued, and a recorded execution duration.
- `tests/test_metrics_server.py` (6) — render, health, 404, stop/restart,
  malformed request, query string.
- `tests/test_e2e.py` (6) — the operator journey against the real API and
  database: sign in, create a project, point an agent at a scripted provider,
  create and run a task, then assert the task detail, the dashboard aggregate,
  the ops log and the usage series all agree; plus the viewer/operator
  permission boundary, unauthenticated rejection, a stable error envelope,
  request-id propagation, and unauthenticated health and metrics.

**Verification**

- Backend suite: 562 passed, 1 skipped (the opt-in scanner test). Ruff, Ruff
  format, strict Mypy (143 source files) and `alembic check` are clean.
- The deployment stack was run for real. `docker compose up -d --build api
  worker bot` produced a healthy API and worker; the worker claimed and failed
  legacy agent-less tasks with an honest `UnprocessableError` instead of leaving
  them running, and then completed two real tasks through a live model provider
  (`worker-ok`, 1047 tokens; `lease-ok`, 1063 tokens) with the full event
  sequence `task.created → task.queued → task.running → task.progress →
  model.usage → task.completed`. Terminal rows carry no lease, and the worker's
  own `:9101/metrics` exposes `agentos_background_jobs_total{kind="task",worker=…}`
  and an `agentos_task_duration_seconds` histogram (a later 21.5 s run landed in
  the 30 s bucket, confirming the observation is recorded on the real path).
- The Caddy edge was exercised against the running stack: `/health/live`,
  an authenticated `/api/v1/agents` (200), an unauthenticated one (401),
  `/login` from the dashboard, `/metrics` (404), the proxy's own `/healthz`, and
  `/api/v1/dashboard/stream` emitting `: connected` + `: keepalive` through the
  proxy without buffering.
- Both Caddyfiles pass `caddy validate`; `docker compose config` is valid for
  the development and production files; `scripts/deploy.sh` passes `bash -n` and
  its `preflight` and `status` were run for real on this host.

**Bugs found by the new tests**
- `/ops/logs?task_id=…` returned an empty page: the filter excluded the task
  event source entirely instead of narrowing it.
- `/dashboard/usage?days=1` returned nothing: `since` was computed from the
  current instant, so every row written earlier that day fell outside the
  window. It now starts at the beginning of the first day in the window.
- The `bot` service inherited the image healthcheck that probes the API port,
  and reported unhealthy forever; it now checks its own process.
- Clearing `claimed_by` on completion erased which worker ran a task; only the
  lease is cleared now.

**Limitations**
- Telegram delivery remains unverified end to end: no bot token or public
  webhook is configured, so the bot process idles by design. Everything up to
  the HTTP call is covered by mocked-transport tests.
- The Compose `api` has no Docker socket, so the docker sandbox path is
  exercised only on a host that mounts one; in Compose it falls back to host
  execution, metered as `agentos_sandbox_fallbacks_total{reason}`.
- The proxy has not been exercised with a real certificate: the host has no
  public DNS name, so Let's Encrypt issuance is unverified. The TLS Caddyfile
  validates and the ACME path is Caddy's standard behaviour, not custom code.
- `update` assumes the release is backwards compatible with the previous
  schema. A destructive migration needs two releases.
- The queue's `FOR UPDATE SKIP LOCKED` ordering is by priority and age; there is
  no fairness between tenants.
