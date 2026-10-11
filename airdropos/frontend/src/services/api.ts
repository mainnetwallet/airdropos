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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
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
    const message =
      (detail as { detail?: string })?.detail ?? `Request failed with ${response.status}`
    throw new ApiError(message, response.status, detail)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
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

export const WS_URL =
  import.meta.env.VITE_WS_URL ??
  `${API_BASE.replace(/^http/, 'ws')}/ws`
