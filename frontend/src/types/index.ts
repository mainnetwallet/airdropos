/** Shared TypeScript types mirroring the backend API contracts. */

export type TaskStatus =
  | 'CREATED'
  | 'RESEARCHING'
  | 'PLANNING'
  | 'READY'
  | 'IN_PROGRESS'
  | 'WAITING'
  | 'NEEDS_USER_ACTION'
  | 'RETRYING'
  | 'PARTIALLY_COMPLETED'
  | 'COMPLETED'
  | 'FAILED'
  | 'CANCELLED'
  | 'UNVERIFIED'

export const TASK_STATUS_COLORS: Record<TaskStatus, string> = {
  CREATED: 'bg-slate-700 text-slate-300',
  RESEARCHING: 'bg-cyan-900 text-cyan-300',
  PLANNING: 'bg-indigo-900 text-indigo-300',
  READY: 'bg-blue-900 text-blue-300',
  IN_PROGRESS: 'bg-blue-600 text-white',
  WAITING: 'bg-amber-900 text-amber-300',
  NEEDS_USER_ACTION: 'bg-orange-600 text-white',
  RETRYING: 'bg-purple-900 text-purple-300',
  PARTIALLY_COMPLETED: 'bg-teal-900 text-teal-300',
  COMPLETED: 'bg-green-700 text-green-200',
  FAILED: 'bg-red-800 text-red-200',
  CANCELLED: 'bg-slate-800 text-slate-500',
  UNVERIFIED: 'bg-yellow-800 text-yellow-200',
}

export interface TaskSummary {
  task_id: string
  title: string
  status: TaskStatus
  priority: number
  progress: number
  network: string | null
  telegram_account?: string | null
  retry_count: number
  created_at: string
  updated_at: string
  next_execution_at: string | null
}

export interface TaskReport {
  task_id: string
  title: string
  description?: string
  status: TaskStatus
  priority: number
  progress: number
  network: string | null
  telegram_account?: string | null
  retry_count: number
  max_retries: number
  dependencies: string[]
  planned_actions: Array<Record<string, unknown>>
  pending_actions: Array<Record<string, unknown>>
  completed_actions: Array<Record<string, unknown>>
  failed_actions: Array<Record<string, unknown>>
  checkpoints: number
  evidence_count: number
  evidence?: Array<{
    id: string
    task_id: string
    type: string
    source: string
    uri: string | null
    label: string | null
    sha256: string | null
    size_bytes: number | null
    captured_at: string | null
    recorded_at: string | null
    is_verified: boolean
  }>
  error_details: Record<string, unknown>
  next_planned_action: string | null
  blocker: string | null
  deadline_at: string | null
  next_execution_at: string | null
  last_updated: string | null
  last_verification_at: string | null
  recent_executions: Array<{
    iteration: number
    status: string
    duration_ms: number | null
    reasoning: string
    error: string | null
    at: string
  }>
  recent_events: Array<{ type: string; severity: string; reason: string | null; at: string }>
  transactions: Array<{
    hash: string | null
    status: string
    network: string
    value_usd: number
    confirmations: number
  }>
  context?: TaskContextView
}

/**
 * The task's own persistent execution context, as the backend resolves it.
 *
 * `proven_actions` and `unproven_attempts` are deliberately separate lists: the backend
 * records what was attempted and what was confirmed as different facts, and collapsing
 * them here is how a UI ends up implying an uncertain action succeeded.
 */
export interface TaskContextView {
  goal: string | null
  normalized_objective: string | null
  campaign_id: string | null
  next_step_rationale: string | null
  current_step_index: number | null
  external_state: Record<string, unknown>
  resource_refs: Record<string, unknown>
  human_intervention: Record<string, unknown>
  authorization_state: Record<string, unknown>
  unresolved_questions: string[]
  outstanding_requirements: Array<{
    text: string
    origin: string
    account_specific: boolean
  }>
  proven_actions: string[]
  unproven_attempts: Array<{ action: string; outcome: string }>
  observation_count: number
  campaign_facts: Array<{ content: string; category: string; confidence: number }>
  global_fact_count: number
  error?: string
}

