# Security model

The platform executes AI-generated code, so every boundary below exists to
answer one question: what can a compromised or prompt-injected model actually
do? Phase 1 established identity, secret handling and the network baseline;
Phase 2 added the runtime controls (permissions, risk gating, argument
validation, SSRF filtering); Phase 5 added filesystem confinement, command
sandboxing and a real human-approval gate; Phase 6 extends that gate to an
action that leaves the building — submitting an entry to an external bounty
platform under this deployment's name and reputation.

## Implemented controls

### Telegram

Webhook requests require a constant-time secret-header check. Commands require
both an allowlisted Telegram ID and an active admin-linked platform account.
Group chats are refused. Mutations require operator privileges; task ownership
and assigned approval checks apply. Update IDs deduplicate mutations. Replies
are plain text and redacted before persistence/delivery. Bot tokens are secret
settings and transport exceptions never expose token-bearing URLs.

### Local research (Phase 7)

Research scope verification and revocation require admin privileges. Findings
and scans require current verified authorization. Scanner execution always uses
the network-disabled research Docker image; it cannot fall back to host shell.
Scope exclusions and workspace confinement apply before execution, and scanner
commands use fixed argv with quoted paths. External targets are stored but never
contacted. Scanner output is a hypothesis, not a confirmed vulnerability.

An independent user must review validation evidence before confirmation.
Reviews bind to the evidence digest and become stale when evidence changes.
Evidence cannot change after disposition. Credential-shaped evidence is refused;
scanner text is redacted, and Gitleaks secret/match/line fields are omitted.
This filtering is heuristic; operators must redact sensitive evidence before
recording it. Report generation is local and cannot submit externally.

### Authentication
- Argon2id password hashing (memory-hard, per-password salt).
- JWT HS256 access tokens with `iss`, `aud`, `type`, `iat`, `exp`, `jti`.
  Tokens are rejected on expiry, signature mismatch, wrong audience/issuer or
  wrong token type.
- Login runs a dummy hash verification for unknown emails to reduce account
  enumeration through timing.
- API keys: 256-bit random, `aos_` prefix, stored only as SHA-256 hash with a
  short display prefix. Optional expiry, revocation and scopes.
- Constant-time comparison used for key material checks.

### Authorization
- Roles: `viewer` < `operator` < `admin` < `owner`; dependencies enforce a
  minimum role.
- API keys additionally require the requested scope unless they hold `*`.
- API keys cannot mint new API keys.
- Role and scope checks are centralized in `app/api/deps.py`.

### Secrets
- `SECRET_KEY` (JWT) and `ENCRYPTION_KEY` (Fernet) are required, length/key
  validated, and wrapped in `SecretStr` so they cannot be logged accidentally.
- Provider API keys are encrypted at rest; only a short hint is stored
  alongside.
- `.env` is gitignored; `.env.example` contains placeholders only.

### Input/output
- Pydantic validation for all request bodies; errors return a structured
  envelope without stack traces.
- Unknown fields are ignored by the default model config; endpoints are
  explicit about accepted fields.
- Responses never include password hashes, encrypted keys or plaintext API
  keys (the one exception is the initial API-key creation response, by design).

### Transport and headers
- Security headers: `X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`, `Permissions-Policy`, `Cross-Origin-Opener-Policy`, and
  HSTS in production.
- CORS allowlist; wildcard rejected in production.
- Docs/OpenAPI disabled in production.

### Agent runtime and tools (Phase 2)
- Tools declare required permissions; a tool the caller may not use is never
  offered to the model and is refused if called anyway.
- Argument validation against the tool's JSON schema rejects unknown fields, so
  a model cannot smuggle extra parameters into an implementation.
- Risk levels gate execution: `high`/`critical` tools refuse to run unless the
  tool slug is in the caller's approval set. Approvals are explicit and come
  from the database (see Phase 5 below), never from model output.
- Every tool runs under its own timeout; a slow or hung tool fails the single
  invocation instead of stalling the run.
- `http_get` refuses non-HTTP(S) schemes, embedded credentials and
  private/loopback/link-local/metadata addresses (SSRF guard).
