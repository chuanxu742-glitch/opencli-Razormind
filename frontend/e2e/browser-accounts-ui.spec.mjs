import { expect, test } from '@playwright/test'

const workspace = { id: 'accounts-ui', name: '研究工作区', active: true }
const otherWorkspace = { id: 'other-ui', name: '第二工作区', active: true }
const account = {
  id: 'account-ui', workspace_id: workspace.id, site: 'example.com', label: '研究账号',
  status: 'saved', revision: 3, auth_required: false, paused: false,
  auth_evidence: 'valid', evidence_source: 'rule_verified',
  evidence_observed_at: '2026-09-08T01:00:00Z',
  node_id: 'node-ui', runtime_bundle_id: 'bundle-ui',
  profile_id: 'profile-ui', profile_version: 1, profile_manifest_id: 'manifest-ui', runtime_bundle_version: null,
  login_rule_id: null, login_rule_version: null, manual_confirmed_by: null,
  platform_identity: { provider: 'example', subject: 'example-user', display_name: '研究员' },
  status_reason_code: null, created_at: '2026-09-08T01:00:00Z', updated_at: '2026-09-08T01:00:00Z',
}

test('账号集群归入执行资源，网页登录提示不显示错误告警', async ({ page }) => {
  const observed = { ...account, status: 'presenting', auth_evidence: 'unknown',
    evidence_source: null, platform_identity: null, status_reason_code: 'browser_login_observed' }
  await mockAccounts(page, { items: [observed], existingSession: true, sessionPurpose: 'browser' })
  await page.goto('/browser-accounts')
  await expect(page.locator('[data-sidebar="sidebar"]').getByRole('link', { name: '账号集群', exact: true })).toHaveCount(0)
  await expect(page.getByRole('navigation', { name: '相关视图' }).getByRole('link', { name: '账号集群', exact: true })).toHaveAttribute('aria-current', 'page')
  await expect(page.getByRole('link', { name: '执行资源', exact: true })).toHaveAttribute('data-active', '')
  await page.getByRole('button', { name: account.label, exact: true }).click()
  const detail = page.getByTestId('browser-account-detail')
  await expect(detail).toContainText('网页已登录')
  await expect(detail.getByRole('status').filter({ hasText: '已识别网页上的个人入口' })).toBeVisible()
  await expect(detail.getByRole('alert')).toHaveCount(0)
})

