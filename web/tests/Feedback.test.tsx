// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ list: vi.fn(), get: vi.fn(), history: vi.fn(), comment: vi.fn(), start: vi.fn(), resolve: vi.fn(), verify: vi.fn(), reopen: vi.fn(), notify: vi.fn() }))
vi.mock('../src/api/endpoints', () => ({ productFeedback: api }))
vi.mock('../src/state/BrandContext', () => ({ useBrands: () => ({ active: [{ id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }], brands: [{ id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }], selected: 'demo-brand', select: vi.fn() }) }))
vi.mock('../src/state/Toast', () => ({ useToast: () => ({ notify: api.notify }) }))
import Feedback from '../src/pages/Feedback'

const base = { id: 'feedback-1', brand_id: 'b1', reporter: 'demo-brand-agent', summary: 'Agent handoff failed', details: 'No safe recovery appeared.', status: 'open', component: 'execution', severity: 'high', fingerprint: 'fp', reproduction: 'Open handoff', expected_behavior: 'Recovery appears', actual_behavior: 'No recovery', workaround: '', related_ids: ['task-1'], first_seen_at: '2026-09-03T10:00:00Z', last_seen_at: '2026-09-03T11:00:00Z', occurrence_count: 2, updated_at: '2026-09-03T11:00:00Z', assignee: null, implementation_links: [], implementation_notes: '', resolution_evidence: '', resolved_by: null, resolved_at: null, verified_by: null, verified_at: null, comments: [], history: [{ sequence: 1, feedback_id: 'feedback-1', action: 'reported', actor: 'demo-brand-agent', at: '2026-09-03T10:00:00Z', from_status: null, to_status: 'open', details: {} }], implementation_matches: [] }

beforeEach(() => { vi.clearAllMocks(); api.list.mockResolvedValue([base]); api.get.mockResolvedValue(base); api.history.mockResolvedValue(base.history); api.comment.mockResolvedValue({}); api.start.mockResolvedValue({}); api.resolve.mockResolvedValue({}); api.verify.mockResolvedValue({}); api.reopen.mockResolvedValue({}) })
afterEach(cleanup)

describe('Product feedback', () => {
  it('shows agent-reported evidence and records comments/start through brand-scoped operator actions', async () => {
    render(<MemoryRouter initialEntries={['/feedback']}><Feedback /></MemoryRouter>)
    expect(await screen.findByText('Agent handoff failed')).toBeInTheDocument(); expect(screen.getAllByText(/demo-brand-agent/).length).toBeGreaterThan(0); expect(screen.getByText(/2 occurrences/)).toBeInTheDocument()
    fireEvent.change(await screen.findByLabelText('Comment'), { target: { value: 'Operator reproduced this.' } }); fireEvent.click(screen.getByRole('button', { name: 'Comment' }))
    await waitFor(() => expect(api.comment).toHaveBeenCalledWith('demo-brand', 'feedback-1', 'Operator reproduced this.'))
    fireEvent.click(screen.getByRole('button', { name: 'Start work' })); const dialog = screen.getByRole('dialog'); fireEvent.change(within(dialog).getByLabelText('Assignee'), { target: { value: 'builder' } }); fireEvent.click(within(dialog).getByRole('button', { name: 'Start work' }))
    await waitFor(() => expect(api.start).toHaveBeenCalledWith('demo-brand', 'feedback-1', expect.objectContaining({ assignee: 'builder' })))
    expect(JSON.stringify([api.comment.mock.calls, api.start.mock.calls])).not.toContain('actor')
  })

  it.each([
    ['in_progress', 'Resolve with evidence', 'Resolution evidence', 'Resolution proof', 'resolve'],
    ['resolved', 'Verify outcome', 'Verification evidence', 'Verification proof', 'verify'],
    ['verified', 'Reopen', 'Reason', 'Failure recurred', 'reopen'],
  ] as const)('wires the %s lifecycle action', async (status, button, field, value, method) => {
    const item = { ...base, status }; api.list.mockResolvedValue([item]); api.get.mockResolvedValue(item)
    render(<MemoryRouter><Feedback /></MemoryRouter>)
    if (status === 'resolved' || status === 'verified') fireEvent.change(await screen.findByLabelText('Feedback status'), { target: { value: 'all' } })
    fireEvent.click(await screen.findByRole('button', { name: button }))
    const dialog = screen.getByRole('dialog'); fireEvent.change(within(dialog).getByLabelText(field), { target: { value } }); fireEvent.click(within(dialog).getByRole('button', { name: button === 'Resolve with evidence' ? 'Resolve' : button === 'Verify outcome' ? 'Verify' : 'Reopen' }))
    await waitFor(() => expect(api[method]).toHaveBeenCalled())
  })
})
