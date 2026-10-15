/**
 * API client for the AirdropOS backend.
 *
 * Secrets are never read back: masked values are display-only and the API does
 * not return stored credentials to the browser.
 */

import type {
  AgentChatResponse,
  AgentInfo,
  AutonomyPolicy,
  DiagnosticsResponse,
  EmergencyStopStatus,
  GoalView,
  ImprovementProposal,
  InboxConversationDetail,
  InboxListResponse,
  InboxReplyResult,
  InboxSummary,
  MemoryView,
  Overview,
  TaskReport,
  TaskSummary,
  ToolDescriptor,
  WalletView,
  TelegramAccount,
  WorkerStatus,
} from '@/types'
import type {
  BrowserProfileInfo,
  BrowserProfileInput,
  BrowserProfilesResponse,
  ControlGrant,
  InputEvent,
  InstalledExtension,
  LiveEventsResponse,
  LiveOverview,
  LiveSession,
} from '@/types/live'

const API_BASE = import.meta.env.VITE_API_URL ?? '/api/v1'

export interface HealthBanner {
  status: string
  service: string
  /** Whether this deployment requires a session token. Older backends omit it. */
  auth_required?: boolean
}

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public detail?: unknown,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

// --- Session token -------------------------------------------------------
// The backend requires a bearer token on every route. The token is kept in
// localStorage (wrapped in try/catch because storage can be unavailable).
const TOKEN_KEY = 'airdropos.token'
const EMAIL_KEY = 'airdropos.email'
export const UNAUTHORIZED_EVENT = 'airdropos:unauthorized'

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? ''
  } catch {
    return ''
  }
}

export function getEmail(): string {
  try {
    return localStorage.getItem(EMAIL_KEY) ?? ''
  } catch {
    return ''
  }
}

function saveSession(token: string, email: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token)
    localStorage.setItem(EMAIL_KEY, email)
  } catch {
    // Storage blocked: the session then lasts only until the page reloads.
  }
}

export function clearSession(): void {
  try {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(EMAIL_KEY)
    // Never leave one operator's agent conversation behind for the next login.
    localStorage.removeItem('airdropos.agentChat.v1')
  } catch {
    // ignore
  }
}

// --- Whether this deployment needs a session ------------------------------
// `null` until the liveness probe has answered, which it does without credentials.
let authRequired: boolean | null = null

export function isAuthRequired(): boolean | null {
  return authRequired
}

export function setAuthRequired(value: boolean | null): void {
  authRequired = value
}

/** True when a session token is present. Says nothing about whether it is still valid. */
export function hasSession(): boolean {
  return getToken() !== ''
}

/**
 * Whether opening the live event stream can succeed right now.
 *
 * With authentication on and no token the handshake is refused before it is
 * accepted, so the browser sees a failed connection, the hook schedules a retry,
 * and every attempt leaves a refused handshake in the log. Deciding locally turns
 * that loop into a socket that is simply not opened. With authentication off the
 * backend accepts an anonymous stream, so one is still opened, and an older backend
 * that does not report the flag keeps the original reconnect behaviour.
 */
export function canOpenLiveStream(): boolean {
  if (authRequired === true) return hasSession()
  return true
}

