import { useCallback, useEffect, useState } from 'react'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import DashboardLayout from '@/layouts/DashboardLayout'
import Overview from '@/pages/Overview'
import AgentChat from '@/pages/AgentChat'
import TaskManagerPage from '@/pages/TaskManager'
import ToolsPage from '@/pages/ToolsPage'
import WorkerLive from '@/pages/WorkerLive'
import Wallets from '@/pages/Wallets'
import SecuritySettings from '@/pages/SecuritySettings'
import Telegram from '@/pages/Telegram'
import BrowserAutomation from '@/pages/BrowserAutomation'
import Login from '@/pages/Login'
import { ErrorState } from '@/components/ui'
import { api, ApiError, UNAUTHORIZED_EVENT } from '@/services/api'
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
      { path: 'worker', element: <WorkerLive /> },
      { path: 'goals', element: <Goals /> },
      { path: 'automation', element: <Automation /> },
      { path: 'browser', element: <BrowserAutomation /> },
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

/** Shows the login page when the backend answers 401; works unchanged if auth is disabled. */
function AuthGate() {
  const [state, setState] = useState<AuthState>('checking')
  const [problem, setProblem] = useState<unknown>(null)

  const check = useCallback(async () => {
    setState('checking')
    try {
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