- Agent runs are bounded by iteration, token and cost budgets plus a wall-clock
  deadline, so a misbehaving model cannot spend unbounded money or time.
- Tool calls, failures and model usage are written to `task_events` and
  `model_usage` for post-hoc review.

### Skills and memory (Phase 4)
- **Skills are an authorization boundary, not a suggestion.** An agent can load
  only skills assigned to it; the API checks on every load. Assignment is how an
  operator grants capability, so a compromised or prompt-injected model cannot
  pull in instructions it was never given.
- Skill import accepts `http(s)` only (no `file://`, no `git+ssh`), and the
  loader refuses paths that escape the managed skills root.
- A SKILL.md is capped at 256 KiB and must be valid UTF-8; frontmatter is parsed
  with `yaml.safe_load`, never an unsafe loader.
- Discovery does not follow directory symlinks, so an imported skill tree cannot
  read files outside itself.
- Builtin skills are `is_system`: they can be disabled but not deleted, so
  deleting one cannot silently strip capability from every agent.
- **Memory writes reject secrets instead of flagging them.** Key/token/private-key
  patterns are matched before persistence and the write fails with
  `memory_sensitive_content`. Storing a credential in memory would place it in
  every agent context that retrieves it, and a flag would rely on a reviewer
  noticing.
- **Memory isolation is enforced in SQL.** Any scope tag recorded on a row
  (`agent_id`, `project_id`, `task_id`, `user_id`) is required again on read;
  unset means shared. Every read path goes through the same `visibility_filter`,
  so agent A cannot retrieve agent B's memories and a project-scoped memory does
  not leak across projects.
- Memory listing for operators passes `agent_scope=False` to see an agent's
  rows, but never skips user scoping.
- Every skill load is recorded in `skill_executions` with the agent and task, so
  an operator can reconstruct which instructions influenced which run.
- Embeddings are derived from memory content and stored in the same row; no
  content leaves the system for indexing beyond the configured provider.

### Workspace, sandbox and approvals (Phase 5)
- **Filesystem confinement.** Every file path is validated against a project
  slug, resolved (symlinks followed) and re-checked for containment. Absolute
  paths, Windows drive paths, `../..` segments and **symlinks planted inside the
  tree** are all refused. Resolving before checking is the point: a resolved
  path cannot lie about where it goes.
- **Bounded reads and writes.** Per-file size caps, a bounded directory walk
  with an entry cap, a large-file skip in search, and noise directories
  (`.git`, `node_modules`, `__pycache__`) excluded.
- **Command execution is environment-scrubbed.** `LocalRunner` passes only a
  small allowlist of variables (`PATH`, `HOME`, `LANG`, …) and drops any name
  starting with `AWS_`, `GITHUB_`, `OPENAI_`, `ANTHROPIC_`, `AGENTOS_` and
  similar, so a shell command cannot read the platform's own credentials out of
  `os.environ`. Commands run with a hard timeout and their process group is
  killed, so a spawned child cannot outlive the call.
- **Docker sandbox limits.** When the sandbox image is used: `--network=none`
  (no egress at all), `--read-only` root filesystem, tmpfs `/tmp`,
  `--pids-limit`, memory and CPU caps, only the workspace bind-mounted, and
  `--user` set to the workspace owner's uid/gid so files the container writes
  are the host user's — a root-owned file in the workspace would otherwise
  become an escalation path for every later command.
- **Downgrades are explicit, never silent.** `resolve_runner()` returns the
  runner *and* a reason. If the image is missing, Docker is unreachable, or
  sandboxing is disabled, the reason is logged and surfaced; an operator can see
  that commands are running on the host.
- **Approval is a persisted record, not a boolean.** A high-risk call creates an
  `approvals` row carrying the exact tool, arguments and risk level, tied to the
  task and requesting agent. The task moves to `waiting_approval`. Only
  **approved** rows for that task are turned into `AgentSpec.approvals`, so a
  re-run can execute exactly what a human signed off and nothing else.
