// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  context: vi.fn(), thirdParty: vi.fn(), connectors: vi.fn(), health: vi.fn(),
  canonical: vi.fn(), beehiivPulls: vi.fn(), onboard: vi.fn(), setEnabled: vi.fn(),
  revalidate: vi.fn(), create: vi.fn(), requestBeehiivPull: vi.fn(), checkHealth: vi.fn(), notify: vi.fn(),
}))
vi.mock('../src/api/endpoints', () => ({
  brands: { context: mocks.context },
  integrations: { connectors: mocks.connectors, health: mocks.health, checkHealth: mocks.checkHealth },
  sourceInputs: { thirdParty: mocks.thirdParty, canonical: mocks.canonical, beehiivPulls: mocks.beehiivPulls, onboard: mocks.onboard, setEnabled: mocks.setEnabled, revalidate: mocks.revalidate, create: mocks.create, requestBeehiivPull: mocks.requestBeehiivPull },
}))
vi.mock('../src/state/BrandContext', () => ({ useBrands: () => ({ selected: 'demo-brand', active: [{ id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }], brands: [{ id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }], select: vi.fn() }) }))
vi.mock('../src/state/Toast', () => ({ useToast: () => ({ notify: mocks.notify }) }))

import Sources from '../src/pages/Sources'

const source = { id: 'source-1', title: 'A public offer changed', body_summary: 'Syndicated summary only.', source_type: 'rss', lifecycle_state: 'fresh', url: 'https://publisher.test/story' }
const feed = { connector_account_id: 'rss-1', brand_id: 'b1', publisher_name: 'Doctor of Credit', homepage_url: 'https://publisher.test', feed_url: 'https://publisher.test/feed.xml', feed_format: 'rss', content_policy: 'title_summary_link_only', polling_interval_seconds: 1800, enabled: true, connector_status: 'connected', last_error: null, health_checked_at: null, running_sync_jobs: 0, schedule: { enabled: true, next_run_at: '2026-09-04T15:00:00Z' } }

beforeEach(() => {
  vi.clearAllMocks()
  mocks.context.mockResolvedValue({ sources: [source] })
  mocks.thirdParty.mockResolvedValue([feed])
  mocks.connectors.mockResolvedValue([{ id: 'bee-1', connector_type: 'beehiiv', account_key: 'pub_points', display_name: 'Newsletter insights', status: 'connected', scopes: ['posts.read'], capabilities: ['posts.read', 'metrics.read'], configuration: { connection_role: 'beehiiv_read', delivery_mode: 'browser_assisted' } }])
  mocks.health.mockResolvedValue([])
  mocks.canonical.mockResolvedValue([{ id: 'rev-1', source_id: 'source-1', observed_at: '2026-09-03T15:00:00Z', status: 'verified', semantic_fact_check: false }])
  mocks.beehiivPulls.mockResolvedValue([{ id: 'pull-1', status: 'completed', scheduled_for: '2026-09-03T14:00:00Z', posts_received: 1, post_measurements_received: 2, measured_campaign_ids: ['campaign-1'] }])
  mocks.onboard.mockResolvedValue(feed); mocks.revalidate.mockResolvedValue({}); mocks.create.mockResolvedValue(source); mocks.requestBeehiivPull.mockResolvedValue({}); mocks.checkHealth.mockResolvedValue([])
})
afterEach(() => { vi.useRealTimers(); cleanup() })

describe('Sources', () => {
  it('shows governed public metadata and the private Beehiiv boundary without subscriber data controls', async () => {
    render(<MemoryRouter><Sources /></MemoryRouter>)
    expect(await screen.findByText('Doctor of Credit')).toBeInTheDocument()
    expect(screen.getByText('read-only inputs · no subscriber data')).toBeInTheDocument()
    expect(screen.getByText(/No newsletter bodies or subscriber records belong here/)).toBeInTheDocument()
    expect(screen.getByText(/not a fact-check/)).toBeInTheDocument()
    expect(screen.getByText(/campaigns campaign-1/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /publish|send|draft newsletter/i })).not.toBeInTheDocument()
  })

  it('configures a feed without credentials or an inline fetch', async () => {
    render(<MemoryRouter><Sources /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: 'Add public feed' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Publisher name'), { target: { value: 'The Points Guy' } })
    fireEvent.change(within(dialog).getByLabelText('Public HTTPS feed URL'), { target: { value: 'https://tpg.test/rss' } })
    fireEvent.change(within(dialog).getByLabelText('Why this source is in scope'), { target: { value: 'Track public points news' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Configure feed' }))
    await waitFor(() => expect(mocks.onboard).toHaveBeenCalledWith('demo-brand', {
      publisher_name: 'The Points Guy', feed_url: 'https://tpg.test/rss', feed_format: 'auto',
      polling_interval_seconds: 1800, reason: 'Track public points news',
    }))
    expect(JSON.stringify(mocks.onboard.mock.calls[0])).not.toMatch(/credential|secret|article_body/)
  })

  it('queues one idempotent public-page metadata revalidation per source and day', async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date('2026-09-03T18:00:00Z'))
    render(<MemoryRouter><Sources /></MemoryRouter>)
    await vi.waitFor(() => expect(screen.getByRole('button', { name: 'Revalidate public page' })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Revalidate public page' }))
    await vi.waitFor(() => expect(mocks.revalidate).toHaveBeenCalledWith('demo-brand', 'source-1', 'operator:source-1:2026-09-03'))
    vi.useRealTimers()
  })

  it('creates a manual canonical input and requests an assisted metadata-plus-metrics read', async () => {
    render(<MemoryRouter><Sources /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: 'Add canonical input' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Title'), { target: { value: 'Issuer terms update' } })
    fireEvent.change(within(dialog).getByLabelText('Summary or source note'), { target: { value: 'Review the canonical terms page.' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save input' }))
    await waitFor(() => expect(mocks.create).toHaveBeenCalledWith('demo-brand', { title: 'Issuer terms update', body_summary: 'Review the canonical terms page.', source_type: 'manual', lifecycle_state: 'draft' }))
    fireEvent.click(screen.getByRole('button', { name: 'Request metadata + metrics' }))
    await waitFor(() => expect(mocks.requestBeehiivPull).toHaveBeenCalledWith('demo-brand', 'bee-1', expect.any(String)))
  })
})
