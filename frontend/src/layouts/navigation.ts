/** Shared navigation model: sidebar sections, bottom tabs, and which tab a route belongs to. */

import type { LucideIcon } from 'lucide-react'
import {
  Activity,
  AppWindow,
  Bot,
  Brain,
  Compass,
  Gauge,
  GitBranch,
  HardDrive,
  Hexagon,
  Home,
  Inbox,
  Layers,
  LifeBuoy,
  ListChecks,
  MessagesSquare,
  Radio,
  RadioTower,
  Send,
  Settings,
  Target,
  Twitter,
  Wallet,
  Wrench,
  Zap,
} from 'lucide-react'

export interface NavItem {
  to: string
  label: string
  icon: LucideIcon
  end?: boolean
  /**
   * Key of a live count shown beside the item. Resolved by the layout, which owns the
   * data; the nav model itself stays free of fetching.
   */
  badge?: string
}

export const NAV_SECTIONS: Array<{ label: string; items: NavItem[] }> = [
  {
    label: 'Command',
    items: [
      { to: '/', label: 'Mission Control', icon: Gauge, end: true },
      { to: '/agent', label: 'AI Agent Console', icon: Bot },
      // Sits beside the console: the console is for general interaction and new
      // tasks, the Inbox is where a specific task asks the operator something.
      { to: '/inbox', label: 'Agent Inbox', icon: Inbox, badge: 'inboxNeedsReply' },
      { to: '/live', label: 'Live Broadcast', icon: RadioTower },
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
    label: 'Automation',
    items: [
      { to: '/browser-profiles', label: 'Browser Profiles', icon: AppWindow },
      // X.com is its own integration rather than a tab on Social Monitors: it has
      // browser profiles, API credentials and a routing preference of its own.
      { to: '/x', label: 'X.com Integration', icon: Twitter },
      // Discord is its own page rather than a tab on Social Monitors: it runs on
      // persistent browser profiles with their own accounts, and it has a login
      // recovery flow the other social pages do not have.
      { to: '/discord', label: 'Discord Integration', icon: MessagesSquare },
      { to: '/telegram', label: 'Telegram Integration', icon: Send },
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

export type TabId = 'home' | 'airdrops' | 'chat' | 'tasks' | 'wallets'

export interface BottomTab {
  id: TabId
  to: string
  label: string
  icon: LucideIcon
}

/** Chat is always the middle (third) entry. */
export const BOTTOM_TABS: BottomTab[] = [
  { id: 'home', to: '/', label: 'Home', icon: Home },
  { id: 'airdrops', to: '/airdrops', label: 'Airdrops', icon: Hexagon },
  { id: 'chat', to: '/agent', label: 'Chat', icon: Bot },
  { id: 'tasks', to: '/tasks', label: 'Tasks', icon: ListChecks },
  { id: 'wallets', to: '/wallets', label: 'Wallets', icon: Wallet },
]

/**
 * Secondary pages highlight the tab they belong to (Tools, Goals and the
 * automation pages sit under Tasks; Telegram under Airdrops; Transactions and
 * the worker/diagnostics screens under Wallets, as drawn in the reference).
 * Pages with no natural tab (Settings, Memory, ...) highlight nothing.
 */
export const ROUTE_TABS: Array<[prefix: string, tab: TabId]> = [
  ['/agent', 'chat'],
  // The Inbox shares the existing Chat tab rather than adding a sixth one, so the
  // mobile order (Home -> Airdrops -> Chat -> Tasks -> Wallets) is unchanged.
  ['/inbox', 'chat'],
  ['/airdrops', 'airdrops'],
  ['/telegram', 'airdrops'],
  ['/x', 'airdrops'],
  ['/discord', 'airdrops'],
  ['/tasks', 'tasks'],
  ['/tools', 'tasks'],
  ['/goals', 'tasks'],
  ['/automation', 'tasks'],
  ['/browser-profiles', 'tasks'],
  ['/live', 'tasks'],
  ['/wallets', 'wallets'],
  ['/transactions', 'wallets'],
  ['/worker', 'wallets'],
  ['/diagnostics', 'wallets'],
]

export function activeTab(pathname: string): TabId | null {
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, '') : pathname
  if (path === '/') return 'home'
  const match = ROUTE_TABS.find(([prefix]) => path === prefix || path.startsWith(`${prefix}/`))
  return match ? match[1] : null
}
