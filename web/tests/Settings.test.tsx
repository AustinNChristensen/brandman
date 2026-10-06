// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ get: vi.fn(), update: vi.fn(), addRateCard: vi.fn(), setSchedule: vi.fn(), tick: vi.fn(), notify: vi.fn() }))
vi.mock('../src/api/endpoints', () => ({ settings: mocks }))
vi.mock('../src/state/BrandContext', () => ({ useBrands: () => ({ selected: 'demo-brand', active: [{ id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }], brands: [{ id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }], select: vi.fn() }) }))
vi.mock('../src/state/Toast', () => ({ useToast: () => ({ notify: mocks.notify }) }))

import Settings from '../src/pages/Settings'

const data = {
  brand: { id: 'b1', slug: 'demo-brand', name: 'Demo Brand', mission: 'Make points practical.', voice: 'Clear and specific.', compliance_rules: 'Verify material claims.', approval_policy: 'human_approval_required', created_at: '', updated_at: '' },
  audit: [{ sequence: 1, brand_id: 'b1', actor: 'chris', reason: 'Clarify mission', before: {}, after: {}, at: '2026-09-03T15:00:00Z' }],
  rate_cards: [{ id: 'r1', brand_id: 'b1', version: 'customer-2026', provider: 'beehiiv', method: 'GET', endpoint_pattern: '/v2/*', billable_category: 'read', unit_name: 'request', unit_price: '0.002', currency: 'USD', effective_at: '2026-09-03T00:00:00Z', configured_by: 'chris', created_at: '' }],
  orchestration: { as_of: '2026-09-03T15:00:00Z', schedules: { total: 1, enabled: 1, due: 1 }, pending_decisions: 0, latest_tick: null },
  schedules: [{ schedule_key: 'connector/bee-1/posts', brand_id: 'b1', connector_account_id: 'bee-1', name: 'Sync newsletter metadata', action_type: 'connector.sync', interval_seconds: 900, enabled: true, next_run_at: '2026-09-03T15:00:00Z', last_run_at: null, last_decision: null, payload: {} }],
}

beforeEach(() => { vi.clearAllMocks(); mocks.get.mockResolvedValue(data); mocks.update.mockResolvedValue(data.brand); mocks.addRateCard.mockResolvedValue({}); mocks.setSchedule.mockResolvedValue({}); mocks.tick.mockResolvedValue({}) })
afterEach(cleanup)

describe('Settings', () => {
  it('saves complete governed brand settings with an explicit audit reason', async () => {
    render(<MemoryRouter><Settings /></MemoryRouter>)
    expect(await screen.findByDisplayValue('Make points practical.')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Mission'), { target: { value: 'Make rewards simpler.' } })
    fireEvent.change(screen.getByLabelText('Reason for this change'), { target: { value: 'Tighten the mission statement' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save governed settings' }))
    await waitFor(() => expect(mocks.update).toHaveBeenCalledWith('demo-brand', {
      mission: 'Make rewards simpler.', voice: 'Clear and specific.', compliance_rules: 'Verify material claims.',
      approval_policy: 'human_approval_required', reason: 'Tighten the mission statement',
    }))
  })

  it('adds a customer-supplied rate and never exposes secret or provider purchase controls', async () => {
    render(<MemoryRouter><Settings /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: 'Add rate' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Unit price'), { target: { value: '0.003' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add rate' }))
    await waitFor(() => expect(mocks.addRateCard).toHaveBeenCalledWith('demo-brand', expect.objectContaining({ operation: 'beehiiv_read', unit_price: '0.003', currency: 'USD' })))
    expect(screen.queryByLabelText(/api key|secret|token/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /buy|purchase|publish|send/i })).not.toBeInTheDocument()
  })

  it('uses only brand-scoped schedule and bounded tick controls', async () => {
    render(<MemoryRouter><Settings /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: 'Disable schedule' }))
    await waitFor(() => expect(mocks.setSchedule).toHaveBeenCalledWith('demo-brand', 'connector/bee-1/posts', false))
    fireEvent.change(screen.getByLabelText('Decision limit'), { target: { value: '500' } })
    fireEvent.click(screen.getByRole('button', { name: 'Run bounded tick' }))
    await waitFor(() => expect(mocks.tick).toHaveBeenCalledWith('demo-brand', 50))
    expect(screen.getByText(/does not call a provider/)).toBeInTheDocument()
  })
})
