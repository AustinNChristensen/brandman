// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ list: vi.fn(), create: vi.fn(), get: vi.fn(), notify: vi.fn() }))
vi.mock('../src/api/endpoints', () => ({ brands: { list: mocks.list, create: mocks.create }, settings: { get: mocks.get } }))
vi.mock('../src/state/Toast', () => ({ useToast: () => ({ notify: mocks.notify }) }))

import Settings from '../src/pages/Settings'
import { BrandProvider, useBrands } from '../src/state/BrandContext'

const demo = { id: 'demo', slug: 'demo-brand', name: 'Demo Brand' }
const created = { id: 'new', slug: 'acme-widgets', name: 'Acme Widgets' }
const governance = { mission: 'Help people pick widgets.', voice: 'Clear and specific.', compliance_rules: 'Verify claims.', approval_policy: 'human_approval_required', updated_at: '' }

function IntegrationsProbe() {
  const { selected } = useBrands()
  const location = useLocation()
  return <p>Integrations for {selected}: {location.search}</p>
}

function openSettings() {
  render(<MemoryRouter initialEntries={['/settings']}><BrandProvider><Routes>
    <Route path="/settings" element={<Settings />} />
    <Route path="/integrations" element={<IntegrationsProbe />} />
  </Routes></BrandProvider></MemoryRouter>)
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.list.mockResolvedValue([demo])
  mocks.create.mockImplementation(async () => { mocks.list.mockResolvedValue([demo, created]); return created })
  mocks.get.mockImplementation(async (slug: string) => ({
    brand: { ...(slug === created.slug ? created : demo), ...governance },
    audit: [], rate_cards: [], schedules: [],
    orchestration: { schedules: { enabled: 0, due: 0 }, pending_decisions: 0 },
  }))
})
afterEach(cleanup)

async function createBrand() {
  await waitFor(() => expect(mocks.list).toHaveBeenCalled())
  fireEvent.change(screen.getByLabelText('Brand name'), { target: { value: created.name } })
  fireEvent.change(screen.getByLabelText('Mission'), { target: { value: governance.mission } })
  fireEvent.click(screen.getByRole('button', { name: 'Create brand' }))
}

describe('brand creation handoff', () => {
  it('refreshes and selects the new brand, then links channel setup to that brand', async () => {
    openSettings()
    await createBrand()
    const link = await screen.findByRole('link', { name: 'Set up channels' })
    expect(link).toHaveAttribute('href', '/integrations?brand=acme-widgets')
    expect(screen.getByText('Mission and voice are saved. Choose your channels and review their connection requirements in Integrations.')).toBeInTheDocument()
    expect(mocks.list).toHaveBeenCalledTimes(2)
    expect(screen.getAllByLabelText('Brand')[0]).toHaveValue(created.slug)
    fireEvent.click(link)
    expect(await screen.findByText('Integrations for acme-widgets: ?brand=acme-widgets')).toBeInTheDocument()
  })

  it('does not show another brand the new-brand handoff', async () => {
    openSettings()
    await createBrand()
    await screen.findByRole('link', { name: 'Set up channels' })
    fireEvent.change(screen.getAllByLabelText('Brand')[0], { target: { value: demo.slug } })
    expect(screen.queryByRole('link', { name: 'Set up channels' })).not.toBeInTheDocument()
    expect(await screen.findByDisplayValue(governance.mission)).toBeInTheDocument()
  })

  it('keeps failed creation in the form without a success handoff', async () => {
    mocks.create.mockRejectedValue(new Error('Brand slug already exists'))
    openSettings()
    await createBrand()
    expect(await screen.findByRole('alert')).toHaveTextContent('Brand slug already exists')
    expect(screen.queryByRole('link', { name: 'Set up channels' })).not.toBeInTheDocument()
    expect(mocks.list).toHaveBeenCalledTimes(1)
    expect(screen.getAllByLabelText('Brand')[0]).toHaveValue('')
  })
})
