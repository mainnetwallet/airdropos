/** Live Broadcast types. They mirror the backend session/event model exactly. */

export type SessionStatus = 'running' | 'waiting_for_user' | 'paused' | 'failed' | 'stopped'

export interface Capabilities {
  visual: boolean
  events: boolean
  pause_resume: boolean
  stop: boolean
  manual_control: boolean
  keyboard: boolean
  metadata: boolean
}

export interface IntegrationInfo {
  id: string
  label: string
  capabilities: Capabilities
  /** A provider that can list real sessions exists. */
  available: boolean
  configured: boolean | null
  note: string | null
  session_count: number
}

export interface LiveSession {
  id: string
  integration: string
  key: string
  ordinal: number
  name: string
  subtitle: string | null
  profile: { id: string; name: string } | null
  profile_saved?: boolean
  account: string | null
  status: SessionStatus
  status_detail: string | null
  created_at: number
  elapsed_seconds: number
  last_activity_at: number
  task: { id: string; title: string | null } | null
  url: string
  title: string
  viewport: { width: number; height: number } | null
  capabilities: Capabilities
  control: { owner: string; mode: 'click' | 'full'; expires_in: number } | null
  waiting: { reason: string | null; since: number | null; timed_out: boolean } | null
  error?: string | null
  meta: Record<string, unknown>
}

export interface LiveResources {
  cpu_percent: number | null
  ram_percent: number | null
  disk_percent: number | null
  network_kbps: number | null
  cpu_source?: string
}

export interface LiveOverview {
  gateway: { available: boolean; reason: string | null }
  /** Integrations whose session list could not be read this time (their sessions are NOT shown). */
  degraded?: string[]
  integrations: IntegrationInfo[]
  sessions: LiveSession[]
  counts: Record<SessionStatus | 'total', number>
  resources: Partial<LiveResources>
  can_control: boolean
  emergency_stop: boolean
}

export interface LiveEvent {
  seq: number
  id: string
  session_id: string | null
  integration: string
  at: string
  type: string
  status: string | null
  message: string
}

export interface LiveEventsResponse {
  epoch: string
  last_seq: number
  events: LiveEvent[]
}

export interface ControlGrant {
  token: string
  mode: 'click' | 'full'
  ttl: number
  last_seq: number
}

export type InputEvent =
  | { type: 'click' | 'dblclick'; fx: number; fy: number; frame_w?: number; frame_h?: number }
  | { type: 'wheel'; fx: number; fy: number; dx: number; dy: number; frame_w?: number; frame_h?: number }
  | { type: 'key'; key: string }
  | { type: 'text'; text: string }

export interface BrowserProfileInfo {
  id: string
  name: string
  description: string
  viewport_width: number
  viewport_height: number
  locale: string
  timezone_id: string
  start_url: string
  created_at: string
  updated_at: string
  has_saved_state: boolean
  // 'unknown' = the worker could not be reached, so the real state cannot be confirmed
  session_status: SessionStatus | 'unknown' | null
}

export interface BrowserProfileInput {
  name: string
  description: string
  viewport_width: number
  viewport_height: number
  locale: string
  timezone_id: string
  start_url: string
}
