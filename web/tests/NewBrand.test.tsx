// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ create: vi.fn() }))
vi.mock('../src/api/endpoints', () => ({ brands: { create: mocks.create } }))

import { NewBrandCard, slugify } from '../src/components/NewBrand'

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('NewBrandCard', () => {
  it('derives the slug from the name and creates a human-approval brand', async () => {
    mocks.create.mockResolvedValue({ id: 'b9', slug: 'acme-widgets', name: 'Acme Widgets' })
    const onCreated = vi.fn()
    render(<NewBrandCard onCreated={onCreated} />)
    fireEvent.change(screen.getByLabelText('Brand name'), { target: { value: 'Acme Widgets!' } })
    fireEvent.change(screen.getByLabelText('Mission'), { target: { value: 'Help people pick widgets.' } })
    expect(screen.getByLabelText('Slug')).toHaveValue('acme-widgets')
    fireEvent.click(screen.getByRole('button', { name: 'Create brand' }))
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith('acme-widgets'))
    expect(mocks.create).toHaveBeenCalledWith(expect.objectContaining({
      slug: 'acme-widgets', name: 'Acme Widgets!', mission: 'Help people pick widgets.', approval_policy: 'human_approval_required',
    }))
  })

  it('shows a conflict instead of navigating', async () => {
    mocks.create.mockRejectedValue(new Error('Brand slug already exists'))
    const onCreated = vi.fn()
    render(<NewBrandCard onCreated={onCreated} />)
    fireEvent.change(screen.getByLabelText('Brand name'), { target: { value: 'Taken' } })
    fireEvent.change(screen.getByLabelText('Mission'), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create brand' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('already exists')
    expect(onCreated).not.toHaveBeenCalled()
  })

  it('slugifies conservatively', () => { expect(slugify('  Hello, World  ')).toBe('hello-world') })
})
