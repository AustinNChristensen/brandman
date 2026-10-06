import { createContext, useContext, useMemo, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'
import { brands as brandsApi } from '../api/endpoints'
import type { Brand } from '../api/types'
import { useLoad } from '../api/useLoad'

interface BrandState {
  brands: Brand[]
  loading: boolean
  error: string | null
  /** Selected brand slug, or null for "all brands". */
  selected: string | null
  /** Brands the current view should operate on. */
  active: Brand[]
  select: (slug: string | null) => void
  bySlug: (slug: string) => Brand | undefined
  byId: (id: string) => Brand | undefined
}

const Ctx = createContext<BrandState | null>(null)

export function BrandProvider({ children }: { children: ReactNode }) {
  const [params, setParams] = useSearchParams()
  const { data, loading, error } = useLoad(() => brandsApi.list(), [])
  const brands = useMemo(() => data ?? [], [data])
  const selected = params.get('brand')
  const value = useMemo<BrandState>(() => {
    const valid = selected && brands.some((b) => b.slug === selected) ? selected : null
    return {
      brands, loading, error,
      selected: valid,
      active: valid ? brands.filter((b) => b.slug === valid) : brands,
      select: (slug) => {
        const next = new URLSearchParams(params)
        if (slug) next.set('brand', slug); else next.delete('brand')
        setParams(next, { replace: true })
      },
      bySlug: (slug) => brands.find((b) => b.slug === slug),
      byId: (id) => brands.find((b) => b.id === id),
    }
  }, [brands, loading, error, selected, params, setParams])
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useBrands(): BrandState {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useBrands outside BrandProvider')
  return ctx
}
