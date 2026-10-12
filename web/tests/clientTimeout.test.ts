import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'
import { brands, performance } from '../src/api/endpoints'

function stalledFetch() {
  return vi.fn((_path: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
    init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
  }))
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('GET timeouts', () => {
  it('aborts a stalled GET after 5000 ms by default', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', stalledFetch())
    const result = expect(api.get('/api/x')).rejects.toThrow('took too long')
    await vi.advanceTimersByTimeAsync(5000)
    await result
  })

  it('honours a per-call override', async () => {
    vi.useFakeTimers()
    const fetchMock = stalledFetch()
    vi.stubGlobal('fetch', fetchMock)
    const signalOf = () => (fetchMock.mock.calls[0][1] as RequestInit).signal as AbortSignal
    const result = expect(api.get('/api/x', { timeoutMs: 15000 })).rejects.toThrow('took too long')
    await vi.advanceTimersByTimeAsync(14999)
    expect(signalOf().aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    await result
  })

  it('never times mutations', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, statusText: 'OK', text: async () => '{}' } as Response))
    await api.post('/api/x', {})
    expect((fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0][1].signal).toBeUndefined()
  })

  it('gives provider usage and performance reads 15000 ms and leaves other reads at the default', async () => {
    vi.useFakeTimers()
    const fetchMock = stalledFetch()
    vi.stubGlobal('fetch', fetchMock)
    const calls = [brands.usage('demo'), performance.list('demo')].map((p) => p.catch((e) => e))
    await vi.advanceTimersByTimeAsync(5000)
    expect(fetchMock.mock.calls.every((c) => !(c[1] as RequestInit).signal?.aborted)).toBe(true)
    await vi.advanceTimersByTimeAsync(10000)
    expect((await Promise.all(calls)).every((e) => String(e).includes('took too long'))).toBe(true)
  })
})
