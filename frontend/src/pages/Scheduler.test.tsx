/**
 * Scheduler page behaviour, mocked at the API boundary.
 *
 * What these pin is what could mislead the operator if it broke on the client:
 *  1. A stopped worker is called out, because schedules silently never fire without it.
 *  2. A finished one-time schedule is shown as Finished and cannot be resumed.
 *  3. "Run now" reports the task it created, so the operator can find it.
 *  4. The form saves only a schedule the backend accepted: an invalid trigger shows the
 *     backend's message and keeps Create disabled.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'
import type { ScheduledTask } from '@/types/scheduler'

const listScheduledTasks = vi.fn()
const workerStatus = vi.fn()
const runScheduledTaskNow = vi.fn()
const createScheduledTask = vi.fn()
const previewSchedule = vi.fn()
const telegramAccounts = vi.fn()

vi.mock('@/services/api', () => ({
  api: {
    listScheduledTasks: () => listScheduledTasks(),
    workerStatus: () => workerStatus(),
    runScheduledTaskNow: (...a: unknown[]) => runScheduledTaskNow(...a),
    createScheduledTask: (...a: unknown[]) => createScheduledTask(...a),
    previewSchedule: (...a: unknown[]) => previewSchedule(...a),
    telegramAccounts: () => telegramAccounts(),
    pauseScheduledTask: vi.fn(),
    resumeScheduledTask: vi.fn(),
    deleteScheduledTask: vi.fn(),
    updateScheduledTask: vi.fn(),
    scheduledTaskRuns: vi.fn().mockResolvedValue({ schedule_id: 'x', runs: [] }),
  },
}))

import Scheduler from '@/pages/Scheduler'

function schedule(overrides: Partial<ScheduledTask> = {}): ScheduledTask {
  return {
    id: 's1',
    name: 'Morning sweep',
    title: 'Check new airdrops',
    description: '',
    category: null,
    network: null,
    priority: 3,
    telegram_account: null,
    skip_if_running: true,
    trigger_type: 'CRON',
    interval_minutes: null,
    cron_expression: '0 9 * * *',
    run_at: null,
    timezone: 'Asia/Dhaka',
    is_active: true,
    next_run_at: new Date(Date.now() + 3600_000).toISOString(),
    last_run_at: null,
    total_runs: 0,
    failure_count: 0,
    last_task_id: null,
    last_run_status: null,
    last_run_error: null,
    created_at: new Date().toISOString(),
    ...overrides,
  }
}

function list(schedules: ScheduledTask[]) {
  return { count: schedules.length, limits: { min_interval_minutes: 5, max_schedules: 100 }, schedules }
}

function wrap(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  workerStatus.mockResolvedValue({ alive: true })
  telegramAccounts.mockResolvedValue({ accounts: [] })
  previewSchedule.mockResolvedValue({ next_runs: [new Date(Date.now() + 3600_000).toISOString()] })
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('Scheduler page', () => {
  it('shows an empty state with no schedules', async () => {
    listScheduledTasks.mockResolvedValue(list([]))
    wrap(<Scheduler />)
    expect(await screen.findByText('No schedules yet')).toBeTruthy()
  })

  it('warns when the worker is not running', async () => {
    listScheduledTasks.mockResolvedValue(list([schedule()]))
    workerStatus.mockResolvedValue({ alive: false })
    wrap(<Scheduler />)
    expect(await screen.findByText('The worker is not running')).toBeTruthy()
  })

  it('describes the trigger and shows a finished one-time schedule as not resumable', async () => {
    listScheduledTasks.mockResolvedValue(
      list([
        schedule({ trigger_type: 'INTERVAL', interval_minutes: 120, cron_expression: null, name: 'Every two' }),
        schedule({
          id: 's2',
          name: 'One shot',
          trigger_type: 'ONCE',
          run_at: '2026-10-19T09:00',
          cron_expression: null,
          is_active: false,
          next_run_at: null,
          total_runs: 1,
        }),
      ]),
    )
    wrap(<Scheduler />)
    expect(await screen.findByText('Every 2 hours')).toBeTruthy()
    expect(screen.getByText('Finished')).toBeTruthy()
    const resume = screen.getByRole('button', { name: /Resume/ }) as HTMLButtonElement
    expect(resume.disabled).toBe(true)
  })

  it('reports the task that Run now created', async () => {
    listScheduledTasks.mockResolvedValue(list([schedule()]))
    runScheduledTaskNow.mockResolvedValue({ task_id: 'AIR-2026-10-18-123456', status: 'READY', schedule: schedule() })
    wrap(<Scheduler />)
    fireEvent.click(await screen.findByRole('button', { name: /Run now/ }))
    expect(await screen.findByText('AIR-2026-10-18-123456')).toBeTruthy()
    expect(runScheduledTaskNow.mock.calls[0][0]).toBe('s1')
  })

  it('creates a schedule with the entered trigger and timezone', async () => {
    listScheduledTasks.mockResolvedValue(list([]))
    createScheduledTask.mockResolvedValue(schedule())
    wrap(<Scheduler />)
    await screen.findByText('No schedules yet')
    fireEvent.click(screen.getByRole('button', { name: /New/ }))

    fireEvent.change(screen.getByPlaceholderText('e.g. Morning airdrop sweep'), { target: { value: 'Daily' } })
    fireEvent.change(screen.getByPlaceholderText('What should the agent do?'), { target: { value: 'Do the thing' } })
    fireEvent.change(screen.getByDisplayValue('0 9 * * *'), { target: { value: '30 8 * * 1-5' } })

    const create = screen.getByRole('button', { name: /Create schedule/ }) as HTMLButtonElement
    await waitFor(() => expect(create.disabled).toBe(false))
    fireEvent.click(create)

    await waitFor(() => expect(createScheduledTask).toHaveBeenCalled())
    const body = createScheduledTask.mock.calls[0][0]
    expect(body).toMatchObject({
      name: 'Daily',
      title: 'Do the thing',
      trigger_type: 'CRON',
      cron_expression: '30 8 * * 1-5',
      interval_minutes: null,
      run_at: null,
    })
    expect(typeof body.timezone).toBe('string')
  })

  it('shows the backend message for an invalid trigger and blocks saving', async () => {
    listScheduledTasks.mockResolvedValue(list([]))
    previewSchedule.mockRejectedValue(new Error('That expression can fire less than 5 minutes apart'))
    wrap(<Scheduler />)
    await screen.findByText('No schedules yet')
    fireEvent.click(screen.getByRole('button', { name: /New/ }))

    fireEvent.change(screen.getByPlaceholderText('e.g. Morning airdrop sweep'), { target: { value: 'Spam' } })
    fireEvent.change(screen.getByPlaceholderText('What should the agent do?'), { target: { value: 'x' } })
    fireEvent.change(screen.getByDisplayValue('0 9 * * *'), { target: { value: '* * * * *' } })

    expect(await screen.findByText(/less than 5 minutes apart/)).toBeTruthy()
    const create = screen.getByRole('button', { name: /Create schedule/ }) as HTMLButtonElement
    expect(create.disabled).toBe(true)
  })
})
