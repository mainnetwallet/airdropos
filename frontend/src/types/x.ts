/**
 * Types for the X.com integration page.
 *
 * Nothing here carries a secret. The backend never returns a client secret, an
 * access token or a refresh token — only masks, scopes and status — so there is
 * nothing here that could be cached or logged by accident.
 */

/** One X API configuration, as the backend is willing to describe it. */
export interface XApiConfiguration {
  id: string
  name: string
  description: string
  /** Masked hint only, e.g. `abcde…9x`. Never the real consumer key. */
  client_id_preview: string
  has_client_secret: boolean
  has_access_token: boolean
  has_refresh_token: boolean
  client_secret_mask: string
  access_token_mask: string
  scopes: string[]
  x_user_id: string
  x_username: string
  is_enabled: boolean
  is_default: boolean
  status: XConfigStatus
  status_detail: string
  last_checked_at: string | null
  last_used_at: string | null
  token_expires_at: string | null
  rate_limit_remaining: number | null
  rate_limit_reset_at: string | null
  created_by: string
  created_at: string | null
  vault_ready: boolean
  can_write: boolean
}

export type XConfigStatus =
  | 'UNVERIFIED'
  | 'OK'
  | 'AUTH_REQUIRED'
  | 'INVALID'
  | 'RATE_LIMITED'
  | 'ERROR'

/** What a new or edited configuration sends. Secrets are write-only. */
export interface XApiConfigurationInput {
  name: string
  description: string
  client_id: string
  client_secret: string
  access_token: string
  refresh_token: string
  username: string
  enabled: boolean
  default: boolean
}

export type XExecutionPreference = 'auto' | 'api_preferred' | 'browser_preferred'

export interface XOperationSpec {
  name: string
  label: string
  write: boolean
  supported_by: Array<'api' | 'browser'>
  api_scope: string
  api_caveat: string
  browser_note: string
}

export interface XCatalog {
  preferences: XExecutionPreference[]
  methods: Array<'api' | 'browser'>
  operations: XOperationSpec[]
}

export interface XBrowserStatus {
  ok: boolean
  session_key?: string
  running?: boolean
  mode?: string
  active_url?: string
  active_title?: string
  last_error?: string | null
  limitations?: string[]
  login?: string
  detail?: string
}

export interface XActivityRecord {
  id: string
  config_id: string
  config_name: string
  browser_profile_id: string
  method: 'api' | 'browser'
  operation: string
  target: string
  status: string
  operation_key: string
  outcome_uncertain: boolean
  result_summary: Record<string, unknown>
  error: string
  error_category: string
  duration_ms: number | null
  task_id: string
  created_at: string | null
}

export interface XOverview {
  api_enabled: boolean
  vault: { ready: boolean; key_source: string; algorithm: string; detail: string }
  configurations: XApiConfiguration[]
  configuration_count: number
  enabled_count: number
  authorized_count: number
  verified_count: number
  browser_profile_id: string
  browser_profile_reason: string
  required_scopes: string[]
  catalog: XCatalog
  note: string
  preference: XExecutionPreference
  browser: XBrowserStatus
  recent_activity: XActivityRecord[]
  counts: {
    recent_success: number
    recent_failed: number
    recent_uncertain: number
    recent_needs_user: number
  }
}

/** One accepted row in a bulk-import dry run. Never carries a value. */
export interface XImportRow {
  row: number
  name: string
  description: string
  client_id_preview: string
  has_client_secret: boolean
  has_access_token: boolean
  has_refresh_token: boolean
  username: string
  enabled: boolean
  default: boolean
}

export interface XImportIssue {
  row: number
  name: string
  field: string
  message: string
}

export interface XImportSchema {
  formats: string[]
  json_shapes: string[]
  fields: Record<string, { required: boolean | string; type: string; notes: string }>
  aliases: Record<string, string>
  max_rows: number
}

export interface XImportPreview {
  valid_count: number
  error_count: number
  rows: XImportRow[]
  errors: XImportIssue[]
  warnings: XImportIssue[]
  duplicates_in_file: string[]
  duplicates_in_database: string[]
  default_conflicts: string[]
  can_import: boolean
  schema: XImportSchema
}

export interface XImportResult {
  created: string[]
  created_count: number
  skipped: Array<{ row: number; name: string; reason: string }>
  errors: XImportIssue[]
  warnings: XImportIssue[]
  configurations: XApiConfiguration[]
}

export interface XTestResult {
  ok: boolean
  status: XConfigStatus
  user?: { id: string; username: string; name: string }
  error?: string
  error_category?: string
  rate_limit_remaining?: number | null
  retry_after_seconds?: number
}

export interface XAuthorizeStart {
  authorization_url: string
  state: string
  redirect_uri: string
  scopes: string[]
  expires_in_seconds: number
  note: string
}

export interface XAuthorizeComplete {
  ok: boolean
  has_access_token: boolean
  has_refresh_token: boolean
  expires_at: string | null
  scopes: string[]
  note: string
}