import { lazy, useCallback, useEffect, useState } from 'react'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import DashboardLayout from '@/layouts/DashboardLayout'
import Login from '@/pages/Login'
import { ErrorState } from '@/components/ui'
import { api, ApiError, hasSession, setAuthRequired, UNAUTHORIZED_EVENT } from '@/services/api'

// Every page is its own chunk. Before this, the whole dashboard (recharts, react-markdown and
// all 20 pages) was one ~730 KB script that had to be downloaded and parsed before the login
// screen could even appear.
const Overview = lazy(() => import('@/pages/Overview'))
const AgentChat = lazy(() => import('@/pages/AgentChat'))
const AgentInbox = lazy(() => import('@/pages/AgentInbox'))
const TaskManagerPage = lazy(() => import('@/pages/TaskManager'))
const ToolsPage = lazy(() => import('@/pages/ToolsPage'))
const WorkerLive = lazy(() => import('@/pages/WorkerLive'))
const Wallets = lazy(() => import('@/pages/Wallets'))
const SecuritySettings = lazy(() => import('@/pages/SecuritySettings'))
const Telegram = lazy(() => import('@/pages/Telegram'))
const BrowserAutomation = lazy(() => import('@/pages/BrowserAutomation'))
const BrowserProfiles = lazy(() => import('@/pages/BrowserProfiles'))
const XIntegration = lazy(() => import('@/pages/XIntegration'))
const Discord = lazy(() => import('@/pages/Discord'))
const LiveBroadcast = lazy(() => import('@/pages/LiveBroadcast'))
const LiveSessionPage = lazy(() => import('@/pages/LiveSession'))
const named = (pick: string) => lazy(() => import('@/pages').then((m) => ({ default: (m as any)[pick] })))
const Airdrops = named('Airdrops')
const Automation = named('Automation')
const Diagnostics = named('Diagnostics')
const Evidence = named('Evidence')
const Goals = named('Goals')
const Improvements = named('Improvements')
const Memory = named('Memory')
const Social = named('Social')
const Transactions = named('Transactions')

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 5_000,
    },
  },
})

const router = createBrowserRouter([
  {
    path: '/',
    element: <DashboardLayout />,
    children: [
      { index: true, element: <Overview /> },
      { path: 'agent', element: <AgentChat /> },
      { path: 'inbox', element: <AgentInbox /> },
      { path: 'worker', element: <WorkerLive /> },
      { path: 'goals', element: <Goals /> },
      { path: 'automation', element: <Automation /> },
      { path: 'browser', element: <BrowserAutomation /> },
      { path: 'browser-profiles', element: <BrowserProfiles /> },
      { path: 'live', element: <LiveBroadcast /> },
      { path: 'live/:integration/:key', element: <LiveSessionPage /> },
      { path: 'telegram', element: <Telegram /> },
      { path: 'airdrops', element: <Airdrops /> },
      { path: 'tasks', element: <TaskManagerPage /> },
      { path: 'evidence', element: <Evidence /> },
      { path: 'memory', element: <Memory /> },
      { path: 'social', element: <Social /> },
      { path: 'x', element: <XIntegration /> },
      { path: 'discord', element: <Discord /> },
      { path: 'wallets', element: <Wallets /> },
      { path: 'transactions', element: <Transactions /> },
      { path: 'tools', element: <ToolsPage /> },
      { path: 'diagnostics', element: <Diagnostics /> },
      { path: 'improvements', element: <Improvements /> },
      { path: 'settings', element: <SecuritySettings /> },
    ],
  },
])

type AuthState = 'checking' | 'login' | 'ready' | 'down'

/**
 * Decides between the login form, the dashboard, and "the backend is not there".
 *
 * The unauthenticated liveness probe is asked first. It answers with 200 either way,
 * so nothing here manufactures a 401 before an operator has had the chance to log in.
 * A 401 is still handled below: a stored session has to be validated, and an older
 * backend that does not report `auth_required` is answered that way.
 */
export function AuthGate() {
  const [state, setState] = useState<AuthState>('checking')
  const [problem, setProblem] = useState<unknown>(null)

  const check = useCallback(async () => {
    setState('checking')
    try {
      // The liveness probe is unauthenticated, so it answers with 200 whether or not
      // a session exists. Asking it first is what keeps a page load from producing a
      // guaranteed 401: no request is made to a protected endpoint unless there is
      // actually a token to present.
      const health = await api.healthBanner()
      const requiresAuth = health.auth_required
      // `undefined` means an older backend that does not report it. Leave that
      // unknown rather than guessing, and let the 401 below answer it.
      setAuthRequired(typeof requiresAuth === 'boolean' ? requiresAuth : null)

      if (requiresAuth === false) {
        // Authentication is switched off on this deployment; there is nothing to log
        // in to, and asking for a token would only ever produce a 401.
        setState('ready')
        return
      }
      if (requiresAuth === true && !hasSession()) {
        setState('login')
        return
      }

      // Either a token exists, or the backend did not say which case it is.
      await api.overview()
      setState('ready')
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setState('login')
      } else {
        setProblem(
          e instanceof TypeError ? new Error('Cannot reach the backend. Is `python run.py` running?') : e,
        )
        setState('down')
      }
    }
  }, [])

  useEffect(() => {
    check()
    const onUnauthorized = () => {
      queryClient.clear()
      setState('login')
    }
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
  }, [check])

  if (state === 'checking') {
    return <div className="flex min-h-full items-center justify-center text-sm text-slate-500">Loading...</div>
  }
  if (state === 'down') {
    return (
      <div className="flex min-h-full items-center justify-center">
        <ErrorState error={problem} onRetry={check} />
      </div>
    )
  }
  if (state === 'login') {
    return (
      <Login
        onSuccess={() => {
          queryClient.clear()
          setState('ready')
        }}
      />
    )
  }
  return <RouterProvider router={router} />
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthGate />
    </QueryClientProvider>
  )
}