async function mockAccounts(page, { items = [], detailAccount, role = 'admin', ready = true, readinessError = false, existingSession = false, sessionStatus, sessionHasTarget = false, sessionPurpose = 'login', desktopReady = false, nativeAvailable = true, nativeError = () => false, nativeResponseGate, sessionControl, newSessionReady = false, staleListedSession = false, saveOnClose = false, workspaceItems = [workspace, otherWorkspace], memberWorkspaces } = {}) {
  const writes = []
  let nativeOpenCalls = 0
  const initialSession = { id: 'session-a', workspace_id: workspace.id, account_id: account.id, revision: 1, epoch: desktopReady ? 4 : 0, status: sessionStatus ?? (desktopReady ? 'presenting' : 'opening'), profile_state: desktopReady ? 'uncommitted' : 'new', view_generation: 0, purpose: sessionPurpose, ...(desktopReady ? { node_id: 'node-ui', node_boot_id: 'boot-ui', lease_id: 'lease-ui', profile_id: 'profile-ui', command_id: 'command-ui' } : {}), ...(sessionHasTarget ? { tab_id: 1, frame_id: 0, document_id: 'document-a', origin: 'https://example.com' } : {}) }
  let currentSession = existingSession ? initialSession : null
  if (sessionControl) sessionControl.replace = next => { currentSession = { ...initialSession, ...next } }
  await page.addInitScript(() => sessionStorage.setItem('opencli.bootstrapIdentityToken', 'accounts-ui-token'))
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const reply = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ data }) })
    if (request.method() !== 'GET') writes.push({ path, body: request.postDataJSON(), headers: request.headers() })
    if (path.endsWith('/auth/me')) return reply({ subject: 'accounts-ui-user', name: 'UI Test', auth_method: 'bootstrap', is_platform_admin: false })
    if (path === '/api/v1/governance/workspaces') return reply(workspaceItems)
    if (path.endsWith('/login-options')) return reply({ items: ['xiaohongshu', 'douyin', 'bilibili'].map((id, i) => ({ id, label: ['小红书', '抖音', '哔哩哔哩'][i], site: id + '.com', qr_supported: false, browser_login_supported: true, available: false, reason_code: 'login_rule_missing', message: '真实登录规则尚未接入，暂不能扫码登录。' })) })
    if (path.endsWith('/native-window-support')) return reply({ available: nativeAvailable, message: nativeAvailable ? '桌面组件已就绪' : '独立桌面窗口尚未启用，请配置桌面组件。' })
    if (path.endsWith('/login-readiness')) return reply({ ready, code: ready ? 'ready' : 'login_rule_missing', message: ready ? '已就绪' : '账号尚未配置登录规则，无法打开登录窗口。' }, readinessError ? 503 : 200)
    if (path.endsWith('/members')) {
      const workspaceId = path.split('/').at(-2)
      return reply(memberWorkspaces?.[workspaceId] ?? [{ subject: 'accounts-ui-user', role, disabled: false }])
    }
    if (path === `/api/v1/workspaces/${otherWorkspace.id}/browser-accounts`) return reply({ items: [], next_cursor: null })
    if (path.endsWith('/browser-accounts') && request.method() === 'POST') {
      const created = { ...account, ...request.postDataJSON(), status: 'unknown', auth_evidence: 'unknown' }
      items.push(created)
      return reply(created, 201)
    }
    if (path.endsWith('/browser-accounts')) return reply({ items, next_cursor: null })
    if (path.endsWith(`/browser-accounts/${account.id}`) && request.method() === 'PATCH') {
      const target = items.find((item) => item.id === account.id)
      Object.assign(target, { label: request.postDataJSON().label, revision: request.postDataJSON().expected_revision + 1 })
      return reply(target)
    }
    if (path.endsWith(`/browser-accounts/${account.id}`)) return reply(detailAccount ?? items.find((item) => item.id === account.id) ?? account)
    if (path.endsWith('/native-window')) {
      if (nativeError(++nativeOpenCalls)) return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: '无法启动独立浏览器窗口，请重试。' }) })
      if (nativeResponseGate) await nativeResponseGate
      return reply({ status: nativeOpenCalls === 1 ? 'opened' : 'already_open', session_id: path.split('/').at(-2), message: '独立浏览器窗口已打开' })
    }
    if (path.endsWith('/login-sessions') && request.method() === 'GET') return reply(currentSession ? [staleListedSession ? initialSession : currentSession] : [])
    if (path.endsWith('/close')) { currentSession = { ...currentSession, status: saveOnClose ? 'saving' : 'closed', revision: 2 }; return reply(currentSession) }
    if (path.endsWith('/login-sessions')) {
      const purpose = request.postDataJSON().purpose
      if (currentSession && !['closed', 'expired', 'error', 'saved', 'dormant'].includes(currentSession.status)) { currentSession = { ...currentSession, purpose, revision: currentSession.revision + 1 }; return reply(currentSession) }
      currentSession = { ...(newSessionReady ? initialSession : {}), id: 'session-b', workspace_id: workspace.id, account_id: account.id, revision: 1, epoch: 2, status: newSessionReady ? 'presenting' : 'opening', profile_state: 'new', view_generation: 1, purpose }
      return reply(currentSession, 201)
    }
    if (path.includes('/login-sessions/')) return reply(currentSession)
    return reply([], 404)
  })
  return writes
}

