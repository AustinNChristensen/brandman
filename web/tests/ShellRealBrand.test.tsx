// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

vi.mock('../src/state/BrandContext', () => ({
  useBrands: () => ({
    brands: [
      {
        id: 'brand-2', slug: 'acme', name: 'Acme',
        mission: '', voice: '', compliance_rules: '',
        approval_policy: 'human_approval_required', created_at: '', updated_at: '',
      },
    ],
    selected: 'acme',
    select: vi.fn(),
  }),
}))

import { Shell } from '../src/components/Shell'

describe('Shell demo-data banner', () => {
  it('stays off a brand the operator created', () => {
    render(
      <MemoryRouter>
        <Shell title="Overview"><div>Body</div></Shell>
      </MemoryRouter>,
    )
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
    expect(screen.queryByText(/Demo data/)).not.toBeInTheDocument()
  })
})
