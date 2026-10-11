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
  retry_count: number
  created_at: string
  updated_at: string
  next_execution_at: string | null
}

export interface TaskReport {
  task_id: string
  title: string
  status: TaskStatus
  priority: number
  progress: number
  network: string | null
  retry_count: number
  max_retries: number
  dependencies: string[]
  planned_actions: Array<Record<string, unknown>>
  pending_actions: Array<Record<string, unknown>>
  completed_actions: Array<Record<string, unknown>>
  failed_actions: Array<Record<string, unknown>>
  checkpoints: number
  evidence_count: number
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
}

export interface AgentInfo {
  base_url: string
  model_id: string
  temperature: number
  max_tokens: number
  timeout_seconds: number
  max_retries: number
  api_key_configured: boolean
  api_key_masked: string
  policy: string
}

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
  supported_networks: string[]
  is_active: boolean
  is_locked: boolean
  total_spent_usd: number
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
    ready: boolean
    hint: string | null
  }
  emergency_stop: boolean
  problems: string[]
  logs: WorkerLogLine[]
}
