import { expect, test } from '@playwright/test'

test('login page renders its local administrator credentials form', async ({ page }) => {
  await page.goto('/login')
  await expect(page.getByText('登录控制台')).toBeVisible()
  await expect(page.getByLabel('用户名')).toHaveValue('admin')
  await expect(page.getByLabel('密码')).toBeVisible()
  await expect(page.getByRole('checkbox', { name: '记住登录 30 天' })).not.toBeChecked()
  await expect(page.getByLabel('用户名')).toHaveAttribute('autocomplete', 'username')
  await expect(page.getByLabel('密码')).toHaveAttribute('autocomplete', 'current-password')
  await expect(page.getByLabel('管理员身份令牌')).toHaveCount(0)
  await expect(page.getByLabel('Fleet API 令牌（可选）')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '进入本地开发模式' })).toHaveCount(0)
})

for (const phase of ['loading', 'recovering']) {
  test(`remembered login remote logout cancels ${phase} and rejects late identity recovery`, async ({ page, context }) => {
    const expiresAt = Date.now() + 24 * 60 * 60 * 1000
    const token = `header.${Buffer.from(JSON.stringify({ auth_method: 'local', exp: expiresAt / 1000 })).toString('base64url')}.signature`
    let releaseResponse
    const gate = new Promise(resolve => { releaseResponse = resolve })
    let validationCalls = 0
    let healthCalls = 0
    await page.addInitScript(({ token, expiresAt }) => {
      localStorage.setItem('opencli.rememberedLocalLogin', JSON.stringify({ token, expiresAt }))
    }, { token, expiresAt })
    await context.route('**/health', async route => {
      healthCalls += 1
      await gate
      return route.fulfill({ json: { status: 'ok' } })
    })
    await context.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname
      if (path.endsWith('/auth/me')) {
        validationCalls += 1
        if (phase === 'recovering' && validationCalls === 1) return route.fulfill({ status: 503, json: { error: 'temporary outage' } })
        await gate
        return route.fulfill({ json: { data: { subject: 'local-admin', name: 'Mock admin', auth_method: 'local', is_platform_admin: true } } })
      }
      return route.fulfill({ json: { data: [] } })
    })
    try {
      await page.goto('/browser-accounts')
      await expect.poll(() => phase === 'loading' ? validationCalls : healthCalls).toBe(1)
      const otherTab = await context.newPage()
      await otherTab.route('**/remember-test-blank', route => route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>Storage test</title>' }))
      await otherTab.goto('/remember-test-blank')
      await otherTab.evaluate(() => localStorage.removeItem('opencli.rememberedLocalLogin'))
      await expect(page).toHaveURL(/\/login\?returnTo=/)
      await expect(page.getByRole('checkbox', { name: '记住登录 30 天' })).not.toBeChecked()
      const response = page.waitForResponse(response => response.url().endsWith(phase === 'loading' ? '/auth/me' : '/health'))
      releaseResponse()
      await response
      await expect(page.getByRole('button', { name: '账号菜单' })).toHaveCount(0)
      await expect(page.getByText('登录控制台')).toBeVisible()
      expect(validationCalls).toBe(1)
      expect(await page.evaluate(() => localStorage.getItem('opencli.rememberedLocalLogin'))).toBeNull()
    } finally {
      releaseResponse()
    }
  })
}

test('production rejects a retained development session on a protected route', async ({ page }) => {
  await page.addInitScript(() => {
    sessionStorage.setItem('opencli.developmentSession', '1')
  })
  await page.goto('/browser-accounts')
  await expect(page).toHaveURL(/\/login\?returnTo=%2Fbrowser-accounts/)
  await expect(page.getByLabel('密码')).toBeVisible()
  await expect(page.getByRole('button', { name: '进入本地开发模式' })).toHaveCount(0)
})
