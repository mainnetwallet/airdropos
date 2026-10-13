import { useCallback, useEffect, useState } from 'react'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import DashboardLayout from '@/layouts/DashboardLayout'
import Overview from '@/pages/Overview'
import AgentChat from '@/pages/AgentChat'
import AgentInbox from '@/pages/AgentInbox'
import TaskManagerPage from '@/pages/TaskManager'
import ToolsPage from '@/pages/ToolsPage'
import WorkerLive from '@/pages/WorkerLive'
import Wallets from '@/pages/Wallets'
import SecuritySettings from '@/pages/SecuritySettings'
import Telegram from '@/pages/Telegram'
import BrowserAutomation from '@/pages/BrowserAutomation'
import BrowserProfiles from '@/pages/BrowserProfiles'
import LiveBroadcast from '@/pages/LiveBroadcast'
import LiveSessionPage from '@/pages/LiveSession'
import Login from '@/pages/Login'
import { ErrorState } from '@/components/ui'
import { api, ApiError, hasSession, setAuthRequired, UNAUTHORIZED_EVENT } from '@/services/api'
import {
  Airdrops,
  Automation,
  Diagnostics,
  Evidence,
  Goals,
  Improvements,
  Memory,
  Social,
  Transactions,
} from '@/pages'

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
