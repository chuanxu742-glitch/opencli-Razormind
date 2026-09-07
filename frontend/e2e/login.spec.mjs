import { expect, test } from '@playwright/test'

test('login page renders its local administrator credentials form', async ({ page }) => {
  await page.goto('/login')
  await expect(page.getByText('登录控制台')).toBeVisible()
  await expect(page.getByLabel('用户名')).toHaveValue('admin')
  await expect(page.getByLabel('密码')).toBeVisible()
  await expect(page.getByLabel('管理员身份令牌')).toHaveCount(0)
  await expect(page.getByLabel('Fleet API 令牌（可选）')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '进入本地开发模式' })).toHaveCount(0)
})

test('production rejects a retained development session on a protected route', async ({ page }) => {
  await page.addInitScript(() => {
    sessionStorage.setItem('opencli.developmentSession', '1')
  })
  await page.goto('/browser-accounts')
  await expect(page).toHaveURL(/\/login\?returnTo=%2Fbrowser-accounts/)
  await expect(page.getByLabel('密码')).toBeVisible()
  await expect(page.getByRole('button', { name: '进入本地开发模式' })).toHaveCount(0)
})