test('浏览器只打开独立桌面窗口，重复点击复用会话且不渲染内嵌画面', async ({ page }) => {
  const writes = await mockAccounts(page, {
    items: [{ ...account, status: 'presenting' }], existingSession: true,
    sessionPurpose: 'browser', sessionStatus: 'presenting', desktopReady: true, readinessError: true,
  })
  await page.goto('/browser-accounts')
  const rowOpen = page.getByLabel('账号列表', { exact: true }).getByRole('button', { name: '打开浏览器', exact: true })
  await rowOpen.click()
  await expect(page.getByTestId('browser-native-window-status')).toBeVisible()
  await expect.poll(() => writes.filter(w => w.path.endsWith('/native-window')).length).toBe(1)
  await expect(page.getByTestId('browser-desktop-viewer')).toHaveCount(0)
  await expect(page.locator('canvas, iframe')).toHaveCount(0)
  await expect(page.getByTestId('login-readiness')).toHaveCount(0)
  await rowOpen.click()
  await expect.poll(() => writes.filter(w => w.path.endsWith('/native-window')).length).toBe(2)
  expect(writes.filter(w => w.path.endsWith('/login-sessions') || w.path.endsWith('/browser-grant'))).toHaveLength(0)
  await page.getByRole('button', { name: '收起详情', exact: true }).click()
  expect(writes.filter(w => w.path.endsWith('/close'))).toHaveLength(0)
})

test('查看详情和刷新页面不会自动弹出独立窗口', async ({ page }) => {
  const writes = await mockAccounts(page, { items: [account], existingSession: true, sessionPurpose: 'browser', desktopReady: true })
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '研究账号', exact: true }).click()
  const session = page.getByTestId('browser-account-session')
  await expect(session).toBeVisible()
  expect(writes).toHaveLength(0)
  await session.getByRole('button', { name: '打开浏览器', exact: true }).click()
  await expect(page.getByTestId('browser-native-window-status')).toBeVisible()
  await page.reload()
  await page.getByRole('button', { name: '研究账号', exact: true }).click()
  await expect(session).toBeVisible()
  expect(writes.filter(w => w.path.endsWith('/native-window'))).toHaveLength(1)
})

test('轮询发现后续会话不会沿用旧的打开意图或成功提示', async ({ page }) => {
  const sessionControl = {}
  const writes = await mockAccounts(page, { items: [account], existingSession: true, sessionPurpose: 'browser', desktopReady: true, sessionControl })
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '打开浏览器', exact: true }).click()
  await expect(page.getByTestId('browser-native-window-status')).toBeVisible()
  sessionControl.replace({ id: 'session-external', revision: 5, status: 'opening', lease_id: null })
  const session = page.getByTestId('browser-account-session')
  await expect(session).toContainText('session-external')
  await expect(page.getByTestId('browser-native-window-status')).toHaveCount(0)
  sessionControl.replace({ id: 'session-external', revision: 6, status: 'presenting' })
  await expect(session.getByRole('button', { name: '打开浏览器', exact: true })).toBeEnabled()
  expect(writes.filter(w => w.path.endsWith('/native-window'))).toHaveLength(1)
  await session.getByRole('button', { name: '打开浏览器', exact: true }).click()
  await expect(page.getByTestId('browser-native-window-status')).toBeVisible()
  expect(writes.filter(w => w.path.endsWith('/native-window')).at(-1).path).toContain('/session-external/')
})

test('旧窗口请求延迟返回不会给新会话显示已打开', async ({ page }) => {
  const sessionControl = {}
  let releaseResponse
  const nativeResponseGate = new Promise(resolve => { releaseResponse = resolve })
  const writes = await mockAccounts(page, { items: [account], existingSession: true, sessionPurpose: 'browser', desktopReady: true, sessionControl, nativeResponseGate })
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '打开浏览器', exact: true }).click()
  await expect.poll(() => writes.filter(w => w.path.endsWith('/native-window')).length).toBe(1)
  sessionControl.replace({ id: 'session-external', revision: 5, status: 'opening', lease_id: null })
  await expect(page.getByTestId('browser-account-session')).toContainText('session-external')
  releaseResponse()
  await expect(page.getByTestId('browser-native-window-status')).toHaveCount(0)
  expect(writes.filter(w => w.path.endsWith('/native-window'))).toHaveLength(1)
})

test('未启用桌面组件时显示原因且不创建浏览器会话', async ({ page }) => {
  const writes = await mockAccounts(page, { items: [account], nativeAvailable: false })
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '打开浏览器', exact: true }).click()
  await expect(page.getByTestId('native-window-support')).toContainText('独立桌面窗口尚未启用')
  expect(writes).toHaveLength(0)
})

