import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from './client'

export interface Loaded<T> {
  data: T | null
  error: string | null
  loading: boolean
  reload: () => void
}

/** Load data for a view; re-runs when `deps` change; safe against stale responses. */
export function useLoad<T>(loader: () => Promise<T>, deps: unknown[]): Loaded<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [tick, setTick] = useState(0)
  const seq = useRef(0)
  useEffect(() => {
    const mine = ++seq.current
    setLoading(true)
    setError(null)
    loader().then(
      (result) => { if (mine === seq.current) { setData(result); setLoading(false) } },
      (err) => { if (mine === seq.current) { setError(describe(err)); setLoading(false) } },
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick])
  const reload = useCallback(() => setTick((t) => t + 1), [])
  return { data, error, loading, reload }
}

export function describe(err: unknown): string {
  if (err instanceof ApiError) return err.status === 401 ? 'Not signed in — reload to enter the preview password.' : err.detail
  if (err instanceof Error) return err.message
  return String(err)
}
