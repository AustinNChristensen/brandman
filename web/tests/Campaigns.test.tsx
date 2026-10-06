// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ list: vi.fn(), templates: vi.fn(), context: vi.fn(), packages: vi.fn(), get: vi.fn(), measurement: vi.fn(), audit: vi.fn(), create: vi.fn(), attach: vi.fn(), detach: vi.fn(), anchor: vi.fn(), reorder: vi.fn(), relate: vi.fn(), addFlight: vi.fn(), metric: vi.fn(), preflight: vi.fn(), instantiate: vi.fn(), bindDestination: vi.fn(), notify: vi.fn() }))
vi.mock('../src/api/endpoints', () => ({ campaignGraphs: api, brands: { context: api.context } }))
vi.mock('../src/state/BrandContext', () => ({ useBrands: () => ({ selected: 'demo-brand', active: [{ id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }], brands: [{ id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }], select: vi.fn() }) }))
vi.mock('../src/state/Toast', () => ({ useToast: () => ({ notify: api.notify }) }))

import Campaigns from '../src/pages/Campaigns'

const anchor = { id: 'm1', campaign_id: 'c1', asset_type: 'newsletter', asset_id: 'issue-1', channel: 'newsletter', role: 'anchor' as const, sequence: 0, phase: 'primary', active: 1, attribution_primary: 1, flight_name: 'primary', notes: '' }
const touchpoint = { ...anchor, id: 'm2', asset_type: 'x_post', asset_id: 'post-1', channel: 'x', role: 'touchpoint' as const, sequence: 1, attribution_primary: 0 }
const graph = { id: 'c1', brand_id: 'b1', source_id: 's1', name: 'Launch', objective: 'Teach readers', status: 'draft', created_at: '2026-09-03T12:00:00Z', memberships: [anchor, touchpoint], relationships: [], flights: [], primary_anchor: anchor }
const template = { template_key: 'newsletter-led', name: 'Newsletter-led', current_version: 1, contract: { questions: [{ id: 'goal', label: 'What should this campaign accomplish?', required: true }, { id: 'audience', label: 'Who most needs this?', required: true }, { id: 'source', label: 'Which source grounds it?', required: true }, { id: 'cta', label: 'What is the next action?', required: true }, { id: 'flight', label: 'When should it run?', required: true }, { id: 'success', label: 'What defines success?', required: true }], recipe: {}, guardrails: { hard: ['draft_only', 'no_auto_publish'] } } }

beforeEach(() => {
  vi.clearAllMocks(); api.list.mockResolvedValue([graph]); api.templates.mockResolvedValue([template]); api.context.mockResolvedValue({ sources: [{ id: 's1', title: 'Official terms' }] }); api.packages.mockResolvedValue([]); api.get.mockResolvedValue(graph); api.measurement.mockResolvedValue({ campaign_id: 'c1', assets: {}, aggregate: {} }); api.audit.mockResolvedValue([]); api.anchor.mockResolvedValue(graph); api.metric.mockResolvedValue({}); api.preflight.mockResolvedValue({ ready: true, creates_nothing: true }); api.instantiate.mockResolvedValue(graph)
})
afterEach(cleanup)

describe('Campaigns', () => {
  it('shows the governed draft graph, anchor, touchpoints, and no-publish boundary', async () => {
    render(<MemoryRouter><Campaigns /></MemoryRouter>)
    expect(await screen.findAllByText('Launch')).not.toHaveLength(0)
    expect(screen.getByText('anchor set')).toBeInTheDocument()
    expect(screen.getByText('draft graph only · no publishing')).toBeInTheDocument()
    expect(screen.getByText('No declared reader paths.')).toBeInTheDocument()
  })

  it('changes the anchor with a server-attributed reason and records aggregate-only evidence', async () => {
    render(<MemoryRouter><Campaigns /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: 'Make anchor' }))
    await waitFor(() => expect(api.anchor).toHaveBeenCalledWith('m2', expect.stringContaining('primary campaign anchor')))
    const recordButtons = screen.getAllByRole('button', { name: 'Record metric' })
    fireEvent.click(recordButtons[1])
    const dialog = screen.getByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Observed at'), { target: { value: '2026-09-03T12:00' } })
    fireEvent.change(within(dialog).getByLabelText('Evidence key (unique provider report ID)'), { target: { value: 'provider-report-1' } })
    fireEvent.change(within(dialog).getByLabelText('Impressions'), { target: { value: '1000' } })
    fireEvent.change(within(dialog).getByLabelText('Clicks (optional)'), { target: { value: '25' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Record evidence' }))
    await waitFor(() => expect(api.metric).toHaveBeenCalledWith('m2', expect.objectContaining({ native_metrics: { impressions: 1000, clicks: 25 }, attribution_confidence: 'operator_reported_aggregate', idempotency_key: 'provider-report-1' })))
    expect(JSON.stringify(api.metric.mock.calls[0])).not.toMatch(/subscriber|email_address|visitor_id/)
  })

  it('requires a creates-nothing preflight before template instantiation', async () => {
    render(<MemoryRouter><Campaigns /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: 'Use template' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Campaign name'), { target: { value: 'September guide' } })
    for (const question of template.contract.questions) fireEvent.change(within(dialog).getByLabelText(question.label), { target: { value: `${question.id} answer` } })
    const create = within(dialog).getByRole('button', { name: 'Create draft graph' })
    expect(create).toBeDisabled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Run read-only preflight' }))
    await waitFor(() => expect(api.preflight).toHaveBeenCalled())
    expect(create).toBeEnabled()
    fireEvent.click(create)
    await waitFor(() => expect(api.instantiate).toHaveBeenCalledWith('demo-brand', 'newsletter-led', expect.objectContaining({ source_id: 's1', objective: 'goal answer' })))
  })
})
