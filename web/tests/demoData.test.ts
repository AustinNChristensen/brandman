import { describe, expect, it } from 'vitest'
import { isSampleBrand } from '../src/lib/demoData'

describe('isSampleBrand', () => {
  it('recognizes shipped sample brands and fictional demo names only', () => {
    expect(isSampleBrand({ slug: 'demo-brand', name: 'Demo Brand' })).toBe(true)
    expect(isSampleBrand({ slug: 'demo-personal', name: 'Demo Personal' })).toBe(true)
    expect(isSampleBrand({ slug: 'demo-fictional-studio', name: 'Cedar' })).toBe(true)
    expect(isSampleBrand({ slug: 'acme', name: '[DEMO] Fictional Cedar Studio' })).toBe(true)
    expect(isSampleBrand({ slug: 'demonstration', name: 'Demonstration Co' })).toBe(false)
    expect(isSampleBrand({ slug: 'acme', name: 'Acme' })).toBe(false)
  })
})
