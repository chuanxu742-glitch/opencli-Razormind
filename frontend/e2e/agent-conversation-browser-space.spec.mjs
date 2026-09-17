import { expect, test } from '@playwright/test'

const TOKEN_KEY = 'opencli.bootstrapIdentityToken'
const workspaceId = 'workspace-agent-e2e'
const conversationId = 'conversation-agent-e2e'
const spaceId = 'space-browser-e2e'
const taskId = 'task-browser-e2e'

const identity = {
  subject: 'test-admin',
  email: null,
  name: 'Test Admin',
  username: 'test-admin',
  picture: null,
  is_platform_admin: true,
  auth_method: 'bootstrap',
}

const workspace = {
  id: workspaceId,
  name: 'Agent E2E Workspace',
  slug: 'agent-e2e-workspace',
}

const response = (data) => ({
  contentType: 'application/json',
  body: JSON.stringify({ success: true, data }),
})

function conversation(turns = []) {
  return {
    id: conversationId,
    workspace_id: workspaceId,
    title: 'Global Agent session',
    status: 'active',
    created_by_user_id: 'user-e2e',
    context_binding: { surface: '浏览器' },
    revision: turns.length,
    created_at: '2026-08-29T00:00:00Z',
    updated_at: '2026-08-29T00:00:00Z',
    turns,
  }
}

const proposal = {
  tool: 'update_provider',
  args: { provider_id: 'provider-e2e', enabled: false },
  summary: '停用测试模型提供商',
  diff: 'enabled: true → false',
  work_item_id: 'work-item-e2e',
  workspace_id: workspaceId,
  proposal_version: 'agent-control-proposal/v1:e2e',
}

function browserSpace(status = 'idle') {
  return {
    id: spaceId,
    workspace_id: workspaceId,
    browser_instance_id: 'browser-instance-e2e',
    binding_id: null,
    owner_type: 'operator',
    owner_id: 'test-admin',
    status,
    granted_capabilities: ['page.metadata'],
    revision: 1,
    last_error_code: null,
    created_at: '2026-08-29T00:00:00Z',
    updated_at: '2026-08-29T00:00:00Z',
  }
}

async function retainTestIdentity(page) {
  await page.addInitScript(
    ({ key }) => sessionStorage.setItem(key, 'retained-test-token'),
    { key: TOKEN_KEY },
  )
}

test('Agent conversation survives refresh and confirms a Workspace-bound proposal', async ({ page }) => {
  await retainTestIdentity(page)
  let turns = []
  let confirmed = false

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path.endsWith('/auth/me')) return route.fulfill(response(identity))
    if (path.endsWith('/workspaces') && request.method() === 'GET') {
      return route.fulfill(response([workspace]))
    }
    if (path.endsWith('/chat/sessions') && request.method() === 'GET') {
      return route.fulfill(response(turns.length ? [conversation(turns)] : []))
    }
    if (path.endsWith(`/chat/sessions/${conversationId}`) && request.method() === 'GET') {
      return route.fulfill(response(conversation(turns)))
    }
    if (path.endsWith('/chat/sessions') && request.method() === 'POST') {
      return route.fulfill(response(conversation()))
    }
    if (path.endsWith(`/chat/sessions/${conversationId}/messages`)) {
      turns = [{
        id: 'turn-agent-e2e',
        sequence: 1,
        request_id: 'request-agent-e2e',
        user_content: '停用测试模型',
        response: { type: 'proposal', proposal },
        context_binding: { surface: '浏览器' },
        tool_trace: [],
        status: 'proposal',
        error_code: null,
        error_message: null,
        created_at: '2026-08-29T00:00:00Z',
        updated_at: '2026-08-29T00:00:00Z',
      }]
      return route.fulfill(response({ conversation_id: conversationId, turn: turns[0] }))
    }
    if (path.endsWith('/chat/confirm')) {
      const body = JSON.parse(request.postData() ?? '{}')
      expect(body.proposal.workspace_id).toBe(workspaceId)
      expect(body.proposal.work_item_id).toBe(proposal.work_item_id)
      confirmed = true
      return route.fulfill(response({ applied: true }))
    }
    return route.fulfill(response([]))
  })

  await page.goto(`/browsers?workspace=${workspaceId}`)
  await page.getByRole('button', { name: 'Agent', exact: true }).click()
  await page.getByLabel('给全局 Agent 的消息').fill('停用测试模型')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(page.getByText('待确认操作')).toBeVisible()
  await expect(page.getByText(proposal.diff)).toBeVisible()

  await page.reload()
  await page.getByRole('button', { name: 'Agent', exact: true }).click()
  await expect(page.getByText('待确认操作')).toBeVisible()
  await page.getByRole('button', { name: '确认执行', exact: true }).click()
  await expect.poll(() => confirmed).toBe(true)
  await expect(page.getByText(`已执行：${proposal.summary}`)).toBeVisible()
})

