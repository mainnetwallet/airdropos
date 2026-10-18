/**
 * Discord — browser-only integration on the operator's own accounts.
 *
 * Five sections, each answering a question the operator actually has:
 *
 *   Account   which browser profile is acting as which Discord account, and is it
 *             signed in? (asked on demand, never polled)
 *   Compose   write the task, pick the profile, choose read / draft / write
 *   Tasks     what is running, how far, and what to pause or cancel
 *   Activity  what was read, sent and replied to, with evidence
 *   Recovery  the Discord login requests waiting in the Agent Inbox
 *
 * Two rules this page holds to, because they are what make the integration safe to
 * point at a real account:
 *
 *   1. A login state is only ever shown as what it is. `UNKNOWN` is rendered as
 *      "not verified", never as "logged out", and a missing browser never reads as a
 *      signed-out account.
 *   2. A task's profile is fixed at creation. The selector is disabled once a task
 *      exists, so nothing on this screen can move a task onto another account.
 *
 * Nothing rendered here comes from browser session storage: the API has no such
 * values to return.
 */

import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  CheckCircle2,
  ExternalLink,
  Loader2,
  LogIn,
  MessageSquare,
  RefreshCw,
  Send,
  ShieldCheck,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import clsx from 'clsx'
import {
  Alert,
  Card,
  Chips,
  EmptyState,
  ErrorState,
  Loading,
  PageHeader,
  Pill,
  ProgressBar,
  SegmentedTabs,
  Stat,
} from '@/components/ui'
import { api } from '@/services/api'
import type {
  DiscordActivityRecord,
  DiscordIntent,
  DiscordLoginReading,
  DiscordProfile,
  DiscordTask,
} from '@/types/discord'

type Tab = 'account' | 'compose' | 'tasks' | 'activity' | 'recovery'

const TABS: Array<{ id: Tab; label: string }> = [
  { id: 'account', label: 'Account' },
  { id: 'compose', label: 'New Task' },
  { id: 'tasks', label: 'Tasks' },
  { id: 'activity', label: 'Activity' },
  { id: 'recovery', label: 'Login Recovery' },
]

const INTENTS: Array<{ id: DiscordIntent | ''; label: string; hint: string }> = [
  { id: '', label: 'Auto-detect', hint: 'Decide from your wording. Reads never send.' },
  { id: 'read', label: 'Read only', hint: 'Reads and summarises. Cannot send anything.' },
  { id: 'draft', label: 'Draft only', hint: 'Reads, then writes proposed replies for you to send.' },
  { id: 'write', label: 'Write enabled', hint: 'May post. Every send is verified against the page.' },
]

// ---------------------------------------------------------------- helpers
function when(iso: string | null | undefined): string {
  if (!iso) return 'never'
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? 'unknown' : date.toLocaleString()
}

function errorText(error: unknown): string {
  if (error instanceof Error) return error.message
  if (typeof error === 'string') return error
  try {
    return JSON.stringify(error)
  } catch {
    return 'Unknown error'
  }
}

function MutationError({ error }: { error: unknown }) {
  if (!error) return null
  return (
    <Alert kind="error" title="That did not work">
      {errorText(error)}
    </Alert>
  )
}

/**
 * How a login reading is presented.
 *
 * Five distinct cases, never merged. In particular `UNKNOWN` is NOT rendered as
 * "logged out": telling the operator to sign in to an account that is already signed
 * in is a worse error than saying nothing was determined.
 */
function loginPresentation(reading: DiscordLoginReading | undefined) {
  if (!reading) {
    return { tone: 'gray' as const, label: 'Not checked', detail: 'Not checked yet.', action: false }
  }
  switch (reading.state) {
    case 'LOGGED_IN':
      return {
        tone: 'green' as const,
        label: 'Signed in',
        detail: `Discord shows a signed-in session (${reading.indicator}).`,
        action: false,
      }
    case 'LOGGED_OUT':
      return {
        tone: 'amber' as const,
        label: 'Login required',
        detail: reading.detail || `Discord is showing its sign-in form (${reading.indicator}).`,
        action: true,
      }
    case 'NO_BROWSER':
      return {
        tone: 'red' as const,
        label: 'Browser not running',
        detail: 'No browser is open for this profile. Open it to use Discord.',
        action: true,
      }
    default:
      return {
        tone: 'gray' as const,
        label: 'Not verified',
        detail:
          reading.detail ||
          'The Discord page could not be read, so the session state is unknown. This is not proof of being signed out.',
        action: false,
      }
  }
}

