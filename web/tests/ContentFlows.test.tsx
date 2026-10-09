// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  listDrafts: vi.fn(), createDraft: vi.fn(), editDraft: vi.fn(), submitDraft: vi.fn(),
  validateDraft: vi.fn(), auditDraft: vi.fn(), listCandidates: vi.fn(), createCandidate: vi.fn(), abandon: vi.fn(), archive: vi.fn(), history: vi.fn(), promote: vi.fn(),
  listCampaigns: vi.fn(), listPosts: vi.fn(), createPost: vi.fn(), editPost: vi.fn(), auditPost: vi.fn(), createPostDispatch: vi.fn(),
  createNewsletter: vi.fn(), notify: vi.fn(),
  previewProposal: vi.fn(), confirmProposal: vi.fn(), workspaceData: [] as unknown[],
}))
vi.mock('../src/api/endpoints', () => ({
  dispatch: { list: api.listDrafts, create: api.createDraft, edit: api.editDraft, submit: api.submitDraft, validation: api.validateDraft, audit: api.auditDraft },
  editorialCandidates: { list: api.listCandidates, create: api.createCandidate, abandon: api.abandon, archive: api.archive, history: api.history, promoteToCampaignPost: api.promote },
  campaignGraphs: { list: api.listCampaigns },
  campaignPosts: { list: api.listPosts, create: api.createPost, edit: api.editPost, audit: api.auditPost, createDispatch: api.createPostDispatch },
  newsletters: { create: api.createNewsletter },
  operatorProposals: { preview: api.previewProposal, confirm: api.confirmProposal },
}))
vi.mock('../src/state/BrandContext', () => ({ useBrands: () => ({
  active: [{ id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }], selected: 'demo-brand',
  brands: [{ id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }], loading: false, error: null,
  select: vi.fn(), bySlug: vi.fn(), byId: vi.fn(),
}) }))
vi.mock('../src/state/Toast', () => ({ useToast: () => ({ notify: api.notify }) }))
vi.mock('../src/state/useWorkspace', () => ({
  useWorkspace: () => ({ data: api.workspaceData, loading: false, error: null, reload: vi.fn() }),
  metersFrom: () => undefined,
}))

import Content from '../src/pages/Content'

const rejected = { id: 'draft-1', brand_id: 'b1', connector: 'x', payload: { body: 'Rejected text' }, status: 'rejected', revision: 2, canonical_post_id: null, approval: null, external_id: null, external_url: null, attempt_count: 0, last_error: null, updated_at: '2026-09-03T12:00:00Z' }
const candidate = { id: 'candidate-1', brand_id: 'b1', duplicate_identity: 'dupe', title: 'Price drop', summary: 'Help readers decide', recommended_treatment: 'newsletter_and_social', score: 8.5, scoring_inputs: {}, rationale: [], supporting_sources: [{ source_id: 'source-1', url: 'https://issuer.test' }], publisher_name: '', cluster_key: '', intelligence: {}, status: 'open', created_at: '', updated_at: '' }
const campaign = { id: 'campaign-1', brand_id: 'b1', source_id: null, name: 'Pricing campaign', objective: 'Help readers decide', status: 'draft', created_at: '', memberships: [], relationships: [], flights: [], primary_anchor: null }
const post = { id: 'post-1', campaign_id: 'campaign-1', brand_id: 'b1', candidate_id: null, created_by: 'preview-operator', channel: 'x', body: 'Canonical draft', status: 'draft', scheduled_for: null, external_post_id: null, revision: 1, created_at: '', updated_at: '' }