test('Browser Space creates, submits a capability task, shows events, and closes', async ({ page }) => {
  await retainTestIdentity(page)
  let space = null
  let events = []

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path.endsWith('/auth/me')) return route.fulfill(response(identity))
    if (path.endsWith('/browser-spaces/instances')) {
      return route.fulfill(response([{ id: 'browser-instance-e2e', capabilities: ['page.metadata'] }]))
    }
    if (path.endsWith('/workspaces') && request.method() === 'GET') {
      return route.fulfill(response([workspace]))
    }
    if (path.endsWith('/browser-spaces') && request.method() === 'GET') {
      return route.fulfill(response(space ? [space] : []))
    }
    if (path.endsWith('/browser-spaces') && request.method() === 'POST') {
      space = browserSpace()
      events = []
      return route.fulfill(response(space))
    }
    if (path.endsWith(`/browser-spaces/${spaceId}/events`)) {
      return route.fulfill(response(events))
    }
    if (path.endsWith(`/browser-spaces/${spaceId}/tasks`)) {
      events = [
        { id: 'event-queued', space_id: spaceId, task_id: taskId, sequence: 1, kind: 'queued', payload: {}, created_at: '2026-08-29T00:00:00Z' },
        { id: 'event-started', space_id: spaceId, task_id: taskId, sequence: 2, kind: 'started', payload: {}, created_at: '2026-08-29T00:00:01Z' },
        { id: 'event-completed', space_id: spaceId, task_id: taskId, sequence: 3, kind: 'completed', payload: { result: { title: 'Example' } }, created_at: '2026-08-29T00:00:02Z' },
      ]
      return route.fulfill(response({
        space_id: spaceId,
        task_id: taskId,
        operation_id: 'operation-browser-e2e',
        capability: 'page.metadata',
        status: 'completed',
        result: { title: 'Example' },
        error: null,
      }))
    }
    if (path.endsWith(`/browser-spaces/${spaceId}/close`)) {
      space = browserSpace('closed')
      return route.fulfill(response(space))
    }
    if (path.endsWith(`/browser-spaces/${spaceId}`) && request.method() === 'GET') {
      return route.fulfill(response({ ...(space ?? browserSpace()), active_task: null }))
    }
    return route.fulfill(response([]))
  })

  await page.goto(`/browsers?workspace=${workspaceId}`)
  await expect(page.getByText('Browser Spaces', { exact: true })).toBeVisible()
  const browserInstanceInput = page.getByLabel('BrowserInstance ID')
  await expect(browserInstanceInput).toBeEnabled()
  await browserInstanceInput.selectOption('browser-instance-e2e')
  await page.getByRole('button', { name: '创建 Browser Space', exact: true }).click()
  await expect(page.getByText('Browser Space 已创建')).toBeVisible()

  await page.getByRole('button', { name: '提交任务', exact: true }).click()
  await expect(page.getByText('已完成')).toBeVisible()
  await expect(page.getByText('Example')).toBeVisible()

  await page.getByRole('button', { name: '关闭 Space', exact: true }).click()
  await page.getByRole('button', { name: '确认关闭', exact: true }).click()
  await expect(page.getByText('Browser Space 已关闭')).toBeVisible()
})