test('未启用桌面组件时仍可保存新账号，不承诺打开窗口', async ({ page }) => {
  const writes = await mockAccounts(page, { nativeAvailable: false })
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '添加账号', exact: true }).click()
  await page.getByLabel('平台', { exact: true }).selectOption('custom')
  await page.getByLabel('测试站点地址', { exact: true }).fill('example.com')
  await page.getByLabel('账号名称', { exact: true }).fill('待开启账号')
  await page.getByRole('button', { name: '保存账号', exact: true }).click()
  await expect(page.getByTestId('browser-account-detail')).toContainText('待开启账号')
  await expect(page.getByTestId('native-window-support')).toContainText('独立桌面窗口尚未启用')
  expect(writes.filter(w => w.path.endsWith('/browser-accounts'))).toHaveLength(1)
  expect(writes.filter(w => w.path.endsWith('/login-sessions') || w.path.endsWith('/native-window'))).toHaveLength(0)
})

test('独立窗口启动失败可手动重试，不反复新建会话或自动重试', async ({ page }) => {
  const writes = await mockAccounts(page, { items: [account], existingSession: true, sessionPurpose: 'browser', desktopReady: true, nativeError: attempt => attempt === 1 })
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '打开浏览器', exact: true }).click()
  const session = page.getByTestId('browser-account-session')
  await expect(session.getByRole('alert')).toContainText('无法启动独立浏览器窗口')
  expect(writes.filter(w => w.path.endsWith('/native-window'))).toHaveLength(1)
  await session.getByRole('button', { name: '打开浏览器', exact: true }).click()
  await expect(page.getByTestId('browser-native-window-status')).toBeVisible()
  expect(writes.filter(w => w.path.endsWith('/native-window'))).toHaveLength(2)
  expect(writes.filter(w => w.path.endsWith('/login-sessions'))).toHaveLength(0)
})

test('新账号会话准备完成后只打开一次独立窗口', async ({ page }) => {
  const writes = await mockAccounts(page, { items: [account], desktopReady: true, newSessionReady: true })
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '打开浏览器', exact: true }).click()
  await expect(page.getByTestId('browser-native-window-status')).toBeVisible()
  expect(writes.filter(w => w.path.endsWith('/login-sessions'))).toHaveLength(1)
  expect(writes.filter(w => w.path.endsWith('/native-window'))).toHaveLength(1)
})

test('已有profile重新启动时等待浏览器完成启动才连接窗口', async ({ page }) => {
  const sessionControl = {}
  const writes = await mockAccounts(page, { items: [account], existingSession: true, desktopReady: true, sessionPurpose: 'browser', sessionStatus: 'opening', sessionControl })
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '打开浏览器', exact: true }).click()
  await expect(page.getByTestId('browser-account-session')).toContainText('正在等待账号浏览器启动')
  expect(writes.filter(w => w.path.endsWith('/native-window'))).toHaveLength(0)
  sessionControl.replace({ status: 'presenting', revision: 2 })
  await expect(page.getByTestId('browser-native-window-status')).toBeVisible()
  expect(writes.filter(w => w.path.endsWith('/native-window'))).toHaveLength(1)
})

test('空账号列表只保留一个添加入口，基础表单在弹窗中', async ({ page }) => {
  await mockAccounts(page)
  await page.goto('/browser-accounts')
  await expect(page.getByRole('heading', { name: '账号集群', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '添加账号', exact: true })).toHaveCount(1)
  await expect(page.getByLabel('平台', { exact: true })).toHaveCount(0)
  await expect(page.getByText('选择账号', { exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: '添加账号', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '添加账号', exact: true })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByLabel('平台', { exact: true })).toBeVisible()
  await expect(dialog.getByLabel('账号名称', { exact: true })).toBeVisible()
  await expect(dialog.getByLabel(/所属节点/)).not.toBeVisible()
  await dialog.getByLabel('平台', { exact: true }).selectOption('custom')
  await dialog.getByText('高级配置', { exact: true }).click()
  await expect(dialog.getByLabel(/所属节点/)).toBeVisible()
})

