/** Shared navigation model: sidebar sections, bottom tabs, and which tab a route belongs to. */

import type { LucideIcon } from 'lucide-react'
import {
  Activity,
  Bot,
  Brain,
  Compass,
  Gauge,
  GitBranch,
  Globe,
  HardDrive,
  Hexagon,
  Home,
  Layers,
  LifeBuoy,
  ListChecks,
  Radio,
  Send,
  Settings,
  Target,
  Wallet,
  Wrench,
  Zap,
} from 'lucide-react'

export interface NavItem {
  to: string
  label: string
  icon: LucideIcon
  end?: boolean
}

export const NAV_SECTIONS: Array<{ label: string; items: NavItem[] }> = [
  {
    label: 'Command',
    items: [
      { to: '/', label: 'Mission Control', icon: Gauge, end: true },
      { to: '/agent', label: 'AI Agent Console', icon: Bot },
      { to: '/worker', label: 'Live Worker', icon: Activity },
      { to: '/goals', label: 'Goals & Strategy', icon: Target },
      { to: '/automation', label: '24/7 Automation', icon: Zap },
      { to: '/browser', label: 'Browser Automation', icon: Globe },
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
      { to: '/telegram', label: 'Telegram Integration', icon: Send },
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
const ROUTE_TABS: Array<[prefix: string, tab: TabId]> = [
  ['/agent', 'chat'],
  ['/airdrops', 'airdrops'],
  ['/telegram', 'airdrops'],
  ['/tasks', 'tasks'],
  ['/tools', 'tasks'],
  ['/goals', 'tasks'],
  ['/automation', 'tasks'],
  ['/browser', 'tasks'],
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
