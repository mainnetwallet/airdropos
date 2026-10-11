/**
 * API client for the AirdropOS backend.
 *
 * Secrets are never read back: masked values are display-only and the API does
 * not return stored credentials to the browser.
 */

import type {
  AgentInfo,
  DiagnosticsResponse,
  EmergencyStopStatus,
  GoalView,
  ImprovementProposal,
  MemoryView,
  Overview,
  TaskReport,
  TaskSummary,
  ToolDescriptor,
  WalletView,
} from '@/types'

const API_BASE = import.meta.env.VITE_API_URL ?? '/api/v1'

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
  } catch {
    // ignore
  }
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
    let detail: unknown
    try {
      detail = await response.json()
    } catch {
      detail = await response.text()
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
  health: () => request<Record<string, unknown>>('/health'),
  readiness: () => request<Record<string, unknown>>('/readiness'),
  diagnostics: () => request<DiagnosticsResponse>('/diagnostics'),

  // --- Agent ------------------------------------------------------------
  agentInfo: () => request<AgentInfo>('/agent/info'),
  agentChat: (message: string) =>
    request<{ reply: string; model: string; usage: Record<string, number> }>(
      '/agent/chat',
      { method: 'POST', body: JSON.stringify({ message }) },
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

  // --- Goals ------------------------------------------------------------
  listGoals: () => request<{ goals: GoalView[] }>('/goals'),
  pauseGoal: (id: string) =>
    request<{ id: string; status: string }>(`/goals/${id}/pause`, { method: 'POST' }),
  resumeGoal: (id: string) =>
    request<{ id: string; status: string }>(`/goals/${id}/resume`, { method: 'POST' }),

  // --- Wallets ----------------------------------------------------------
  listWallets: () => request<{ wallets: WalletView[] }>('/wallets'),

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

const WS_BASE =
  import.meta.env.VITE_WS_URL ??
  (API_BASE.startsWith('http')
    ? `${API_BASE.replace(/^http/, 'ws')}/ws`
    : `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}${API_BASE}/ws`)

/** WebSocket URL including the session token (the backend reads it from ?token=). */
export function wsUrl(): string {
  const token = getToken()
  return token ? `${WS_BASE}?token=${encodeURIComponent(token)}` : WS_BASE
}
