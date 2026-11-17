export interface User {
  id: string;
  email: string;
  display_name: string;
  role: "viewer" | "operator" | "admin" | "owner";
  is_active: boolean;
  telegram_user_id: number | null;
  last_login_at: string | null;
  created_at: string;
}

export interface Agent {
  id: string;
  slug: string;
  name: string;
  description: string;
  system_prompt: string;
  model_provider_id: string | null;
  model_name: string | null;
  temperature: number;
  top_p: number;
  max_output_tokens: number;
  max_iterations: number;
  token_budget: number;
  timeout_seconds: number;
  permissions: Record<string, unknown>;
  memory_config: Record<string, unknown>;
  status: "draft" | "active" | "paused" | "archived";
  is_system: boolean;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface AgentDetail extends Agent {
  tools: { id: string; tool_id: string; enabled: boolean; config: Record<string, unknown> }[];
  skills: { id: string; skill_id: string; enabled: boolean; config: Record<string, unknown> }[];
}

export interface Tool {
  id: string;
  slug: string;
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  required_permissions: string[];
  risk_level: string;
  timeout_seconds: number;
  execution_environment: string;
  enabled: boolean;
  version: string;
}

export type TaskStatus =
  | "pending"
  | "planning"
  | "queued"
  | "running"
  | "waiting_approval"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

export interface Task {
  id: string;
  title: string;
  objective: string;
  description: string;
  status: TaskStatus;
  priority: number;
  assigned_agent_id: string | null;
  project_id: string | null;
  created_by: string | null;
  budget_tokens: number | null;
  used_tokens: number;
  budget_usd: string | null;
  cost_usd: string;
  max_iterations: number;
  used_iterations: number;
  timeout_seconds: number;
  output: Record<string, unknown>;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
  deadline_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface TaskStep {
  id: string;
  seq: number;
  kind: string;
  status: string;
  title: string;
  description: string;
  agent_id: string | null;
  attempt: number;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface TaskEvent {
  id: string;
  event_type: string;
  level: string;
  message: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface TaskDetail extends Task {
  steps: TaskStep[];
  events: TaskEvent[];
}

export interface TaskPage {
  total: number;
  limit: number;
  offset: number;
  items: Task[];
}

export interface TaskRunResponse {
  task_id: string;
  status: TaskStatus;
  content: string;
  iterations: number;
  total_tokens: number;
  cost_usd: string;
  error: string | null;
  tool_invocations: Record<string, unknown>[];
  pending_approvals: Record<string, unknown>[];
}

export interface Approval {
  id: string;
  task_id: string | null;
  step_id: string | null;
  finding_id: string | null;
  requested_by_agent_id: string | null;
  assigned_to_user_id: string | null;
  decided_by_user_id: string | null;
  action_type: string;
  title: string;
  description: string;
  payload: Record<string, unknown>;
  status: "pending" | "approved" | "rejected" | "expired" | "cancelled";
  decision_note: string;
  expires_at: string | null;
  decided_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ApprovalPage {
  total: number;
  limit: number;
  offset: number;
  items: Approval[];
}

export interface Competition {
  id: string;
  program_id: string;
  project_id: string | null;
  external_id: string;
  title: string;
  description: string;
  url: string;
  reward_amount: string | null;
  reward_currency: string;
  status: string;
  starts_at: string | null;
  deadline_at: string | null;
  submitted_at: string | null;
  scoring: Record<string, unknown>;
  meta: Record<string, unknown>;
  created_at: string;
}

export interface CompetitionDetail extends Competition {
  requirements_summary: Record<string, unknown>;
}

export interface BountyProgram {
  id: string;
  slug: string;
  name: string;
  platform: string;
  url: string;
  description: string;
  created_at: string;
}

export interface CompetitionEntry {
  id: string;
  competition_id: string;
  project_id: string | null;
  approval_id: string | null;
  status: string;
  submission_id: string | null;
  title: string;
  repo_url: string;
  commit_sha: string;
  transcript_paths: string[];
  checks: Record<string, unknown>[];
  score: string | null;
  rank: number | null;
  prize_usd: string | null;
  error: string | null;
  submitted_at: string | null;
  last_synced_at: string | null;
  created_at: string;
}

export interface CompetitionSyncResult {
  created: string[];
  updated: string[];
  total: number;
  next_cursor: string | null;
  synced_at: string;
}

export interface Quota {
  max_per_day?: number | null;
  max_entries_per_account?: number | null;
  [key: string]: unknown;
}

export interface Scope {
  id: string;
  project_id: string;
  name: string;
  local_path: string;
  excluded_paths: string[];
  external_targets: string[];
  authorization: string;
  allowed_tools: string[];
  expires_at: string | null;
  verified_by: string | null;
  verified_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

export interface Finding {
  id: string;
  scope_id: string | null;
  project_id: string | null;
  created_by: string | null;
  title: string;
  category: string;
  hypothesis: string;
  description: string;
  severity: "info" | "low" | "medium" | "high" | "critical";
  confidence: number;
  cvss_score: string | null;
  cwe_id: string | null;
  remediation: string;
  status: string;
  meta: Record<string, unknown>;
  created_at: string;
}

export interface Evidence {
  id: string;
  finding_id: string;
  kind: string;
  title: string;
  description: string;
  content: string;
  content_hash: string;
  storage_path: string | null;
  meta: Record<string, unknown>;
  created_at: string;
}

export interface Skill {
  id: string;
  slug: string;
  name: string;
  description: string;
  version: string;
  source: string;
  enabled: boolean;
  is_system: boolean;
  content_hash: string;
  frontmatter: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface SkillDetail extends Skill {
  body: string;
  files: string[];
  assigned_agent_ids: string[];
}

export interface Provider {
  id: string;
  slug: string;
  name: string;
  provider_type: string;
  base_url: string | null;
  api_key_hint: string | null;
  default_model: string | null;
  enabled: boolean;
  health_status: string | null;
  last_health_check_at: string | null;
  config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface Project {
  id: string;
  slug: string;
  name: string;
  description: string;
  owner_id: string | null;
  workspace_path: string;
  status: string;
  meta: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface DashboardSummary {
  generated_at: string;
  health: Record<string, string>;
  agents_by_status: Record<string, number>;
  tasks_by_status: Record<string, number>;
  agents_total: number;
  tasks_total: number;
  approvals_pending: number;
  open_competitions: number;
  findings_by_severity: Record<string, number>;
  total_tokens: number;
  total_cost_usd: string;
  activity: ActivityItem[];
  limits: Record<string, unknown>;
}

export interface ActivityItem {
  id: string;
  kind: string;
  action: string;
  resource_type: string;
  resource_id: string | null;
  success: boolean;
  actor_user_id: string | null;
  created_at: string;
}

export interface UsageSeries {
  days: number;
  points: { day: string; requests: number; tokens: number; cost_usd: string }[];
  total_requests: number;
  total_tokens: number;
  total_cost_usd: string;
}

export interface DomainEvent {
  id: string;
  type: string;
  occurred_at: string;
  source: string;
  task_id: string | null;
  agent_id: string | null;
  payload: Record<string, unknown>;
}

export interface Setting {
  key: string;
  value: Record<string, unknown>;
  description: string;
  is_secret: boolean;
  editable: boolean;
  updated_at: string;
}

export interface SettingList {
  items: Setting[];
  environment: Record<string, unknown>;
}

export interface ScannerInfo {
  name: string;
  available: boolean;
  sandbox: string;
  network: string;
  reason: string;
}

export interface Scan {
  id: string;
  scope_id: string;
  tool: string;
  target_path: string;
  status: string;
  command: string;
  exit_code: number | null;
  output: Record<string, unknown>;
  error: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface ProjectFile {
  path: string;
  type: string;
  size: number;
}

export interface CodingSession {
  id: string;
  project_id: string;
  task_id: string | null;
  engine: string;
  model: string | null;
  session_id: string | null;
  status: string;
  instruction: string;
  summary: string;
  transcript_path: string | null;
  exit_code: number;
  duration_ms: number;
  changes: Record<string, unknown>[];
  error: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface CommandResult {
  project_id: string;
  command: string;
  exit_code: number;
  stdout: string;
  stderr: string;
  duration_ms: number;
  timed_out: boolean;
  truncated: boolean;
  sandbox: string;
  ok: boolean;
}

export interface WorkspaceListing {
  project_id: string;
  slug: string;
  path: string;
  exists: boolean;
  file_count: number;
  total_bytes: number;
  repositories: number;
}

export interface LogEntry {
  id: string;
  source: string;
  level: string;
  event_type: string;
  message: string;
  task_id: string | null;
  agent_id: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface LogPage {
  total: number;
  limit: number;
  offset: number;
  items: LogEntry[];
}

export interface OpsSandboxStatus {
  enabled: boolean;
  allow_host_execution: boolean;
  docker_available: boolean;
  image: string;
  mode: string;
  max_timeout_seconds: number;
  memory_limit: string;
  cpu_limit: number;
  pids_limit: number;
}

export interface OpsResourceUsage {
  workspace_root: string;
  workspace_filesystem_free_bytes: number | null;
  workspace_filesystem_total_bytes: number | null;
  workspace_bytes: number;
  workspace_quota_bytes: number;
  database_tasks: number;
  running_tasks: number;
  queued_tasks: number;
  pending_approvals: number;
  oldest_pending_approval_seconds: number | null;
  model_calls_24h: number;
  tokens_24h: number;
  cost_usd_24h: number;
}

export interface OpsStatus {
  generated_at: string;
  environment: string;
  version: string;
  rate_limit_enabled: boolean;
  rate_limit_requests: number;
  rate_limit_window_seconds: number;
  metrics_enabled: boolean;
  metrics_token_required: boolean;
  retention: Record<string, number>;
  sandbox: OpsSandboxStatus;
  resources: OpsResourceUsage;
  components: Record<string, string>;
}