test('创建账号继续同一账号登录且发送工作区与幂等信息', async ({ page }) => {
  const writes = await mockAccounts(page)
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '添加账号', exact: true }).click()
  await page.getByLabel('平台', { exact: true }).selectOption('custom')
  await page.getByLabel('测试站点地址', { exact: true }).fill('example.com')
  await page.getByLabel('账号名称', { exact: true }).fill('新研究账号')
  await page.getByRole('button', { name: '创建并打开浏览器', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '添加账号', exact: true })).toHaveCount(0)
  await expect(page.getByTestId('browser-account-detail')).toContainText('新研究账号')
  await expect.poll(() => writes.filter((write) => write.path.endsWith('/login-sessions')).length).toBe(1)
  const create = writes.find((write) => write.path.endsWith('/browser-accounts'))
  expect(create.body).toEqual({ workspace_id: workspace.id, site: 'example.com', label: '新研究账号' })
  expect(create.headers['idempotency-key']).toBeTruthy()
  expect(writes.find((write) => write.path.endsWith('/login-sessions')).body.expected_revision).toBe(3)
})

test('已有账号以列表展示，详情按需打开，切换工作区清除详情', async ({ page }) => {
  await mockAccounts(page, { items: [account] })
  await page.goto('/browser-accounts')
  await expect(page.getByRole('table')).toContainText('研究账号')
  await expect(page.getByTestId('browser-account-detail')).toHaveCount(0)
  await page.getByRole('button', { name: '研究账号', exact: true }).click()
  await expect(page.getByTestId('browser-account-detail')).toContainText('研究账号')
  await page.getByLabel('工作区', { exact: true }).selectOption(otherWorkspace.id)
  await expect(page.getByTestId('browser-account-detail')).toHaveCount(0)
  await expect(page.getByText('研究账号', { exact: true })).toHaveCount(0)
})

test('只读成员不能创建账号或开启登录会话', async ({ page }) => {
  await mockAccounts(page, { items: [{ ...account, auth_evidence: 'unknown', status: 'dormant' }], role: 'viewer' })
  await page.goto('/browser-accounts')
  const add = page.getByRole('button', { name: '添加账号', exact: true })
  await expect(add).toBeDisabled()
  await expect(page.getByLabel('账号列表', { exact: true }).getByRole('button', { name: '打开浏览器', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '研究账号', exact: true }).click()
  await expect(page.getByTestId('browser-account-detail').getByRole('button', { name: '打开浏览器', exact: true })).toBeDisabled()
  await page.getByTestId('browser-account-detail').getByRole('button', { name: '更多', exact: true }).click()
  await expect(page.getByRole('menuitem', { name: '重命名', exact: true })).toBeDisabled()
})

test('390px下创建弹窗不超出视口且可以关闭', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 })
  await mockAccounts(page, { items: [account] })
  await page.goto('/browser-accounts')
  await expect(page.getByRole('button', { name: '打开浏览器', exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  await page.getByRole('button', { name: '添加账号', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '添加账号', exact: true })
  await dialog.getByLabel('平台', { exact: true }).selectOption('custom')
  await dialog.getByText('高级配置', { exact: true }).click()
  const box = await dialog.boundingBox()
  expect(box.x).toBeGreaterThanOrEqual(0)
  expect(box.x + box.width).toBeLessThanOrEqual(390)
  expect(box.height).toBeLessThanOrEqual(800)
  await expect(dialog.getByRole('button', { name: '创建并打开浏览器', exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
})


test('内置三平台无需填写地址，未接入时只保存且不请求登录', async ({ page }) => {
  const writes = await mockAccounts(page, { ready: false })
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '添加账号', exact: true }).click()
  const platform = page.getByLabel('平台', { exact: true })
  for (const [id, name] of [['xiaohongshu', '小红书'], ['douyin', '抖音'], ['bilibili', '哔哩哔哩']]) {
    await platform.selectOption(id)
    await expect(page.getByRole('dialog')).toContainText(name)
    await expect(page.getByLabel('测试站点地址')).toHaveCount(0)
    await expect(page.getByRole('button', { name: '创建并打开浏览器', exact: true })).toHaveCount(0)
  }
  await platform.selectOption('xiaohongshu')
  await page.getByLabel('账号名称', { exact: true }).fill('小红书运营账号')
  await page.getByRole('button', { name: '保存账号', exact: true }).click()
  await expect(page.getByTestId('login-readiness')).toContainText('账号尚未配置登录规则')
  await expect(page.getByTestId('browser-account-detail').getByRole('button', { name: '打开浏览器', exact: true })).toBeEnabled()
  expect(writes.filter(write => write.path.endsWith('/login-sessions'))).toHaveLength(0)
  expect(writes.find(write => write.path.endsWith('/browser-accounts')).body.site).toBe('xiaohongshu.com')
})

test('卡住账号显示未就绪原因且不误报登录成功', async ({ page }) => {
  const writes = await mockAccounts(page, { ready: false, existingSession: true, items: [{ ...account, status: 'opening' }] })
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '研究账号', exact: true }).click()
  await expect(page.getByTestId('login-readiness')).toContainText('暂时无法登录')
  await expect(page.getByTestId('browser-account-session')).toContainText('登录会话请求正在排队')
  await expect(page.getByText('登录会话已打开', { exact: true })).toHaveCount(0)
  expect(writes).toHaveLength(0)
  await page.getByTestId('browser-account-session').getByRole('button', { name: '更多', exact: true }).click()
  await page.getByRole('menuitem', { name: '取消排队', exact: true }).click()
  await expect.poll(() => writes.filter(write => write.path.endsWith('/close')).length).toBe(1)
  await expect(page.getByTestId('browser-account-session')).toContainText('已关闭')
})

