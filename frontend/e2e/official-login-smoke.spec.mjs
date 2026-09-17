import { expect, test } from '@playwright/test'

// Explicit opt-in only. Fresh Playwright contexts never reuse a personal profile.
// No credentials, response bodies, QR images, source URLs, or traces are persisted.
test.skip(process.env.OPENCLI_OFFICIAL_LOGIN_SMOKE !== '1', 'External official sites require explicit opt-in')

for (const [platform, url] of [
  ['bilibili', 'https://passport.bilibili.com/login'],
  ['douyin', 'https://creator.douyin.com/'],
]) {
  test(`${platform}: official anonymous login offers QR`, async ({ page }, testInfo) => {
    await page.goto(url, { waitUntil: 'domcontentloaded' })
    await expect(page.getByText(/扫码登录/).first()).toBeVisible({ timeout: 30_000 })
    if (platform === 'douyin') {
      console.log(JSON.stringify({platform,entry:await page.getByText(/扫码登录/).first().evaluate(e=>({tag:e.tagName,class:e.className,text:e.textContent}))}))
      await page.getByText(/扫码登录/).first().click()
    }
    const qr = page.getByRole('img', {name: platform === 'bilibili' ? 'Scan me!' : '二维码', exact:true})
    await expect(qr).toHaveCount(1)
    await expect(qr).toBeVisible({timeout:30_000})
    const evidence = await qr.evaluate(element => ({
      tag: element.tagName,
      class: element.getAttribute('class'),
      alt: element.getAttribute('alt'),
      ariaLabel: element.getAttribute('aria-label'),
      parentClass: element.parentElement?.getAttribute('class'),
      grandparentClass: element.parentElement?.parentElement?.getAttribute('class'),
      ancestors: Array.from((function* () { let item = element.parentElement; for (let i = 0; item && i < 5; i++, item = item.parentElement) yield item })()).map(item => ({tag:item.tagName,class:item.getAttribute('class')})),
      width: Math.round(element.getBoundingClientRect().width),
      height: Math.round(element.getBoundingClientRect().height),
      sourceType: (element.getAttribute('src') || '').split(':')[0],
    }))
    expect(evidence.sourceType).toBe('data')
    expect(evidence.width).toBeGreaterThan(90)
    expect(evidence.height).toBeGreaterThan(90)
    const report = {platform, origin:new URL(page.url()).origin, path:new URL(page.url()).pathname, qr:evidence}
    await testInfo.attach('safe-qr-dom-evidence', {body:JSON.stringify(report,null,2),contentType:'application/json'})
    console.log(JSON.stringify(report))
  })
}
