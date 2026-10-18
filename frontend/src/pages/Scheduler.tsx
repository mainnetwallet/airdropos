/**
 * Scheduler: run a task automatically every N minutes, on a cron expression, or once.
 *
 * Each schedule is a template. Every time it fires the backend creates a normal task and
 * queues it, so the result appears in Task Manager like any other task. Schedules only fire
 * while the worker is running, which the page says out loud when it is not.
 */

import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CalendarClock, History, Loader2, Pause, Pencil, Play, Plus, Trash2, Zap } from 'lucide-react'
import clsx from 'clsx'
import { api } from '@/services/api'
import { Alert, Card, EmptyState, ErrorState, IconTile, Loading, PageHeader, Pill } from '@/components/ui'
import ConfirmModal from '@/components/ConfirmModal'
import Modal from '@/components/live/Modal'
import type { ScheduledTask, ScheduledTaskInput, TriggerType } from '@/types/scheduler'

// ------------------------------------------------------------------ helpers
const CRON_PRESETS: Array<{ label: string; value: string }> = [
  { label: 'Every hour', value: '0 * * * *' },
  { label: 'Every 6 hours', value: '0 */6 * * *' },
  { label: 'Daily 09:00', value: '0 9 * * *' },
  { label: 'Weekdays 09:00', value: '0 9 * * 1-5' },
  { label: 'Mondays 09:00', value: '0 9 * * 1' },
]

function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

function knownTimezones(): string[] {
  try {
    const fn = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf
    const list = fn?.('timeZone')
    if (list && list.length) return ['UTC', ...list.filter((z) => z !== 'UTC')]
  } catch {
    // fall through
  }
  return ['UTC', browserTimezone()]
}

function formatWhen(iso: string | null | undefined, timeZone?: string): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  const options: Intl.DateTimeFormatOptions = { dateStyle: 'medium', timeStyle: 'short' }
  try {
    return new Intl.DateTimeFormat(undefined, { ...options, timeZone }).format(date)
  } catch {
    return new Intl.DateTimeFormat(undefined, options).format(date)
  }
}

function relative(iso: string | null | undefined): string {
  if (!iso) return ''
  const diff = new Date(iso).getTime() - Date.now()
  if (Number.isNaN(diff)) return ''
  const abs = Math.abs(diff)
  const minutes = Math.round(abs / 60000)
  let text: string
  if (minutes < 1) text = 'a moment'
  else if (minutes < 60) text = `${minutes} min`
  else if (minutes < 60 * 48) text = `${Math.round(minutes / 60)} h`
  else text = `${Math.round(minutes / 1440)} d`
  return diff >= 0 ? `in ${text}` : `${text} ago`
}

function everyText(minutes: number): string {
  if (minutes % 1440 === 0) return minutes === 1440 ? 'Every day' : `Every ${minutes / 1440} days`
  if (minutes % 60 === 0) return minutes === 60 ? 'Every hour' : `Every ${minutes / 60} hours`
  return `Every ${minutes} minutes`
}

function describeTrigger(s: ScheduledTask): string {
  if (s.trigger_type === 'INTERVAL') return everyText(s.interval_minutes ?? 0)
  if (s.trigger_type === 'CRON') return `Cron ${s.cron_expression} (${s.timezone})`
  return `Once, ${s.run_at ? s.run_at.replace('T', ' ').slice(0, 16) : ''} (${s.timezone})`
}

function runTone(status: string | null): 'green' | 'amber' | 'red' | 'blue' | 'gray' {
  switch (status) {
    case 'SUCCESS':
      return 'green'
    case 'SKIPPED':
      return 'amber'
    case 'FAILED':
      return 'red'
    case 'RUNNING':
      return 'blue'
    default:
      return 'gray'
  }
}

function useDebounced<T>(value: T, ms = 400): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return debounced
}

// ------------------------------------------------------------------ form
type Unit = 'minutes' | 'hours' | 'days'
const UNIT_MINUTES: Record<Unit, number> = { minutes: 1, hours: 60, days: 1440 }