export interface ContextBudget {
  context_window: number
  reserved_output: number
  safety_margin: number
  usable_input: number
  trigger: number
  target: number
}

export interface AgentInfo {
  base_url: string
  model_id: string
  temperature: number
  max_tokens: number | null
  timeout_seconds: number
  max_retries: number
  api_key_configured: boolean
  api_key_masked: string
  policy: string
  /**
   * What the provider actually said it served.
   *
   * `model_id` is what AirdropOS REQUESTED. The backend never fabricates a
   * served model, so an entry here that differs from `model_id` means the
   * endpoint routed the request somewhere else -- which AirdropOS cannot
   * prevent, but an operator must be able to see.
   */
  context_budget?: ContextBudget
  served_models_observed?: string[]
  provider_omitted_model?: boolean
}

/**
 * One tool the agent actually ran, as reported by the backend.
 *
 * Every field here came from a real `ToolRegistry.invoke` call: the status is the
 * result status, `data` is the service's own output, and `evidence` is whatever the
 * tool attached. The UI renders this rather than deciding for itself what happened.
 */
export interface AgentToolCall {
  tool: string
  status: 'success' | 'failure' | 'needs_user_action' | 'not_authorized' | 'unavailable'
  arguments: Record<string, unknown>
  data: Record<string, unknown>
  error?: string
  error_category?: string
  retryable?: boolean
  duration_ms: number
  evidence?: { authoritative?: boolean; kind?: string } & Record<string, unknown>
}

/** A value-moving operation that is prepared but still needs the operator. */
export interface AgentApprovalRequest {
  proposal_id: string
  kind: string
  network: string
  chain_id: number
  wallet: string
  to: string
  asset: string
  amount: string
  estimated_fee_native: string
  warnings: string[]
  approval_fields: Record<string, unknown>
  state: string
}

/** One configured rule from the autonomous execution policy. */
export interface AutonomousRule {
  rule_id: string
  category: string
  action: 'allow' | 'deny'
  max_value_usd: number | null
  max_value_native: string | null
  allowed_networks: string[] | null
  allowed_tokens: string[] | null
  allowed_destinations: string[] | null
  allowed_source_wallets: string[] | null
  require_receipt: boolean
  description: string
}

/**
 * What the agent may do without asking. Read from backend configuration.
 *
 * The UI only ever displays this. Every allow/deny decision is made by the backend
 * policy; nothing here grants authority.
 */
export interface AutonomyPolicy {
  policy: {
    enabled: boolean
    source: string
    rules: AutonomousRule[]
    value_moving: boolean
    categories_known: string[]
    note: string
  }
  value_moving_enabled: boolean
  emergency_stop: Record<string, unknown>
  note: string
}

/** A prepared transfer the backend has authorized on its own. */
export interface AutonomousApproval {
  tool: string
  rule_id: string
  network: string
  to: string
  asset: string
  amount: string
  estimated_fee_native: string
  tx_hash?: string
}

export interface AgentChatResponse {
  reply: string
  tool_calls: AgentToolCall[]
  task: {
    task_id: string
    status: string
    title: string
    priority: number
    worker_will_execute?: boolean
  } | null
  approvals_required: AgentApprovalRequest[]
  blockers: string[]
  needs_user_action: string[]
  verified: Array<{ tool: string; evidence: Record<string, unknown> }>
  usage: Record<string, number>
  model?: string
  truncated: boolean
  note?: string
}

/**
 * One event of a streamed agent turn (`POST /agent/chat/stream`).
 *
 * `reply_delta` text is a live preview of the answer as it is written; the `done` event
 * carries the authoritative result, identical to the blocking `/agent/chat` response, and
 * is what the UI keeps. A tool is only ever reported as done by a `tool_result` (or the
 * final `done`), never inferred from the preview text.
 */
