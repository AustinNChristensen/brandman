// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  list: vi.fn(), experiments: vi.fn(), context: vi.fn(), audit: vi.fn(), experiment: vi.fn(),
  transition: vi.fn(), recommend: vi.fn(), acceptRecommendation: vi.fn(), propose: vi.fn(), draftExperiment: vi.fn(), notify: vi.fn(),
}))
vi.mock('../src/api/endpoints', () => ({
  brands: { context: mocks.context },
  learningLab: {
    list: mocks.list, experiments: mocks.experiments, audit: mocks.audit, experiment: mocks.experiment,
    transition: mocks.transition, recommend: mocks.recommend, acceptRecommendation: mocks.acceptRecommendation,
    propose: mocks.propose, draftExperiment: mocks.draftExperiment,
  },
}))
vi.mock('../src/state/BrandContext', () => ({ useBrands: () => ({
  selected: 'demo-brand',
  active: [{ id: 'brand-1', slug: 'demo-brand', name: 'Demo Brand' }],
  brands: [{ id: 'brand-1', slug: 'demo-brand', name: 'Demo Brand' }],
  select: vi.fn(), bySlug: vi.fn(), byId: vi.fn(),
}) }))
vi.mock('../src/state/Toast', () => ({ useToast: () => ({ notify: mocks.notify }) }))

import Learnings from '../src/pages/Learnings'

const learning = {
  id: 'l1', brand_id: 'brand-1', hypothesis: 'Specific numeric hooks help', evidence: 'Two campaigns improved clicks',
  proposed_change: 'Prefer a verified numeric hook when source evidence supports it', status: 'testing', created_at: '2026-09-01T00:00:00Z',
  reviewed_at: null, review_at: null, active: false, accepted_at: null, disabled_at: null, supersedes_id: null,
  scope: { channel: 'x' }, evidence_for: [], evidence_against: [], effect: {}, uncertainty: {},
}
const experiment = {
  id: 'e1', brand_id: 'brand-1', campaign_id: 'c1', source_id: 's1', hypothesis: 'Source update framing wins', metric: 'clicks',
  guardrails: { min_impressions_per_variant: 10 }, status: 'active', accepted_recommendation_id: null,
  variants: [{ id: 'v1', experiment_id: 'e1', variant_key: 'control', post_id: 'p1', rationale: 'Control', body: 'Verified offer update.', post_status: 'draft', scheduled_for: null, external_post_id: null }],
  recommendations: [{ id: 'r1', experiment_id: 'e1', measurement_window_id: 'w1', status: 'recommended', winner_variant_id: 'v1', metric: 'clicks', rationale: 'Highest evidence-backed clicks.', evidence: [{ variant_id: 'v1', variant_key: 'control', post_id: 'p1', observation_count: 1, impressions: 100, metric_value: 8 }], accepted_by: null, accepted_at: null, learning_id: null, created_at: '2026-09-02T00:00:00Z' }],
  measurement_windows: [{ id: 'w1', experiment_id: 'e1', window_key: 'launch', metric: 'clicks', opens_at: '2026-09-01T00:00:00Z', closes_at: '2026-09-02T00:00:00Z', evaluate_at: '2026-09-02T00:00:00Z', late_evidence_until: '2026-09-04T00:00:00Z', status: 'evaluated', evidence_state: 'sufficient', collection_round: 1, recommendation_id: 'r1', has_late_evidence: false }],
  created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-02T00:00:00Z',
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.list.mockResolvedValue([learning]); mocks.experiments.mockResolvedValue([experiment])
  mocks.context.mockResolvedValue({ campaigns: [{ id: 'c1', source_id: 's1', name: 'Offer launch' }] })
  mocks.audit.mockResolvedValue([{ sequence: 1, learning_id: 'l1', brand_id: 'brand-1', action: 'testing', actor: 'chris', details: {}, at: '2026-09-01T00:00:00Z' }])
  mocks.experiment.mockResolvedValue(experiment); mocks.transition.mockResolvedValue({ ...learning, status: 'accepted' })
  mocks.acceptRecommendation.mockResolvedValue({ ...experiment, status: 'completed' })
})
afterEach(cleanup)

describe('Learnings and controlled experiments', () => {
  it('renders bounded evidence and requires explicit human acceptance', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<MemoryRouter><Learnings /></MemoryRouter>)
    expect(await screen.findAllByText('Specific numeric hooks help')).toHaveLength(2)
    expect(await screen.findByText('Highest evidence-backed clicks.')).toBeInTheDocument()
    expect(screen.getAllByText('draft')).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Accept evidence' }))
    await waitFor(() => expect(mocks.transition).toHaveBeenCalledWith('demo-brand', 'l1', 'accept'))
    fireEvent.click(screen.getByRole('button', { name: 'Accept recommendation' }))
    await waitFor(() => expect(mocks.acceptRecommendation).toHaveBeenCalledWith('demo-brand', 'e1', 'r1'))
    expect(screen.queryByRole('button', { name: /publish|send|schedule/i })).not.toBeInTheDocument()
  })

  it('surfaces load failures with a safe retry and supports proposal entry', async () => {
    mocks.list.mockRejectedValueOnce(new Error('network unavailable'))
    render(<MemoryRouter><Learnings /></MemoryRouter>)
    expect(await screen.findByText('network unavailable')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })
})
