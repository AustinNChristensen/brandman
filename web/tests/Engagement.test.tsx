// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ list: vi.fn(), get: vi.fn(), history: vi.fn(), draft: vi.fn(), submit: vi.fn(), dismiss: vi.fn(), notify: vi.fn() }))
vi.mock('../src/api/endpoints', () => ({ engagement: { list: mocks.list, get: mocks.get, history: mocks.history, draft: mocks.draft, submit: mocks.submit, dismiss: mocks.dismiss } }))
vi.mock('../src/state/BrandContext', () => ({ useBrands: () => ({
  selected: 'demo-brand', active: [{ id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }],
  brands: [{ id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }], select: vi.fn(),
}) }))
vi.mock('../src/state/Toast', () => ({ useToast: () => ({ notify: mocks.notify }) }))

import Engagement from '../src/pages/Engagement'

const opportunity = {
  id: 'o1', brand_id: 'b1', connector_account_id: 'x-read', event_identity: 'mention:1', external_post_id: '1',
  opportunity_type: 'mention', text: 'How should I use these credits?', author: { id: 'reader-1', username: 'reader' },
  thread_context: { external_url: 'https://x.com/reader/status/1', parent_context: [{ id: 'parent', text: 'Original context' }] },
  source_query: null, target_user_id: null, ranking_score: 80, ranking_reasons: ['direct question'], state: 'new',
  requires_approval: true, material_fingerprint: 'abc', dispatch_item_id: null, action_type: null, result: null,
  first_seen_at: '2026-09-03T12:00:00Z', last_seen_at: '2026-09-03T12:00:00Z', updated_at: '2026-09-03T12:00:00Z', resurfaced_count: 0,
}

beforeEach(() => {
  vi.clearAllMocks(); mocks.list.mockResolvedValue([opportunity]); mocks.get.mockResolvedValue(opportunity)
  mocks.history.mockResolvedValue([{ id: 'h1', opportunity_id: 'o1', action: 'ingested', actor: 'projector', detail: {}, created_at: '2026-09-03T12:00:00Z' }])
  mocks.draft.mockResolvedValue({ opportunity: { ...opportunity, state: 'drafted', dispatch_item_id: 'd1' }, dispatch_item: { id: 'd1', status: 'draft' } })
  mocks.submit.mockResolvedValue({ opportunity: { ...opportunity, state: 'awaiting_approval', dispatch_item_id: 'd1' }, dispatch_item: { id: 'd1', status: 'awaiting_approval', approval: null } })
  mocks.dismiss.mockResolvedValue({ ...opportunity, state: 'dismissed' })
})
afterEach(cleanup)

describe('Governed engagement inbox', () => {
  it('creates only a draft and never exposes direct external actions', async () => {
    render(<MemoryRouter><Engagement /></MemoryRouter>)
    expect(await screen.findAllByText('How should I use these credits?')).toHaveLength(2)
    fireEvent.click(screen.getByRole('button', { name: 'Draft governed action' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Reply draft'), { target: { value: 'Check the pricing page before changing plans.' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create draft only' }))
    await waitFor(() => expect(mocks.draft).toHaveBeenCalledWith('demo-brand', 'o1', 'reply', 'Check the pricing page before changing plans.'))
    expect(screen.queryByRole('button', { name: /^(send|publish|schedule|like|follow)$/i })).not.toBeInTheDocument()
  })

  it('submits an existing draft for approval and attributes no actor in browser payload', async () => {
    const drafted = { ...opportunity, state: 'drafted', dispatch_item_id: 'd1', action_type: 'reply' }
    mocks.list.mockResolvedValue([drafted]); mocks.get.mockResolvedValue(drafted)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<MemoryRouter><Engagement /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: 'Submit for approval' }))
    await waitFor(() => expect(mocks.submit).toHaveBeenCalledWith('demo-brand', 'o1'))
  })

  it('fails safely on network errors', async () => {
    mocks.list.mockRejectedValueOnce(new Error('network unavailable'))
    render(<MemoryRouter><Engagement /></MemoryRouter>)
    expect(await screen.findByText('network unavailable')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })
})
