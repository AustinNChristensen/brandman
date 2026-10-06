import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:8011/app/',
    extraHTTPHeaders: {
      Authorization: `Basic ${Buffer.from('operator:brand-os-e2e-only').toString('base64')}`,
    },
    headless: true,
    launchOptions: { executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' },
  },
  webServer: {
    command: 'uv run --project .. python ../scripts/run_dashboard_e2e.py',
    cwd: process.cwd(),
    url: 'http://127.0.0.1:8011/app/',
    reuseExistingServer: false,
    timeout: 30_000,
  },
})
