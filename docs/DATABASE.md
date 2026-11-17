# Database design

PostgreSQL 16. 34 tables, UUID primary keys, timezone-aware timestamps, JSONB
for evolving configuration, `ON DELETE` rules chosen for auditability (history
is preserved with `SET NULL`, ownership uses `CASCADE`).

Migrations: `9d6bea85789b` (initial), `9962ec813293`, `df885b5d878f` (skills,
memory, pgvector), `b41c7f2d9ab0` (coding sessions) and `021384d3b1e9`
(competition entries), `9bbfe701af18` (research scopes, scans and reviews).
Head: `e80e55457f5e` (Telegram update deduplication).

`telegram_updates` stores a unique Bot API update ID, sender/chat IDs, processing
state and redacted reply. Domain mutations and the reply commit together before
outbound delivery; retries do not reapply mutations.

## Identity and access

| Table | Purpose | Key constraints |
| --- | --- | --- |
| `users` | Operators and dashboard users | unique email, unique telegram id, role check |
| `api_keys` | Machine credentials | unique prefix/hash, nullable expiry/revocation |

## Agents

| Table | Purpose | Key constraints |
| --- | --- | --- |
| `agents` | Agent definitions (prompt, model, limits, status) | unique slug, self-referencing parent |
| `agent_configs` | Arbitrary per-agent key/value config | unique (agent, key) |
| `agent_tools` | Tool assignments and overrides | FKs cascade with agent/tool |
| `agent_skills` | Skill assignments | FKs cascade |

Agent limits are first-class columns: `max_iterations`, `token_budget`,
`timeout_seconds`, `max_output_tokens`, `temperature`, `top_p`. Permissions and
memory configuration are JSONB.

## Skills and tools

| Table | Purpose |
| --- | --- |
| `skills` | Installed SKILL.md packages, versioned, hashed |
| `skill_versions` | Every saved revision of a skill's content |
| `skill_executions` | One row per `load_skill` call: who, which task, ok/failed |
| `agent_skills` | Skill → agent assignment |
| `tool_registry` | Tool contracts: schema, permissions, risk, timeout, environment |

Tools are `enabled=false` by default. High-risk tools must be paired with an
approval (see below).

A skill is stored twice on purpose: `skills.content` holds the current SKILL.md
text and `skills.content_hash` backs install idempotency (startup sync
re-installs only what changed), while `skill_versions` keeps the history so an
upgrade can be audited and rolled back. `is_system` marks builtin skills, which
can be disabled but not deleted — deleting one would silently remove capability
from every agent that was granted it.

`skill_executions` is the observability record for skill use: `event` is `load`
or `error`, `ok` says whether it succeeded, and `task_id`/`agent_id` say under
which run. It is written through the tool layer, not the HTTP layer, so a skill
loaded by an agent mid-task is recorded exactly like an explicit load.

## Tasks and execution

| Table | Purpose | Notes |
| --- | --- | --- |
| `tasks` | Unit of work | status machine, budgets, deadlines, correlation id, `parent_task_id`, retry counters (`attempts`, `max_attempts`) |
| `task_steps` | Ordered plan steps | unique (task, seq), retry counters, `depends_on` |
| `task_dependencies` | Task DAG | unique pair, no self-reference check |
| `task_events` | Append-only execution log | indexed by (task, created_at) |
| `agent_messages` | Agent-to-agent messages | task/conversation scoped |
| `conversations` | Chat/agent sessions | channel, status, last message |
| `conversation_messages` | Conversation turns | role, tool calls, token count |

Task status: `pending → planning → queued → running → completed|failed|cancelled`,
with `waiting_approval` and `paused` as interrupt states. Terminal statuses are
`completed`, `failed`, `cancelled`.

### Orchestration

- `tasks.parent_task_id` (self-FK, one level deep) links a delegated step task
  to its orchestrating parent; the orchestrator only ever delegates one level.
- `tasks.output["plan"]` holds the validated `ExecutionPlan`, and
  `tasks.output["children"]` maps plan key → child task id. That map is what
  makes a resume idempotent: existing children are reused, never re-created.
- `tasks.output["children"]` and the per-step `task_steps` rows are derived from
  the same run, so the task detail view and the orchestration view never
  disagree. `task_steps.kind = 'agent'` marks delegated steps.
- `tasks.attempts` / `tasks.max_attempts` bound `reopen_for_retry()`, which only
  moves `failed → queued` and writes an audited `task.retry` event.
- `task_dependencies` encodes the plan DAG between child tasks; the scheduler
  orders by plan level, so both representations must agree (the service writes
  them from the same plan).
- `agent_messages` records one `handoff` per completed step (from the specialist
  slug to `main-orchestrator`), which is what the transcript endpoint renders.
