/**
 * Types for the Discord page.
 *
 * Nothing here carries a secret, because the backend cannot return one: the Discord
 * integration is browser-only and stores no credential at all. A Discord account is
 * an existing browser profile that the operator signed into by hand, and the API only
 * ever describes it by id, name and launch settings.
 *
 * The status vocabulary is deliberately four-valued rather than a boolean. `UNKNOWN`
 * is a real answer meaning "the page could not be read", and collapsing it into
 * "logged out" would send the operator to sign in to an account that is already fine.
 */
import type { TaskStatus } from './index'

/** Login state as actually observed on the discord.com page. */
export type DiscordLoginState = 'LOGGED_IN' | 'LOGGED_OUT' | 'UNKNOWN' | 'NO_BROWSER'

export interface DiscordLoginReading {
  state: DiscordLoginState
  /** What the verdict was based on, e.g. `login_form`, `app_chrome`. */
  indicator: string
  detail: string
  observed_at: string
}

export type DiscordIntent = 'read' | 'draft' | 'write' | 'unknown'

/** An existing browser profile, described as a Discord account. */
export interface DiscordProfile {
  id: string
  name: string
  description: string
  launch_mode: string
  browser_type: string
  headless: boolean
  user_data_dir: string
  start_url: string
  is_default_agent: boolean
  last_used_at: string
  last_opened_at: string
  last_error: string
  /** False when the profile has a configuration problem and cannot be launched. */
  can_open: boolean
  open_problems: string[]
}

export interface DiscordChannelRef {
  server_id: string
  server_name: string
  channel_id: string
  channel_name: string
  label: string
}

export interface DiscordActivityRecord {
  id: string
  browser_profile_id: string
  operation: string
  intent: string
  target: string
  status: string
  operation_key: string
  outcome_uncertain: boolean
  channel: Partial<DiscordChannelRef>
  message_id: string
  permalink: string
  /** A bounded excerpt. Never the full message body. */
  excerpt: string
  evidence_path: string
  error: string
  error_category: string
  duration_ms: number | null
  task_id: string
  created_at: string
}

export interface DiscordCatalogOperation {
  name: string
  label: string
  intent: DiscordIntent
  write: boolean
  consequential: boolean
  note: string
}

export interface DiscordCatalog {
  intents: DiscordIntent[]
  operations: DiscordCatalogOperation[]
  write_operations: string[]
  consequential_operations: string[]
  draft_behaviour: string
}

export interface DiscordOverview {
  mode: string
  note: string
  profiles: DiscordProfile[]
  profile_count: number
  openable_profile_count: number
  selected_profile_id: string
  catalog: DiscordCatalog
  policy_note: string
  anti_spam_note: string
  recent_activity: DiscordActivityRecord[]
  counts: {
    recent_total: number
    recent_success: number
    recent_failed: number
    recent_uncertain: number
    recent_needs_user: number
  }
  duplicate_window_hours: number
}

/** What a resume needs to know, read back from the task checkpoint. */
export interface DiscordResumeState {
  browser_profile_id: string
  operation: string
  intent: string
  channel: Partial<DiscordChannelRef>
  completed_steps: string[]
  pending_action: string
  unconfirmed_sends: string[]
  last_evidence: string
  login_intervention_id: string
  deadline: string
  last_page_state: Record<string, string>
}

export interface DiscordTask {
  task_id: string
  title: string
  status: TaskStatus | string
  progress: number
  retry_count: number
  max_retries: number
  next_planned_action: string
  last_blocker: string
  created_at: string
  updated_at: string
  deadline_at: string
  browser_profile_id: string
  intent: string
  resume: DiscordResumeState
  checkpoint: { name: string; saved_at: string }
  activity?: DiscordActivityRecord[]
  login_intervention?: {
    request_id: string
    status: string
    required_action: string
    problem_summary: string
  } | null
}

export interface DiscordLoginStateResponse {
  ok: boolean
  browser_profile_id: string
  url?: string
  login_state: DiscordLoginReading
  /** True only for a positive reading either way. */
  verified: boolean
}

export interface DiscordOpenProfileResponse {
  profile_id: string
  profile_name: string
  url: string
  running: boolean
  reused: boolean
  mode: string
  note: string
}

export interface DiscordCreateTaskInput {
  browser_profile_id: string
  description: string
  server_id?: string
  channel_id?: string
  channel_name?: string
  intent?: DiscordIntent | ''
  title?: string
  deadline_hours?: number
}

export interface DiscordCreateTaskResult {
  task_id: string
  browser_profile_id: string
  intent: DiscordIntent
  detected_signals: string[]
  status: string
  title: string
}

export interface DiscordStatusResponse {
  profiles: DiscordProfile[]
  manage_url: string
  default_agent_profile_id: string
  note: string
}