test('已有登录会话时打开浏览器原地提升而不创建副本', async ({ page }) => {
  const writes = await mockAccounts(page, { items: [{ ...account, auth_evidence: 'unknown', status: 'challenge' }], existingSession: true })
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '打开浏览器', exact: true }).click()
  await expect(page.getByTestId('browser-account-session')).toBeVisible()
  expect(writes.filter((write) => write.path.endsWith('/login-sessions'))).toHaveLength(1)
  expect(writes.find((write) => write.path.endsWith('/login-sessions')).body.purpose).toBe('browser')
})

test('同一账号重复按行登录时复用现有本地会话，不创建副本', async ({ page }) => {
  const writes = await mockAccounts(page, { items: [{ ...account, auth_evidence: 'unknown', status: 'dormant' }] })
  await page.goto('/browser-accounts')
  await page.getByLabel('账号列表', { exact: true }).getByRole('button', { name: '打开浏览器', exact: true }).click()
  await expect.poll(() => writes.filter((write) => write.path.endsWith('/login-sessions')).length).toBe(1)
  await page.getByLabel('账号列表', { exact: true }).getByRole('button', { name: '打开浏览器', exact: true }).click()
  await page.waitForTimeout(100)
  expect(writes.filter((write) => write.path.endsWith('/login-sessions'))).toHaveLength(1)
})

test('关闭本地会话后可对同一账号再次登录', async ({ page }) => {
  const writes = await mockAccounts(page, { items: [{ ...account, auth_evidence: 'unknown', status: 'dormant' }] })
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '打开浏览器', exact: true }).click()
  await expect.poll(() => writes.filter((write) => write.path.endsWith('/login-sessions')).length).toBe(1)
  await page.getByTestId('browser-account-session').getByRole('button', { name: '更多', exact: true }).click()
  await page.getByRole('menuitem', { name: '取消排队', exact: true }).click()
  await expect.poll(() => writes.filter((write) => write.path.endsWith('/close')).length).toBe(1)
  await page.getByLabel('账号列表', { exact: true }).getByRole('button', { name: '打开浏览器', exact: true }).click()
  await expect.poll(() => writes.filter((write) => write.path.endsWith('/login-sessions')).length).toBe(2)
})

test('行继续登录复用已有会话而不创建新会话', async ({ page }) => {
  const writes = await mockAccounts(page, { items: [{ ...account, auth_evidence: 'unknown', status: 'presenting' }], existingSession: true, sessionStatus: 'presenting' })
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '打开浏览器', exact: true }).click()
  await expect(page.getByTestId('browser-account-session')).toContainText('展示中')
  expect(writes.filter((write) => write.path.endsWith('/login-sessions'))).toHaveLength(1)
  expect(writes.find((write) => write.path.endsWith('/login-sessions')).body.purpose).toBe('browser')
})

