import { expect, test } from '@playwright/test'

for (const width of [1440, 390]) {
  for (const scope of ['', '?brand=demo-personal']) {
    test(`seeded overview avoids absent mission reads at ${width}px ${scope || 'all brands'}`, async ({ page }) => {
      await page.setViewportSize({ width, height: 1000 })
      const missing: string[] = []
      const personalMissionReads: string[] = []
      page.on('response', response => {
        if (response.url().includes('/api/') && response.status() === 404) missing.push(response.url())
      })
      page.on('request', request => {
        if (request.url().includes('/api/brands/demo-personal/mission')) personalMissionReads.push(request.url())
      })
      await page.goto(`./${scope}`)
      await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible()
      await expect(page.getByText('Loading...')).toHaveCount(0)
      await expect(page.getByText('Operator loop').first()).toBeVisible()
      expect(personalMissionReads).toEqual([])
      expect(missing).toEqual([])
      if (!scope) await expect(page.getByText('Demo Brand 30-day growth')).toBeVisible()
    })
  }
}
