import { expect, test } from '@playwright/test'

test.skip(process.env.OPENCLI_OFFICIAL_LOGIN_SMOKE !== '1', 'External official site requires explicit opt-in')

test('XHS anonymous login exposes official identity response schema', async ({ page }, testInfo) => {
  const metadata = []
  page.on('response', async response => {
    const url = new URL(response.url())
    if (!url.hostname.endsWith('.xiaohongshu.com') || !/user|login|session/.test(url.pathname)
      || !response.headers()['content-type']?.includes('application/json')) return
    try {
      const body = await response.json()
      metadata.push({origin:url.origin,path:url.pathname,httpStatus:response.status(),
        keys:Object.keys(body ?? {}),dataKeys:Object.keys(body?.data ?? {}),
        code:typeof body?.code === 'number' ? body.code : null,
        success:typeof body?.success === 'boolean' ? body.success : null})
    } catch { /* Schema-only evidence; bodies and tokens are never persisted. */ }
  })
  await page.goto('https://www.xiaohongshu.com/explore', {waitUntil:'domcontentloaded'})
  await expect(page.locator('img.qrcode-img')).toBeVisible({timeout:30_000})
  console.log(JSON.stringify(metadata))
  await testInfo.attach('official-identity-schema', {body:JSON.stringify(metadata,null,2),contentType:'application/json'})
})
