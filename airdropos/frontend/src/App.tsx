import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import DashboardLayout from '@/layouts/DashboardLayout'
import Overview from '@/pages/Overview'
import AgentChat from '@/pages/AgentChat'
import TaskManagerPage from '@/pages/TaskManager'
import ToolsPage from '@/pages/ToolsPage'
import Wallets from '@/pages/Wallets'
import SecuritySettings from '@/pages/SecuritySettings'
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
      { path: 'goals', element: <Goals /> },
      { path: 'automation', element: <Automation /> },
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

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  )
}
