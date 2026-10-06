// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

const { selectBrand } = vi.hoisted(() => ({ selectBrand: vi.fn() }))

vi.mock('../src/state/BrandContext', () => ({
  useBrands: () => ({
    brands: [
      {
        id: 'brand-1', slug: 'demo-brand', name: 'Demo Brand',
        mission: '', voice: '', compliance_rules: '',
        approval_policy: 'human_approval_required', created_at: '', updated_at: '',
      },
      {
        id: 'brand-2', slug: 'second-brand', name: 'Second Brand',
        mission: '', voice: '', compliance_rules: '',
        approval_policy: 'human_approval_required', created_at: '', updated_at: '',
      },
    ],
    selected: 'demo-brand',
    select: selectBrand,
  }),
}))

import { Shell } from '../src/components/Shell'

describe('Shell responsive navigation', () => {
  it('keeps navigation and brand switching available in the mobile surface', () => {
    render(
      <MemoryRouter initialEntries={['/planner?brand=demo-brand']}>
        <Shell title="Planner"><div>Planner body</div></Shell>
      </MemoryRouter>,
    )

    const mobileNav = screen.getByRole('navigation', { name: 'Primary navigation' })
    expect(within(mobileNav).getByRole('link', { name: 'Approvals' })).toHaveAttribute(
      'href', '/approvals?brand=demo-brand',
    )
    const brandSelectors = screen.getAllByLabelText('Brand')
    expect(brandSelectors).toHaveLength(2)
    fireEvent.change(brandSelectors[1], { target: { value: 'second-brand' } })
    expect(selectBrand).toHaveBeenCalledWith('second-brand')
  })
})
