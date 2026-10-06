// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { consoleMock, controlsMock } = vi.hoisted(() => ({
  consoleMock: vi.fn(), controlsMock: vi.fn(),
}))

vi.mock('../src/state/BrandContext', () => ({
  useBrands: () => ({ selected: 'demo-brand', brands: [], active: [], select: vi.fn() }),
}))
vi.mock('../src/api/endpoints', () => ({
  execution: {
    console: consoleMock, controls: controlsMock,
    setControl: vi.fn(), bindDestination: vi.fn(), claim: vi.fn(), confirmX: vi.fn(),
    beehiivManifest: vi.fn(), begin: vi.fn(), receipt: vi.fn(), audit: vi.fn(),
  },
}))

import Execution from '../src/pages/Execution'
import { ToastProvider } from '../src/state/Toast'

beforeEach(() => {
  controlsMock.mockResolvedValue({ controls: [
    { provider: 'all', enabled: true, effective_enabled: true },
    { provider: 'beehiiv', enabled: true, effective_enabled: true },
    { provider: 'x', enabled: true, effective_enabled: true },
  ], audit: [] })
  consoleMock.mockResolvedValue({
    schema_version: 1,
    tasks: [{
      id: 'task-x-1', brand_id: 'brand-1', provider: 'x', resource_type: 'dispatch_item',
      resource_id: 'dispatch-1', revision: 4, connector_account_id: 'account-x',
      execution_payload: { body: 'Exact approved Demo Brand post' },
      material_fingerprint: `sha256:${'a'.repeat(64)}`, status: 'pending', claimed_by: null,
      claim_expires_at: null, receipt_external_id: null, receipt_external_url: null,
      receipt_status: null, receipt_recorded_at: null, external_action_started_at: null,
      destination_options: [], destination_binding_required: false, action_boundary: 'not_started',
      confirmation_current: false, public_action_confirmation_expires_at: null,
      receipt_requirements: { status: 'posted', external_id: 'X post ID', external_url: 'Canonical x.com status URL' },
      operator_next_action: { code: 'claim_in_execution_helper', text: 'Claim in a fresh helper.' },
    }],
    execution_agents: [{ brand_id: 'brand-1', agent_id: 'x-helper', channel: 'x', enabled: true, last_heartbeat_at: null, ready_to_claim: false }],
    beehiiv_aggregate_pulls: [],
    safety: { provider_write_performed: false, claim_grants_approval: false, receipt_reconciles_existing_result: true, subscriber_data_allowed: false },
  })
})

describe('Execution console safety', () => {
  it('shows exact material and blocks claiming when no matching agent is fresh', async () => {
    render(<MemoryRouter><ToastProvider><Execution /></ToastProvider></MemoryRouter>)
    expect(await screen.findByText('Exact approved Demo Brand post')).toBeInTheDocument()
    expect(screen.getByText('No matching agent has a fresh heartbeat.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Claim exact task' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: /^publish$/i })).not.toBeInTheDocument()
    await waitFor(() => expect(consoleMock).toHaveBeenCalledWith('demo-brand'))
  })
})