beforeEach(() => {
  vi.clearAllMocks(); api.workspaceData = [{ brand: { id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }, newsletters: [], calendar: [{ item_type: 'source', id: 'source-1', title: 'Official terms', body_summary: 'Bonus ends Friday', channel: 'manual', status: 'published', scheduled_for: null }], awaiting: [], usage: null }]; api.listDrafts.mockResolvedValue([rejected]); api.listCandidates.mockResolvedValue([candidate])
  api.listCampaigns.mockResolvedValue([campaign]); api.listPosts.mockResolvedValue([post]); api.createPost.mockResolvedValue(post); api.editPost.mockResolvedValue({ ...post, revision: 2 }); api.createPostDispatch.mockResolvedValue({}); api.validateDraft.mockResolvedValue({ connector: 'x', valid: true, effective_length: 13, errors: [] }); api.auditDraft.mockResolvedValue([{ item_id: 'draft-1', action: 'created', actor: 'preview-operator', at: '2026-09-03T12:00:00Z', revision: 1, detail: null }]); api.promote.mockResolvedValue({ campaign, post, candidate: { ...candidate, status: 'selected' } })
  api.createDraft.mockResolvedValue({ ...rejected, id: 'new-draft', status: 'draft', revision: 1 }); api.editDraft.mockResolvedValue({ ...rejected, status: 'draft', revision: 3 }); api.submitDraft.mockResolvedValue({}); api.createCandidate.mockResolvedValue(candidate); api.createNewsletter.mockResolvedValue({})
  api.history.mockResolvedValue([{ id: 'event-1', action: 'abandoned', actor: 'preview-operator', from_state: 'open', to_state: 'abandoned', reason: 'No longer timely', created_at: '2026-09-03T12:00:00Z' }])
  const proposal = { id: 'proposal-1', brand_id: 'b1', command: 'Build decision support', status: 'previewed', evidence_fingerprint: 'sha256:evidence', guideline_id: 'g1', guideline_version_id: 'gv1', guideline_fingerprint: 'sha256:guideline', created_by: 'preview-operator', created_at: '', confirmed_by: null, confirmed_at: null, evidence: [{ kind: 'source', id: 'source-1', title: 'Official terms', summary: 'Bonus ends Friday' }], guideline: { id: 'g1', version_id: 'gv1', version: 1, name: 'House style', content_fingerprint: 'sha256:guideline' }, preview: { campaign: { name: 'Official terms', objective: 'Build decision support', status: 'draft' }, newsletter: { subject: 'Official terms', editorial_thesis: 'Build decision support', source_provenance: [{ source_id: 'source-1' }] }, x_draft: { body: 'Official terms: Bonus ends Friday', status: 'draft' } }, result: null }
  api.previewProposal.mockResolvedValue(proposal); api.confirmProposal.mockResolvedValue({ ...proposal, status: 'confirmed', confirmed_by: 'preview-operator', result: { campaign_id: 'campaign-1', newsletter_issue_id: 'issue-1', x_post_id: 'post-1' } })
})
afterEach(cleanup)

