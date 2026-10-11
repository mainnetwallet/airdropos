/** AI Agent Console: natural-language chat with the central agent. */

import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Send, ShieldCheck } from 'lucide-react'
import { api } from '@/services/api'
import { Alert, Card } from '@/components/ui'

interface Message {
  id: string
  role: 'user' | 'agent'
  content: string
  at: string
}

export default function AgentChat() {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')

  const { data: info } = useQuery({ queryKey: ['agent-info'], queryFn: api.agentInfo })

  const send = useMutation({
    mutationFn: (message: string) => api.agentChat(message),
    onSuccess: (response) => {
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: 'agent',
          content: response.reply,
          at: new Date().toISOString(),
        },
      ])
    },
    onError: (error: Error) => {
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: 'agent',
          content: `Request failed: ${error.message}`,
          at: new Date().toISOString(),
        },
      ])
    },
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    const text = input.trim()
    if (!text || send.isPending) return

    setMessages((prev) => [
      ...prev,
      { id: crypto.randomUUID(), role: 'user', content: text, at: new Date().toISOString() },
    ])
    setInput('')
    send.mutate(text)
  }

  return (
    <div className="flex h-full flex-col gap-4">
      <header>
        <h1 className="text-2xl font-bold text-slate-100">AI Agent Console</h1>
        <p className="mt-1 text-sm text-slate-400">
          Natural-language control of the central autonomous agent
        </p>
      </header>

      {info && (
        <Alert kind="info" title="Single fixed model">
          All reasoning runs on <span className="font-mono">{info.model_id}</span> at{' '}
          <span className="font-mono">{info.base_url}</span>. There is no model switching, fallback,
          or routing. API key is configured but never returned to the browser.
        </Alert>
      )}

      <Card className="flex min-h-0 flex-1 flex-col">
        <div className="flex-1 space-y-3 overflow-y-auto pb-4">
          {messages.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center gap-2 text-center">
              <ShieldCheck className="h-10 w-10 text-slate-600" />
              <p className="font-medium text-slate-300">Ask the agent to do something</p>
              <p className="max-w-md text-sm text-slate-500">
                e.g. “Discover legitimate airdrops and research eligibility for my configured
                wallets”, “Show me the status of AIR-2026-000001”, or “Report today's activity”.
              </p>
            </div>
          ) : (
            messages.map((m) => (
              <div
                key={m.id}
                className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}
              >
                <div
                  className={`max-w-[80%] whitespace-pre-wrap rounded-2xl px-4 py-2.5 text-sm ${
                    m.role === 'user'
                      ? 'bg-accent text-white'
                      : 'bg-surface-200 text-slate-200'
                  }`}
                >
                  {m.content}
                </div>
              </div>
            ))
          )}
          {send.isPending && (
            <div className="flex justify-start">
              <div className="rounded-2xl bg-surface-200 px-4 py-2.5">
                <span className="animate-pulse text-sm text-slate-400">Thinking…</span>
              </div>
            </div>
          )}
        </div>

        <form onSubmit={handleSubmit} className="flex gap-2 border-t border-surface-300 pt-3">
          <input
            className="input flex-1"
            placeholder="Send a message to the agent…"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            disabled={send.isPending}
          />
          <button type="submit" className="btn-primary" disabled={!input.trim() || send.isPending}>
            <Send className="h-4 w-4" />
            Send
          </button>
        </form>
      </Card>
    </div>
  )
}
