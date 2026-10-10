import { expect, test } from '@playwright/test'
import path from 'node:path'

for (const width of [1440, 390]) {
  test(`seeded readiness distinguishes implementation and missing connections at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 })
    await page.goto('./agents?brand=demo-brand')
    const summary = page.locator('.agents-summary')
    await expect(summary.getByText('Capabilities implemented')).toBeVisible()
    await expect(summary.getByText('100%')).toBeVisible()
    await expect(summary.getByText('Provider connections')).toBeVisible()
    await expect(summary.getByText('0/5')).toBeVisible()
    const readiness = page.locator('.agents-readiness')
    await expect(readiness.getByText('needs connection').first()).toBeVisible()
    await expect(readiness.getByText('ready', { exact: true })).toHaveCount(0)
    await expect(page.getByText('Connection or proof incomplete')).toBeVisible()
    const proof = readiness.locator('.card').last()
    await expect(proof.getByText('Next:', { exact: false }).first()).toBeVisible()
    for (const panel of [summary, readiness]) {
      const box = await panel.boundingBox()
      expect(box!.width).toBeLessThanOrEqual(width)
      expect(box!.x + box!.width).toBeLessThanOrEqual(width)
    }
    const output = process.env.BRANDMAN_SCREENSHOT_DIR
    if (output) {
      await summary.screenshot({ style: '.mobile-bar, .top { position: static !important; }', path: path.join(output, `summary-${width}.png`) })
      await readiness.screenshot({ style: '.mobile-bar, .top { position: static !important; }', path: path.join(output, `readiness-${width}.png`) })
    }
  })

  test(`connected fixture still needs receipt proof at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 })
    await page.route('**/api/brands/demo-brand/readiness', async route => {
      const response = await route.fetch()
      const data = await response.json()
      for (const check of data.checks) {
        if (typeof check.account_connected !== 'boolean') continue
        Object.assign(check, { account_connected: true, configured: true, healthy: true, status: 'ready', missing_scopes: [], actions: [] })
      }
      await route.fulfill({ json: data })
    })
    await page.goto('./agents?brand=demo-brand')
    await expect(page.locator('.agents-summary').getByText('5/5')).toBeVisible()
    await expect(page.getByText('Connection or proof incomplete')).toBeVisible()
    await expect(page.locator('.agents-readiness').getByText(/needs setup|needs health proof|needs proof/).first()).toBeVisible()
    await expect(page.getByText('live requirements met')).toHaveCount(0)
  })
}
