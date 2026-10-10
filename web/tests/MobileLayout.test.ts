import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('../src/styles/global.css', import.meta.url), 'utf8')
const mobile = css.slice(css.indexOf('@media (max-width: 900px)'))

describe('mobile layout contract', () => {
  it('wraps the mobile nav instead of clipping it in a horizontal scroller', () => {
    const rule = mobile.match(/\.mobile-nav \{[^}]*\}/)?.[0] ?? ''
    expect(rule).toContain('flex-wrap: wrap')
    expect(rule).not.toContain('overflow-x')
  })

  it('collapses Overview stat and brand grids on narrow screens', () => {
    expect(mobile).toMatch(/\.overview-kpis \{ grid-template-columns: repeat\(2/)
    expect(mobile).toMatch(/\.overview-brands \{ grid-template-columns: minmax\(0, 1fr\)/)
    expect(mobile).toMatch(/\.overview-main \{ grid-template-columns: minmax\(0, 1fr\)/)
    expect(mobile).toMatch(/max-width: 480px\) \{\s*\.overview-kpis \{ grid-template-columns: minmax\(0, 1fr\)/)
  })

  it('lets brand names wrap in Overview brand cards', () => {
    expect(css).toMatch(/\.overview-brands \.brand \{ white-space: normal/)
  })
})