test('关闭 A 后创建 B 时，陈旧列表 A 不会覆盖本地 B', async ({ page }) => {
  const writes = await mockAccounts(page, { items: [{ ...account, auth_evidence: 'unknown', status: 'dormant' }], existingSession: true, staleListedSession: true })
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '打开浏览器', exact: true }).click()
  await page.getByTestId('browser-account-session').getByRole('button', { name: '更多', exact: true }).click()
  await page.getByRole('menuitem', { name: '取消排队', exact: true }).click()
  await expect.poll(() => writes.filter((write) => write.path.endsWith('/close')).length).toBe(1)
  await expect(page.getByTestId('browser-account-session')).toContainText('已关闭')
  const startsBeforeReopen = writes.filter((write) => write.path.endsWith('/login-sessions')).length
  await page.getByLabel('账号列表', { exact: true }).getByRole('button', { name: '打开浏览器', exact: true }).click()
  await expect.poll(() => writes.filter((write) => write.path.endsWith('/login-sessions')).length).toBe(startsBeforeReopen + 1)
  await page.getByTestId('browser-account-session').getByText('会话诊断信息', { exact: true }).click()
  await expect(page.getByTestId('browser-account-session')).toContainText('session-b')
})

test('已有会话在就绪检查失败时仍可继续打开其门户且不新建会话', async ({ page }) => {
  const writes = await mockAccounts(page, { items: [{ ...account, auth_evidence: 'unknown', status: 'presenting' }], readinessError: true, existingSession: true, sessionStatus: 'presenting', sessionHasTarget: true })
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '研究账号', exact: true }).click()
  await expect(page.getByTestId('login-readiness')).toContainText('登录条件检查失败')
  await expect(page.getByTestId('browser-account-portal')).toBeVisible()
  await page.getByRole('button', { name: '关闭门户', exact: true }).click()
  await expect(page.getByTestId('browser-account-portal')).toHaveCount(0)
  await page.getByTestId('browser-account-session').getByRole('button', { name: '查看登录画面', exact: true }).click()
  await expect(page.getByTestId('browser-account-portal')).toBeVisible()
  expect(writes.filter((write) => write.path.endsWith('/login-sessions'))).toHaveLength(0)
})

test('单个有效工作区时 URL 指向无权工作区仍可切换且不请求账号数据', async ({ page }) => {
  await mockAccounts(page, { workspaceItems: [workspace], memberWorkspaces: { [workspace.id]: [{ subject: 'accounts-ui-user', role: 'admin', disabled: false }], [otherWorkspace.id]: [] } })
  await page.goto(`/browser-accounts?workspace=${otherWorkspace.id}`)
  await expect(page.getByText('无权访问此工作区')).toBeVisible()
  await expect(page.getByLabel('工作区', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '添加账号', exact: true })).toHaveCount(0)
})

test('列表新版本覆盖陈旧详情，行登录使用最新账号状态和 revision', async ({ page }) => {
  const changingAccount = { ...account, auth_evidence: 'unknown', status: 'opening' }
  const writes = await mockAccounts(page, { items: [changingAccount], detailAccount: { ...changingAccount } })
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '研究账号', exact: true }).click()
  await expect(page.getByTestId('browser-account-detail')).toContainText('正在登录')
  changingAccount.status = 'dormant'
  changingAccount.revision = 4
  await page.getByRole('button', { name: '刷新账号列表', exact: true }).click()
  await expect(page.getByTestId('browser-account-detail')).toContainText('待登录')
  await page.getByLabel('账号列表', { exact: true }).getByRole('button', { name: '打开浏览器', exact: true }).click()
  await expect.poll(() => writes.filter((write) => write.path.endsWith('/login-sessions')).length).toBe(1)
  expect(writes.find((write) => write.path.endsWith('/login-sessions')).body.expected_revision).toBe(4)
})

