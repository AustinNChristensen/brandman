// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  get: vi.fn(), revisions: vi.fn(), history: vi.fn(), factCheck: vi.fn(), exportJobs: vi.fn(),
  providerReconciliations: vi.fn(), revise: vi.fn(), policyReview: vi.fn(), authorizeQuickHit: vi.fn(),
  transition: vi.fn(), recordFactCheck: vi.fn(), exportPreview: vi.fn(), exportDraft: vi.fn(),
  createDistributionPackage: vi.fn(), abandon: vi.fn(), archive: vi.fn(),
  context: vi.fn(), activeGuideline: vi.fn(), notify: vi.fn(),
  console: vi.fn(),
}))

vi.mock('../src/api/endpoints', () => ({
  newsletters: {
    get: mocks.get, revisions: mocks.revisions, history: mocks.history, factCheck: mocks.factCheck,
    exportJobs: mocks.exportJobs, providerReconciliations: mocks.providerReconciliations,
    revise: mocks.revise, policyReview: mocks.policyReview, authorizeQuickHit: mocks.authorizeQuickHit,
    transition: mocks.transition, recordFactCheck: mocks.recordFactCheck, exportPreview: mocks.exportPreview,
    exportDraft: mocks.exportDraft, createDistributionPackage: mocks.createDistributionPackage,
    abandon: mocks.abandon, archive: mocks.archive,
  },
  brands: { context: mocks.context, activeGuideline: mocks.activeGuideline },
  execution: { console: mocks.console },
}))
vi.mock('../src/state/BrandContext', () => ({ useBrands: () => ({
  selected: 'demo-brand', byId: () => ({ id: 'brand-1', slug: 'demo-brand', name: 'Demo Brand' }),
  brands: [], active: [], select: vi.fn(),
}) }))
vi.mock('../src/state/Toast', () => ({ useToast: () => ({ notify: mocks.notify }) }))

import ContentItem from '../src/pages/ContentItem'

const issue = {
  id: 'issue-1', brand_id: 'brand-1', candidate_id: null, lifecycle: 'approved', current_revision: 2,
  approved_revision: 2, approved_by: 'chris', approved_at: '2026-09-03T00:00:00Z',
  beehiiv_external_id: null, beehiiv_preview_url: null, scheduled_for: null, published_at: null,
  created_at: '2026-09-02T00:00:00Z', updated_at: '2026-09-03T00:00:00Z', approval_valid: true,
  governance: { reviewable: true, fact_check_valid: true, next_safe_action: 'Prepare a private draft.', blockers: [] },
  content: { id: 'revision-2', issue_id: 'issue-1', revision: 2, editorial_thesis: 'Explain the offer', target_reader: 'Readers', intended_outcome: 'Read the guide', working_title: '', final_title: 'Exact approved newsletter', subject: 'Exact subject', preview_text: 'Exact preview', sections: [{ heading: 'Lead', body: 'Hey, exact material.' }], cta: { label: 'Read', url: 'https://points.test' }, seo: { title: 'Exact', description: 'Description' }, content_basis: {}, claims: [], source_provenance: [{ source_id: 'source-1' }], change_note: null, created_by: 'chris', created_at: '2026-09-03T00:00:00Z' },
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.get.mockResolvedValue(issue); mocks.revisions.mockResolvedValue([issue.content]); mocks.history.mockResolvedValue([])
  mocks.factCheck.mockResolvedValue({ revision: 2, reviewer: 'chris', passed: true })
  mocks.exportJobs.mockResolvedValue([]); mocks.providerReconciliations.mockResolvedValue([])
  mocks.context.mockResolvedValue({ sources: [{ id: 'source-1', title: 'Issuer source' }] })
  mocks.activeGuideline.mockResolvedValue({ rules: { operator_checklist: ['one_thesis'] } })
  mocks.console.mockResolvedValue({ tasks: [], execution_agents: [], beehiiv_aggregate_pulls: [], safety: {} })
  mocks.revise.mockResolvedValue({}); mocks.exportPreview.mockResolvedValue({ payload_fingerprint: `sha256:${'a'.repeat(64)}` })
})
afterEach(cleanup)

const renderPage = () => render(<MemoryRouter initialEntries={['/content/newsletter/issue-1?brand=demo-brand']}><Routes><Route path="/content/newsletter/:id" element={<ContentItem />} /></Routes></MemoryRouter>)

describe('Newsletter completion', () => {
  it('shows draft-only completion controls and no direct send or publish action', async () => {
    renderPage()
    expect(await screen.findByText('Exact approved newsletter', { selector: 'h1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Build distribution package' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Preview exact Beehiiv export' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Queue private Beehiiv draft' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^(send|publish|schedule)$/i })).not.toBeInTheDocument()
  })

  it('creates a revision without accepting an actor from the browser', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Create revised material' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Title'), { target: { value: 'Corrected newsletter' } })
    fireEvent.change(within(dialog).getByLabelText('Why this changed'), { target: { value: 'Correct source-backed title' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create revision' }))
    await waitFor(() => expect(mocks.revise).toHaveBeenCalledWith('issue-1', {
      final_title: 'Corrected newsletter', subject: 'Exact subject', preview_text: 'Exact preview',
    }, 'Correct source-backed title'))
    expect(JSON.stringify(mocks.revise.mock.calls[0])).not.toContain('created_by')
  })
})
