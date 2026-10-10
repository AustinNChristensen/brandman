// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

const brand = { id: 'b1', slug: 'demo-brand', name: 'Demo Brand', approval_policy: 'human_required' }
const item = { id: 'draft-1', brand_id: 'b1', connector: 'x', payload: { body: 'Awaiting text' }, status: 'awaiting_approval', revision: 1, canonical_post_id: null, approval: null, external_id: null, external_url: null, attempt_count: 0, last_error: null, updated_at: '2026-09-03T12:00:00Z' }
const workspace = [{ brand, awaiting: [item], newsletters: [], usage: null }]
vi.mock('../src/api/endpoints', () => ({
  dispatch: { validation: vi.fn().mockResolvedValue({ connector: 'x', valid: true, effective_length: 13, errors: [] }), audit: vi.fn().mockResolvedValue([]), approvalScope: vi.fn().mockResolvedValue(null), approve: vi.fn(), reject: vi.fn() },
  newsletters: {},
}))
vi.mock('../src/state/BrandContext', () => ({ useBrands: () => ({ active: [brand], selected: null, brands: [brand], loading: false, error: null, select: vi.fn(), bySlug: vi.fn(), byId: vi.fn() }) }))
vi.mock('../src/state/Toast', () => ({ useToast: () => ({ notify: vi.fn() }) }))
vi.mock('../src/state/useWorkspace', () => ({
  useWorkspace: () => ({ data: workspace, loading: false, error: null, reload: vi.fn() }),
  metersFrom: () => undefined,
}))

import Approvals from '../src/pages/Approvals'

afterEach(cleanup)

describe('Approvals edit guidance', () => {
  it('links to the editable Social drafts tab instead of API/MCP-only instructions', async () => {
    render(<MemoryRouter initialEntries={['/approvals/dispatch/draft-1']}><Approvals /></MemoryRouter>)
    const link = await screen.findByRole('link', { name: 'Edit this draft in Content' })
    expect(link).toHaveAttribute('href', '/content?brand=demo-brand&tab=drafts')
    expect(document.body.textContent).not.toMatch(/API or MCP/)
  })
})
