/** Security regression tests for the Wallet Manager UI (secret handling, watch-only). */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import Wallets from './Wallets'
import type { WalletView } from '@/types'

const SECRET = '0x' + '11'.repeat(32)

const wallet = (over: Partial<WalletView>): WalletView => ({
  id: 'w-1',
  label: 'Signer',
  address: '0x' + 'aa'.repeat(20),
  chain_type: 'EVM',
  derivation_path: null,
  supported_networks: ['ethereum'],
  is_active: true,
  is_locked: false,
  has_key_material: true,
  can_sign: true,
  total_spent_usd: 0,
  notes: null,
  created_at: null,
  ...over,
})

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <Wallets />
    </QueryClientProvider>,
  )
  return client
}

/** The header toggle and the form's submit share the label "Import"; the form comes last. */
function clickSubmit() {
  const buttons = screen.getAllByRole('button', { name: /^import$/i })
  fireEvent.click(buttons[buttons.length - 1])
}

let wallets: WalletView[]
let requests: Array<{ url: string; method: string; body?: string }>

beforeEach(() => {
  wallets = []
  requests = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      requests.push({ url, method, body: init?.body as string | undefined })
      if (method === 'POST' && url.endsWith('/wallets/import')) {
        const created = wallet({ id: 'w-new', label: 'Imported' })
        wallets = [created]
        return new Response(JSON.stringify(created), { status: 201 })
      }
      return new Response(JSON.stringify({ wallets }), { status: 200 })
    }),
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('Wallet Manager secret handling', () => {
  it('masks the secret input and never persists or retains the secret after import', async () => {
    const logs = [
      vi.spyOn(console, 'log').mockImplementation(() => {}),
      vi.spyOn(console, 'error').mockImplementation(() => {}),
      vi.spyOn(console, 'warn').mockImplementation(() => {}),
    ]
    const client = renderPage()
    await screen.findByText('No wallets configured')

    fireEvent.click(screen.getByRole('button', { name: /^import$/i }))
    fireEvent.change(screen.getByLabelText(/^label/i), { target: { value: 'Imported' } })
    const secretInput = screen.getByLabelText(/private key/i) as HTMLInputElement
    expect(secretInput.type).toBe('password')
    expect(secretInput.autocomplete).toBe('off')
    fireEvent.change(secretInput, { target: { value: SECRET } })
    clickSubmit()

    await waitFor(() => expect(requests.some((r) => r.url.endsWith('/wallets/import'))).toBe(true))

    // The secret is sent exactly once, in the request body only.
    const withSecret = requests.filter((r) => r.body?.includes(SECRET))
    expect(withSecret).toHaveLength(1)
    expect(withSecret[0].url.includes(SECRET)).toBe(false)

    await screen.findByText('Imported')
    await waitFor(() => expect(screen.queryByLabelText(/private key/i)).toBeNull())

    // Not in the DOM, browser storage, or the react-query caches.
    expect(document.body.innerHTML).not.toContain(SECRET)
    expect(JSON.stringify({ ...localStorage })).not.toContain(SECRET)
    expect(JSON.stringify({ ...sessionStorage })).not.toContain(SECRET)
    expect(JSON.stringify(client.getQueryCache().getAll().map((q) => q.state.data))).not.toContain(SECRET)
    expect(JSON.stringify(client.getMutationCache().getAll())).not.toContain(SECRET)
    for (const spy of logs) {
      expect(JSON.stringify(spy.mock.calls)).not.toContain(SECRET)
    }
  })

  it('clears the secret field even when the import is rejected', async () => {
    const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
    fetchMock.mockImplementation(
      async (url: string, init?: RequestInit) => {
        if (init?.method === 'POST') {
          return new Response(JSON.stringify({ detail: 'The supplied key could not be used.' }), {
            status: 400,
          })
        }
        return new Response(JSON.stringify({ wallets: [] }), { status: 200 })
      },
    )
    renderPage()
    await screen.findByText('No wallets configured')
    fireEvent.click(screen.getByRole('button', { name: /^import$/i }))
    fireEvent.change(screen.getByLabelText(/^label/i), { target: { value: 'Bad' } })
    fireEvent.change(screen.getByLabelText(/private key/i), { target: { value: SECRET } })
    clickSubmit()

    await screen.findByText('The supplied key could not be used.')
    expect((screen.getByLabelText(/private key/i) as HTMLInputElement).value).toBe('')
    expect(document.body.innerHTML).not.toContain(SECRET)
  })
})

describe('Wallet Manager watch-only wallets', () => {
  it('labels watch-only wallets and offers no lock/unlock control', async () => {
    wallets = [
      wallet({ id: 'w-watch', label: 'Watcher', has_key_material: false, can_sign: false }),
      wallet({ id: 'w-key', label: 'Keyed' }),
    ]
    renderPage()
    await screen.findByText('Watcher')

    expect(screen.getAllByText('watch-only').length).toBeGreaterThan(0)
    // Only the keyed wallet can be locked; the watch-only one has no signing controls.
    expect(screen.getAllByRole('button', { name: /^lock$/i })).toHaveLength(1)
  })
})
