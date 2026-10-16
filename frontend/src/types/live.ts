/** Live Broadcast types. They mirror the backend session/event model exactly. */

export type SessionStatus = 'running' | 'waiting_for_user' | 'paused' | 'failed' | 'stopped'

export interface Capabilities {
  visual: boolean
  events: boolean
  pause_resume: boolean
  stop: boolean
  manual_control: boolean
  keyboard: boolean
  chat?: boolean
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

export interface InstalledExtension {
  id: string
  name: string
  version: string
  enabled: boolean
  from_webstore: boolean
  description: string
  homepage: string
  manifest_version: number
  permissions: string[]
}

export interface BrowserTab {
  tab_id: string
  index: number
  url: string
  raw_scheme: string
  active: boolean
  closed: boolean
  title: string
}

/** How a profile's browser was obtained. All of these are real, reported states. */
export type BrowserConnection = 'managed_persistent' | 'ephemeral' | 'attached' | 'not_running' | null

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
  // Chrome profile configuration (backend-resolved)
  browser_type: 'chrome' | 'chromium'
  launch_mode: 'managed' | 'attach'
  executable_path: string
  resolved_executable: string
  user_data_dir: string
  resolved_user_data_dir: string
  headless: boolean
  debugger_host: string
  debugger_port: number
  unpacked_extensions: string
  is_default_agent: boolean
  last_used_at: string
  last_opened_at: string
  last_error: string
  last_recovery: string
  configuration_problems: string[]
  // 'unknown' = the worker could not be reached, so the real state cannot be confirmed
  session_status: SessionStatus | 'unknown' | null
  browser_running: boolean
  connection: BrowserConnection
  tabs: BrowserTab[]
  tab_count: number | null
  active_url: string
  active_title: string
  extensions: InstalledExtension[]
  extension_count: number | null
  limitations: string[]
}

export interface BrowserProfileInput {
  name: string
  description: string
  viewport_width: number
  viewport_height: number
  locale: string
  timezone_id: string
  start_url: string
  browser_type: 'chrome' | 'chromium'
  launch_mode: 'managed' | 'attach'
  executable_path: string
  user_data_dir: string
  headless: boolean | null
  debugger_host: string
  debugger_port: number
  unpacked_extensions: string
}

export interface ChromeInfo {
  found: boolean
  path: string
  source: string
  version: string
  detail: string
}

export interface BrowserProfilesResponse {
  profiles: BrowserProfileInfo[]
  default_agent_profile: string | null
  worker_reachable: boolean
  chrome: ChromeInfo
  profile_root: string
  persistent_profiles_enabled: boolean
}

// --- Chat view (message-based sessions such as Telegram) -----------------------
export interface ChatDialog {
  peer: string
  title: string
  username: string | null
  kind: string | null
  unread: number
  last_text: string
  last_from_me: boolean
  last_date: string | null
  pinned: boolean
}

export interface ChatButton {
  row: number
  col: number
  text: string
  kind: string
  clickable: boolean
  miniapp?: boolean
  url: string | null
}

export interface ChatMediaInfo {
  kind: 'photo' | 'sticker' | 'gif' | 'video' | 'voice' | 'audio' | 'image' | 'file'
  name: string
  size: number | null
  mime: string | null
  preview: boolean
  downloadable: boolean
}

export interface ChatMessage {
  id: number
  from_me: boolean
  date: string | null
  sender: string | null
  text: string
  media: string | null
  media_info?: ChatMediaInfo | null
  reply_to: number | null
  edited: boolean
  buttons: ChatButton[][]
}

export interface ChatHistory {
  peer: string
  title: string
  username: string | null
  is_bot: boolean
  messages: ChatMessage[]
}

export interface LiveTelegramAccount {
  label: string
  name: string | null
  username: string | null
  is_default: boolean
  session_ready: boolean
  open: boolean
}
