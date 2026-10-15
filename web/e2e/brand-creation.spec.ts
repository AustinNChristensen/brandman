import { expect, test } from '@playwright/test'

for (const width of [1440, 390]) {
  test(`brand creation guides channel setup at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 })
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.goto('./settings')
    if (page.url().includes('/login')) {
      await page.getByLabel('Preview password').fill('brand-os-e2e-only')
      await page.getByRole('button', { name: 'Open BrandMan' }).click()
    }
    const slug = `channel-setup-${width}`
    await page.getByLabel('Brand name').fill(`Channel Setup ${width}`)
    await page.getByLabel('Mission', { exact: true }).fill('Help our audience choose useful tools.')
    await page.getByRole('button', { name: 'Create brand', exact: true }).click()
    const setup = page.getByRole('link', { name: 'Set up channels' })
    await expect(setup).toBeVisible()
    await expect(setup).toHaveAttribute('href', `/app/integrations?brand=${slug}`)
    await expect(page.getByLabel('Brand', { exact: true }).first()).toHaveValue(slug)
    const card = page.locator('.card').filter({ has: setup })
    const bounds = await card.boundingBox()
    expect(bounds).not.toBeNull()
    expect(bounds!.x).toBeGreaterThanOrEqual(0)
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width)
    await setup.click()
    await expect(page.getByRole('heading', { name: 'Integrations', exact: true })).toBeVisible()
    await expect(page).toHaveURL(new RegExp(`/integrations\\?brand=${slug}$`))
    await expect(page.getByText('Loading...')).toHaveCount(0)
    for (const demo of ['demo-brand', 'demo-personal']) {
      await page.goto(`./?brand=${demo}`)
      await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible()
      await expect(page.getByText('Operator loop', { exact: true })).toBeVisible()
    }
    expect(errors).toEqual([])
  })
}
