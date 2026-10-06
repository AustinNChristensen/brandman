// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  readiness: vi.fn(), console: vi.fn(), controls: vi.fn(), configure: vi.fn(),
  heartbeat: vi.fn(), setControl: vi.fn(), notify: vi.fn(),
}))

vi.mock('../src/api/endpoints', () => ({
  agents: { readiness: mocks.readiness, configure: mocks.configure, heartbeat: mocks.heartbeat },
  execution: { console: mocks.console, controls: mocks.controls, setControl: mocks.setControl },
}))
vi.mock('../src/state/BrandContext', () => ({
  useBrands: () => ({
    active: [{ id: 'brand-1', slug: 'demo-brand', name: 'Demo Brand', mission: '', voice: '',
      compliance_rules: '', approval_policy: 'human_approval_required', created_at: '', updated_at: '' }],
    brands: [{ id: 'brand-1', slug: 'demo-brand', name: 'Demo Brand' }], selected: 'demo-brand', select: vi.fn(),
  }),
}))
vi.mock('../src/state/Toast', () => ({ useToast: () => ({ notify: mocks.notify }) }))

import Agents from '../src/pages/Agents'

afterEach(cleanup)

beforeEach(() => {
  vi.clearAllMocks()
  mocks.readiness.mockResolvedValue({
    brand: { id: 'brand-1', slug: 'demo-brand', name: 'Demo Brand' }, generated_at: '2026-09-03T12:00:00Z', ready: false,
    summary: { checks_total: 3, checks_ready: 2, required_checks_total: 2, required_checks_ready: 1,
      code_ready_percent: 100, live_ready_percent: 50, connector_accounts_ready: 0, connector_accounts_total: 5 },
    checks: [
      { id: 'preview_auth', label: 'Preview authentication', status: 'ready', code_ready: true,
        configured: true, healthy: true, detail: 'Code and boundary are ready.', actions: [] },
      { id: 'assisted_execution', label: 'Browser/MCP-assisted execution', status: 'blocked', code_ready: true,
        configured: true, healthy: false, detail: 'Provider receipt proof is incomplete.',
        actions: ['Complete one exact-revision handoff.'] },
    ],
  })
  mocks.console.mockResolvedValue({ schema_version: 1,
    execution_agents: [{ brand_id: 'brand-1', agent_id: 'browser-one', channel: 'browser', enabled: true,
      last_heartbeat_at: null, ready_to_claim: false }],
    tasks: [{ id: 'task-1', provider: 'x', status: 'needs_attention', resource_type: 'dispatch_item',
      resource_id: 'dispatch-1', connector_account_id: 'x-1',
      operator_next_action: { code: 'record_provider_receipt', text: 'Do not repeat the provider action. Record its existing receipt.' } }],
    beehiiv_aggregate_pulls: [], safety: { provider_write_performed: false, claim_grants_approval: false,
      receipt_reconciles_existing_result: true, subscriber_data_allowed: false } })
  mocks.controls.mockResolvedValue({ controls: [
    { brand_id: 'brand-1', provider: 'all', enabled: true, effective_enabled: true, updated_by: null, updated_at: null },
    { brand_id: 'brand-1', provider: 'x', enabled: true, effective_enabled: true, updated_by: null, updated_at: null },
  ], audit: [] })
  mocks.configure.mockResolvedValue({})
  mocks.heartbeat.mockResolvedValue({})
  mocks.setControl.mockResolvedValue({})
})

describe('Agents and live readiness', () => {
  it('separates code readiness from provider proof and renders safe recovery guidance', async () => {
    render(<MemoryRouter><Agents /></MemoryRouter>)
    expect(await screen.findByText('100%')).toBeInTheDocument()
    expect(screen.getByText('50%')).toBeInTheDocument()
    expect(screen.getAllByText('Provider receipt proof is incomplete.')).toHaveLength(2)
    expect(screen.getByText(/Do not repeat the provider action/)).toBeInTheDocument()
    expect(screen.getByText('heartbeat needed')).toBeInTheDocument()
  })

  it('uses only supported configure, heartbeat, and provider-control mutations', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<MemoryRouter><Agents /></MemoryRouter>)
    await screen.findByText('browser-one')

    fireEvent.click(screen.getByRole('button', { name: /Heartbeat/ }))
    await waitFor(() => expect(mocks.heartbeat).toHaveBeenCalledWith('demo-brand', 'browser-one'))

    fireEvent.change(screen.getByLabelText('Agent ID for Demo Brand'), { target: { value: 'browser-two' } })
    fireEvent.click(screen.getByRole('button', { name: 'Configure & enable' }))
    await waitFor(() => expect(mocks.configure).toHaveBeenCalledWith('demo-brand', 'browser-two', 'browser', true))

    const disableButtons = screen.getAllByRole('button', { name: 'Disable' })
    fireEvent.click(disableButtons.at(-1)!)
    await waitFor(() => expect(mocks.setControl).toHaveBeenCalledWith('demo-brand', 'x', false))
  })
})