- **Approval is single-shot.** Deciding an already-decided or expired request is
  rejected with a conflict; approvals carry an expiry (72 h default).
- **Duplicate requests are suppressed**, so a model looping on a blocked tool
  cannot flood the operator with identical approvals.
- **A blocked tool call returns a "did not run" result** to the model. The model
  is told plainly what happened rather than being allowed to assume success.

### External bounty submission (Phase 6)
- **Two independent gates, neither controlled by the model.** Submitting to
  make.fun requires an `approvals` row of `action_type=submit_competition` in
  state `approved` for that specific entry, *and* — for the `makefun_submit_entry`
  tool — a runtime approval of that tool for that run. `submit_entry` re-reads
  the entry's approval immediately before the HTTP call, so a revoked or
  superseded approval cannot be replayed from an earlier run.
- **The agent that prepares an entry cannot submit it.** The `make-fun`
  specialist holds `bounty:write` (prepare) but not `bounty:submit`; the submit
  tool is `CRITICAL` risk and is not granted to any specialist. An operator
  approves, and the platform submits.
- **A lapsed API key never marks an entry rejected.** `401`/`403` and `5xx` are
  classified as *deployment* failures (`MakeFunError`, our API answers `502`) and
  leave the entry in `awaiting_approval` with no `submission_id`. Only a `4xx`
  problem document — the platform's own verdict — is stored on the entry. The
  distinction matters: a bug in our retry logic must not be able to burn a
  competition entry by recording a rejection that never happened.
- **Redirects are refused.** The submission endpoint decides acceptance, so a
  `3xx` raises instead of moving that decision to a host of the redirect's
  choosing.
- **The API key is a secret, used minimally.** `MAKEFUN_API_KEY` is an optional
  `SecretStr`, sent only as `x-api-key`, never logged and never echoed in an
  error body. A missing key is a valid deployment state: all discovery and
  requirements work read-only without it.
- **Transcripts are recorded, not fabricated.** `transcript_paths` points into
  the entry's workspace. Nothing generates or regenerates a transcript to satisfy
  a platform requirement — a fabricated transcript would be an integrity failure
  the platform cannot detect, so the platform refuses rather than invents.
- **Preparation is offline.** `prepare_entry` makes no network calls; the
  payload is written, an approval is requested, and an operator decides. This
  keeps an unreviewed model under explicit human control at exactly the step
  where the platform's identity is asserted.
- **Partial success is recorded, not abandoned.** `submission_id` is written
  only after the platform accepts an entry, and `202` is treated as accepted-once
  because the claim code is returned only once.

### Audit
- Successful and failed logins, API-key creation and revocation are recorded
  with actor, IP and user agent in the same transaction as the change.
- Provider creation/update/deletion, agent changes, task creation, execution and
  cancellation are audited with the acting user.

### Deployment
- API container runs as UID 10001, `no-new-privileges`.
- Postgres and Redis bind to `127.0.0.1` on the host by default.
- **No Docker socket is mounted into the API container** (deliberate). The
  consequence is documented rather than hidden: command execution from the API
  falls back to `LocalRunner` on the host with a logged reason. Mounting the
  socket would let a compromised API drive the host daemon, which is a larger
  blast radius than host execution inside the API container itself.

### Request-level controls
- **Rate limiting.** Fixed-window counters per client identity (bearer-token
  subject when present, else client IP). Defaults: 240 requests/minute overall,
  10 per 5 minutes for `/auth/login` and `/auth/password/reset`. Redis holds the
  shared counters so limits hold across replicas; if Redis is unreachable the
  limiter degrades to a per-process memory counter and logs
  `rate_limiter_degraded`. It fails **closed**, not open: an outage must not
  become an unbounded credential-guessing window. Responses carry
  `X-RateLimit-Limit`/`-Remaining`/`-Reset`, and a rejection is
  `429 rate_limited` with `Retry-After`.
- **`X-Forwarded-For` is ignored unless `TRUST_FORWARDED_FOR=true`.** Honouring
  it behind no proxy would let a client choose its own bucket.
