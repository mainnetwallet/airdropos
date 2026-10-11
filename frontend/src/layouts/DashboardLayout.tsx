/** Dashboard shell: sidebar navigation, header, and emergency-stop banner. */

import { NavLink, Outlet } from 'react-router-dom'
import {
  Activity,
  Bot,
  Brain,
  Compass,
  Gauge,
  GitBranch,
  HardDrive,
  Layers,
  LifeBuoy,
  LogOut,
  Radio,
  Settings,
  ShieldAlert,
  Target,
  Wallet,
  Wrench,
  Zap,
} from 'lucide-react'
import clsx from 'clsx'
import { useLiveEvents } from '@/hooks'
import { useQuery } from '@tanstack/react-query'
import { api, getEmail, UNAUTHORIZED_EVENT } from '@/services/api'

const NAV_SECTIONS = [
  {
    label: 'Command',
    items: [
      { to: '/', label: 'Mission Control', icon: Gauge, end: true },
      { to: '/agent', label: 'AI Agent Console', icon: Bot },
      { to: '/worker', label: 'Live Worker', icon: Activity },
      { to: '/goals', label: 'Goals & Strategy', icon: Target },
      { to: '/automation', label: '24/7 Automation', icon: Zap },
    ],
  },
  {
    label: 'Intelligence',
    items: [
      { to: '/airdrops', label: 'Airdrop Explorer', icon: Compass },
      { to: '/tasks', label: 'Task Manager', icon: Layers },
      { to: '/evidence', label: 'Evidence & Changes', icon: HardDrive },
      { to: '/memory', label: 'Memory & Learning', icon: Brain },
      { to: '/social', label: 'Social Monitors', icon: Radio },
    ],
  },
  {
    label: 'Assets',
    items: [
      { to: '/wallets', label: 'Wallets', icon: Wallet },
      { to: '/transactions', label: 'Transactions', icon: Activity },
      { to: '/tools', label: 'Tool Registry', icon: Wrench },
    ],
  },
  {
    label: 'System',
    items: [
      { to: '/diagnostics', label: 'Diagnostics', icon: LifeBuoy },
      { to: '/improvements', label: 'Self-Improvement', icon: GitBranch },
      { to: '/settings', label: 'Security & Settings', icon: Settings },
    ],
  },
]

export default function DashboardLayout() {
  const { connected } = useLiveEvents()
  const { data: stop } = useQuery({
    queryKey: ['emergency-stop'],
    queryFn: api.stopStatus,
    refetchInterval: 15000,
  })

  // "Live" above only means the browser reached the API. Whether tasks actually
  // run depends on the separate worker process, so show that on its own.
  const { data: worker } = useQuery({
    queryKey: ['worker-status-mini'],
    queryFn: api.workerStatus,
    refetchInterval: 5000,
    retry: false,
  })
  const workerAlive = worker?.alive === true

  return (
    <div className="flex h-screen overflow-hidden">
      {/* Sidebar */}
      <aside className="flex w-60 shrink-0 flex-col border-r border-surface-300 bg-surface-100">
        <div className="flex items-center gap-2 border-b border-surface-300 px-5 py-4">
          <ShieldAlert className="h-6 w-6 text-accent" />
          <div>
            <p className="font-bold text-slate-100">AirdropOS</p>
            <p className="text-[11px] text-slate-500">Autonomous Agent</p>
          </div>
        </div>

        <nav className="flex-1 space-y-5 overflow-y-auto px-3 py-4">
          {NAV_SECTIONS.map((section) => (
            <div key={section.label}>
              <p className="mb-1 px-2 text-[10px] font-semibold uppercase tracking-widest text-slate-600">
                {section.label}
              </p>
              <ul className="space-y-0.5">
                {section.items.map(({ to, label, icon: Icon, end }) => (
                  <li key={to}>
                    <NavLink
                      to={to}
                      end={end}
                      className={({ isActive }) =>
                        clsx(
                          'flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition-colors',
                          isActive
                            ? 'bg-accent/15 text-accent font-medium'
                            : 'text-slate-400 hover:bg-surface-200 hover:text-slate-200',
                        )
                      }
                    >
                      <Icon className="h-4 w-4 shrink-0" />
                      <span className="truncate">{label}</span>
                    </NavLink>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>

        <div className="border-t border-surface-300 px-4 py-3 text-xs">
          <div className="flex items-center gap-2">
            <span
              className={clsx(
                'h-2 w-2 rounded-full',
                connected ? 'animate-pulse bg-status-success' : 'bg-status-idle',
              )}
            />
            <span className="text-slate-400">
              {connected ? 'Live' : 'Disconnected'}
            </span>
          </div>
          <NavLink to="/worker" className="mt-1.5 flex items-center gap-2">
            <span
              className={clsx(
                'h-2 w-2 rounded-full',
                worker === undefined
                  ? 'bg-status-idle'
                  : workerAlive
                    ? 'animate-pulse bg-status-success'
                    : 'bg-status-error',
              )}
            />
            <span className={workerAlive ? 'text-slate-400' : 'text-status-error'}>
              {worker === undefined ? 'Worker: checking' : workerAlive ? 'Worker running' : 'Worker offline'}
            </span>
          </NavLink>
          {getEmail() && (
            <p className="mt-2 truncate text-[11px] text-slate-500" title={getEmail()}>
              {getEmail()}
            </p>
          )}
          <button
            onClick={async () => {
              await api.logout()
              window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
            }}
            className="mt-2 flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-slate-400 transition-colors hover:bg-surface-200 hover:text-slate-200"
          >
            <LogOut className="h-3.5 w-3.5" />
            Log out
          </button>
        </div>
      </aside>

      {/* Main */}
      <div className="flex min-w-0 flex-1 flex-col">
        {stop?.emergency_stop_enabled && (
          <div className="border-b border-status-error/40 bg-status-error/10 px-6 py-2">
            <p className="text-sm font-medium text-status-error">
              EMERGENCY STOP ACTIVE — new execution, signing, and transaction submission are
              blocked. {stop.disclaimer}
            </p>
          </div>
        )}
        <main className="flex-1 overflow-y-auto p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