function splitInterval(minutes: number | null): { value: string; unit: Unit } {
  if (!minutes) return { value: '1', unit: 'hours' }
  if (minutes % 1440 === 0) return { value: String(minutes / 1440), unit: 'days' }
  if (minutes % 60 === 0) return { value: String(minutes / 60), unit: 'hours' }
  return { value: String(minutes), unit: 'minutes' }
}

function ScheduleForm({
  existing,
  minInterval,
  onClose,
  onSaved,
}: {
  existing: ScheduledTask | null
  minInterval: number
  onClose: () => void
  onSaved: (message: string) => void
}) {
  const queryClient = useQueryClient()
  const initialInterval = splitInterval(existing?.interval_minutes ?? null)
  const [name, setName] = useState(existing?.name ?? '')
  const [title, setTitle] = useState(existing?.title ?? '')
  const [description, setDescription] = useState(existing?.description ?? '')
  const [priority, setPriority] = useState(existing?.priority ?? 5)
  const [network, setNetwork] = useState(existing?.network ?? '')
  const [account, setAccount] = useState(existing?.telegram_account ?? '')
  const [skipIfRunning, setSkipIfRunning] = useState(existing?.skip_if_running ?? true)
  const [trigger, setTrigger] = useState<TriggerType>(existing?.trigger_type ?? 'CRON')
  const [intervalValue, setIntervalValue] = useState(initialInterval.value)
  const [intervalUnit, setIntervalUnit] = useState<Unit>(initialInterval.unit)
  const [cronExpr, setCronExpr] = useState(existing?.cron_expression ?? '0 9 * * *')
  const [runAt, setRunAt] = useState((existing?.run_at ?? '').slice(0, 16))
  const [timezone, setTimezone] = useState(existing?.timezone ?? browserTimezone())

  const accounts = useQuery({ queryKey: ['telegram-accounts'], queryFn: api.telegramAccounts })
  const zones = useMemo(knownTimezones, [])

  const intervalMinutes = Math.round(Number(intervalValue) * UNIT_MINUTES[intervalUnit])
  const trig = {
    trigger_type: trigger,
    interval_minutes: trigger === 'INTERVAL' ? intervalMinutes : null,
    cron_expression: trigger === 'CRON' ? cronExpr.trim() : null,
    run_at: trigger === 'ONCE' ? runAt : null,
    timezone: timezone.trim() || 'UTC',
  }

  // The backend is the one source of truth for what a trigger means, so the preview asks it
  // rather than re-implementing cron in the browser.
  const triggerReady =
    (trigger === 'INTERVAL' && intervalMinutes > 0) ||
    (trigger === 'CRON' && cronExpr.trim() !== '') ||
    (trigger === 'ONCE' && runAt !== '')
  const debouncedTrig = useDebounced(JSON.stringify(trig))
  const preview = useQuery({
    queryKey: ['schedule-preview', debouncedTrig],
    queryFn: () => api.previewSchedule({ ...JSON.parse(debouncedTrig), count: 5 }),
    enabled: triggerReady,
    retry: false,
    staleTime: 0,
  })

  const save = useMutation({
    mutationFn: () => {
      const body: ScheduledTaskInput = {
        name: name.trim(),
        title: title.trim(),
        description,
        priority,
        network: network.trim() || null,
        telegram_account: account || null,
        skip_if_running: skipIfRunning,
        ...trig,
      }
      return existing ? api.updateScheduledTask(existing.id, body) : api.createScheduledTask(body)
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['scheduled-tasks'] })
      onSaved(existing ? 'Schedule updated' : 'Schedule created')
    },
  })

  const canSave = name.trim() !== '' && title.trim() !== '' && triggerReady && !preview.isError && !save.isPending
  const previewError = preview.isError ? (preview.error as Error).message : null
  const toggle = (id: TriggerType, label: string) => (
    <button
      key={id}
      type="button"
      onClick={() => setTrigger(id)}
      aria-pressed={trigger === id}
      className={clsx(
        'min-h-[40px] flex-1 rounded-xl px-2 text-sm font-semibold transition-colors',
        trigger === id ? 'bg-accent text-white shadow-sm' : 'text-slate-400 hover:text-slate-200',
      )}
    >
      {label}
    </button>
  )

  return (
    <Modal title={existing ? 'Edit schedule' : 'New schedule'} onClose={onClose}>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault()
          if (canSave) save.mutate()
        }}
      >
        <label className="block text-xs font-medium text-slate-400">
          Schedule name
          <input
            className="input mt-1 w-full"
            placeholder="e.g. Morning airdrop sweep"
            maxLength={120}
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoFocus
          />
        </label>

        <div className="space-y-3 rounded-xl border border-surface-300 p-3">
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Task to create</p>
          <label className="block text-xs font-medium text-slate-400">
            Task title
            <input
              className="input mt-1 w-full"
              placeholder="What should the agent do?"
              maxLength={500}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </label>
          <label className="block text-xs font-medium text-slate-400">
            Details (optional)
            <textarea
              className="input mt-1 w-full"
              rows={3}
              maxLength={6000}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          <div className="grid grid-cols-2 gap-3">
            <label className="block text-xs font-medium text-slate-400">
              Priority
              <select className="input mt-1 w-full" value={priority} onChange={(e) => setPriority(Number(e.target.value))}>
                {Array.from({ length: 10 }, (_, i) => i + 1).map((p) => (
                  <option key={p} value={p}>
                    {p}
                    {p === 1 ? ' (highest)' : p === 10 ? ' (lowest)' : ''}
                  </option>
                ))}
              </select>
            </label>
            <label className="block text-xs font-medium text-slate-400">
              Network (optional)
              <input className="input mt-1 w-full" maxLength={100} value={network} onChange={(e) => setNetwork(e.target.value)} />
            </label>
          </div>
          {(accounts.data?.accounts.length ?? 0) > 0 && (
            <label className="block text-xs font-medium text-slate-400">
              Telegram account (optional)
              <select className="input mt-1 w-full" value={account} onChange={(e) => setAccount(e.target.value)}>
                <option value="">Default</option>
                {accounts.data?.accounts.map((a) => (
                  <option key={a.label} value={a.label}>
                    {a.label}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>

        <div className="space-y-3 rounded-xl border border-surface-300 p-3">
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">When</p>
          <div role="group" aria-label="Schedule type" className="flex gap-1.5 rounded-2xl bg-surface-200 p-1">
            {toggle('INTERVAL', 'Every…')}
            {toggle('CRON', 'Cron')}
            {toggle('ONCE', 'Once')}
          </div>

          {trigger === 'INTERVAL' && (
            <div className="flex gap-2">
              <label className="block flex-1 text-xs font-medium text-slate-400">
                Every
                <input
                  className="input mt-1 w-full"
                  type="number"
                  min={1}
                  value={intervalValue}
                  onChange={(e) => setIntervalValue(e.target.value)}
                />
              </label>
              <label className="block flex-1 text-xs font-medium text-slate-400">
                Unit
                <select className="input mt-1 w-full" value={intervalUnit} onChange={(e) => setIntervalUnit(e.target.value as Unit)}>
                  <option value="minutes">minutes</option>
                  <option value="hours">hours</option>
                  <option value="days">days</option>
                </select>
              </label>
            </div>
          )}

          {trigger === 'CRON' && (
            <div className="space-y-2">
              <label className="block text-xs font-medium text-slate-400">
                Cron expression (minute hour day month weekday)
                <input
                  className="input mt-1 w-full font-mono"
                  value={cronExpr}
                  maxLength={100}
                  spellCheck={false}
                  onChange={(e) => setCronExpr(e.target.value)}
                />
              </label>
              <div className="flex flex-wrap gap-1.5">
                {CRON_PRESETS.map((p) => (
                  <button
                    key={p.value}
                    type="button"
                    onClick={() => setCronExpr(p.value)}
                    className={clsx(
                      'rounded-full border px-3 py-1 text-xs',
                      cronExpr.trim() === p.value ? 'border-accent text-accent' : 'border-surface-300 text-slate-400',
                    )}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            </div>
          )}

          {trigger === 'ONCE' && (
            <label className="block text-xs font-medium text-slate-400">
              Run at
              <input className="input mt-1 w-full" type="datetime-local" value={runAt} onChange={(e) => setRunAt(e.target.value)} />
            </label>
          )}

          <label className="block text-xs font-medium text-slate-400">
            Timezone
            <input
              className="input mt-1 w-full"
              list="scheduler-timezones"
              value={timezone}
              onChange={(e) => setTimezone(e.target.value)}
            />
            <datalist id="scheduler-timezones">
              {zones.map((z) => (
                <option key={z} value={z} />
              ))}
            </datalist>
          </label>

          <div className="rounded-lg bg-surface-200 p-3 text-xs">
            {previewError ? (
              <p className="text-status-error">{previewError}</p>
            ) : preview.data ? (
              <>
                <p className="font-semibold text-slate-300">Next runs</p>
                <ul className="mt-1 space-y-0.5 text-slate-400">
                  {preview.data.next_runs.map((r) => (
                    <li key={r}>{formatWhen(r, timezone.trim() || 'UTC')}</li>
                  ))}
                </ul>
              </>
            ) : (
              <p className="text-slate-500">{triggerReady ? 'Checking…' : 'Fill in the schedule to see the next runs.'}</p>
            )}
          </div>
          <p className="text-xs text-slate-500">Shortest allowed gap between runs: {minInterval} minutes.</p>
        </div>

        <label className="flex items-start gap-2 text-xs text-slate-300">
          <input type="checkbox" className="mt-0.5" checked={skipIfRunning} onChange={(e) => setSkipIfRunning(e.target.checked)} />
          <span>
            Skip a run if the previous task from this schedule has not finished yet
            <span className="block text-slate-500">Prevents tasks piling up when one runs longer than the interval.</span>
          </span>
        </label>

        {save.isError && <p className="text-sm text-status-error">{(save.error as Error).message}</p>}

        <div className="flex gap-2">
          <button type="button" className="btn btn-ghost flex-1 justify-center" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary flex-1 justify-center" disabled={!canSave}>
            {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            {existing ? 'Save changes' : 'Create schedule'}
          </button>
        </div>
      </form>
    </Modal>
  )
}

// ------------------------------------------------------------------ history
function RunHistory({ id, timezone }: { id: string; timezone: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['scheduled-runs', id],
    queryFn: () => api.scheduledTaskRuns(id, 15),
    refetchInterval: 15000,
  })
  if (isLoading) return <p className="text-xs text-slate-500">Loading history…</p>
  if (error) return <p className="text-xs text-status-error">{(error as Error).message}</p>
  const runs = data?.runs ?? []
  if (runs.length === 0) return <p className="text-xs text-slate-500">It has not run yet.</p>
  return (
    <ul className="divide-y divide-surface-300 text-xs">
      {runs.map((r) => (
        <li key={r.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2">
          <Pill tone={runTone(r.status)}>{r.status}</Pill>
          <span className="text-slate-300">{formatWhen(r.started_at, timezone)}</span>
          {r.manual && <span className="text-slate-500">manual</span>}
          {r.task_id && (
            <Link to="/tasks" className="font-mono text-accent">
              {r.task_id}
            </Link>
          )}
          {r.skipped_reason && <span className="text-slate-500">{r.skipped_reason}</span>}
          {r.error && <span className="text-status-error">{r.error}</span>}
        </li>
      ))}
    </ul>
  )
}

// ------------------------------------------------------------------ page
export default function Scheduler() {
  const queryClient = useQueryClient()
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['scheduled-tasks'],
    queryFn: api.listScheduledTasks,
    refetchInterval: 15000,
  })
  const worker = useQuery({ queryKey: ['worker-status'], queryFn: api.workerStatus, refetchInterval: 15000 })

  const [editing, setEditing] = useState<ScheduledTask | 'new' | null>(null)
  const [deleting, setDeleting] = useState<ScheduledTask | null>(null)
  const [openHistory, setOpenHistory] = useState<string | null>(null)
  const [notice, setNotice] = useState<{ kind: 'success' | 'error'; text: string; taskId?: string } | null>(null)

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['scheduled-tasks'] })
  const fail = (e: unknown) => setNotice({ kind: 'error', text: (e as Error).message })

  const pause = useMutation({ mutationFn: api.pauseScheduledTask, onSuccess: refresh, onError: fail })
  const resume = useMutation({ mutationFn: api.resumeScheduledTask, onSuccess: refresh, onError: fail })
  const runNow = useMutation({
    mutationFn: api.runScheduledTaskNow,
    onSuccess: (r) => {
      setNotice({ kind: 'success', text: 'Task created and queued', taskId: r.task_id })
      void refresh()
      void queryClient.invalidateQueries({ queryKey: ['scheduled-runs'] })
    },
    onError: fail,
  })
  const remove = useMutation({
    mutationFn: api.deleteScheduledTask,
    onSuccess: () => {
      setDeleting(null)
      void refresh()
    },
    onError: (e) => {
      setDeleting(null)
      fail(e)
    },
  })

  if (isLoading) return <Loading label="Loading schedules" />
  if (error || !data) return <ErrorState error={error} onRetry={() => refetch()} />

  const schedules = data.schedules
  const active = schedules.filter((s) => s.is_active).length
  const workerDown = worker.data ? !worker.data.alive : false

  return (
    <div className="space-y-4 pt-1 lg:space-y-6 lg:pt-0">
      <PageHeader
        title="Scheduler"
        subtitle={
          schedules.length
            ? `${active} active of ${schedules.length} ${schedules.length === 1 ? 'schedule' : 'schedules'}`
            : 'Run tasks automatically on a schedule'
        }
        action={
          <button type="button" className="btn btn-primary" onClick={() => setEditing('new')}>
            <Plus className="h-4 w-4" />
            <span className="hidden sm:inline">New schedule</span>
            <span className="sm:hidden">New</span>
          </button>
        }
      />

      {workerDown && (
        <Alert kind="warning" title="The worker is not running">
          Schedules only fire while the worker is running. Start it with <code>python run.py</code>. A schedule that came due while the
          worker was down runs once when it starts, then continues from its next slot.
        </Alert>
      )}

      {notice && (
        <Alert kind={notice.kind} title={notice.kind === 'success' ? notice.text : 'Something went wrong'}>
          {notice.kind === 'error' && notice.text}
          {notice.kind === 'success' && notice.taskId && (
            <>
              <span className="font-mono">{notice.taskId}</span> is waiting for the worker.{' '}
              <Link to="/tasks" className="font-semibold underline">
                Open Task Manager
              </Link>
            </>
          )}
          <button type="button" className="ml-3 text-xs underline" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </Alert>
      )}

      {schedules.length === 0 ? (
        <EmptyState
          title="No schedules yet"
          description="Create one to have a task started automatically, for example every morning at 09:00."
          icon={<CalendarClock className="h-8 w-8" />}
        />
      ) : (
        <div className="grid gap-3 lg:grid-cols-2 lg:gap-4">
          {schedules.map((s) => {
            const busy =
              (pause.isPending && pause.variables === s.id) ||
              (resume.isPending && resume.variables === s.id) ||
              (runNow.isPending && runNow.variables === s.id)
            const finished = !s.is_active && s.trigger_type === 'ONCE' && s.total_runs > 0
            return (
              <Card key={s.id}>
                <div className="flex items-start gap-3">
                  <IconTile tone={s.is_active ? 'orange' : 'slate'}>
                    <CalendarClock />
                  </IconTile>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-start justify-between gap-2">
                      <h3 className="min-w-0 truncate font-medium text-slate-100">{s.name}</h3>
                      <Pill tone={s.is_active ? 'green' : finished ? 'blue' : 'gray'}>
                        {s.is_active ? 'Active' : finished ? 'Finished' : 'Paused'}
                      </Pill>
                    </div>
                    <p className="mt-0.5 truncate text-sm text-slate-400">{s.title}</p>
                  </div>
                </div>

                <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
                  <div className="col-span-2">
                    <dt className="text-slate-500">Schedule</dt>
                    <dd className="text-slate-300">{describeTrigger(s)}</dd>
                  </div>
                  <div>
                    <dt className="text-slate-500">Next run</dt>
                    <dd className="text-slate-300">
                      {s.next_run_at ? (
                        <>
                          {formatWhen(s.next_run_at, s.timezone)}
                          <span className="block text-slate-500">{relative(s.next_run_at)}</span>
                        </>
                      ) : (
                        '—'
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-slate-500">Last run</dt>
                    <dd className="text-slate-300">
                      {s.last_run_at ? (
                        <>
                          {formatWhen(s.last_run_at, s.timezone)}
                          {s.last_run_status && (
                            <span className="ml-1.5 align-middle">
                              <Pill tone={runTone(s.last_run_status)}>{s.last_run_status}</Pill>
                            </span>
                          )}
                        </>
                      ) : (
                        'Never'
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-slate-500">Runs</dt>
                    <dd className="text-slate-300">
                      {s.total_runs}
                      {s.failure_count > 0 && <span className="text-status-error"> · {s.failure_count} failed</span>}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-slate-500">Priority</dt>
                    <dd className="text-slate-300">{s.priority}</dd>
                  </div>
                </dl>

                {s.last_run_error && <p className="mt-2 text-xs text-status-error">{s.last_run_error}</p>}

                <div className="mt-4 flex flex-wrap gap-2 border-t border-surface-300 pt-3">
                  <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => runNow.mutate(s.id)}>
                    {runNow.isPending && runNow.variables === s.id ? <Loader2 className="h-4 w-4 animate-spin" /> : <Zap className="h-4 w-4" />}
                    Run now
                  </button>
                  {s.is_active ? (
                    <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => pause.mutate(s.id)}>
                      <Pause className="h-4 w-4" /> Pause
                    </button>
                  ) : (
                    <button type="button" className="btn btn-ghost" disabled={busy || finished} onClick={() => resume.mutate(s.id)}>
                      <Play className="h-4 w-4" /> Resume
                    </button>
                  )}
                  <button type="button" className="btn btn-ghost" onClick={() => setEditing(s)}>
                    <Pencil className="h-4 w-4" /> Edit
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost"
                    aria-expanded={openHistory === s.id}
                    onClick={() => setOpenHistory(openHistory === s.id ? null : s.id)}
                  >
                    <History className="h-4 w-4" /> History
                  </button>
                  <button type="button" className="btn btn-ghost ml-auto text-status-error" aria-label={`Delete ${s.name}`} onClick={() => setDeleting(s)}>
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>

                {openHistory === s.id && (
                  <div className="mt-3 rounded-lg bg-surface-200 p-3">
                    <RunHistory id={s.id} timezone={s.timezone} />
                  </div>
                )}
              </Card>
            )
          })}
        </div>
      )}

      {editing && (
        <ScheduleForm
          key={editing === 'new' ? 'new' : editing.id}
          existing={editing === 'new' ? null : editing}
          minInterval={data.limits.min_interval_minutes}
          onClose={() => setEditing(null)}
          onSaved={(text) => {
            setEditing(null)
            setNotice({ kind: 'success', text })
          }}
        />
      )}

      {deleting && (
        <ConfirmModal
          title="Delete schedule"
          confirmLabel={remove.isPending ? 'Deleting…' : 'Delete'}
          onClose={() => setDeleting(null)}
          onConfirm={() => remove.mutate(deleting.id)}
        >
          Delete <strong>{deleting.name}</strong> and its run history? Tasks it already created are kept.
        </ConfirmModal>
      )}
    </div>
  )
}