- **Request correlation.** `X-Request-ID` is always issued; an inbound
  `traceparent` is parsed into `X-Trace-ID` (propagated, never trusted for
  authorization).
- **Security headers** on every response: `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`.
- **Slow-request logging** above `SLOW_REQUEST_MS`, so a latency change is
  visible without a profiler.

### Workspace quota and sandbox limits
- Every command is clamped to `SANDBOX_MAX_TIMEOUT_SECONDS`, whatever the tool
  requested. The Docker runner additionally applies `--read-only`, a bounded
  non-executable `/tmp` tmpfs, and memory/CPU/PID limits.
- A workspace cannot grow past `WORKSPACE_QUOTA_MB`: a write that would cross it
  is refused (`413 workspace_quota_exceeded`), and a command is checked before
  and after running, since a shell can write without going through the tool.
- The host fallback is metered (`agentos_sandbox_fallbacks_total{reason}`), never
  silent. `SANDBOX_ALLOW_HOST_EXECUTION=false` converts it into a refusal.
- Sandbox settings live in the database and can only be narrowed by the
  environment, so an env var cannot widen what an admin disabled.

### Credential lifecycle
- `ENCRYPTION_KEY_PREVIOUS` keeps old Fernet keys readable during a rotation;
  `agentos rotate-encryption-key` re-encrypts provider credentials under the
  current key. A row no configured key can decrypt is reported as `failed` and
  left untouched — the command never overwrites a value it could not read.
- `agentos scan-secrets` detects 13 credential shapes and prints matches with the
  value redacted, so scan output is not itself a leak. `--fail-on-findings`
  makes it a CI gate.

### Observability and operations
- `/metrics` is open by default; set `METRICS_TOKEN` to require a bearer token
  (constant-time comparison). A scraper cannot hold a JWT, so network isolation
  or the token is the control — never assume it is authenticated.
- `/api/v1/ops/status` (viewer) exposes configuration and resource usage;
  `/api/v1/ops/logs` (operator) reads the merged audit + task timeline, so
  neither requires shell access to the host.
- `scripts/backup.sh` captures PostgreSQL, Redis and the workspace volume in one
  tarball with a manifest; `scripts/restore.sh` requires typing `restore`.
- `agentos prune` enforces the retention windows; `RETENTION_*_DAYS=0` keeps a
  table forever.

## Not yet implemented (later phases)

- Sandbox use from the API container (the Docker socket is deliberately not
  mounted, so the API falls back to host execution — metered and refusable, but
  still host execution).
- cgroups enforcement outside the container flags, and a per-agent egress
  policy for host execution (the Docker path already has `--network=none`).
- Worker isolation and queue authentication.
- Login lockout beyond rate limiting (no per-account failure counter yet).
- MFA, refresh tokens, session revocation lists.
- Prompt-injection defenses for untrusted repositories/web content (memory
  writes are already scanned; tool *output* is not yet redacted). Note that a
  repository the agent reads is untrusted input: it can contain instructions.
  The sandbox limits what such instructions can reach, not what the model
  believes.
- Approval granularity is per tool per task, not per argument set: approving
  `run_command` once permits any command in that task's later runs.
- Signature verification of imported skills.
- Signing a make.fun submission with a Solana keypair (`entry_signature`) is not
  implemented, so escrowed prizes cannot be entered.
- Bounty transcripts are stored as workspace paths but not yet assembled into
  the submitted multipart body.
- Backup encryption (the tarball is plaintext; encrypt it before it leaves the
  host). Key rotation is implemented for provider credentials.

These are scheduled in Phases 5 and 10 and are tracked in `docs/PHASES.md`.

## Operator checklist

1. Generate unique `SECRET_KEY` and `ENCRYPTION_KEY`; never reuse across
   environments.
2. Use a strong `POSTGRES_PASSWORD`; do not expose ports 5432/6379 publicly.
3. Set `ENVIRONMENT=production` (enforces debug/CORS rules).
4. Terminate TLS at a reverse proxy; the API expects `--proxy-headers`.
5. Create the owner with `agentos create-admin`, then clear
   `BOOTSTRAP_ADMIN_PASSWORD` from the environment.
