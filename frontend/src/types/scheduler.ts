/** Scheduled Tasks. They mirror /api/v1/scheduled-tasks exactly. */

export type TriggerType = 'INTERVAL' | 'CRON' | 'ONCE'

export interface ScheduledTask {
  id: string
  name: string
  title: string
  description: string
  category: string | null
  network: string | null
  priority: number
  telegram_account: string | null
  skip_if_running: boolean
  trigger_type: TriggerType
  interval_minutes: number | null
  cron_expression: string | null
  /** ISO date-time of a one-time schedule, as the operator entered it. */
  run_at: string | null
  timezone: string
  is_active: boolean
  next_run_at: string | null
  last_run_at: string | null
  total_runs: number
  failure_count: number
  last_task_id: string | null
  /** SUCCESS, SKIPPED, FAILED, RUNNING; null before the first run. */
  last_run_status: string | null
  last_run_error: string | null
  created_at: string | null
}

export interface ScheduledTaskList {
  count: number
  limits: { min_interval_minutes: number; max_schedules: number }
  schedules: ScheduledTask[]
}

export interface ScheduledTaskInput {
  name: string
  title: string
  description: string
  category?: string | null
  network?: string | null
  priority: number
  telegram_account?: string | null
  skip_if_running: boolean
  trigger_type: TriggerType
  interval_minutes?: number | null
  cron_expression?: string | null
  run_at?: string | null
  timezone: string
}

export interface ScheduleRun {
  id: string
  status: string
  task_id: string | null
  skipped_reason: string | null
  manual: boolean
  error: string | null
  duration_ms: number | null
  started_at: string | null
}
