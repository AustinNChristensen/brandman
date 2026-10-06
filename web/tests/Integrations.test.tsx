// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ onboarding: vi.fn(), readiness: vi.fn(), connectors: vi.fn(), connections: vi.fn(), health: vi.fn(), prepareLane: vi.fn(), connect: vi.fn(), disconnect: vi.fn(), checkHealth: vi.fn(), notify: vi.fn() }))
vi.mock('../src/api/endpoints', () => ({ integrations: mocks }))
vi.mock('../src/state/BrandContext', () => ({ useBrands: () => ({ selected: 'demo-brand', active: [{ id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }], brands: [{ id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }], select: vi.fn() }) }))
vi.mock('../src/state/Toast', () => ({ useToast: () => ({ notify: mocks.notify }) }))

import Integrations from '../src/pages/Integrations'

const lane = { lane: 'beehiiv_read', provider: 'beehiiv' as const, title: 'Beehiiv insights', purpose: 'Read newsletter posts and aggregate performance.', scopes: ['posts.read'], capabilities: ['posts.read', 'metrics.read'], credential_inputs: [{ key: 'api_key', label: 'API token', secret: true }], can_read: true, can_write: false, write_boundary: 'Cannot create, schedule, publish, or send newsletters.' }
const manifest = { encryption: { ready: true, required_for: 'Every standalone connection', operator_action: null }, modes: [{ mode: 'assisted' as const, title: 'Browser-assisted', available_now: true, credentials_stored: false, best_for: 'Starting now.', tradeoff: 'Helper required.' }, { mode: 'standalone' as const, title: 'Standalone API', available_now: true, credentials_stored: true, best_for: 'Scheduled reads.', tradeoff: 'API access required.' }], providers: { beehiiv: { authentication: 'API token', availability: 'Plan dependent.', lanes: [lane] }, x: { authentication: 'OAuth', availability: 'Developer account required.', lanes: [] } }, pricing: { source: 'customer_supplied_rate_card', vendor_prices_bundled: false, explanation: 'Customer supplied.' } }
const readiness = { brand: { id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }, generated_at: '2026-09-02T12:00:00Z', ready: false, summary: { checks_total: 4, checks_ready: 2, required_checks_total: 3, required_checks_ready: 2, code_ready_percent: 100, live_ready_percent: 66.7, connector_accounts_ready: 0, connector_accounts_total: 1 }, checks: [{ id: 'beehiiv_read', label: 'Beehiiv read', status: 'not_configured', code_ready: true, configured: false, healthy: false, account_connected: false, account_id: 'c1', missing_scopes: ['posts.read'], actions: ['Connect credentials for Beehiiv account pub_demo.'], detail: 'No account satisfies requirements.', required_for_live: false }] }
const account = { id: 'c1', brand_id: 'b1', connector_type: 'beehiiv' as const, account_key: 'pub_demo', display_name: 'Demo newsletter', status: 'disconnected', scopes: ['posts.read'], capabilities: ['posts.read', 'metrics.read'], configuration: { connection_role: 'beehiiv_read', delivery_mode: 'api' } }

beforeEach(() => {
  vi.clearAllMocks(); mocks.onboarding.mockResolvedValue(manifest); mocks.readiness.mockResolvedValue(readiness); mocks.connectors.mockResolvedValue([account]); mocks.connections.mockResolvedValue([]); mocks.health.mockResolvedValue([]); mocks.connect.mockResolvedValue({}); mocks.checkHealth.mockResolvedValue([]); mocks.disconnect.mockResolvedValue({})
})
afterEach(cleanup)

describe('Integrations', () => {
  it('shows mode tradeoffs, readiness delta, exact scopes, and supported recovery', async () => {
    render(<MemoryRouter><Integrations /></MemoryRouter>)
    expect(await screen.findByText('Browser-assisted')).toBeInTheDocument()
    expect(screen.getByText('Standalone API')).toBeInTheDocument()
    expect(screen.getByText('1', { selector: '.val' })).toBeInTheDocument()
    expect(screen.getAllByText('posts.read').length).toBeGreaterThan(0)
    expect(screen.getByText('Connect credentials for Beehiiv account pub_demo.')).toBeInTheDocument()
    expect(screen.getByText('secrets are write-only')).toBeInTheDocument()
  })

  it('keeps a reconnect secret masked and sends only the exact lane contract', async () => {
    render(<MemoryRouter><Integrations /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: 'Connect' }))
    const dialog = screen.getByRole('dialog'), secret = within(dialog).getByLabelText('API token')
    expect(secret).toHaveAttribute('type', 'password')
    fireEvent.change(secret, { target: { value: 'super-secret-token' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save credential' }))
    await waitFor(() => expect(mocks.connect).toHaveBeenCalledWith('demo-brand', lane, 'pub_demo', 'Demo newsletter', { api_key: 'super-secret-token' }))
    await waitFor(() => expect(screen.queryByDisplayValue('super-secret-token')).not.toBeInTheDocument())
  })

  it('queues a read-only health check instead of calling a provider inline', async () => {
    render(<MemoryRouter><Integrations /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: 'Check health' }))
    await waitFor(() => expect(mocks.checkHealth).toHaveBeenCalledWith('demo-brand', 'c1'))
    expect(mocks.notify).toHaveBeenCalledWith('Read-only health check queued.')
  })
})
