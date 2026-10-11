/** Mobile/tablet shell pieces: top header, slide-in menu, and the bottom tab bar. */

import { useEffect, useState } from 'react'
import { Link, NavLink, useLocation, useNavigate } from 'react-router-dom'
import { ArrowLeft, Bell, LogOut, Menu, Navigation, X } from 'lucide-react'
import clsx from 'clsx'
import { getEmail } from '@/services/api'
import { BOTTOM_TABS, NAV_SECTIONS, activeTab } from '@/layouts/navigation'

// ---------------------------------------------------------------- keyboard
/** True while the on-screen keyboard is likely open, so fixed bars can get out of the way. */
export function useKeyboardOpen(): boolean {
  const [open, setOpen] = useState(false)

  useEffect(() => {
    const editable = (el: EventTarget | null) =>
      el instanceof HTMLElement &&
      (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable)

    const viewportShrunk = () => {
      const vv = window.visualViewport
      return vv ? vv.height < window.screen.height * 0.8 : true
    }

    const onFocusIn = (e: FocusEvent) => {
      if (editable(e.target) && viewportShrunk()) setOpen(true)
    }
    const onFocusOut = () => setOpen(false)
    const onResize = () => {
      const active = document.activeElement
      setOpen(editable(active) && viewportShrunk())
    }

    document.addEventListener('focusin', onFocusIn)
    document.addEventListener('focusout', onFocusOut)
    window.visualViewport?.addEventListener('resize', onResize)
    return () => {
      document.removeEventListener('focusin', onFocusIn)
      document.removeEventListener('focusout', onFocusOut)
      window.visualViewport?.removeEventListener('resize', onResize)
    }
  }, [])

  return open
}

// ---------------------------------------------------------------- header
function Logo() {
  return (
    <span className="flex items-center gap-1.5">
      <Navigation className="h-[18px] w-[18px] rotate-45 fill-accent text-accent" aria-hidden />
      <span className="text-[17px] font-bold tracking-tight">
        Airdrop<span className="text-accent">OS</span>
      </span>
    </span>
  )
}

export function MobileHeader({
  onMenu,
  overHero,
  alerts,
  insetTop = true,
}: {
  onMenu: () => void
  /** Home draws the header over its dark hero image. */
  overHero: boolean
  /** Tasks that need the operator; shown as a dot on the bell. */
  alerts: number
  /** False when a banner above already clears the status bar / notch. */
  insetTop?: boolean
}) {
  const location = useLocation()
  const navigate = useNavigate()
  const isChat = location.pathname.startsWith('/agent')
  const initial = (getEmail() ?? 'A').trim().charAt(0).toUpperCase() || 'A'

  const goBack = () => {
    // react-router keeps its position in history.state; with nothing to go back to, land on Home.
    if ((window.history.state?.idx ?? 0) > 0) navigate(-1)
    else navigate('/')
  }

  const iconButton = clsx(
    'flex h-10 w-10 items-center justify-center rounded-full transition-colors',
    overHero ? 'text-white hover:bg-white/10' : 'text-slate-200 hover:bg-slate-500/10',
  )

  return (
    <header
      className={clsx(
        'z-30 shrink-0 lg:hidden',
        insetTop && 'pt-safe',
        overHero ? 'absolute inset-x-0 top-0 text-white' : 'relative text-slate-100',
      )}
    >
      <div className="mx-auto flex h-14 max-w-3xl items-center justify-between px-3">
        {isChat ? (
          <button onClick={goBack} className={iconButton} aria-label="Go back">
            <ArrowLeft className="h-6 w-6" />
          </button>
        ) : (
          <button onClick={onMenu} className={iconButton} aria-label="Open menu">
            <Menu className="h-6 w-6" />
          </button>
        )}

        {overHero ? (
          <span className="mr-auto ml-1 text-lg font-semibold">AirdropOS</span>
        ) : (
          <Logo />
        )}

        {overHero ? (
          <Link
            to="/settings"
            aria-label="Settings"
            className="flex h-10 w-10 items-center justify-center rounded-full bg-white/20 text-sm font-semibold ring-2 ring-white/60"
          >
            {initial}
          </Link>
        ) : (
          <Link
            to="/tasks?status=NEEDS_USER_ACTION"
            aria-label={alerts > 0 ? `${alerts} tasks need your action` : 'Notifications'}
            className={clsx(iconButton, 'relative')}
          >
            <Bell className="h-[22px] w-[22px]" />
            {alerts > 0 && (
              <span className="absolute right-2 top-2 h-2.5 w-2.5 rounded-full bg-status-error ring-2 ring-surface-50" />
            )}
          </Link>
        )}
      </div>
    </header>
  )
}