test('重命名保存备注名称，列表和刷新后同步且不创建登录会话', async ({ page }) => {
  const writes = await mockAccounts(page, { items: [{ ...account }] })
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '研究账号', exact: true }).click()
  await page.getByTestId('browser-account-detail').getByRole('button', { name: '更多', exact: true }).click()
  await page.getByRole('menuitem', { name: '重命名', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '重命名账号', exact: true })
  await expect(dialog.getByLabel('账号名称')).toHaveValue('研究账号')
  await dialog.getByLabel('账号名称').fill('   ')
  await expect(dialog.getByRole('button', { name: '保存名称', exact: true })).toBeDisabled()
  await dialog.getByLabel('账号名称').fill('  GitHub 工作号  ')
  await dialog.getByRole('button', { name: '保存名称', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'GitHub 工作号', exact: true })).toBeVisible()
  expect(writes).toHaveLength(1)
  expect(writes[0].body).toEqual({ label: 'GitHub 工作号', expected_revision: 3 })
  expect(writes[0].headers['if-match']).toBe('3')
  await page.reload()
  await expect(page.getByRole('button', { name: 'GitHub 工作号', exact: true })).toBeVisible()
})

test('取消重命名不保存，重新打开仍显示原名称', async ({ page }) => {
  const writes = await mockAccounts(page, { items: [{ ...account }] })
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '研究账号', exact: true }).click()
  const more = page.getByTestId('browser-account-detail').getByRole('button', { name: '更多', exact: true })
  await more.click()
  await page.getByRole('menuitem', { name: '重命名', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '重命名账号', exact: true })
  await dialog.getByLabel('账号名称').fill('未保存的名称')
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await more.click()
  await page.getByRole('menuitem', { name: '重命名', exact: true }).click()
  await expect(dialog.getByLabel('账号名称')).toHaveValue('研究账号')
  expect(writes).toHaveLength(0)
})


test('关闭保存期间抵御旧轮询且不重复打开或关闭，完成后才可重开', async ({ page }) => {
  const sessionControl = {}
  const writes = await mockAccounts(page, {
    items: [{ ...account, status: 'presenting' }], existingSession: true,
    sessionPurpose: 'browser', desktopReady: true, sessionHasTarget: true,
    sessionControl, saveOnClose: true, staleListedSession: true,
  })
  await page.goto('/browser-accounts')
  await page.getByRole('button', { name: '研究账号', exact: true }).click()
  const session = page.getByTestId('browser-account-session')
  await session.getByRole('button', { name: '更多', exact: true }).click()
  await page.getByRole('menuitem', { name: '关闭浏览器并保存', exact: true }).click()
  await expect(page.getByTestId('browser-saving-status')).toBeVisible()
  await session.getByRole('button', { name: '更多', exact: true }).click()
  await expect(page.getByRole('menuitem', { name: '关闭浏览器并保存', exact: true })).toBeDisabled()
  await session.getByRole('button', { name: '更多', exact: true }).click()
  await expect(page.getByRole('menu')).toBeHidden()
  await expect(session.getByRole('button', { name: '打开浏览器', exact: true })).toBeDisabled()
  // Even equal-revision stale snapshots cannot undo the acknowledged saving state.
  sessionControl.replace({ status: 'presenting', revision: 2 })
  await page.getByLabel('账号列表', { exact: true }).getByRole('button', { name: '打开浏览器', exact: true }).click()
  await expect.poll(() => writes.filter(w => w.path.endsWith('/close')).length).toBe(1)
  await page.waitForResponse(response => response.url().includes('/login-sessions/session-a') && response.request().method() === 'GET')
  await expect(page.getByTestId('browser-saving-status')).toBeVisible()
  expect(writes.filter(w => w.path.endsWith('/native-window') || w.path.endsWith('/login-sessions'))).toHaveLength(0)
  sessionControl.replace({ status: 'saved', revision: 3, profile_state: 'committed' })
  await expect(page.getByTestId('browser-saving-status')).toHaveCount(0)
  const open = page.getByTestId('browser-account-detail').getByRole('button', { name: '打开浏览器', exact: true }).first()
  await expect(open).toBeEnabled()
  expect(writes.filter(w => w.path.endsWith('/login-sessions'))).toHaveLength(0)
  await open.click()
  await expect.poll(() => writes.filter(w => w.path.endsWith('/login-sessions')).length).toBe(1)
})
