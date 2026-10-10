import { expect, test } from '@playwright/test'

const output = process.env.BRANDMAN_SCREENSHOT_DIR ?? 'test-results/loading'
for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  const size = viewport.width === 390 ? 'mobile' : 'desktop'
  test(`AUS-20 Overview isolates stalled brand details and retries at ${size}`, async ({ page }) => {
    await page.setViewportSize(viewport)
    await page.route('**/api/brands/demo-brand/operator-workflow', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 6500))
      await route.continue().catch(() => {})
    })
    await page.goto('./')
    await expect(page.getByText('Loading mission and workflow for Demo Brand…')).toBeVisible()
    await expect(page.getByText('Nothing is waiting on you.')).toBeVisible({ timeout: 2500 })
    await expect(page.getByText(/Demo Brand: The request took too long/)).toBeVisible({ timeout: 6000 })
    await expect(page.getByText('Loading mission and workflow for Demo Brand…')).toHaveCount(0)
    await page.screenshot({ path: `${output}/overview-error-${size}.png`, fullPage: true })
    await page.unroute('**/api/brands/demo-brand/operator-workflow')
    await page.getByRole('button', { name: 'Retry', exact: true }).click()
    await expect(page.getByText(/Demo Brand: The request took too long/)).toHaveCount(0)
    await expect(page.getByText('Operator loop')).toHaveCount(2)
    await page.screenshot({ path: `${output}/overview-ready-${size}.png`, fullPage: true })
  })
  for (const view of ['content', 'approvals']) {
    test(`AUS-20 ${view} renders while usage is stalled at ${size}`, async ({ page }) => {
      await page.setViewportSize(viewport)
      await page.route('**/provider-usage', async (route) => {
        await new Promise((resolve) => setTimeout(resolve, 6500))
        await route.continue().catch(() => {})
      })
      await page.goto(`./${view}?brand=demo-brand`)
      const primary = page.getByText(view === 'content' ? 'No active newsletter issues. Create one to begin at the idea stage.' : 'Nothing awaiting approval for this brand.')
      await expect(primary).toBeVisible({ timeout: 2500 })
      await expect(page.getByText('Loading…', { exact: true })).toHaveCount(0)
      await page.screenshot({ path: `${output}/${view}-${size}.png`, fullPage: true })
    })
  }
}