- `approvals` gates a run: `pending` → `approved` | `rejected` | `expired`, with
  `expires_at` (72h default). A decision is single-shot — a second decision is
  rejected with a conflict rather than overwriting the first.

**Invariants**
- A child task never has a plan of its own; only the parent orchestrates.
- A plan referencing an unknown agent, an undefined dependency or a cycle is
  rejected before any child task is created.
- `used_tokens`/`cost_usd`/`used_iterations` on the parent are the sum of the
  child task columns, never independently accumulated.

## Memory

`memories` stores six layers (`working`, `conversation`, `episodic`, `semantic`,
`project`, `agent`) with:
- `content_hash` + `dedup_key` for deduplication,
- `version` + `parent_memory_id` for versioning,
- `importance`/`confidence` for relevance scoring,
- `source`/`source_ref` for provenance,
- `is_sensitive` + `expires_at` for protection and retention,
- `access_count` + `accessed_at` for usage-based scoring,
- `embedding` (JSONB float list) and `embedding_model`,
- `embedding_vec` (`vector(1536)`) + `embedding_dim` for similarity search.

> Secrets are rejected, not just flagged. `app/memory/sensitivity.py` matches
> key/token/private-key patterns and the write path raises
> `memory_sensitive_content` before anything is persisted.

**Two embedding columns.** `embedding` (JSONB) is the portable record: it works
on any PostgreSQL and survives a database that cannot install pgvector, so a
memory is never lost when the extension is unavailable. `embedding_vec` is the
search index, typed and HNSW-indexed for `cosine_distance` ordering. They hold
the same vector; the JSONB copy is also what a future re-index would read from.
A dimension mismatch is refused at write time — the column is `vector(1536)`,
and `memory_embedding_dimensions` is validated against it when building the
embedder rather than at insert.

**Deduplication and versioning.** Writing content whose hash already exists under
the same `dedup_key` does not create a row: it bumps `access_count` and refreshes
`accessed_at`/`expires_at`. New content under an existing key creates a new row
with `version + 1` and `parent_memory_id` set to the previous version, so the
history stays readable.

**Isolation is enforced in SQL, not in Python.** `visibility_filter` builds the
`WHERE` clause, and its rule is that any scope tag recorded on a row
(`agent_id`, `project_id`, `task_id`, `user_id`) is *required again* on read;
rows that leave a tag unset are shared. Filtering after the query would be one
forgotten `if` away from a cross-agent leak, so the constraint lives in the
statement that fetches.

## Projects, workspaces and repositories

`projects` own a workspace path (unique); the directory is created under
`WORKSPACE_ROOT` and every file tool confines itself to it. `repositories`
belong to a project (unique project+url) and track provider, branch and last
analysis.

### `coding_sessions`

One row per coding-engine invocation, so a run is auditable after the process
is gone:

| Column | Notes |
| --- | --- |
| `project_id` | FK `CASCADE` — a session has no meaning without its project |
| `task_id`, `agent_id` | FK `SET NULL` — history survives deletion |
| `requested_by` | FK `SET NULL` to `users` |
| `engine`, `model` | which engine ran, and on which model |
| `status` | `running`, `completed`, `failed`, `timed_out` |
| `instruction`, `summary` | what was asked, what the engine reported |
| `changes`, `commands`, `meta` | JSONB: changed paths, executed commands, engine detail |
| `transcript_path` | where the full event stream was written |
| `exit_code`, `duration_ms`, `finished_at` | outcome |

Indexes: `(project_id, created_at)`, `(task_id)`, `(status)`.

**Why a table and not a log line.** A coding run is the most expensive and most
consequential thing an agent does. Persisting what it changed and which commands
it ran is what makes "the agent broke my repo" answerable. The three FKs choose
different delete behaviours on purpose: the project cascades (the session is
meaningless without it), while task/agent/user are `SET NULL` because the
session must outlive them.

### Approval ↔ tool linkage

`approvals.payload` carries `tool`, `arguments`, `risk_level` and
`tool_call_id` for `action_type = run_high_risk_tool`. That payload is the
authority: `approved_tool_slugs()` reads approved rows for a task and the
runtime only allows exactly those tools. An approval is therefore a database
fact, not a flag the model can talk its way into.

## Bounties

`bounty_programs` describe platforms (Make.fun, etc.) and scope.
`competitions` belong to a program (unique program+external id), carry
requirements, scoring, reward, deadlines and status, and optionally link to a
project workspace.

Status values (`discovered`, `evaluating`, `in_progress`, `submitted`, `graded`,
`closed`, `archived`) are never moved backwards by a sync: the sync writes what
the platform published, but a listing whose `deadline` has passed is stored
`closed` regardless of what the listing says, because the API refuses entries
after that point.