function profileName(profiles: DiscordProfile[], id: string): string {
  return profiles.find((p) => p.id === id)?.name ?? (id || 'unknown')
}

// ================================================================= page
export default function Discord() {
  const [tab, setTab] = useState<Tab>('account')
  const [selectedProfile, setSelectedProfile] = useState('')
  const [checkedProfile, setCheckedProfile] = useState('')
  const queryClient = useQueryClient()

  const refreshAll = () => queryClient.invalidateQueries({ queryKey: ['discord'] })

  const status = useQuery({
    queryKey: ['discord', 'status'],
    queryFn: api.discordStatus,
    refetchInterval: 30_000,
  })

  const tasks = useQuery({
    queryKey: ['discord', 'tasks'],
    queryFn: () => api.discordTasks({ limit: 25 }),
    refetchInterval: 15_000,
  })

  const activity = useQuery({
    queryKey: ['discord', 'activity'],
    queryFn: () => api.discordActivity({ limit: 100 }),
    refetchInterval: 30_000,
  })

  const profiles = status.data?.profiles ?? []
  const effectiveProfile = selectedProfile || status.data?.selected_profile_id || ''

  if (status.isLoading) return <Loading label="Loading Discord integration" />
  if (status.error) {
    return <ErrorState error={status.error} onRetry={() => status.refetch()} />
  }
  if (!status.data) return <ErrorState error="No Discord status." onRetry={refreshAll} />

  return (
    <div className="space-y-4 pt-1 lg:space-y-6 lg:pt-0">
      <PageHeader
        title={
          <>
            <span className="lg:hidden">Discord</span>
            <span className="hidden lg:inline">Discord Integration</span>
          </>
        }
        subtitle={
          <>
            Drive Discord through your own signed-in browser profiles. There is no bot token and no
            stored credential — you log in by hand and AirdropOS reuses that session.
          </>
        }
        action={
          <button
            type="button"
            onClick={refreshAll}
            className="btn-ghost"
            aria-label="Refresh"
          >
            <RefreshCw className="h-4 w-4" />
          </button>
        }
      />

      <Alert kind="info" title="Browser only, no Discord bot">
        {status.data.note}
      </Alert>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="Profiles" value={status.data.profile_count} />
        <Stat
          label="Can open"
          value={status.data.openable_profile_count}
          hint="profiles with no configuration problems"
        />
        <Stat label="Tasks" value={tasks.data?.count ?? 0} />
        <Stat
          label="Unconfirmed sends"
          value={status.data.counts.recent_uncertain}
          tone={status.data.counts.recent_uncertain > 0 ? 'warning' : 'neutral'}
          hint="never repeated automatically"
        />
      </div>

      <SegmentedTabs value={tab} onChange={setTab} items={TABS} />

      {tab === 'account' && (
        <AccountTab
          profiles={profiles}
          selected={effectiveProfile}
          onSelect={setSelectedProfile}
          checked={checkedProfile}
          onChecked={setCheckedProfile}
          defaultAgentProfile={status.data.selected_profile_id}
        />
      )}
      {tab === 'compose' && (
        <ComposeTab
          profiles={profiles}
          selected={effectiveProfile}
          onSelect={setSelectedProfile}
          draftBehaviour={status.data.catalog.draft_behaviour}
          onCreated={() => {
            void refreshAll()
            setTab('tasks')
          }}
        />
      )}
      {tab === 'tasks' && (
        <TasksTab
          tasks={tasks.data?.tasks ?? []}
          profiles={profiles}
          loading={tasks.isLoading}
          onChanged={refreshAll}
        />
      )}
      {tab === 'activity' && <ActivityTab activity={activity.data?.activity ?? []} loading={activity.isLoading} />}
      {tab === 'recovery' && <RecoveryTab tasks={tasks.data?.tasks ?? []} profiles={profiles} onChanged={refreshAll} />}
    </div>
  )
}

