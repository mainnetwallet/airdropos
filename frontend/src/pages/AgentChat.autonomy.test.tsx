/**
 * Agent Console autonomy tests.
 *
 * The panel is display-only: it must show what the backend policy says and must not
 * imply authority the backend did not grant. These tests stub `/agent/autonomy` with a
 * fixed policy and assert the console renders exactly that.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import AgentChat from './AgentChat'
import { agentChatStore } from '@/services/agentChatStore'
import type { AgentChatResponse, AutonomyPolicy } from '@/types'

function policy(over: Partial<AutonomyPolicy['policy']> = {}): AutonomyPolicy {
  return {
    policy: {
      enabled: false,
      source: 'AUTONOMOUS_POLICY',
      rules: [],
      value_moving: false,
      categories_known: ['web.fetch', 'tx.native_transfer'],
      note: 'Autonomy is deny-by-default.',
      ...over,
    },
    value_moving_enabled: false,
    emergency_stop: {},
    note: 'The Emergency Stop overrides every rule.',
  }
}

let autonomy: AutonomyPolicy
let chatResponse: AgentChatResponse

function stubFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url.includes('/agent/chat')) {
        return new Response(JSON.stringify(chatResponse), { status: 200 })
      }
      if (url.includes('/agent/autonomy')) {
        return new Response(JSON.stringify(autonomy), { status: 200 })
      }
      if (url.includes('/agent/info')) {
        return new Response(
          JSON.stringify({
            base_url: 'https://example.invalid/v1',
            model_id: 'fixed-model',
            temperature: 0.2,
            max_tokens: 4096,
            timeout_seconds: 60,
            max_retries: 3,
            api_key_configured: true,
            api_key_masked: 'sk-***-key',
            policy: 'single_fixed_model',
          }),
          { status: 200 },
        )
      }
      return new Response('{}', { status: 200 })
    }),
  )
}

function renderConsole() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AgentChat />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  agentChatStore.reset()
  autonomy = policy()
  chatResponse = {
    reply: '',
    tool_calls: [],
    task: null,
    approvals_required: [],
    blockers: [],
    needs_user_action: [],
    verified: [],
    usage: {},
    truncated: false,
  }
  stubFetch()
  Element.prototype.scrollIntoView = vi.fn()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  agentChatStore.reset()
})

describe('Agent Console autonomy panel', () => {
  it('says disabled and rule-free when no policy is configured', async () => {
    renderConsole()
    await screen.findByText(/autonomous execution/i)
    expect(screen.getByText('disabled')).toBeTruthy()
    expect(screen.getByText(/every operation is refused/i)).toBeTruthy()
  })

  it('lists the rules and marks value-moving capability', async () => {
    autonomy = policy({
      enabled: true,
      value_moving: true,
      source: 'data/autonomy.json',
      rules: [
        {
          rule_id: 'eth-transfers',
          category: 'tx.native_transfer',
          action: 'allow',
          max_value_usd: null,
          max_value_native: '0.05',
          allowed_networks: ['ethereum', 'base'],
          allowed_tokens: null,
          allowed_destinations: null,
          allowed_source_wallets: null,
          require_receipt: true,
          description: '',
        },
        {
          rule_id: 'reads',
          category: 'web.fetch',
          action: 'allow',
          max_value_usd: null,
          max_value_native: null,
          allowed_networks: null,
          allowed_tokens: null,
          allowed_destinations: null,
          allowed_source_wallets: null,
          require_receipt: true,
          description: '',
        },
      ],
    })

    renderConsole()
    await screen.findByText('enabled')
    expect(screen.getByText('can move funds')).toBeTruthy()
    expect(screen.getByText('tx.native_transfer')).toBeTruthy()
    expect(screen.getByText('eth-transfers')).toBeTruthy()
    // The ceiling is shown, so the operator can see the bound before trusting it.
    expect(screen.getByText('max 0.05')).toBeTruthy()
    expect(screen.getByText('2 rules from data/autonomy.json')).toBeTruthy()
    // And the interlock is stated, not implied.
    expect(screen.getByText(/emergency stop overrides every rule/i)).toBeTruthy()
  })

  it('renders an explicit deny rule as a deny, not as an allow', async () => {
    autonomy = policy({
      enabled: true,
      rules: [
        {
          rule_id: 'no-swaps',
          category: 'tx.token_transfer',
          action: 'deny',
          max_value_usd: null,
          max_value_native: null,
          allowed_networks: null,
          allowed_tokens: null,
          allowed_destinations: null,
          allowed_source_wallets: null,
          require_receipt: true,
          description: '',
        },
      ],
    })

    renderConsole()
    await screen.findByText('enabled')
    expect(screen.getByText('deny')).toBeTruthy()
    expect(screen.getByText('tx.token_transfer')).toBeTruthy()
    // Deny must not be dressed up as a capability.
    expect(screen.queryByText('can move funds')).toBeNull()
  })
})

describe('Agent Console autonomous transfer reporting', () => {
  it('reports a policy-authorized broadcast as a real hash, not as pending', async () => {
    chatResponse = {
      ...chatResponse,
      reply: 'Sent under the autonomous policy.',
      tool_calls: [
        {
          tool: 'approve_transfer',
          status: 'success',
          arguments: { network: 'ethereum' },
          data: {
            approval_id: 'apr-1',
            authorized_by: 'policy:autonomous:eth-transfers',
            authorized_by_policy: true,
            new_approval: true,
          },
          duration_ms: 3,
        },
        {
          tool: 'submit_prepared_transfer',
          status: 'success',
          arguments: { approval_id: 'apr-1' },
          data: { tx_hash: '0x' + '11'.repeat(32), status: 'CONFIRMED', value_wei: '10000000000000000' },
          duration_ms: 900,
          evidence: { authoritative: true, kind: 'chain_receipt', status: 'CONFIRMED' },
        },
      ],
    }

    renderConsole()
    const box = screen.getByLabelText(/message to the agent/i)
    const form = box.closest('form') as HTMLFormElement
    fireEvent.change(box, { target: { value: 'send 0.01 ETH' } })
    fireEvent.submit(form)

    await screen.findByText('submit_prepared_transfer')
    expect(screen.getAllByText('success').length).toBe(2)
    // The real hash from the backend, and proof it came from a receipt.
    expect(screen.getByText('0x' + '11'.repeat(32))).toBeTruthy()
    expect(screen.getByText(/evidence: chain_receipt/)).toBeTruthy()
  })

  it('shows an already-broadcast repeat as not resent', async () => {
    chatResponse = {
      ...chatResponse,
      reply: 'This exact transfer already went out.',
      tool_calls: [
        {
          tool: 'submit_prepared_transfer',
          status: 'success',
          arguments: { approval_id: 'apr-1' },
          data: {
            tx_hash: '0x' + '22'.repeat(32),
            status: 'ALREADY_BROADCAST',
            resent: false,
          },
          duration_ms: 4,
          evidence: { authoritative: false, kind: 'chain_receipt', status: 'ALREADY_BROADCAST' },
        },
      ],
    }

    renderConsole()
    const box = screen.getByLabelText(/message to the agent/i)
    fireEvent.change(box, { target: { value: 'send 0.01 ETH again' } })
    fireEvent.submit(box.closest('form') as HTMLFormElement)

    await screen.findByText(/already went out/i)
    expect(screen.getByText('0x' + '22'.repeat(32))).toBeTruthy()
  })

  it('shows a policy refusal with the exact constraint that refused it', async () => {
    chatResponse = {
      ...chatResponse,
      reply: 'The policy does not cover that amount.',
      tool_calls: [
        {
          tool: 'approve_transfer',
          status: 'needs_user_action',
          arguments: { network: 'ethereum' },
          data: {},
          error: 'Amount 0.01 exceeds the autonomous limit of 0.001 set by rule small.',
          error_category: 'HUMAN_INTERVENTION_REQUIRED',
          duration_ms: 1,
        },
      ],
      needs_user_action: ['Amount 0.01 exceeds the autonomous limit of 0.001 set by rule small.'],
    }

    renderConsole()
    const box = screen.getByLabelText(/message to the agent/i)
    fireEvent.change(box, { target: { value: 'send 0.01 ETH' } })
    fireEvent.submit(box.closest('form') as HTMLFormElement)

    await screen.findByText(/needs you:/i)
    // The specific limit, not a vague refusal.
    expect(
      screen.getAllByText(/exceeds the autonomous limit of 0\.001 set by rule small/i).length,
    ).toBeGreaterThan(0)
  })
})