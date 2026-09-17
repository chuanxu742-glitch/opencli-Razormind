import { expect, test, type Page } from '@playwright/test'

const identity = {
  subject: 'select-display-e2e',
  email: null,
  name: 'Select Display E2E',
  username: 'select-display-e2e',
  picture: null,
  is_platform_admin: true,
  auth_method: 'test',
}

const workspace = {
  id: 'workspace-select-e2e',
  name: '这是一个故意很长的工作区名称，用于验证窄卡片中的截断显示',
  slug: 'select-display-e2e',
  active: true,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
}

const secondWorkspace = {
  ...workspace,
  id: 'workspace-select-e2e-second',
  name: '第二工作区',
  slug: 'select-display-e2e-second',
}

const projects = [
  { id: 'project-select-e2e', name: '这是一个很长的项目名称，用于验证工具栏布局', created_at: '', updated_at: '' },
  { id: 'project-select-e2e-second', name: '第二个项目', created_at: '', updated_at: '' },
]

const graphPreview = (projectId: string) => ({
  workspace_id: workspace.id,
  project_id: projectId,
  project_name: projects.find((project) => project.id === projectId)?.name ?? projects[0].name,
  strategy: 'server-aggregated-sample',
  truncated: false,
  max_nodes: 700,
  nodes: [],
  edges: [],
  stats: {
    total_records: 1,
    sampled_records: 1,
    hidden_records: 0,
    total_sources: 0,
    total_workflows: 0,
    total_runs: 0,
    visible_nodes: 0,
    visible_edges: 0,
  },
  generated_at: '2026-09-01T00:00:00Z',
})

async function mockApi(page: Page) {
  await page.addInitScript(() => sessionStorage.setItem('opencli.bootstrapIdentityToken', 'select-display-token'))
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const reply = (data: unknown, status = 200) => route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify({ data }),
    })

    if (path.endsWith('/auth/me')) return reply(identity)
    if (path === '/api/v1/workspaces' || path === '/api/v1/governance/workspaces') {
      return reply([workspace, secondWorkspace])
    }
    if (path === `/api/v1/workspaces/${workspace.id}/projects`) return reply(projects)
    if (path.match(new RegExp(`/api/v1/workspaces/${workspace.id}/projects/[^/]+/record-graph$`))) {
      return reply(graphPreview(path.split('/').at(-2) ?? projects[0].id))
    }
    if (path === `/api/v1/workspaces/${workspace.id}/workbench/repositories`) {
      return reply([{ id: 'repository-select-e2e', name: 'opencli-admin', defaultRef: 'main' }])
    }
    if (path === `/api/v1/workspaces/${workspace.id}/workbench/runtimes`) {
      return reply([{
        id: 'runtime-select-e2e',
        name: '编码运行时',
        publishedVersion: 1,
        runtimeType: 'codex',
        readiness: 'ready',
        reasonCode: null,
        reason: null,
      }])
    }
    if (path === `/api/v1/workspaces/${workspace.id}/workbench/threads`) return reply([])
    return reply({}, 404)
  })
}

test('工作台初始显示工作区名称并保持窄卡片内可访问', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 })
  await mockApi(page)
  await page.goto('/agent-workbench')

  const workspaceSelect = page.getByRole('combobox', { name: '工作区' })
  await expect(workspaceSelect).toContainText(workspace.name)
  const dimensions = await workspaceSelect.evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
  }))
  expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth)

  await workspaceSelect.click()
  const secondWorkspaceOption = page.getByRole('option', { name: secondWorkspace.name })
  await expect(secondWorkspaceOption).toBeVisible()
  await page.keyboard.press('End')
  await page.keyboard.press('Enter')
  await expect(workspaceSelect).toContainText(secondWorkspace.name)
})

test('图谱工具栏在窄视口显示选中项目且不横向溢出', async ({ page }) => {
  await page.setViewportSize({ width: 600, height: 800 })
  await mockApi(page)
  await page.goto('/records/graph')

  const selects = page.locator('[data-slot="select-trigger"]')
  const projectSelect = selects.nth(0)
  const densitySelect = selects.nth(1)
  await expect(projectSelect).toContainText(projects[0].name)
  await expect(densitySelect).toContainText('标准 · 700 节点')

  const toolbar = page.locator('section').first()
  await expect.poll(() => toolbar.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true)
  const projectBox = await projectSelect.boundingBox()
  const densityBox = await densitySelect.boundingBox()
  expect(projectBox).not.toBeNull()
  expect(densityBox).not.toBeNull()
  expect(densityBox!.y).toBeGreaterThan(projectBox!.y)
  await projectSelect.click()
  const secondProjectOption = page.getByRole('option', { name: projects[1].name })
  await expect(secondProjectOption).toBeVisible()
  await page.keyboard.press('End')
  await page.keyboard.press('Enter')
  await expect(projectSelect).toContainText(projects[1].name)
})

test('图谱没有项目时禁用选择器，不展开空白横线菜单', async ({ page }) => {
  await mockApi(page)
  await page.route(`**/api/v1/workspaces/${workspace.id}/projects`, (route) =>
    route.fulfill({ contentType: 'application/json', body: JSON.stringify({ data: [] }) }))
  await page.goto('/records/graph')

  const projectSelect = page.getByRole('combobox', { name: '预览项目' })
  await expect(projectSelect).toContainText('暂无可预览的项目')
  await expect(projectSelect).toBeDisabled()
  await expect(projectSelect).toHaveAttribute('aria-expanded', 'false')
  await expect(page.getByRole('combobox', { name: '图谱密度' })).toBeDisabled()
  await expect(page.getByRole('textbox', { name: '搜索当前项目预览' })).toBeDisabled()
  await expect(page.locator('[data-slot="select-content"]')).toHaveCount(0)
  await expect(page.getByText('还没有可预览的项目', { exact: true })).toBeVisible()
})

test('图谱等待项目请求时显示加载状态，返回后恢复选择', async ({ page }) => {
  await mockApi(page)
  let release!: () => void
  const pending = new Promise<void>((resolve) => { release = resolve })
  await page.route(`**/api/v1/workspaces/${workspace.id}/projects`, async (route) => {
    await pending
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ data: projects }) })
  })
  try {
    await page.goto('/records/graph')
    const projectSelect = page.getByRole('combobox', { name: '预览项目' })
    await expect(projectSelect).toContainText('正在加载项目…')
    await expect(projectSelect).toBeDisabled()
    release()
    await expect(projectSelect).toContainText(projects[0].name)
    await expect(projectSelect).toBeEnabled()
    await projectSelect.click()
    await expect(page.getByRole('option', { name: projects[1].name })).toBeVisible()
  } finally {
    release()
  }
})

test('图谱项目请求失败时明确显示错误并禁用空菜单', async ({ page }) => {
  await mockApi(page)
  await page.route(`**/api/v1/workspaces/${workspace.id}/projects`, (route) =>
    route.fulfill({ status: 403, contentType: 'application/json', body: JSON.stringify({ detail: '无法读取项目' }) }))
  await page.goto('/records/graph')
  const projectSelect = page.getByRole('combobox', { name: '预览项目' })
  await expect(projectSelect).toContainText('项目加载失败', { timeout: 15000 })
  await expect(projectSelect).toBeDisabled()
  await expect(page.locator('[data-slot="select-content"]')).toHaveCount(0)
})