/** Turn the backend's error body ({detail: string | {error, hint}}) into readable text. */
function errorMessage(body: unknown, status: number): string {
  const detail = (body as { detail?: unknown } | undefined)?.detail ?? body
  if (typeof detail === 'string' && detail) return detail
  if (detail && typeof detail === 'object') {
    const d = detail as { error?: unknown; hint?: unknown; message?: unknown }
    const main = [d.error, d.message].find((v) => typeof v === 'string') as string | undefined
    if (main) return typeof d.hint === 'string' ? `${main} ${d.hint}` : main
    try {
      return JSON.stringify(detail)
    } catch {
      // fall through
    }
  }
  return `Request failed with ${status}`
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken()
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {}),
    },
  })

  if (!response.ok) {
    // Read the body exactly once; json() followed by text() throws
    // "body stream already read" and hides the real server error.
    const raw = await response.text()
    let detail: unknown
    try {
      detail = raw ? JSON.parse(raw) : raw
    } catch {
      detail = raw
    }
    // A 401 on any call except the login form itself means the session is gone.
    if (response.status === 401 && path !== '/auth/login') {
      clearSession()
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
    }
    throw new ApiError(errorMessage(detail, response.status), response.status, detail)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  // --- Auth -------------------------------------------------------------
  /**
   * The unauthenticated liveness probe, which also reports whether this deployment
   * needs a session token.
   *
   * Deliberately NOT sent with a session token: it needs none, and this is the call
   * the login gate makes on every page load, including before there is a token.
   * Sending no credentials is also why it can never fail with a 401.
   */
  healthBanner: async () => {
    const response = await fetch(`${API_BASE}/health`, {
      headers: { Accept: 'application/json' },
      cache: 'no-store',
    })
    if (!response.ok) throw new ApiError(`Service health check failed with ${response.status}`, response.status)
    return (await response.json()) as HealthBanner
  },
  login: async (email: string, password: string) => {
    const session = await request<{ token: string; email: string; expires_at: string }>(
      '/auth/login',
      { method: 'POST', body: JSON.stringify({ email, password }) },
    )
    saveSession(session.token, session.email)
    return session
  },
  logout: async () => {
    try {
      await request<unknown>('/auth/logout', { method: 'POST' })
    } catch {
      // Even if the server call fails, forget the token locally.
    }
    clearSession()
  },

  // --- System -----------------------------------------------------------
  overview: () => request<Overview>('/overview'),
  readiness: () => request<Record<string, unknown>>('/readiness'),
  diagnostics: () => request<DiagnosticsResponse>('/diagnostics'),
  workerStatus: () => request<WorkerStatus>('/worker/status'),
  telegramAccounts: () => request<{ accounts: TelegramAccount[] }>('/telegram/accounts'),
  telegramLoginStart: (body: { label: string; phone: string; make_default: boolean }) =>
    request<{ login_id: string; stage: 'code'; expires_in: number }>('/telegram/accounts/login/start', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  telegramLoginCode: (login_id: string, code: string) =>
    request<{ stage: 'password' | 'done'; label?: string }>('/telegram/accounts/login/code', {
      method: 'POST',
      body: JSON.stringify({ login_id, code }),
    }),
  telegramLoginPassword: (login_id: string, password: string) =>
    request<{ stage: 'done'; label: string }>('/telegram/accounts/login/password', {
      method: 'POST',
      body: JSON.stringify({ login_id, password }),
    }),
  telegramLoginCancel: (login_id: string) =>
    request<{ cancelled: boolean }>('/telegram/accounts/login/cancel', {
      method: 'POST',
      body: JSON.stringify({ login_id }),
    }),
  telegramSetDefault: (label: string) =>
    request<{ label: string; is_default: boolean }>(`/telegram/accounts/${encodeURIComponent(label)}/default`, {
      method: 'POST',
    }),
  telegramRemoveAccount: (label: string, revoke: boolean) =>
    request<{ removed: string; revoked_on_telegram: boolean }>(
      `/telegram/accounts/${encodeURIComponent(label)}?revoke=${revoke}`,
      { method: 'DELETE' },
    ),
  clearWorkerLogs: () =>
    request<{ cleared: number; cleared_at: string | null }>('/worker/logs/clear', { method: 'POST' }),

  // --- Agent ------------------------------------------------------------
  agentInfo: () => request<AgentInfo>('/agent/info'),
  /**
   * What the agent may do without asking. Display-only: the backend makes every
   * allow/deny decision, and the Emergency Stop overrides all of it.
   */
  autonomy: () => request<AutonomyPolicy>('/agent/autonomy'),
  /**
   * Send one operator message. The backend runs any tools the model selected and
   * returns their real results, so the response is a report, not just prose.
   */
  agentChat: (
    message: string,
    history: Array<{ role: 'user' | 'assistant'; content: string }> = [],
  ) =>
    request<AgentChatResponse>(
      '/agent/chat',
      { method: 'POST', body: JSON.stringify({ message, history }) },
    ),

  // --- Tasks ------------------------------------------------------------
  listTasks: (params?: { status?: string; limit?: number; offset?: number }) => {
    const query = new URLSearchParams()
    if (params?.status) query.set('status', params.status)
    if (params?.limit) query.set('limit', String(params.limit))
    if (params?.offset) query.set('offset', String(params.offset))
    const qs = query.toString()
    return request<{ total: number; count: number; tasks: TaskSummary[] }>(
      `/tasks${qs ? `?${qs}` : ''}`,
    )
  },
  getTask: (taskId: string) => request<TaskReport>(`/tasks/${encodeURIComponent(taskId)}`),
  createTask: (payload: {
    title: string
    description?: string
    category?: string
    network?: string
    priority?: number
  }) =>
    request<{ task_id: string; status: string; created_at: string }>('/tasks', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  deleteTask: (taskId: string) =>
    request<{ task_id: string; deleted: boolean }>(
      `/tasks/${encodeURIComponent(taskId)}`,
      { method: 'DELETE' },
    ),
  cancelTask: (taskId: string) =>
    request<{ task_id: string; status: string }>(
      `/tasks/${encodeURIComponent(taskId)}/cancel`,
      { method: 'POST' },
    ),
  retryTask: (taskId: string) =>
    request<{ task_id: string; status: string; retry_count: number }>(
      `/tasks/${encodeURIComponent(taskId)}/retry`,
      { method: 'POST' },
    ),

  // --- Agent Inbox -------------------------------------------------------
  // Task-scoped conversations. The task is resolved server-side from the
  // conversation id, so nothing here passes a task id it could tamper with.
  listInboxConversations: (params?: {
    search?: string
    needs_reply?: boolean
    include_archived?: boolean
    limit?: number
  }) => {
    const query = new URLSearchParams()
    if (params?.search) query.set('search', params.search)
    if (params?.needs_reply !== undefined) query.set('needs_reply', String(params.needs_reply))
    if (params?.include_archived) query.set('include_archived', 'true')
    if (params?.limit) query.set('limit', String(params.limit))
    const qs = query.toString()
    return request<InboxListResponse>(`/inbox/conversations${qs ? `?${qs}` : ''}`)
  },
  getInboxConversation: (conversationId: string, params?: { limit?: number; before_seq?: number }) => {
    const query = new URLSearchParams()
    if (params?.limit) query.set('limit', String(params.limit))
    if (params?.before_seq !== undefined) query.set('before_seq', String(params.before_seq))
    const qs = query.toString()
    return request<InboxConversationDetail>(
      `/inbox/conversations/${encodeURIComponent(conversationId)}${qs ? `?${qs}` : ''}`,
    )
  },
  sendInboxMessage: (
    conversationId: string,
    message: string,
    clientToken: string,
  ) =>
    request<InboxReplyResult>(
      `/inbox/conversations/${encodeURIComponent(conversationId)}/messages`,
      {
        method: 'POST',
        body: JSON.stringify({ message, client_token: clientToken }),
      },
    ),
  /**
   * Delete one conversation with its messages and intervention requests. The task it belongs
   * to is never touched: it is not cancelled, modified or deleted.
   */
  deleteInboxConversation: (conversationId: string) =>
    request<{
      conversation_id: string
      task_id: string | null
      deleted: boolean
      messages: number
      intervention_requests: number
    }>(`/inbox/conversations/${encodeURIComponent(conversationId)}`, { method: 'DELETE' }),
  inboxSummary: () => request<InboxSummary>('/inbox/summary'),

  // --- Goals ------------------------------------------------------------
  listGoals: () => request<{ goals: GoalView[] }>('/goals'),
  pauseGoal: (id: string) =>
    request<{ id: string; status: string }>(`/goals/${id}/pause`, { method: 'POST' }),
  resumeGoal: (id: string) =>
    request<{ id: string; status: string }>(`/goals/${id}/resume`, { method: 'POST' }),

  // --- Wallets ----------------------------------------------------------
  listWallets: () => request<{ wallets: WalletView[] }>('/wallets'),
  getWallet: (id: string) => request<WalletView>(`/wallets/${encodeURIComponent(id)}`),
  /** Generate a new wallet server-side. The key stays in the vault and is never returned. */
  createWallet: (payload: { label: string; notes?: string }) =>
    request<WalletView>('/wallets/create', { method: 'POST', body: JSON.stringify(payload) }),
  /**
   * Import a private key or mnemonic. The secret is sent once over the request body
   * and is never returned. Callers must not store it in query/mutation caches or storage.
   */
  importWallet: (payload: { label: string; secret: string; notes?: string }) =>
    request<WalletView>('/wallets/import', { method: 'POST', body: JSON.stringify(payload) }),
  watchWallet: (payload: { label: string; address: string; notes?: string }) =>
    request<WalletView>('/wallets/watch', { method: 'POST', body: JSON.stringify(payload) }),
  setWalletLocked: (id: string, locked: boolean) =>
    request<WalletView>(`/wallets/${encodeURIComponent(id)}/lock?locked=${locked}`, {
      method: 'POST',
    }),
  setWalletActive: (id: string, active: boolean) =>
    request<WalletView>(`/wallets/${encodeURIComponent(id)}/deactivate?active=${active}`, {
      method: 'POST',
    }),
  setWalletDefault: (id: string, makeDefault: boolean) =>
    request<WalletView>(`/wallets/${encodeURIComponent(id)}/default?default=${makeDefault}`, {
      method: 'POST',
    }),
  deleteWallet: (id: string) =>
    request<void>(`/wallets/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  // --- Tools ------------------------------------------------------------
  listTools: () =>
    request<{ tools: ToolDescriptor[]; health: Array<Record<string, unknown>> }>('/tools'),

  // --- Memory / Learning / Improvements ----------------------------------
  searchMemory: (q = '', limit = 20) =>
    request<{ memories: MemoryView[]; stats: Record<string, unknown> }>(
      `/memory?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),
  analyzeLearning: () =>
    request<{ insight_count: number; insights: Array<Record<string, unknown>> }>(
      '/learning/analyze',
      { method: 'POST' },
    ),
  listImprovements: () =>
    request<{ proposals: ImprovementProposal[] }>('/improvements'),

  // --- Emergency Stop ---------------------------------------------------
  stopStatus: () => request<EmergencyStopStatus>('/emergency-stop'),
  engageStop: (reason: string, activatedBy = 'dashboard') =>
    request<Record<string, unknown>>('/emergency-stop', {
      method: 'POST',
      body: JSON.stringify({ reason, activated_by: activatedBy }),
    }),
  releaseStop: () => request<Record<string, unknown>>('/emergency-stop/resume', { method: 'POST' }),
}

// --- Live Broadcast ----------------------------------------------------------
const ref = (integration: string, key: string) =>
  `/live/sessions/${encodeURIComponent(integration)}/${encodeURIComponent(key)}`

export interface FetchedFrame {
  blob: Blob
  width: number
  height: number
  frameId: string
  capturedAt: number
}

/**
 * One frame of a session. Fetched with the Authorization header (never a token in the
 * URL) and turned into a blob by the caller.
 */
export async function fetchFrame(
  integration: string,
  key: string,
  quality: 'thumb' | 'full',
  signal?: AbortSignal,
): Promise<FetchedFrame> {
  const token = getToken()
  const response = await fetch(`${API_BASE}${ref(integration, key)}/frame?quality=${quality}`, {
    signal,
    cache: 'no-store',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!response.ok) {
    if (response.status === 401) {
      clearSession()
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
    }
    throw new ApiError(`Frame request failed with ${response.status}`, response.status)
  }
  const blob = await response.blob()
  return {
    blob,
    width: Number(response.headers.get('X-Frame-Width')) || 0,
    height: Number(response.headers.get('X-Frame-Height')) || 0,
    frameId: response.headers.get('X-Frame-Id') ?? '',
    capturedAt: Number(response.headers.get('X-Captured-At')) || 0,
  }
}

/** Best-effort release when the page is closing; a normal fetch would be cancelled. */
export function releaseControlOnUnload(integration: string, key: string, token: string): void {
  try {
    const auth = getToken()
    void fetch(`${API_BASE}${ref(integration, key)}/control/release`, {
      method: 'POST',
      keepalive: true,
      headers: { 'Content-Type': 'application/json', ...(auth ? { Authorization: `Bearer ${auth}` } : {}) },
      body: JSON.stringify({ token }),
    })
  } catch {
    // The lock also expires on its own after a short time without heartbeats.
  }
}

Object.assign(api, {
  liveOverview: () => request<LiveOverview>('/live/overview'),
  liveEvents: (params: { session?: string; limit?: number } = {}) => {
    const q = new URLSearchParams()
    if (params.session) q.set('session', params.session)
    q.set('limit', String(params.limit ?? 100))
    return request<LiveEventsResponse>(`/live/events?${q.toString()}`)
  },
  liveCreateSession: (profileId: string, url?: string) =>
    request<{ session: LiveSession }>('/live/sessions', {
      method: 'POST',
      body: JSON.stringify({ integration: 'browser', profile_id: profileId, url: url || null }),
    }),
  livePause: (integration: string, key: string) =>
    request<Record<string, unknown>>(`${ref(integration, key)}/pause`, { method: 'POST' }),
  liveResume: (integration: string, key: string) =>
    request<{ status: string; task_requeued?: string | null; resumed_from?: string[] }>(
      `${ref(integration, key)}/resume`,
      { method: 'POST' },
    ),
  liveStop: (integration: string, key: string) =>
    request<Record<string, unknown>>(`${ref(integration, key)}/stop`, { method: 'POST' }),
  liveAcquireControl: (integration: string, key: string, mode: 'click' | 'full') =>
    request<ControlGrant>(`${ref(integration, key)}/control`, {
      method: 'POST',
      body: JSON.stringify({ mode }),
    }),
  liveRenewControl: (integration: string, key: string, token: string) =>
    request<Record<string, unknown>>(`${ref(integration, key)}/control/renew`, {
      method: 'POST',
      body: JSON.stringify({ token }),
    }),
  liveReleaseControl: (integration: string, key: string, token: string) =>
    request<{ released: boolean; counts?: Record<string, number> }>(`${ref(integration, key)}/control/release`, {
      method: 'POST',
      body: JSON.stringify({ token }),
    }),
  liveInput: (integration: string, key: string, token: string, seq: number, event: InputEvent) =>
    request<Record<string, unknown>>(`${ref(integration, key)}/input`, {
      method: 'POST',
      body: JSON.stringify({ token, seq, ...event }),
    }),

  // --- Browser profiles ---------------------------------------------------
  // Every call here returns backend-derived state. The page never invents a
  // "running" or "installed" state of its own.
  browserProfiles: () => request<BrowserProfilesResponse>('/browser/profiles'),
  createBrowserProfile: (body: BrowserProfileInput) =>
    request<BrowserProfileInfo>('/browser/profiles', { method: 'POST', body: JSON.stringify(body) }),
  updateBrowserProfile: (id: string, body: Partial<BrowserProfileInput>) =>
    request<BrowserProfileInfo>(`/browser/profiles/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),
  /** Start (or reuse) the real browser. Rejects unless the worker confirms it is running. */
  openBrowserProfile: (id: string, url?: string) =>
    request<{ profile: BrowserProfileInfo; reused: boolean }>(
      `/browser/profiles/${encodeURIComponent(id)}/open`,
      { method: 'POST', body: JSON.stringify({ url: url ?? '' }) },
    ),
  reconnectBrowserProfile: (id: string) =>
    request<{ profile: BrowserProfileInfo }>(
      `/browser/profiles/${encodeURIComponent(id)}/reconnect`,
      { method: 'POST' },
    ),
  /** Stops the browser. The Chrome profile data on disk is never touched. */
  closeBrowserProfile: (id: string) =>
    request<{ closed: string; was_running: boolean; profile_data_kept: boolean }>(
      `/browser/profiles/${encodeURIComponent(id)}/close`,
      { method: 'POST' },
    ),
  setDefaultBrowserProfile: (profileId: string | null) =>
    request<{ default_agent_profile: string | null }>('/browser/profiles/default', {
      method: 'POST',
      body: JSON.stringify({ profile_id: profileId }),
    }),
  browserProfileExtensions: (id: string) =>
    request<{
      installed: InstalledExtension[]
      installed_count: number
      unpacked_loaded_this_session: string[]
      capabilities: Record<string, unknown>
      note?: string
    }>(`/browser/profiles/${encodeURIComponent(id)}/extensions`),
  /** Removes the profile CONFIGURATION only; the Chrome data directory is kept. */
  deleteBrowserProfile: (id: string) =>
    request<{ deleted: string; chrome_data_kept_at: string }>(
      `/browser/profiles/${encodeURIComponent(id)}`,
      { method: 'DELETE' },
    ),
  /** Irreversible: permanently deletes the Chrome profile data. `confirm` must equal the id. */
  deleteBrowserProfileData: (id: string) =>
    request<{ deleted: boolean; path: string; bytes_freed?: number }>(
      `/browser/profiles/${encodeURIComponent(id)}/data`,
      { method: 'DELETE', body: JSON.stringify({ confirm: id }) },
    ),
  clearBrowserProfileState: (id: string) =>
    request<{ cleared: string }>(`/browser/profiles/${encodeURIComponent(id)}/state`, { method: 'DELETE' }),
})

/** Typed view of the methods attached above (keeps the main `api` literal untouched). */
export type LiveApi = {
  liveOverview: () => Promise<LiveOverview>
  liveEvents: (p?: { session?: string; limit?: number }) => Promise<LiveEventsResponse>
  liveCreateSession: (profileId: string, url?: string) => Promise<{ session: LiveSession }>
  livePause: (i: string, k: string) => Promise<Record<string, unknown>>
  liveResume: (i: string, k: string) => Promise<{ status: string; task_requeued?: string | null; resumed_from?: string[] }>
  liveStop: (i: string, k: string) => Promise<Record<string, unknown>>
  liveAcquireControl: (i: string, k: string, mode: 'click' | 'full') => Promise<ControlGrant>
  liveRenewControl: (i: string, k: string, token: string) => Promise<Record<string, unknown>>
  liveReleaseControl: (i: string, k: string, token: string) => Promise<{ released: boolean; counts?: Record<string, number> }>
  liveInput: (i: string, k: string, token: string, seq: number, e: InputEvent) => Promise<Record<string, unknown>>
  browserProfiles: () => Promise<BrowserProfilesResponse>
  createBrowserProfile: (b: BrowserProfileInput) => Promise<BrowserProfileInfo>
  updateBrowserProfile: (id: string, b: Partial<BrowserProfileInput>) => Promise<BrowserProfileInfo>
  openBrowserProfile: (id: string, url?: string) => Promise<{ profile: BrowserProfileInfo; reused: boolean }>
  reconnectBrowserProfile: (id: string) => Promise<{ profile: BrowserProfileInfo }>
  closeBrowserProfile: (id: string) => Promise<{ closed: string; was_running: boolean; profile_data_kept: boolean }>
  setDefaultBrowserProfile: (id: string | null) => Promise<{ default_agent_profile: string | null }>
  browserProfileExtensions: (id: string) => Promise<{
    installed: InstalledExtension[]
    installed_count: number
    unpacked_loaded_this_session: string[]
    capabilities: Record<string, unknown>
    note?: string
  }>
  deleteBrowserProfile: (id: string) => Promise<{ deleted: string; chrome_data_kept_at: string }>
  deleteBrowserProfileData: (id: string) => Promise<{ deleted: boolean; path: string; bytes_freed?: number }>
  clearBrowserProfileState: (id: string) => Promise<{ cleared: string }>
}
export const liveApi = api as typeof api & LiveApi

const WS_BASE =
  import.meta.env.VITE_WS_URL ??
  (API_BASE.startsWith('http')
    ? `${API_BASE.replace(/^http/, 'ws')}/ws`
    : `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}${API_BASE}/ws`)

/** WebSocket URL. The session token is never placed in the URL (it would leak into logs). */
export function wsUrl(): string {
  return WS_BASE
}

/**
 * WebSocket subprotocols carrying the session token: ["bearer", token].
 * Browsers cannot set an Authorization header on a WebSocket, so the token rides in
 * the Sec-WebSocket-Protocol handshake; the server echoes only "bearer".
 */
export function wsProtocols(): string[] {
  const token = getToken()
  return token ? ['bearer', token] : []
}
