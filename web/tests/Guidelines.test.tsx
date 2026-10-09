// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ list: vi.fn(), audit: vi.fn(), create: vi.fn(), createVersion: vi.fn(), activate: vi.fn(), archive: vi.fn(), notify: vi.fn() }))
vi.mock('../src/api/endpoints', () => ({ guidelines: mocks }))
vi.mock('../src/state/BrandContext', () => ({ useBrands: () => ({ selected: 'demo-brand', active: [{ id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }], brands: [{ id: 'b1', slug: 'demo-brand', name: 'Demo Brand' }], select: vi.fn() }) }))
vi.mock('../src/state/Toast', () => ({ useToast: () => ({ notify: mocks.notify }) }))

import Guidelines from '../src/pages/Guidelines'

const version1 = { id: 'v1', guideline_id: 'g1', version: 1, instructions: 'Write in the brand voice. Open with “Hey,” and close with “— The Team”.', rules: { minimum_words: 750, operator_checklist: ['one_thesis'] }, source_ref: 'https://example.test/house-style', change_reason: 'Seed house style.', created_by: 'system', content_fingerprint: 'sha256:one', created_at: '2026-09-01T12:00:00Z' }
const version2 = { ...version1, id: 'v2', version: 2, instructions: `${version1.instructions} Include a useful decision.`, change_reason: 'Add decision support.', created_by: 'preview-operator', content_fingerprint: 'sha256:two', created_at: '2026-09-03T12:00:00Z' }
const guideline = { id: 'g1', brand_id: 'b1', content_type: 'newsletter', channel: 'beehiiv', name: 'Demo Brand newsletter house style', status: 'active', active_version_id: 'v1', created_at: '2026-09-01T12:00:00Z', updated_at: '2026-09-03T12:00:00Z', versions: [version2, version1], active_version: version1 }

beforeEach(() => {
  vi.clearAllMocks(); mocks.list.mockResolvedValue([guideline]); mocks.audit.mockResolvedValue([{ sequence: 1, guideline_id: 'g1', version_id: 'v1', action: 'activated', actor: 'preview-operator', reason: 'Use reviewed house style.', details: {}, at: '2026-09-01T12:00:00Z' }]); mocks.createVersion.mockResolvedValue(guideline); mocks.activate.mockResolvedValue({ ...guideline, active_version_id: 'v2', active_version: version2 })
})
afterEach(cleanup)

describe('Guidelines', () => {
  it('shows the exact immutable Demo Brand instructions, rules, source, and fingerprint used by generation', async () => {
    render(<MemoryRouter><Guidelines /></MemoryRouter>)
    expect(await screen.findByDisplayValue(version1.instructions)).toBeInTheDocument()
    expect(screen.getByLabelText('Active structured rules')).toHaveValue(JSON.stringify(version1.rules, null, 2))
    expect(screen.getAllByText('sha256:one')).toHaveLength(2)
    expect(screen.getByText('immutable versions · human activation')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /generate|publish|send/i })).not.toBeInTheDocument()
  })

  it('creates an inactive immutable version without changing the active generation input', async () => {
    render(<MemoryRouter><Guidelines /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: 'Create version' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Exact generation instructions'), { target: { value: 'A reviewed replacement instruction.' } })
    fireEvent.change(within(dialog).getByLabelText('Structured rules (JSON object)'), { target: { value: '{"minimum_words":800,"operator_checklist":[]}' } })
    fireEvent.change(within(dialog).getByLabelText('Change reason'), { target: { value: 'Raise the reviewed floor.' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create inactive version' }))
    await waitFor(() => expect(mocks.createVersion).toHaveBeenCalledWith('g1', { instructions: 'A reviewed replacement instruction.', rules: { minimum_words: 800, operator_checklist: [] }, reason: 'Raise the reviewed floor.', source_ref: 'https://example.test/house-style' }))
    expect(mocks.activate).not.toHaveBeenCalled()
  })

  it('requires a reason and activates the exact selected version as a separate human boundary', async () => {
    render(<MemoryRouter><Guidelines /></MemoryRouter>)
    const activate = await screen.findByRole('button', { name: 'Activate' })
    fireEvent.click(activate)
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText(/invalidates stale newsletter reviews and approvals/)).toBeInTheDocument()
    const action = within(dialog).getByRole('button', { name: 'Activate exact version' })
    expect(action).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText('Operator reason'), { target: { value: 'Use the reviewed decision-support version.' } })
    fireEvent.click(action)
    await waitFor(() => expect(mocks.activate).toHaveBeenCalledWith('g1', 2, 'Use the reviewed decision-support version.'))
  })
})
