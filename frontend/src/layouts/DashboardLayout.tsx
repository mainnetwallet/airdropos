/**
 * Dashboard shell.
 *  - Desktop (lg and up): sidebar navigation, unchanged.
 *  - Mobile / tablet: top header, slide-in menu and the bottom tab bar with the centre Chat button.
 * Both share the emergency-stop banner and the same routed content.
 */

import { useCallback, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { LogOut, ShieldAlert } from 'lucide-react'
import clsx from 'clsx'
import { useLiveEvents } from '@/hooks'
import { useQuery } from '@tanstack/react-query'
import { api, getEmail, UNAUTHORIZED_EVENT } from '@/services/api'
import { NAV_SECTIONS } from '@/layouts/navigation'
import { BottomNav, MobileDrawer, MobileHeader, useKeyboardOpen } from '@/components/mobile/MobileChrome'

export default function DashboardLayout() {
  const { connected } = useLiveEvents()
  const { pathname } = useLocation()
  const [menuOpen, setMenuOpen] = useState(false)
  const closeMenu = useCallback(() => setMenuOpen(false), [])
  const keyboardOpen = useKeyboardOpen()

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

  // Shared with the Home screen (same key) so it costs no extra request.
  const { data: overview } = useQuery({
    queryKey: ['overview'],
    queryFn: api.overview,
    refetchInterval: 10000,
  })

  // How many task conversations are waiting on this operator. Small, and polled slowly
  // because it is a hint; the Inbox page refetches precisely when it is open.
  const { data: inboxSummary } = useQuery({
    queryKey: ['inbox-summary'],
    queryFn: api.inboxSummary,
    refetchInterval: 30000,
    retry: false,
  })
  const badges: Record<string, number> = { inboxNeedsReply: inboxSummary?.needs_reply ?? 0 }

  const logout = async () => {
    await api.logout()
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
  }

  const isChat = pathname.startsWith('/agent')
  const isHome = pathname === '/'
  // The Inbox is a three-panel workspace: it needs the full width, and on mobile it
  // manages its own scrolling so the composer stays above the keyboard.
  const isInbox = pathname.startsWith('/inbox')
  const fullBleed = isChat || isHome || isInbox
  const stopActive = stop?.emergency_stop_enabled === true

  return (
    <div className={clsx('app-shell flex h-dvh overflow-hidden', isChat && 'mobile-dark')}>
      {/* Sidebar (desktop) */}
      <aside className="hidden w-60 shrink-0 flex-col border-r border-surface-300 bg-surface-100 lg:flex">
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
                {section.items.map(({ to, label, icon: Icon, end, badge }) => (
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
                      {badge && badges[badge] > 0 && (
                        <span
                          className="ml-auto shrink-0 rounded-full bg-status-warning/20 px-1.5 py-0.5 text-[10px] font-bold text-status-warning"
                          aria-label={`${badges[badge]} awaiting a reply`}
                        >
                          {badges[badge]}
                        </span>
                      )}
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
            <span className="text-slate-400">{connected ? 'Live' : 'Disconnected'}</span>
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
            onClick={logout}
            className="mt-2 flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-slate-400 transition-colors hover:bg-surface-200 hover:text-slate-200"
          >
            <LogOut className="h-3.5 w-3.5" />
            Log out
          </button>
        </div>
      </aside>

      {/* Main */}
      <div className="relative flex min-w-0 flex-1 flex-col">
        {stopActive && (
          <div className="pt-safe border-b border-status-error/40 bg-status-error/10 lg:pt-0">
            <p className="px-4 py-2 text-sm font-medium text-status-error lg:px-6">
              EMERGENCY STOP ACTIVE — new execution, signing, and transaction submission are
              blocked. {stop.disclaimer}
            </p>
          </div>
        )}

        <MobileHeader
          onMenu={() => setMenuOpen(true)}
          overHero={isHome && !stopActive}
          insetTop={!stopActive}
          alerts={overview?.needs_user_action ?? 0}
        />

        <main
          className={clsx(
            'flex-1 overflow-y-auto lg:p-6',
            fullBleed ? 'px-0 pt-0' : 'px-4 pt-1',
            isInbox && 'flex flex-col overflow-hidden',
            !keyboardOpen && 'pb-nav lg:pb-0',
          )}
        >
          <div
            className={clsx(
              'mx-auto w-full lg:max-w-none',
              (isChat || isInbox) && 'h-full',
              !fullBleed && 'max-w-3xl',
            )}
          >
            <Outlet />
          </div>
        </main>

        <BottomNav hidden={keyboardOpen} />
      </div>

      <MobileDrawer open={menuOpen} onClose={closeMenu} onLogout={logout} />
    </div>
  )
}