export type AgentStreamEvent =
  | { type: 'round_start'; round: number }
  | { type: 'reply_delta'; round: number; text: string }
  | { type: 'tool_start'; round: number; tool: string; arguments?: Record<string, unknown> }
  | {
      type: 'tool_result'
      round: number
      tool: string
      status: string
      duration_ms?: number
      error?: string | null
    }
  | ({ type: 'done' } & AgentChatResponse)
  | { type: 'error'; error: string; category?: string; retryable?: boolean }

export interface ToolDescriptor {
  name: string
  version: string
  description: string
  input_schema: Record<string, unknown>
  output_schema: Record<string, unknown>
  required_permissions: string[]
  supported_networks: string[]
  side_effect: string
  timeout_seconds: number
  is_idempotent: boolean
  enabled: boolean
  authorized?: boolean
  disabled_reason?: string
  authorization_reason?: string
  metrics: {
    total_calls: number
    successes: number
    failures: number
    success_rate: number | null
    avg_duration_ms: number | null
  }
}

export interface WalletView {
  id: string
  label: string
  address: string
  chain_type: string
  derivation_path?: string | null
  supported_networks: string[]
  is_active: boolean
  is_locked: boolean
  /** The wallet the agent uses when a task asks for an address and names none. */
  is_default?: boolean
  /** True when an encrypted key is stored. The key itself is never sent. */
  has_key_material: boolean
  /** Capability flag: key present, wallet active and unlocked. Watch-only is always false. */
  can_sign: boolean
  total_spent_usd: number
  notes?: string | null
  created_at?: string | null
}

export interface Overview {
  projects: number
  campaigns: number
  goals: number
  active_tasks: number
  completed_tasks: number
  failed_tasks: number
  needs_user_action: number
  wallets: number
  tracked_cost_usd: number
  upcoming_deadlines: Array<{ task_id: string; title: string; deadline: string }>
  agent: AgentInfo
  emergency_stop: boolean
  recent_notifications: NotificationRecord[]
}

export interface NotificationRecord {
  title: string
  body: string
  severity: string
  task_id?: string | null
  sent_at: string
  results: Record<string, unknown>
}

export interface EmergencyStopStatus {
  emergency_stop_enabled: boolean
  activated_at: string | null
  activated_by: string | null
  reason: string | null
  policy: string[]
  can_execute_tasks: boolean
  can_submit_transactions: boolean
  can_sign: boolean
  can_run_jobs: boolean
  disclaimer: string
}

export interface DiagnosticsResponse {
  subsystems: Record<string, {
    status: string
    detail?: unknown
    last_checked_at?: string | null
  }>
  overall: string
  note: string
}

export interface GoalView {
  id: string
  goal_code: string
  title: string
  status: string
  progress: number
  next_planned_action: string | null
  blockers: string[]
}

export interface ImprovementProposal {
  id: string
  title: string
  status: string
  security_review_required: boolean
  affected_files: string[]
  required_tests: string[]
  branch_name: string | null
  rollback_plan: string | null
  created_at: string
}

export interface MemoryView {
  id: string
  category: string
  content: string
  source: string
  confidence: number
  is_inference: boolean
  created_at: string
}

export interface WsEvent {
  type: string
  payload: Record<string, unknown>
  at: string
}

// ======================================================================
// Agent Inbox
// ======================================================================

/**
 * One task-associated conversation. Every field here is reported by the backend;
 * nothing on this screen is inferred client-side.
 */
export interface InboxConversation {
  conversation_id: string
  task_id: string | null
  title: string | null
  task_status: TaskStatus | string | null
  task_progress: number | null
  status: string
  requires_reply: boolean
  last_message_at: string | null
  last_seq: number
  last_message_preview?: string | null
}

export interface InboxMessage {
  id: string
  role: 'user' | 'assistant' | 'system' | 'tool' | string
  kind: string
  content: string
  seq: number
  created_at: string | null
  intervention_request_id: string | null
}