// ================================================================= account
function AccountTab({
  profiles,
  selected,
  onSelect,
  checked,
  onChecked,
  defaultAgentProfile,
}: {
  profiles: DiscordProfile[]
  selected: string
  onSelect: (id: string) => void
  checked: string
  onChecked: (id: string) => void
  defaultAgentProfile: string
}) {
  const open = useMutation({
    mutationFn: (id: string) => api.openDiscordProfile(id),
    onSuccess: () => {
      onChecked(selected)
      void api.discordLoginState(selected).catch(() => undefined)
    },
  })

  // Only asked for when the operator asks. Never polled: a healthy session must
  // produce no background traffic and no interruptions.
  const login = useQuery({
    queryKey: ['discord', 'login', checked],
    queryFn: () => api.discordLoginState(checked),
    enabled: Boolean(checked),
    retry: false,
  })

  const chosen = profiles.find((p) => p.id === selected)
  const reading = login.data?.login_state
  const shown = login.error ? undefined : reading

  return (
    <div className="space-y-3">
      <Card title="Browser Account">
        <div className="space-y-3">
          <label className="block text-xs font-medium text-slate-400">
            Browser profile to use as the Discord account
            <select
              className="input mt-1 w-full"
              value={selected}
              onChange={(event) => {
                onSelect(event.target.value)
                onChecked('')
              }}
            >
              <option value="">Select a browser profile…</option>
              {profiles.map((profile) => (
                <option key={profile.id} value={profile.id}>
                  {profile.name}
                  {profile.id === defaultAgentProfile ? ' (agent default)' : ''}
                </option>
              ))}
            </select>
          </label>

          {!selected && (
            <Alert kind="warning" title="Select a profile">
              Every Discord task must name the browser profile of the account it should act as.
              AirdropOS will not pick one for you, because that would act as the wrong account.
            </Alert>
          )}

          {chosen && (
            <dl className="grid gap-2 text-xs sm:grid-cols-2">
              <Row label="Profile ID" value={chosen.id} mono />
              <Row label="Profile name" value={chosen.name} />
              <Row label="Chrome mode" value={`${chosen.launch_mode} / ${chosen.browser_type}`} />
              <Row label="Headless" value={chosen.headless ? 'yes (nothing on your screen)' : 'no (a real window)'} />
            </dl>
          )}

          {chosen && !chosen.can_open && (
            <Alert kind="error" title="This profile cannot be opened">
              <ul className="list-disc pl-4">
                {chosen.open_problems.map((problem) => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
              <Link className="btn-ghost mt-2" to="/browser-profiles">
                Manage browser profiles
              </Link>
            </Alert>
          )}

          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="btn btn-primary"
              disabled={!selected || open.isPending || (chosen ? !chosen.can_open : true)}
              onClick={() => open.mutate(selected)}
            >
              {open.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <LogIn className="h-4 w-4" />}
              Open Discord in this profile
            </button>
            <button
              type="button"
              className="btn"
              disabled={!selected || login.isFetching}
              onClick={() => onChecked(selected)}
            >
              {login.isFetching ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <ShieldCheck className="h-4 w-4" />
              )}
              Check login status
            </button>
            <Link className="btn-ghost" to="/browser-profiles">
              Manage profiles
              <ExternalLink className="h-4 w-4" />
            </Link>
          </div>

          <MutationError error={open.error} />

          {open.data && (
            <Alert kind="success" title={open.data.reused ? 'Browser already running' : 'Discord opened'}>
              {open.data.note}
            </Alert>
          )}
        </div>
      </Card>

      <Card title="Session status">
        {!checked ? (
          <EmptyState
            title="Not checked yet"
            description="Open the profile and press Check login status. AirdropOS never polls Discord, so a healthy session stays silent."
            icon={<ShieldCheck className="h-8 w-8" />}
          />
        ) : login.isLoading ? (
          <Loading label="Reading the Discord page" />
        ) : login.error ? (
          <Alert kind="warning" title="Could not check">
            {errorText(login.error)}. The session state is unknown, which is not the same as being
            signed out.
          </Alert>
        ) : (
          <LoginCard reading={shown} url={login.data?.url} />
        )}
      </Card>
    </div>
  )
}

function LoginCard({
  reading,
  url,
}: {
  reading: DiscordLoginReading | undefined
  url?: string
}) {
  const shown = loginPresentation(reading)
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <Pill tone={shown.tone}>{shown.label}</Pill>
        {reading?.observed_at && (
          <span className="text-[11px] text-slate-500">observed {when(reading.observed_at)}</span>
        )}
      </div>
      <p className="text-sm text-slate-300">{shown.detail}</p>
      {url && (
        <p className="truncate text-[11px] text-slate-500" title={url}>
          {url}
        </p>
      )}
      {shown.action && (
        <Alert kind="warning" title="Action needed">
          Sign in to Discord yourself in this profile. AirdropOS will not type your password, pass
          MFA, or read your session. Then press{' '}
          <strong>Check login status</strong> again.
        </Alert>
      )}
    </div>
  )
}

