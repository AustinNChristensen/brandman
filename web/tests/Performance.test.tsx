// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ context: vi.fn(), list: vi.fn(), record: vi.fn(), graphs: vi.fn(), measurement: vi.fn(), scorecard: vi.fn(), morningPlan: vi.fn(), recordKpi: vi.fn(), trackedLinks: vi.fn(), learnings: vi.fn(), planning: vi.fn(), audit: vi.fn(), notify: vi.fn() }))
vi.mock('../src/api/endpoints', () => ({
  brands: { context: mocks.context, scorecard: mocks.scorecard, morningPlan: mocks.morningPlan, recordKpi: mocks.recordKpi, learnings: mocks.learnings },
  campaignGraphs: { list: mocks.graphs, measurement: mocks.measurement },
  performance: { list: mocks.list, record: mocks.record, trackedLinks: mocks.trackedLinks, planning: mocks.planning, planningAudit: mocks.audit },
}))
vi.mock('../src/state/BrandContext', () => ({ useBrands: () => ({ selected: 'demo-brand', active: [{ id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }], brands: [{ id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }], select: vi.fn() }) }))
vi.mock('../src/state/Toast', () => ({ useToast: () => ({ notify: mocks.notify }) }))

import Performance from '../src/pages/Performance'

beforeEach(() => {
  vi.clearAllMocks()
  mocks.context.mockResolvedValue({ sources: [{ id: 'source-1', title: 'Canonical offer terms' }] })
  mocks.list.mockResolvedValue([{ id: 'p1', brand_id: 'b1', channel: 'x', observed_at: '2026-09-03T15:00:00Z', impressions: 100, clicks: 12, engagements: 20, conversions: 2, revenue_cents: 1200, notes: 'Provider aggregate' }])
  mocks.graphs.mockResolvedValue([{ id: 'c1', name: 'Launch', objective: 'Qualified readers', status: 'active', memberships: [{ asset_type: 'post', asset_id: 'post-1', channel: 'x' }] }])
  mocks.measurement.mockResolvedValue({ campaign_id: 'c1', assets: {}, cross_channel_rollup: { clicks: 12, conversions: 2, revenue_cents: 1200, cross_channel_ctr: null, reason: 'Unsafe across channels', reach_not_summed: { x: { impressions: 100 }, newsletter: { delivered: 80 } } }, channels: { x: { native_totals: { impressions: 100, clicks: 12 }, rates: { ctr: { value: .12, numerator_metric: 'clicks', denominator_metric: 'impressions' } } } }, conversion_confidence: 'reported', deduplication: { strategy: 'evidence_key', unique_records: 1 } })
  mocks.scorecard.mockResolvedValue(null)
  mocks.morningPlan.mockResolvedValue({ kind: 'morning_plan', date: '2026-09-03', mission_name: 'Grow Demo Brand', goals: [], actions: [{ title: 'Measure the launch', reason: 'Close the evidence loop.', owner: 'Chris' }] })
  mocks.trackedLinks.mockResolvedValue([{ id: 't1', campaign_id: 'c1', artifact_id: 'a1', cta_id: 'cta1', source: 'x', medium: 'social', destination: 'https://example.test/offer', tracked_url: 'https://brand.test/t/t1', created_at: '2026-09-03T15:00:00Z' }])
  mocks.learnings.mockResolvedValue([{ id: 'l1', hypothesis: 'Focused offers convert', proposed_change: 'Repeat one focused CTA', evidence_for: ['p1'], evidence_against: [], status: 'accepted', active: true }])
  mocks.planning.mockImplementation(async (_slug, channel) => ({ status: 'evidence_available', bounded_prior: { score_adjustment_points: channel === 'web' ? 0.5 : 1.25, reason: 'Bounded by policy.' }, evidence: { included_count: 1 }, repetition_guard: { rule: 'Avoid repeating a recent angle', matching_campaigns_last_30_days: 1 } }))
  mocks.audit.mockResolvedValue([{ id: 'audit-1' }])
  mocks.record.mockResolvedValue({}); mocks.recordKpi.mockResolvedValue({})
})
afterEach(cleanup)

describe('Performance', () => {
  it('uses only safe additive outcomes and preserves channel-native denominators', async () => {
    render(<MemoryRouter><Performance /></MemoryRouter>)
    expect(await screen.findByText('Launch')).toBeInTheDocument()
    expect(screen.getByText('Reach is never summed across channels.')).toBeInTheDocument()
    expect(screen.getByText(/does not invent a cross-channel CTR/)).toBeInTheDocument()
    expect(screen.getByText('12.0%')).toBeInTheDocument()
    expect(screen.getByText(/clicks \/ impressions/)).toBeInTheDocument()
    expect(screen.getByText('aggregate evidence · no PII')).toBeInTheDocument()
  })

  it('reloads a bounded, audited planning prior without exposing provider or approval mutations', async () => {
    render(<MemoryRouter><Performance /></MemoryRouter>)
    expect(await screen.findByText('1.25 pts')).toBeInTheDocument()
    expect(screen.getByText(/audit entries: 1/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Planning channel'), { target: { value: 'web' } })
    await waitFor(() => expect(mocks.planning).toHaveBeenLastCalledWith('demo-brand', 'web'))
    expect(await screen.findByText('0.50 pts')).toBeInTheDocument()
    expect(screen.getByText(/cannot change compliance, approve, schedule, send, or publish/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /approve|publish|send|schedule/i })).not.toBeInTheDocument()
  })

  it('exposes normalized aggregate evidence, tracked attribution, KPI verification, and the morning plan', async () => {
    render(<MemoryRouter><Performance /></MemoryRouter>)
    expect(await screen.findByText('Measure the launch')).toBeInTheDocument()
    expect(screen.getByText('campaign c1 · artifact a1 · CTA cta1')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Record aggregate outcome' }))
    fireEvent.click(screen.getByRole('button', { name: 'Record evidence' }))
    expect(mocks.record).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('Campaign item or source'), { target: { value: 'source:source-1' } })
    fireEvent.change(screen.getByLabelText('Clicks'), { target: { value: '9' } })
    fireEvent.click(screen.getByRole('button', { name: 'Record evidence' }))
    await waitFor(() => expect(mocks.record).toHaveBeenCalledWith('demo-brand', expect.objectContaining({ channel: 'x', clicks: 9, post_id: null, source_id: 'source-1' })))
    fireEvent.click(screen.getByRole('button', { name: 'Record verified KPI' }))
    fireEvent.change(screen.getByLabelText('Verified aggregate value'), { target: { value: '21' } })
    fireEvent.change(screen.getByLabelText('How you verified it'), { target: { value: 'Checked the authenticated account total.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save verified value' }))
    await waitFor(() => expect(mocks.recordKpi).toHaveBeenCalledWith('demo-brand', expect.objectContaining({ metric: 'x_followers', value: 21, source: 'manual' })))
  })
})