/** The structured record of what is blocking a task. */
export interface InterventionRequest {
  request_id: string
  task_id: string
  conversation_id: string
  reason_code: string
  problem_summary: string
  detailed_explanation: string | null
  required_action: string | null
  instructions: string[]
  integration_ref: string | null
  blocking_step: string | null
  error_category: string | null
  checkpoint_name?: string | null
  status: string
  created_at: string | null
  resolved_at: string | null
  resolved_by: string | null
  verification_method: string | null
  verification_outcome: string | null
  machine_verifiable: boolean
  requires_explicit_authorization: boolean
}

/** Real task state for the details panel. Absent fields are absent, never invented. */
export interface InboxTaskSummary {
  task_id: string
  title: string
  status: TaskStatus | string
  progress: number
  retry_count: number
  max_retries: number
  next_planned_action: string | null
  next_execution_at: string | null
  last_updated: string | null
  error_details: Record<string, unknown>
  detail_url: string
}

export interface InboxConversationDetail extends InboxConversation {
  messages: InboxMessage[]
  message_count: number
  has_more: boolean
  open_request: InterventionRequest | null
  task: InboxTaskSummary | null
}

export interface InboxListResponse {
  conversations: InboxConversation[]
  count: number
  total: number
  needs_reply: number
}

export interface InboxSummary {
  conversations: number
  needs_reply: number
  active: number
}

/** What the backend decided to do with a reply. */
export interface InboxReplyResult {
  status: 'RESUMED' | 'CLARIFIED' | 'CANCELLED' | 'NEEDS_MORE_INFO' | 'RECORDED' | 'IGNORED' | string
  task_id: string
  conversation_id: string
  message_id: string
  agent_message_id: string | null
  request_id: string | null
  task_status: string
  resumed: boolean
  detail?: string
  verification?: {
    method: string
    ok: boolean
    checkable: boolean
    outcome: string
    detail: Record<string, unknown>
  }
  /** How the language model read the reply. It proposes only; the backend decides. */
  interpretation?: {
    decision:
      | 'SUFFICIENT_INFORMATION'
      | 'NEEDS_CLARIFICATION'
      | 'BLOCKER_REQUIRES_VERIFICATION'
      | 'EXPLICIT_APPROVAL_REQUIRED'
      | 'CANCEL_REQUESTED'
      | 'UNRELATED_OR_INSUFFICIENT_REPLY'
    explanation: string
    extracted_information: string[]
    missing_information: string[]
    recommended_next_step: string
  }
}

export interface WorkerLogLine {
  t: string
  level: string
  source: string
  message: string
}

export interface WorkerStatus {
  alive: boolean
  heartbeat_age_seconds: number | null
  stale_after_seconds: number
  worker: {
    worker_id?: string
    started_at?: string | null
    tool_count?: number
    tools?: string[]
    telegram_tools?: string[]
    note?: string
    current_task?: { task_id: string; title: string; since: string } | null
    last_task?: { task_id: string; outcome: string; at: string } | null
    last_error?: { at: string; task_id: string | null; type: string; message: string } | null
    last_poll_at?: string | null
    polls?: number
    claimed?: number
    finished?: number
  }
  queue: {
    ready: number
    in_progress: number
    retrying: number
    needs_user_action: number
    oldest_waiting_seconds: number | null
  }
  telegram: {
    enabled: boolean
    api_id_set: boolean
    api_hash_set: boolean
    telethon_installed: boolean
    session_file_exists: boolean
    accounts_count?: number
    ready: boolean
    hint: string | null
  }
  emergency_stop: boolean
  problems: string[]
  logs: WorkerLogLine[]
}

export interface TelegramAccount {
  label: string
  profile_name: string | null
  first_name: string | null
  last_name: string | null
  username: string | null
  user_id: number | null
  is_default: boolean
  logged_in: boolean
  legacy: boolean
  added_at: string | null
  /** True while unfinished tasks are bound to this account; it cannot be stopped or removed. */
  in_use?: boolean
  /** Public ids of those tasks. */
  active_tasks?: string[]
}
