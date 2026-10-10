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
  const previousDeps = useRef(deps)
  useEffect(() => {
    const mine = ++seq.current
    let active = true
    // Retain data on a same-scope refresh, but never show a previous scope.
    if (deps.length !== previousDeps.current.length || deps.some((value, i) => !Object.is(value, previousDeps.current[i]))) setData(null)
    previousDeps.current = deps
    setLoading(true)
    setError(null)
    loader().then(
      (result) => { if (active && mine === seq.current) { setData(result); setLoading(false) } },
      (err) => { if (active && mine === seq.current) { setError(describe(err)); setLoading(false) } },
    )
    return () => { active = false }
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