// ================================================================= compose
function ComposeTab({
  profiles,
  selected,
  onSelect,
  onCreated,
  draftBehaviour,
}: {
  profiles: DiscordProfile[]
  selected: string
  onSelect: (id: string) => void
  onCreated: () => void
  draftBehaviour: string
}) {
  const [description, setDescription] = useState('')
  const [intent, setIntent] = useState<DiscordIntent | ''>('')
  const [serverId, setServerId] = useState('')
  const [channelName, setChannelName] = useState('')
  const [notice, setNotice] = useState<string | null>(null)

  const usable = profiles.filter((p) => p.can_open)
  const effective = selected || usable[0]?.id || ''

  const create = useMutation({
    mutationFn: () =>
      api.createDiscordTask({
        browser_profile_id: effective,
        description: description.trim(),
        intent,
        server_id: serverId.trim(),
        channel_name: channelName.trim(),
      }),
    onSuccess: (result) => {
      setNotice(
        `Task ${result.task_id} created as a ${result.intent} task on profile ${profileName(
          profiles,
          result.browser_profile_id,
        )}.`,
      )
      setDescription('')
      onCreated()
    },
  })

  return (
    <Card title="New Discord task">
      <form
        className="space-y-3"
        onSubmit={(event) => {
          event.preventDefault()
          setNotice(null)
          create.mutate()
        }}
      >
        <fieldset className="rounded-lg border border-surface-200 p-3">
          <legend className="px-1 text-xs font-semibold text-slate-300">Browser account</legend>
          <label className="block text-xs font-medium text-slate-400">
            Profile (the Discord account)
            <select
              className="input mt-1 w-full"
              value={effective}
              onChange={(event) => onSelect(event.target.value)}
              required
            >
              {profiles.map((profile) => (
                <option key={profile.id} value={profile.id} disabled={!profile.can_open}>
                  {profile.name}
                  {profile.can_open ? '' : ' (cannot be opened)'}
                </option>
              ))}
            </select>
          </label>
          <p className="mt-1 text-[11px] text-slate-500">
            The profile <em>is</em> the account. It is saved on the task and reused on every retry
            and resume — this task will never switch to another account.
          </p>
        </fieldset>

        <label className="block text-xs font-medium text-slate-400">
          What should it do?
          <textarea
            className="input mt-1 min-h-[96px] w-full"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            maxLength={4000}
            required
            placeholder="Read the latest announcements in #announcements and summarise them."
          />
        </label>

        <fieldset className="rounded-lg border border-surface-200 p-3">
          <legend className="px-1 text-xs font-semibold text-slate-300">Intent</legend>
          <div className="grid gap-1.5 sm:grid-cols-2">
            {INTENTS.map((option) => (
              <label
                key={option.id || 'auto'}
                className={clsx(
                  'flex cursor-pointer items-start gap-2 rounded-md border p-2 text-xs',
                  intent === option.id
                    ? 'border-accent bg-accent/10'
                    : 'border-surface-200 hover:border-surface-300',
                )}
              >
                <input
                  type="radio"
                  name="intent"
                  className="mt-0.5"
                  checked={intent === option.id}
                  onChange={() => setIntent(option.id)}
                />
                <span>
                  <span className="block font-medium text-slate-200">{option.label}</span>
                  <span className="block text-[11px] text-slate-400">{option.hint}</span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>

        <fieldset className="rounded-lg border border-surface-200 p-3">
          <legend className="px-1 text-xs font-semibold text-slate-300">Scope (optional)</legend>
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="block text-xs font-medium text-slate-400">
              Server id
              <input
                className="input mt-1 w-full"
                value={serverId}
                onChange={(event) => setServerId(event.target.value)}
                maxLength={32}
                placeholder="optional"
              />
            </label>
            <label className="block text-xs font-medium text-slate-400">
              Channel
              <input
                className="input mt-1 w-full"
                value={channelName}
                onChange={(event) => setChannelName(event.target.value)}
                maxLength={80}
                placeholder="#announcements"
              />
            </label>
          </div>
        </fieldset>

        <p className="text-[11px] text-slate-500">
          {draftBehaviour}
        </p>

        <MutationError error={create.error} />
        {notice && (
          <Alert kind="success" title="Task created">
            {notice}
          </Alert>
        )}

        <button
          type="submit"
          className="btn btn-primary w-full"
          disabled={create.isPending || !effective || !description.trim()}
        >
          {create.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
          Start task
        </button>
      </form>
    </Card>
  )
}

// ================================================================= tasks
function TasksTab({
  tasks,
  profiles,
  loading,
  onChanged,
}: {
  tasks: DiscordTask[]
  profiles: DiscordProfile[]
  loading: boolean
  onChanged: () => void
}) {
  const [openId, setOpenId] = useState('')

  const cancel = useMutation({
    mutationFn: (id: string) => api.cancelTask(id),
    onSuccess: onChanged,
  })

  const retry = useMutation({
    mutationFn: (id: string) => api.retryTask(id),
    onSuccess: onChanged,
  })

  if (loading) return <Loading label="Loading Discord tasks" />
  if (!tasks.length) {
    return (
      <EmptyState
        title="No Discord tasks yet"
        description="Create one from the New Task tab. Each task is bound to one browser profile — the Discord account it acts as."
        icon={<MessageSquare className="h-8 w-8" />}
      />
    )
  }

  return (
    <div className="space-y-2">
      <MutationError error={cancel.error ?? retry.error} />
      {tasks.map((task) => (
        <Card key={task.task_id}>
          <div className="space-y-2">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-slate-100">{task.title}</p>
                <p className="text-[11px] text-slate-500">
                  {task.task_id} · profile {profileName(profiles, task.browser_profile_id)} ·{' '}
                  {task.intent || 'unknown'} intent
                </p>
              </div>
              <div className="flex items-center gap-1.5">
                <Pill tone={taskTone(task.status)}>{task.status.replace(/_/g, ' ')}</Pill>
                <button
                  type="button"
                  className="btn-ghost text-xs"
                  onClick={() => setOpenId(openId === task.task_id ? '' : task.task_id)}
                >
                  {openId === task.task_id ? 'Hide' : 'Details'}
                </button>
              </div>
            </div>

            <ProgressBar value={task.progress} />

            {task.next_planned_action && (
              <p className="text-xs text-slate-400">
                <span className="text-slate-500">Next:</span> {task.next_planned_action}
              </p>
            )}
            {task.last_blocker && (
              <p className="text-xs text-status-warning">
                <span className="text-slate-500">Blocked:</span> {task.last_blocker}
              </p>
            )}

            <div className="flex flex-wrap items-center gap-1.5">
              <Pill tone="gray">updated {when(task.updated_at)}</Pill>
              <Pill tone="gray">
                retries {task.retry_count}/{task.max_retries}
              </Pill>
              {task.login_intervention && (
                <Pill tone="amber">login requested</Pill>
              )}
              <span className="flex-1" />
              {!isTerminal(task.status) && (
                <>
                  <Link className="btn-ghost text-xs" to={`/tasks?task=${encodeURIComponent(task.task_id)}`}>
                    Open in Task Manager
                  </Link>
                  <button
                    type="button"
                    className="btn text-xs"
                    disabled={cancel.isPending || retry.isPending}
                    onClick={() => cancel.mutate(task.task_id)}
                  >
                    Cancel
                  </button>
                </>
              )}
              {['FAILED', 'UNVERIFIED', 'PARTIALLY_COMPLETED'].includes(task.status) && (
                <button
                  type="button"
                  className="btn text-xs"
                  disabled={retry.isPending}
                  onClick={() => retry.mutate(task.task_id)}
                >
                  Retry
                </button>
              )}
            </div>

            {openId === task.task_id && <TaskDetail task={task} />}
          </div>
        </Card>
      ))}
    </div>
  )
}

function TaskDetail({ task }: { task: DiscordTask }) {
  const detail = useQuery({
    queryKey: ['discord', 'task', task.task_id],
    queryFn: () => api.discordTask(task.task_id),
    retry: false,
  })

  const rows = detail.data?.task.activity ?? []
  const resume = detail.data?.task.resume ?? task.resume

  return (
    <div className="space-y-2 border-t border-surface-200 pt-2">
      <dl className="grid gap-1.5 text-xs sm:grid-cols-2">
        <Row label="Checkpoint" value={task.checkpoint.name || 'none yet'} mono />
        <Row label="Saved" value={when(task.checkpoint.saved_at)} />
        <Row label="Completed steps" value={String(resume.completed_steps?.length ?? 0)} />
        <Row label="Pending action" value={resume.pending_action || 'none'} mono />
        <Row label="Unconfirmed sends" value={String(resume.unconfirmed_sends?.length ?? 0)} />
        <Row label="Last evidence" value={resume.last_evidence || 'none'} mono />
        <Row label="Deadline" value={resume.deadline ? when(resume.deadline) : 'none'} />
        <Row label="Intervention" value={resume.login_intervention_id || 'none'} mono />
      </dl>

      {resume.unconfirmed_sends && resume.unconfirmed_sends.length > 0 && (
        <Alert kind="warning" title="A send could not be confirmed">
          An earlier send from this task returned no confirmation, so it may or may not have been
          posted. It will not be repeated automatically — check the channel first.
        </Alert>
      )}

      {detail.isLoading && <Loading label="Loading activity" />}
      {detail.error && <Alert kind="warning" title="Could not load activity">{errorText(detail.error)}</Alert>}

      {rows.length > 0 && <ActivityTable activity={rows} />}
      {rows.length === 0 && !detail.isLoading && (
        <p className="text-xs text-slate-500">No Discord actions recorded for this task yet.</p>
      )}
    </div>
  )
}

function isTerminal(status: string): boolean {
  return ['COMPLETED', 'CANCELLED', 'FAILED'].includes(status)
}

function taskTone(status: string): 'green' | 'blue' | 'amber' | 'red' | 'gray' {
  if (status === 'COMPLETED') return 'green'
  if (status === 'IN_PROGRESS' || status === 'READY') return 'blue'
  if (status === 'NEEDS_USER_ACTION' || status === 'UNVERIFIED' || status === 'WAITING') return 'amber'
  if (status === 'FAILED' || status === 'CANCELLED') return 'red'
  return 'gray'
}

// ================================================================= activity
function ActivityTab({
  activity,
  loading,
}: {
  activity: DiscordActivityRecord[]
  loading: boolean
}) {
  const [filter, setFilter] = useState('all')
  const [operation, setOperation] = useState('')

  const operations = useMemo(
    () => Array.from(new Set(activity.map((row) => row.operation))).sort(),
    [activity],
  )

  const counts = useMemo(() => {
    const out: Record<string, number> = { all: activity.length }
    for (const row of activity) {
      out[row.status] = (out[row.status] ?? 0) + 1
      if (row.outcome_uncertain) out.uncertain = (out.uncertain ?? 0) + 1
    }
    return out
  }, [activity])

  const filtered = useMemo(
    () =>
      activity.filter((row) => {
        if (operation && row.operation !== operation) return false
        if (filter === 'all') return true
        if (filter === 'uncertain') return row.outcome_uncertain
        return row.status === filter
      }),
    [activity, filter, operation],
  )

  if (loading) return <Loading label="Loading activity" />
  if (!activity.length) {
    return (
      <EmptyState
        title="No Discord activity yet"
        description="Every channel read, message sent and reply recorded by a Discord task appears here, with the evidence that proves it."
        icon={<MessageSquare className="h-8 w-8" />}
      />
    )
  }

  const chipItems = [
    { id: 'all', label: 'All', count: counts.all ?? 0 },
    { id: 'SUCCESS', label: 'Succeeded', count: counts.SUCCESS ?? 0 },
    { id: 'FAILED', label: 'Failed', count: counts.FAILED ?? 0 },
    { id: 'uncertain', label: 'Unconfirmed', count: counts.uncertain ?? 0 },
  ]

  return (
    <div className="space-y-3">
      <Chips value={filter} onChange={setFilter} items={chipItems} />
      <label className="block text-xs font-medium text-slate-400">
        Operation
        <select
          className="input mt-1 w-full sm:max-w-xs"
          value={operation}
          onChange={(event) => setOperation(event.target.value)}
        >
          <option value="">All operations</option>
          {operations.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </label>
      <ActivityTable activity={filtered} />
    </div>
  )
}

function ActivityTable({ activity }: { activity: DiscordActivityRecord[] }) {
  if (!activity.length) {
    return <p className="text-xs text-slate-500">Nothing matches this filter.</p>
  }
  return (
    <>
      <Alert kind="info" title="What an unconfirmed send means">
        The message was submitted but Discord did not confirm it arrived. AirdropOS never repeats
        such a message automatically — verify the channel first, or an operator posts twice.
      </Alert>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="py-1.5 pr-3 font-medium">When</th>
              <th className="py-1.5 pr-3 font-medium">Profile</th>
              <th className="py-1.5 pr-3 font-medium">Operation</th>
              <th className="py-1.5 pr-3 font-medium">Channel</th>
              <th className="py-1.5 pr-3 font-medium">Result</th>
              <th className="py-1.5 pr-3 font-medium">Task</th>
              <th className="py-1.5 font-medium">Detail</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-surface-300">
            {activity.map((row) => (
              <tr key={row.id} className="align-top">
                <td className="py-1.5 pr-3 whitespace-nowrap text-slate-500">{when(row.created_at)}</td>
                <td className="py-1.5 pr-3 text-slate-300">{row.browser_profile_id || '—'}</td>
                <td className="py-1.5 pr-3 text-slate-300">{row.operation}</td>
                <td className="py-1.5 pr-3 text-slate-400">
                  {row.channel?.label || '—'}
                </td>
                <td className="py-1.5 pr-3">
                  <Pill tone={row.outcome_uncertain ? 'amber' : row.status === 'SUCCESS' ? 'green' : 'red'}>
                    {row.outcome_uncertain ? 'UNCONFIRMED' : row.status}
                  </Pill>
                </td>
                <td className="py-1.5 pr-3 text-slate-500">
                  {row.task_id ? (
                    <Link to={`/tasks?task=${encodeURIComponent(row.task_id)}`}>{row.task_id}</Link>
                  ) : (
                    '—'
                  )}
                </td>
                <td className="py-1.5 text-slate-400">
                  {row.error ? (
                    <span className="text-status-warning">{row.error}</span>
                  ) : (
                    row.excerpt || '—'
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

// ================================================================= recovery
function RecoveryTab({
  tasks,
  profiles,
  onChanged,
}: {
  tasks: DiscordTask[]
  profiles: DiscordProfile[]
  onChanged: () => void
}) {
  const pending = tasks.filter((task) => Boolean(task.login_intervention))
  const paused = tasks.filter(
    (task) => task.status === 'NEEDS_USER_ACTION' && !task.login_intervention,
  )

  const open = useMutation({
    mutationFn: (id: string) => api.openDiscordProfile(id),
    onSuccess: () => onChanged(),
  })

  return (
    <div className="space-y-3">
      <Alert kind="info" title="How login recovery works">
        A Discord task that finds a sign-in form stops and asks you here. It saves its checkpoint
        first, so it resumes where it stopped rather than starting over. Sign in by hand, then
        confirm in the Agent Inbox — AirdropOS re-checks the live page before resuming, so a
        confirmation on its own is never treated as proof.
      </Alert>

      <MutationError error={open.error} />

      {pending.length === 0 && paused.length === 0 ? (
        <EmptyState
          title="No Discord login requests"
          description="Nothing is waiting on you. If a task needs a login, it will appear here and in the Agent Inbox."
          icon={<CheckCircle2 className="h-8 w-8" />}
        />
      ) : (
        <>
          {pending.map((task) => (
            <Card key={task.task_id} title="Discord login required">
              <div className="space-y-2">
                <p className="text-sm text-slate-300">
                  {task.login_intervention?.problem_summary ||
                    'Discord requires you to log in before this task can continue.'}
                </p>
                {task.login_intervention?.required_action && (
                  <p className="text-xs text-slate-400">{task.login_intervention.required_action}</p>
                )}
                <dl className="grid gap-1.5 text-xs sm:grid-cols-2">
                  <Row label="Task" value={task.task_id} mono />
                  <Row label="Profile" value={profileName(profiles, task.browser_profile_id)} />
                  <Row label="Completed steps" value={String(task.resume?.completed_steps?.length ?? 0)} />
                  <Row label="Pending action" value={task.resume?.pending_action || 'none'} mono />
                </dl>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={open.isPending || !task.browser_profile_id}
                    onClick={() => open.mutate(task.browser_profile_id)}
                  >
                    {open.isPending ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <LogIn className="h-4 w-4" />
                    )}
                    Open Discord browser
                  </button>
                  <Link
                    className="btn"
                    to={`/inbox?conversation=${encodeURIComponent(
                      task.login_intervention?.request_id ?? '',
                    )}`}
                  >
                    Confirm in Agent Inbox
                  </Link>
                  <Link className="btn-ghost" to={`/tasks?task=${encodeURIComponent(task.task_id)}`}>
                    Open task
                  </Link>
                </div>
                <p className="text-[11px] text-slate-500">
                  No password, cookie or token is stored with this request. Sign in yourself, then
                  press “I have logged in — Resume task”.
                </p>
              </div>
            </Card>
          ))}

          {paused.length > 0 && (
            <Alert kind="warning" title="Paused for another reason">
              <ul className="list-disc pl-4">
                {paused.map((task) => (
                  <li key={task.task_id}>
                    {task.task_id} — {task.last_blocker || 'waiting on the operator'}
                  </li>
                ))}
              </ul>
              These are not login requests. Open the task in the Agent Inbox to see what is needed.
            </Alert>
          )}
        </>
      )}

      <Card title="Policy">
        <ul className="space-y-1 text-xs text-slate-400">
          <li>No password is ever typed, and MFA or a CAPTCHA is never bypassed.</li>
          <li>Discord message text is treated as untrusted data, never as instructions.</li>
          <li>A login state is only reported when the page actually shows it.</li>
          <li>A confirmation for one profile never resumes another profile's task.</li>
        </ul>
      </Card>
    </div>
  )
}

// ---------------------------------------------------------------- shared
function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-slate-500">{label}</dt>
      <dd className={clsx('break-all text-slate-300', mono && 'font-mono text-[11px]')}>{value || '—'}</dd>
    </div>
  )
}