// ---------------------------------------------------------------- drawer
export function MobileDrawer({
  open,
  onClose,
  onLogout,
}: {
  open: boolean
  onClose: () => void
  onLogout: () => void
}) {
  const location = useLocation()

  // Close after navigating, and on Escape.
  useEffect(() => {
    onClose()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname])
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  return (
    <div className={clsx('fixed inset-0 z-50 lg:hidden', !open && 'pointer-events-none')} aria-hidden={!open}>
      <div
        className={clsx(
          'absolute inset-0 bg-black/50 transition-opacity',
          open ? 'opacity-100' : 'opacity-0',
        )}
        onClick={onClose}
      />
      <aside
        className={clsx(
          'pt-safe absolute inset-y-0 left-0 flex w-[82%] max-w-xs flex-col bg-surface-100 shadow-2xl transition-transform duration-200',
          open ? 'translate-x-0' : '-translate-x-full',
        )}
        aria-label="Menu"
      >
        <div className="flex items-center justify-between px-4 py-3 text-slate-100">
          <Logo />
          <button
            onClick={onClose}
            className="flex h-10 w-10 items-center justify-center rounded-full hover:bg-slate-500/10"
            aria-label="Close menu"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <nav className="flex-1 space-y-5 overflow-y-auto px-3 pb-4">
          {NAV_SECTIONS.map((section) => (
            <div key={section.label}>
              <p className="mb-1 px-2 text-[10px] font-semibold uppercase tracking-widest text-slate-500">
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
                          'flex min-h-[44px] items-center gap-3 rounded-xl px-3 text-sm transition-colors',
                          isActive
                            ? 'bg-accent/15 font-semibold text-accent'
                            : 'text-slate-300 hover:bg-surface-200',
                        )
                      }
                    >
                      <Icon className="h-[18px] w-[18px] shrink-0" />
                      <span className="truncate">{label}</span>
                    </NavLink>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>

        <div className="border-t border-surface-300 px-4 py-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))]">
          {getEmail() && <p className="mb-2 truncate text-xs text-slate-500">{getEmail()}</p>}
          <button
            onClick={onLogout}
            className="flex min-h-[44px] w-full items-center gap-2 rounded-xl px-3 text-sm text-slate-300 hover:bg-surface-200"
          >
            <LogOut className="h-4 w-4" />
            Log out
          </button>
        </div>
      </aside>
    </div>
  )
}

// ---------------------------------------------------------------- bottom nav
export function BottomNav({ hidden }: { hidden: boolean }) {
  const { pathname } = useLocation()
  const current = activeTab(pathname)

  return (
    <nav
      aria-label="Primary"
      className={clsx(
        'fixed inset-x-0 bottom-0 z-40 transition-transform duration-150 lg:hidden',
        hidden && 'translate-y-full',
      )}
    >
      <div className="mx-auto max-w-2xl rounded-t-[28px] border-t border-surface-300/70 bg-surface-100 px-2 pb-[calc(0.5rem+env(safe-area-inset-bottom))] pt-2 shadow-nav">
        <ul className="grid grid-cols-5 items-end">
          {BOTTOM_TABS.map(({ id, to, label, icon: Icon }) => {
            const active = current === id

            if (id === 'chat') {
              return (
                <li key={id} className="flex justify-center">
                  <Link
                    to={to}
                    aria-label={label}
                    aria-current={active ? 'page' : undefined}
                    className={clsx(
                      '-mt-9 flex h-[62px] w-[62px] items-center justify-center rounded-full text-white ring-[5px] ring-surface-100 transition-all',
                      active
                        ? 'bg-gradient-to-b from-sky-400 to-accent-hover shadow-[0_0_0_7px_rgba(59,130,246,0.22),0_10px_24px_rgba(37,99,235,0.55)]'
                        : 'bg-gradient-to-b from-[#1b2a4a] to-[#0b1426] shadow-[0_10px_22px_rgba(11,20,38,0.45)]',
                    )}
                  >
                    <Icon className="h-7 w-7" strokeWidth={1.75} />
                  </Link>
                </li>
              )
            }

            return (
              <li key={id}>
                <Link
                  to={to}
                  aria-current={active ? 'page' : undefined}
                  className="flex min-h-[52px] flex-col items-center justify-center gap-0.5"
                >
                  <span
                    className={clsx(
                      'flex h-8 w-11 items-center justify-center rounded-xl transition-colors',
                      active ? 'bg-accent/15 text-accent' : 'text-slate-400',
                    )}
                  >
                    <Icon className="h-[22px] w-[22px]" strokeWidth={active ? 2.25 : 1.75} />
                  </span>
                  <span
                    className={clsx(
                      'text-[11px] leading-none',
                      active ? 'font-semibold text-accent' : 'font-medium text-slate-400',
                    )}
                  >
                    {label}
                  </span>
                </Link>
              </li>
            )
          })}
        </ul>
      </div>
    </nav>
  )
}