describe('Content authoring flows', () => {
  it('creates a social draft and submits the exact returned draft without actor or provider controls', async () => {
    render(<MemoryRouter><Content /></MemoryRouter>); await waitFor(() => expect(api.listDrafts).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: 'New X draft' }))
    const dialog = screen.getByRole('dialog'); fireEvent.change(within(dialog).getByLabelText('Post text'), { target: { value: 'A useful standalone post.' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save & submit for review' }))
    await waitFor(() => expect(api.createDraft).toHaveBeenCalledWith('demo-brand', 'x', { body: 'A useful standalone post.' }))
    expect(api.submitDraft).toHaveBeenCalledWith('new-draft')
    expect(JSON.stringify(api.createDraft.mock.calls)).not.toMatch(/actor|publish|schedule|send/)
  })

  it('revises rejected material before resubmitting and preserves candidate lineage into a newsletter idea', async () => {
    render(<MemoryRouter><Content /></MemoryRouter>); await waitFor(() => expect(api.listDrafts).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: /Social drafts/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Revise & resubmit' }))
    let dialog = screen.getByRole('dialog'); fireEvent.change(within(dialog).getByLabelText('Post text'), { target: { value: 'Revised text' } }); fireEvent.click(within(dialog).getByRole('button', { name: 'Revise & resubmit' }))
    await waitFor(() => expect(api.editDraft).toHaveBeenCalledWith('draft-1', { body: 'Revised text' })); expect(api.submitDraft).toHaveBeenCalledWith('draft-1')

    fireEvent.click(screen.getByRole('button', { name: /Ideas/ })); fireEvent.click(screen.getByRole('button', { name: 'Draft newsletter' }))
    dialog = screen.getByRole('dialog'); fireEvent.change(within(dialog).getByLabelText('Target reader'), { target: { value: 'Readers' } }); fireEvent.change(within(dialog).getByLabelText('Intended outcome'), { target: { value: 'Evaluate the change' } }); fireEvent.click(within(dialog).getByRole('button', { name: 'Create newsletter idea' }))
    await waitFor(() => expect(api.createNewsletter).toHaveBeenCalledWith('demo-brand', expect.objectContaining({ final_title: 'Price drop', source_provenance: [expect.objectContaining({ source_id: 'source-1' })] }), 'candidate-1'))
    expect(JSON.stringify(api.createNewsletter.mock.calls)).not.toContain('created_by')
  })

  it('shows the selected editorial idea history without mutating it', async () => {
    render(<MemoryRouter><Content /></MemoryRouter>); await waitFor(() => expect(api.listCandidates).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: /Ideas/ })); fireEvent.click(screen.getByRole('button', { name: 'History' }))
    expect(await screen.findByText(/No longer timely/)).toBeInTheDocument()
    expect(api.history).toHaveBeenCalledWith('candidate-1')
    expect(api.abandon).not.toHaveBeenCalled(); expect(api.archive).not.toHaveBeenCalled()
  })

  it('creates and revises canonical campaign posts without submitting or executing them', async () => {
    render(<MemoryRouter><Content /></MemoryRouter>); await waitFor(() => expect(api.listPosts).toHaveBeenCalledWith('campaign-1'))
    fireEvent.click(screen.getByRole('button', { name: 'New campaign post' }))
    let dialog = screen.getByRole('dialog'); fireEvent.change(within(dialog).getByLabelText('Post text'), { target: { value: 'New canonical material' } }); fireEvent.click(within(dialog).getByRole('button', { name: 'Save draft only' }))
    await waitFor(() => expect(api.createPost).toHaveBeenCalledWith('campaign-1', 'x', 'New canonical material'))
    fireEvent.click(screen.getByRole('button', { name: /Campaign posts/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    dialog = screen.getByRole('dialog'); fireEvent.change(within(dialog).getByLabelText('Post text'), { target: { value: 'Revised canonical material' } }); fireEvent.click(within(dialog).getByRole('button', { name: 'Save draft only' }))
    await waitFor(() => expect(api.editPost).toHaveBeenCalledWith('post-1', 'Revised canonical material'))
    expect(JSON.stringify([api.createPost.mock.calls, api.editPost.mock.calls])).not.toMatch(/actor|approve|schedule|send|publish/)
  })

  it('promotes an idea into draft campaign material and exposes exact dispatch validation history', async () => {
    render(<MemoryRouter><Content /></MemoryRouter>); await waitFor(() => expect(api.listCandidates).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: /Ideas/ })); fireEvent.click(screen.getByRole('button', { name: 'Draft campaign post' }))
    let dialog = screen.getByRole('dialog'); fireEvent.change(within(dialog).getByLabelText('X post text'), { target: { value: 'Candidate-grounded draft' } }); fireEvent.click(within(dialog).getByRole('button', { name: 'Create draft material' }))
    await waitFor(() => expect(api.promote).toHaveBeenCalledWith('demo-brand', 'candidate-1', expect.objectContaining({ campaign_id: null, channel: 'x', body: 'Candidate-grounded draft' })))
    fireEvent.click(screen.getByRole('button', { name: /Social drafts/ })); fireEvent.click(screen.getByRole('button', { name: 'Validation & history' }))
    expect(await screen.findByText('Exact material is valid')).toBeInTheDocument(); expect(within(screen.getByRole('dialog')).getByText('preview-operator')).toBeInTheDocument(); expect(within(screen.getByRole('dialog')).getByText('created')).toBeInTheDocument()
    expect(api.validateDraft).toHaveBeenCalledWith('draft-1'); expect(api.auditDraft).toHaveBeenCalledWith('draft-1')
  })

  it('previews exact evidence and guideline binding before explicit draft-only confirmation', async () => {
    render(<MemoryRouter><Content /></MemoryRouter>); await waitFor(() => expect(api.listCandidates).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: 'Build governed package' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Operator command'), { target: { value: 'Build decision support from the official terms.' } })
    fireEvent.click(within(dialog).getByText('Official terms'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Preview proposed changes' }))
    await waitFor(() => expect(api.previewProposal).toHaveBeenCalledWith('demo-brand', { command: 'Build decision support from the official terms.', source_ids: ['source-1'], candidate_ids: [] }))
    expect(await within(dialog).findByText('sha256:guideline')).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Save inert drafts' })).toBeDisabled()
    fireEvent.click(within(dialog).getByLabelText('Confirm inert drafts'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save inert drafts' }))
    await waitFor(() => expect(api.confirmProposal).toHaveBeenCalledWith('demo-brand', 'proposal-1'))
    expect(await within(dialog).findByText(/Saved as inert drafts by preview-operator/)).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: /send|publish|schedule|approve/i })).not.toBeInTheDocument()
  })
})
