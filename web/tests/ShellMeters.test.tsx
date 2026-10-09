// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../src/state/BrandContext', () => ({
  useBrands: () => ({ brands: [], selected: null, select: vi.fn() }),
}))

import { Shell } from '../src/components/Shell'

function renderShell(meters?: { xRequests?: string; spend?: string }) {
  render(<MemoryRouter><Shell title="Overview" meters={meters}><div /></Shell></MemoryRouter>)
}

afterEach(cleanup)

describe('Shell sidebar metrics', () => {
  it('explains what each metric is and why it is unavailable, without rate-card jargon', () => {
    renderShell()
    expect(screen.getByText('X API requests this month')).toBeInTheDocument()
    expect(screen.getByText('Estimated spend this month')).toBeInTheDocument()
    expect(screen.getByText(/usage data has not loaded/i)).toBeInTheDocument()
    expect(screen.getByText(/no price is set/i)).toBeInTheDocument()
    const hint = screen.getByText(/no price is set/i)
    expect(within(hint).getByRole('link', { name: 'Settings' })).toHaveAttribute('href', '/settings')
    expect(screen.queryByText(/rate cards/i)).not.toBeInTheDocument()
  })

  it('drops the explanations once values are available', () => {
    renderShell({ xRequests: '12', spend: '$3.40' })
    expect(screen.getByText('12')).toBeInTheDocument()
    expect(screen.getByText('$3.40')).toBeInTheDocument()
    expect(screen.queryByText(/not shown yet/i)).not.toBeInTheDocument()
  })
})
