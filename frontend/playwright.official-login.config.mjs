import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  testMatch: ['official-login-smoke.spec.mjs', 'official-xhs-identity-smoke.spec.mjs'],
  workers: 1,
  retries: 0,
  timeout: 60_000,
  use: { browserName: 'chromium', viewport: { width: 1280, height: 900 }, trace: 'off', screenshot: 'off', video: 'off' },
})