6. Back up `pgdata` and the encryption key separately: losing the key makes
   stored provider credentials unrecoverable.
7. Assign skills deliberately. `POST /skills/{id}/assign/{agent}` is a
   capability grant; review the SKILL.md body before assigning it to an agent
   that can reach production systems.
8. If an embedding provider is configured, remember that memory content is sent
   to it for indexing — choose a provider you trust with that content, and leave
   `MEMORY_EMBEDDING_PROVIDER` unset to keep everything local (lexical only).
9. Run `POST /memory/purge-expired` on a schedule; expired memories are excluded
   from retrieval but are only deleted when purged.
10. Review pending approvals before re-running a paused task. Approving a
    high-risk tool is a real capability grant for that task: read the arguments
    in the approval payload, not just the tool name.
11. Watch for `sandbox_fallback` in the logs. It means commands are running on
    the host instead of in the sandbox image; either accept it knowingly or
    build the sandbox image and mount the socket in a dedicated worker.
12. Set `MAKEFUN_API_KEY` only when submissions are wanted; without it the
    platform reads the bounty board and prepares entries but cannot submit, and
    a submit attempt fails as `502 makefun_error` rather than a fake success.
    Rotate the key every 30 days — make.fun keys expire on that schedule, and an
    expired key is indistinguishable from a missing one at submit time.
13. Read a bounty approval's payload before approving it. It contains the repo
    URL, commit SHA and title that will be published under the platform's name.
14. The dashboard keeps its JWT in `localStorage`. That is what makes it a
    static SPA with no cookie session, and it means an XSS on the dashboard
    origin can read the token. Keep the origin on a trusted network, keep the
    session TTL short, and do not add `dangerouslySetInnerHTML` or render
    model-authored Markdown as HTML without sanitising it downstream. Moving to
    an HttpOnly cookie would need a BFF in front of the SSE stream.
15. `NEXT_PUBLIC_API_URL` is compiled into the dashboard bundle and is
    therefore public: never put a credential in a `NEXT_PUBLIC_*` variable.
16. Only keys in the settings API allowlist can be written through
    `/api/v1/settings`, and secret values are never returned. Do not widen that
    allowlist to anything that grants a capability — a setting is data, not an
    approval.
17. Treat a cloned repository as hostile input. The workspace confines what the
    agent's tools can touch, not what the model can be persuaded to attempt.
18. Set `METRICS_TOKEN` (or keep the API port off the monitoring network):
    `/metrics` has no JWT by design and reports route names and error rates.
19. Set `TRUST_FORWARDED_FOR=true` **only** when a proxy you control overwrites
    `X-Forwarded-For`. Otherwise a client picks its own rate-limit bucket.
20. In production, decide about `SANDBOX_ALLOW_HOST_EXECUTION` deliberately.
    `true` keeps the host fallback working when Docker is unavailable; `false`
    refuses to run commands at all. Both are defensible; leaving it unexamined
    is not. `GET /api/v1/ops/status` reports the resolved mode.
21. Rotate the encryption key in the documented order: new key in
    `ENCRYPTION_KEY`, old in `ENCRYPTION_KEY_PREVIOUS`, restart,
    `agentos rotate-encryption-key`, verify provider calls still authenticate,
    then remove the old key. Removing it early makes those rows unreadable
    (they are reported as `failed`, not silently destroyed).
22. Run `scripts/backup.sh` on a schedule and test `scripts/restore.sh` on a
    throwaway stack. A backup that has never been restored is a hypothesis.
    Keep `ENCRYPTION_KEY` outside the backup: the dump is useless without it and
    is a full credential disclosure with it.
23. Run `agentos prune` periodically for the retention windows you configured;
    nothing prunes automatically.
24. Watch `agentos_workspace_quota_rejected_total` and
    `agentos_sandbox_fallbacks_total`. The first means an agent hit its disk
    ceiling; the second means commands are running outside the sandbox.
