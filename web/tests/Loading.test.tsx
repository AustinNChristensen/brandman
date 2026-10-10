// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'
import { BrandProvider } from '../src/state/BrandContext'
import Overview from '../src/pages/Overview'
import Content from '../src/pages/Content'
import Approvals from '../src/pages/Approvals'

const brand = { id: 'b1', slug: 'demo-brand', name: 'Demo Brand', approval_policy: 'human_approval_required' }
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status })
function hang(signal?: AbortSignal | null): Promise<Response> {
  return new Promise((_, reject) => signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError'))))
}
function fixture(blocked: (path: string) => boolean = () => false) {
  return vi.fn(async (path: string, init: RequestInit = {}) => {
    if (blocked(path)) return hang(init.signal)
    if (path === '/api/brands') return response([brand])
    if (path.endsWith('/operator-workflow')) return response({ completed_steps: 0, total_steps: 9, progress_percent: 0, next_action: { text: 'Add your first source' } })
    if (path.endsWith('/mission/scorecard')) return response({ goals: [] })
    if (path.endsWith('/provider-usage')) return response({ totals: [], unpriced_request_count: 0 })
    return response([])
  })
}
function view(Page: typeof Overview) {
  return render(<MemoryRouter><BrandProvider><Page /></BrandProvider></MemoryRouter>)
}
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('dashboard loading isolation', () => {
  it('shows Overview cards and queue without waiting for usage metrics', async () => {
    vi.stubGlobal('fetch', fixture((path) => path.endsWith('/provider-usage')))
    view(Overview)
    expect(await screen.findByText('Add your first source')).toBeVisible()
    expect(await screen.findByText('Nothing is waiting on you.')).toBeVisible()
    expect(screen.queryByText(/^Loading/)).not.toBeInTheDocument()
  })
  it('shows newsletter issues without waiting for studio or calendar data', async () => {
    vi.stubGlobal('fetch', fixture((path) => /provider-usage|campaign-graphs|calendar/.test(path)))
    view(Content)
    expect(await screen.findByText('No active newsletter issues. Create one to begin at the idea stage.')).toBeVisible()
    expect(screen.queryByText(/^Loading/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Campaign posts/ }))
    expect(screen.getByText('Loading social drafts, ideas, and campaign posts…')).toBeVisible()
  })
  it('shows the approval queue without waiting for usage metrics', async () => {
    const base = fixture((path) => path.endsWith('/provider-usage'))
    vi.stubGlobal('fetch', vi.fn((path: string, init: RequestInit) => {
      if (path.includes('/dispatch-items?status=')) return Promise.resolve(response([{ id: 'd1', connector: 'x', revision: 1, status: 'awaiting_approval', payload: { body: 'Review this exact material' }, updated_at: '', approval_scope: null }]))
      if (path.endsWith('/validation')) return Promise.resolve(response({ valid: true, errors: [] }))
      return base(path, init)
    }))
    view(Approvals)
    expect(await screen.findByText('1 waiting')).toBeVisible()
    await waitFor(() => expect(screen.queryByText(/^Loading/)).not.toBeInTheDocument())
  })
  it('turns stalled brand details into an actionable error and successfully retries', async () => {
    vi.useFakeTimers()
    let blocked = true
    vi.stubGlobal('fetch', fixture((path) => blocked && path.endsWith('/operator-workflow')))
    view(Overview)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByText('Loading mission and workflow for Demo Brand…')).toBeVisible()
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(screen.getByText(/Demo Brand: The request took too long/)).toBeVisible()
    expect(screen.getByText('Nothing is waiting on you.')).toBeVisible()
    blocked = false
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByText('Add your first source')).toBeVisible()
    expect(screen.queryByText(/The request took too long/)).not.toBeInTheDocument()
  })
  it('surfaces brand-list failures instead of declaring the queue empty and retries discovery', async () => {
    const fetcher = fixture()
    fetcher.mockImplementationOnce(async () => response({ detail: 'Brand discovery unavailable' }, 503))
    vi.stubGlobal('fetch', fetcher)
    view(Approvals)
    expect(await screen.findByText('Brand discovery unavailable')).toBeVisible()
    expect(screen.queryByText('Nothing awaiting approval.')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Nothing awaiting approval.')).toBeVisible()
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/brands')).toHaveLength(2)
  })
})

describe('bounded reads', () => {
  it('aborts a stalled GET and reports a retryable timeout', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn((_path: string, init: RequestInit) => hang(init.signal))
    vi.stubGlobal('fetch', fetcher)
    const result = api.get('/slow').catch((error: Error) => error.message)
    await vi.advanceTimersByTimeAsync(5000)
    expect(await result).toMatch(/took too long/)
    expect(fetcher.mock.calls[0][1].signal?.aborted).toBe(true)
  })
  it('does not abort or retry a mutation when its response is slow', async () => {
    vi.useFakeTimers()
    let finish!: (r: Response) => void
    const fetcher = vi.fn(() => new Promise<Response>((resolve) => { finish = resolve }))
    vi.stubGlobal('fetch', fetcher)
    const result = api.post('/draft', {})
    await vi.advanceTimersByTimeAsync(10000)
    expect(fetcher).toHaveBeenCalledExactlyOnceWith('/draft', expect.not.objectContaining({ signal: expect.anything() }))
    finish(response({ saved: true }))
    expect(await result).toEqual({ saved: true })
  })
  it('clears read timers after success', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn(async () => response([])))
    await api.get('/fast')
    expect(vi.getTimerCount()).toBe(0)
  })
})
