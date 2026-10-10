import { useMemo } from 'react'
import { brands as brandsApi, dispatch, newsletters } from '../api/endpoints'
import { optional } from '../api/client'
import type { Brand, CalendarItem, DispatchItem, NewsletterIssue, OperatorWorkflow, ProviderUsage, Scorecard } from '../api/types'
import { useLoad, type Loaded } from '../api/useLoad'
import { useBrands } from './BrandContext'

export type Part = 'awaiting' | 'newsletters' | 'calendar' | 'usage' | 'workflow' | 'scorecard'

export interface BrandBundle {
  brand: Brand
  awaiting: DispatchItem[]
  newsletters: NewsletterIssue[]
  calendar: CalendarItem[]
  usage: ProviderUsage | null
  workflow: OperatorWorkflow | null
  scorecard: Scorecard | null
}

/**
 * Load the requested data for every active brand in parallel. Each part is
 * fetched per brand; a failure in one brand surfaces as the view's error so
 * partial data is never mistaken for the whole picture.
 */
export function useWorkspace(parts: Part[]): Loaded<BrandBundle[]> & { brandsReady: boolean } {
  const { active, loading: brandsLoading } = useBrands()
  const key = active.map((b) => `${b.slug}:${b.has_active_mission ?? 'unknown'}`).join(',')
  const partKey = parts.join(',')
  const loaded = useLoad<BrandBundle[]>(async () => {
    if (brandsLoading) return []
    return Promise.all(active.map(async (brand) => {
      const want = (p: Part) => parts.includes(p)
      const [awaiting, issues, calendar, usage, workflow, scorecard] = await Promise.all([
        want('awaiting') ? dispatch.list(brand.slug, 'awaiting_approval') : Promise.resolve([]),
        want('newsletters') ? newsletters.list(brand.slug) : Promise.resolve([]),
        want('calendar') ? brandsApi.calendar(brand.slug) : Promise.resolve([]),
        want('usage') ? brandsApi.usage(brand.slug) : Promise.resolve(null),
        want('workflow') ? brandsApi.workflow(brand.slug) : Promise.resolve(null),
        want('scorecard') && brand.has_active_mission !== false ? optional(brandsApi.scorecard(brand.slug)).catch(() => null) : Promise.resolve(null),
      ])
      return { brand, awaiting, newsletters: issues, calendar, usage, workflow, scorecard }
    }))
  }, [key, partKey, brandsLoading])
  const brandsReady = !brandsLoading
  return useMemo(() => ({ ...loaded, brandsReady }), [loaded, brandsReady])
}

/** Sidebar meters derived from whatever bundles a page already loaded. */
export function metersFrom(bundles: BrandBundle[] | null) {
  if (!bundles) return undefined
  const approvalsWaiting = bundles.reduce((n, b) => n + b.awaiting.length + b.newsletters.filter((i) => i.lifecycle === 'fact_checked').length, 0)
  let xRequests = 0
  let spend = 0
  let priced = false
  for (const b of bundles) {
    for (const t of b.usage?.totals ?? []) {
      if (t.provider === 'x') xRequests += t.requests
      if (t.estimated_cost !== null && t.currency === 'USD') { spend += Number(t.estimated_cost); priced = true }
    }
  }
  return {
    approvalsWaiting,
    xRequests: bundles.some((b) => b.usage) ? String(xRequests) : undefined,
    spend: priced ? `$${spend.toFixed(2)}` : undefined,
  }
}
