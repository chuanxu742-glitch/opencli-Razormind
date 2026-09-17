import { expect, test } from '@playwright/test'
import lz from 'lz-string'

async function mockSession(page) {
  await page.addInitScript(() => sessionStorage.setItem('opencli.bootstrapIdentityToken', 'input-validation-test'))
  await page.route('**/api/v1/**', (route) => {
    const isIdentity = new URL(route.request().url()).pathname.endsWith('/auth/me')
    return route.fulfill({
      status: isIdentity ? 200 : 503,
      contentType: 'application/json',
      body: JSON.stringify(isIdentity
        ? { data: { subject: 'input-validation-test', name: 'Test', auth_method: 'bootstrap', is_platform_admin: true } }
        : { error: 'Test backend unavailable' }),
    })
  })
  await page.route('**/api/workflow/**', (route) =>
    route.fulfill({ status: 503, contentType: 'application/json', body: '{}' }))
}

test('登录拒绝反斜杠外站返回地址', async ({ page }) => {
  await mockSession(page)
  let externalRequests = 0
  await page.route('**://outside.invalid/**', (route) => {
    externalRequests += 1
    return route.abort()
  })
  await page.goto(`/login?returnTo=${encodeURIComponent('/\\outside.invalid/review')}`)
  await expect(page).toHaveURL(/\/studio$/)
  expect(externalRequests).toBe(0)
})

test('畸形分享链接不覆盖画布或导致渲染崩溃', async ({ page }) => {
  await mockSession(page)
  const errors = []
  page.on('pageerror', (error) => errors.push(error.message))
  const flow = lz.compressToEncodedURIComponent(JSON.stringify({ schema: 'react-flow-powerpack.share.v1' }))
  await page.goto(`/prototype/workflow-studio?flow=${flow}`)
  await page.getByRole('button', { name: '节点工作流', exact: true }).click()
  await expect(page.locator('.react-flow')).toBeVisible()
  await expect(page.locator('.react-flow__node').first()).toBeVisible()
  await expect(page.getByText('已从分享 URL 恢复 workflow', { exact: true })).toHaveCount(0)
  expect(errors).toEqual([])
})
