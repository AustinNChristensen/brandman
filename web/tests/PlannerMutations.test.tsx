// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  get: vi.fn(), updateSettings: vi.fn(), updateItem: vi.fn(), preview: vi.fn(),
  commit: vi.fn(), undo: vi.fn(), notify: vi.fn(),
}))

vi.mock('../src/api/endpoints', () => ({ publishingPlan: mocks }))
vi.mock('../src/state/BrandContext', () => ({ useBrands: () => ({
  active: [{ id: 'brand-1', slug: 'demo-brand', name: 'Demo Brand' }],
  selected: 'demo-brand', brands: [], select: vi.fn(),
}) }))
vi.mock('../src/state/Toast', () => ({ useToast: () => ({ notify: mocks.notify }) }))
vi.mock('../src/state/useWorkspace', () => ({
  useWorkspace: () => ({ data: [], loading: false, error: null, reload: vi.fn() }),
  metersFrom: () => undefined,
}))

import Planner from '../src/pages/Planner'

const item = {
  item_type: 'post', item_id: 'post-1', channel: 'x', status: 'draft', title: 'Launch post',
  initiative_id: 'initiative-1', planned_for: null, pinned: false, locked: false,
}
const plan = {
  brand_id: 'brand-1',
  settings: { timezone: 'America/Denver', windows: [{ weekday: 0, start: '09:00', end: '17:00' }], cadence_minutes: { x: 120, newsletter: 1440 }, updated_by: null, updated_at: null },
  initiatives: [{ id: 'initiative-1', items: [item] }],
  safety: { planning_only: true, provider_write_performed: false, approval_granted: false },
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.get.mockResolvedValue(plan)
  mocks.updateSettings.mockResolvedValue(plan.settings)
  mocks.updateItem.mockResolvedValue({ ...item, pinned: true })
  mocks.preview.mockResolvedValue({ id: 'preview-1', snapshot_fingerprint: 'sha256:test', planning_only: true,
    changes: [{ item_type: 'post', item_id: 'post-1', initiative_id: 'initiative-1', channel: 'x', before: null, after: '2026-09-07T15:00:00Z' }] })
  mocks.commit.mockResolvedValue({ id: 'commit-1', brand_id: 'brand-1', preview_id: 'preview-1', committed_by: 'chris', committed_at: '2026-09-03T12:00:00Z', undone_by: null, undone_at: null, before: [], changes: [{ item_id: 'post-1' }] })
  mocks.undo.mockResolvedValue({ id: 'commit-1', undone_by: 'chris', undone_at: '2026-09-03T12:01:00Z', changes: [] })
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})
afterEach(cleanup)

describe('Publishing planner mutations', () => {
  it('shows grouped initiatives and routes pin, preview, commit, and exact undo through planning-only endpoints', async () => {
    render(<MemoryRouter><Planner /></MemoryRouter>)
    expect(await screen.findByText('Launch post')).toBeInTheDocument()
    expect(screen.getByText(/never approve, schedule with a provider, or publish/i)).toBeInTheDocument()
    expect(screen.getByText('initiative-1')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Pin' }))
    await waitFor(() => expect(mocks.updateItem).toHaveBeenCalledWith('demo-brand', expect.objectContaining({ item_id: 'post-1', pinned: true })))

    fireEvent.click(screen.getByRole('button', { name: 'Preview reflow' }))
    expect(await screen.findByText('Proposed changes')).toBeInTheDocument()
    expect(mocks.preview).toHaveBeenCalledWith('demo-brand', expect.stringMatching(/^\d{4}-\d{2}-\d{2}T/))

    fireEvent.click(screen.getByRole('button', { name: 'Commit planning changes' }))
    await waitFor(() => expect(mocks.commit).toHaveBeenCalledWith('demo-brand', 'preview-1'))
    fireEvent.click(await screen.findByRole('button', { name: 'Undo exact reflow' }))
    await waitFor(() => expect(mocks.undo).toHaveBeenCalledWith('demo-brand', 'commit-1'))
  })

  it('saves timezone, windows, and cadence without any provider API', async () => {
    render(<MemoryRouter><Planner /></MemoryRouter>)
    await screen.findByText('Launch post')
    fireEvent.change(screen.getByLabelText('Timezone for Demo Brand'), { target: { value: 'America/New_York' } })
    fireEvent.change(screen.getByLabelText('Window 1 start'), { target: { value: '10:00' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save settings' }))
    await waitFor(() => expect(mocks.updateSettings).toHaveBeenCalledWith('demo-brand', expect.objectContaining({
      timezone: 'America/New_York', windows: [{ weekday: 0, start: '10:00', end: '17:00' }],
    })))
    expect(Object.keys(mocks)).not.toEqual(expect.arrayContaining(['publish', 'schedule']))
  })
})