### `competition_entries`

One row per attempt to win a competition:

| Column | Notes |
| --- | --- |
| `competition_id` | FK `CASCADE` — an entry has no meaning without its competition |
| `project_id`, `approval_id`, `created_by` | FK `SET NULL` — history survives deletion |
| `status` | `draft`, `prepared`, `awaiting_approval`, `submitted`, `rejected`, `validating`, `graded`, `placed`, `won`, `failed`, `withdrawn` |
| `submission_id` | the platform's identifier; null until it accepts one, never invented |
| `repo_url`, `commit_sha`, `title` | exactly what was submitted |
| `transcript_paths` | JSONB: workspace-relative transcript paths, preserved |
| `checks` | JSONB: the platform's per-check verdicts, verbatim |
| `payload`, `meta` | JSONB: preparation and response detail |
| `score`, `rank`, `prize_usd` | the platform's outcome; `Numeric(18,2)` for money |
| `error`, `submitted_at`, `last_synced_at` | outcome and freshness |

Indexes: `(competition_id)`, `(status, created_at)`, `(submission_id)`.

**Why entries are separate from competitions.** A competition is somebody else's
listing; an entry is our act. Keeping them apart lets one competition collect
several attempts (the API allows retries until the deadline) and lets an entry
record its own approval, repo commit, transcripts and verdict. `checks` is stored
rather than summarised because a rejection is only diagnosable if the failing
check and its `fix` are readable months later.

**`approval_id` is the gate.** `submit_entry` re-reads it and refuses unless it
is `approved`, so the authority to submit externally is a database fact rather
than a parameter on the request.

## Security research

Phase 7 adds `research_scopes` (project authorization, local subtree, exclusions,
permitted tools, verifier, expiry and revocation), `security_scans` (tool,
command, exit code, normalized output and errors) and `finding_reviews`
(independent reviewer, decision, rationale and evidence digest).
`findings.scope_id` references its authorization scope with `SET NULL`.
Reviews and scans are indexed by parent and creation time. Reviews are bound to
the current finding/evidence digest; confirmation refuses stale reviews.

| Table | Purpose |
| --- | --- |
| `findings` | Hypotheses and confirmed findings with severity, CVSS, CWE, status |
| `evidence` | Evidence records linked to a finding, hashed and optionally stored |
| `approvals` | Human approval gates for external or high-risk actions |

Finding lifecycle: `hypothesis → validating → confirmed → reported →
accepted|rejected`, with `false_positive` as an alternative outcome. Scanner
output is not a finding until validation produces evidence.

## Models and observability

| Table | Purpose |
| --- | --- |
| `model_providers` | Provider endpoints, encrypted keys, defaults, health |
| `model_usage` | Per-request token/cost/latency records |
| `audit_logs` | Security-relevant actions with actor, resource, IP, outcome |
| `system_settings` | Operator-tunable runtime configuration |

## Indexing strategy

- Composite indexes match real access paths: `(status, created_at)` on tasks,
  `(task_id, created_at)` on events, `(agent_id, layer)` on memories,
  `(status, deadline_at)` on competitions, `(status, severity)` on findings.
- Unique indexes back natural keys (email, slug, prefix, hashes, project+url).
- `ix_memories_embedding` is an HNSW index with `vector_cosine_ops` on
  `embedding_vec`. HNSW is chosen over IVFFlat because it needs no training pass
  — a table that grows one row at a time (which memory does) never has a stable
  set to build lists from, and IVFFlat would return poor recall until rebuilt.
  The default `m`/`ef_construction` are kept; the table is small enough that
  build cost is not the constraint.
- JSONB is intentionally not GIN-indexed yet; queries will drive index choices
  in the phases that introduce them. Memory retrieval filters on btree columns
  (`agent_id`, `layer`) before ranking, so it does not need a JSONB index.

## Invariants

1. Every task event belongs to a task; deleting a task removes its events.
2. Users, agents and projects referenced by historical records are `SET NULL`,
   never deleted transitively.
3. A task cannot depend on itself.
4. API keys and provider keys are never stored in plaintext.
5. Money uses `NUMERIC`, never floats.
6. A memory row's recorded scope tags are required again on every read; a tag
   left unset means "shared", never "match anything".
7. An embedding vector is either both stored (`embedding` + `embedding_vec`) or
   neither; a half-written pair is a bug, and search filters on
   `embedding_vec IS NOT NULL`.
8. `skill_executions` rows are append-only and never cascade-delete a task's
   history: the FK is `SET NULL`, so deleting a task keeps the record that the
   skill was loaded.
