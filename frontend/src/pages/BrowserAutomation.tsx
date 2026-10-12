/** Browser Automation: the browser-capable tools in the registry and what the dashboard can (not) change. */

import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Globe, Info } from 'lucide-react'
import { api } from '@/services/api'
import { Card, EmptyState, ErrorState, IconTile, Loading, PageHeader, Pill } from '@/components/ui'

export default function BrowserAutomation() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['tools'],
    queryFn: api.listTools,
    refetchInterval: 15000,
  })

  if (isLoading) return <Loading label="Loading browser tools" />
  if (error) return <ErrorState error={error} onRetry={() => refetch()} />

  const tools = (data?.tools ?? []).filter(
    (t) => t.name.toLowerCase().includes('browser') || t.side_effect === 'web_interaction',
  )
  const enabled = tools.filter((t) => t.enabled).length

  return (
    <div className="space-y-4 pt-1 lg:space-y-6 lg:pt-0">
      <PageHeader title="Browser Automation" subtitle="Automate web tasks safely" />

      {/* Status: derived from the registry, not from a running browser */}
      <Card
        className={
          enabled > 0
            ? 'flex items-center gap-3 border-emerald-500/30 bg-emerald-500/10'
            : 'flex items-center gap-3'
        }
      >
        <IconTile tone={enabled > 0 ? 'green' : 'slate'} className="h-12 w-12">
          <Globe />
        </IconTile>
        <div className="min-w-0">
          <p className="font-semibold text-slate-100">
            {enabled > 0 ? 'Browser tools enabled' : 'No browser tools enabled'}
          </p>
          <p className="text-xs text-slate-500">
            {tools.length === 0
              ? 'None are registered in the tool registry'
              : `${enabled} of ${tools.length} registered browser tools are enabled`}
          </p>
        </div>
      </Card>

      <Card title="Browser tools">
        {tools.length === 0 ? (
          <EmptyState
            title="No browser tools registered"
            description="Browser tools register at backend startup when browser automation is configured."
            icon={<Globe className="h-8 w-8" />}
          />
        ) : (
          <ul className="divide-y divide-surface-300/70">
            {tools.map((tool) => (
              <li key={tool.name} className="py-3 first:pt-0 last:pb-0">
                <div className="flex items-start justify-between gap-2">
                  <p className="truncate font-mono text-sm text-slate-100">{tool.name}</p>
                  <div className="flex shrink-0 gap-1">
                    <Pill tone={tool.enabled ? 'green' : 'gray'}>{tool.enabled ? 'Enabled' : 'Disabled'}</Pill>
                    {tool.authorized === false && <Pill tone="amber">Unauthorized</Pill>}
                  </div>
                </div>
                <p className="mt-1 text-xs leading-relaxed text-slate-500">{tool.description}</p>
                {tool.disabled_reason && <p className="mt-1 text-xs text-status-warning">{tool.disabled_reason}</p>}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Sessions & settings">
        <p className="text-sm text-slate-400">
          Proxy and headless mode are set on the server. Create isolated browser profiles on the Browser
          Profiles page, and watch or take over running sessions on Live Broadcast.
        </p>
        <div className="mt-3 flex flex-wrap gap-4 text-sm font-medium text-accent">
          <Link to="/browser-profiles">Browser Profiles →</Link>
          <Link to="/live">Live Broadcast →</Link>
          <Link to="/settings">Integrations in Settings →</Link>
        </div>
      </Card>

      <p className="flex items-start gap-2 px-1 text-xs text-slate-500">
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        The agent can only use tools that are enabled and authorized here.
      </p>
    </div>
  )
}
