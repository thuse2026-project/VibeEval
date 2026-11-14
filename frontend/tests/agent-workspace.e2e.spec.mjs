import { test, expect } from '@playwright/test'

async function mockedWorkspace(page) {
  const contents = {
    'README.md': '# Datasette task\nInspect the database and implement the requested query.\n',
    'query.sql': 'select * from fixtures limit 10;\n',
  }
  const state = {
    contents,
    versions: { 'README.md': 1, 'query.sql': 1 },
    fileChange: null,
    phase: 'idle',
    prompt: '',
    terminalConnections: 0,
  }

  await page.routeWebSocket('**/api/v1/sessions/**/terminal', socket => {
    state.terminalConnections++
    socket.send(JSON.stringify({
      type: 'output',
      data: Array.from({ length: 240 }, (_, index) => `terminal output ${index}\r\n`).join(''),
    }))
  })

  await page.route('**/api/v1/**', async route => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname

    if (path.endsWith('/events/stream')) {
      const sequence = Number(url.searchParams.get('after_sequence') ?? 0) + 1
      return route.fulfill({
        contentType: 'text/event-stream',
        body: `retry: 1000\n\nevent: agent.event\ndata: {"sequence":${sequence},"kind":"fixture.updated","data":{}}\n\nevent: stream.end\ndata: {"code":"CONNECTION_WINDOW_ENDED","retryable":true}\n\n`,
      })
    }

    if (path.endsWith('/current') || path.endsWith('/status')) {
      return route.fulfill({ json: {
        session_id: 'ses-agent-ui',
        status: 'in_progress',
        started_at: Date.now() / 1000,
        deadline_at: Date.now() / 1000 + 3600,
        candidate: { id: 'candidate', name: 'UI test' },
        exam_info: { id: 'datasette-1', title: 'Datasette query task', duration_minutes: 60 },
      } })
    }
    if (path.endsWith('/files')) {
      return route.fulfill({ json: { files: Object.keys(contents).map(filePath => ({
        path: filePath,
        is_dir: false,
        readonly: filePath === 'README.md',
        version: state.versions[filePath],
      })) } })
    }
    if (path.endsWith('/file')) {
      const filePath = url.searchParams.get('path')
      return route.fulfill({ json: { path: filePath, content: contents[filePath], version: state.versions[filePath] } })
    }
    if (path.endsWith('/agent/conversations')) {
      return route.fulfill({ json: { items: [{ conversation_id: 'conv-ui', title: 'Datasette helper' }] } })
    }
    if (path.endsWith('/history')) {
      const turns = state.phase === 'idle' ? [] : [{
        user_message: state.prompt,
        steps: state.phase === 'completed' ? [{
          response: { message: { content: 'The command completed and the result is ready.' } },
        }] : [],
      }]
      return route.fulfill({ json: { turns } })
    }
    if (path.endsWith('/runs')) {
      const status = state.phase === 'idle' ? null
        : state.phase === 'approved' ? 'queued'
          : state.phase
      return route.fulfill({ json: { items: status ? [{ run_id: 'run-ui', status, error_code: null, updated_at: Date.now() }] : [] } })
    }
    if (path.endsWith('/reads')) {
      const items = state.phase === 'idle' ? [] : [{
        observation_id: 'read-ui',
        tool_name: 'read_file',
        action_kind: 'read',
        status: 'succeeded',
        result: { content: JSON.stringify({ path: 'query.sql' }) },
      }]
      return route.fulfill({ json: { items } })
    }
    if (path.endsWith('/operations')) {
      const items = state.fileChange ? [{
        operation_id: state.fileChange.operationId,
        kind: 'file_edit',
        status: 'succeeded',
        reason_code: null,
        arguments: { path: state.fileChange.path },
        exit_code: 0,
        change: { path: state.fileChange.path },
        file_result: { change: {
          path: state.fileChange.path,
          applied: true,
          current_version: state.fileChange.version,
        } },
      }] : ['waiting_approval', 'approved', 'completed'].includes(state.phase) ? [{
        operation_id: 'operation-ui',
        kind: 'command',
        status: state.phase === 'completed' ? 'succeeded' : 'waiting_approval',
        reason_code: null,
        arguments: { argv: ['datasette', 'fixtures.db', '--get', '/fixtures.json'] },
        exit_code: state.phase === 'completed' ? 0 : null,
        change: null,
        file_result: null,
      }] : []
      return route.fulfill({ json: { items } })
    }
    if (path.endsWith('/interactions')) {
      const items = state.phase === 'waiting_approval' ? [{
        interaction_id: 'interaction-ui',
        kind: 'approval',
        status: 'pending',
        request: { argv: ['datasette', 'fixtures.db', '--get', '/fixtures.json'] },
      }] : []
      return route.fulfill({ json: { items } })
    }
    if (path.endsWith('/turns') && request.method() === 'POST') {
      state.prompt = request.postDataJSON().text
      state.phase = 'queued'
      return route.fulfill({ status: 202, json: { run_id: 'run-ui', status: 'queued', error_code: null } })
    }
    if (path.endsWith('/advance') && request.method() === 'POST') {
      state.phase = state.phase === 'approved' ? 'completed' : 'waiting_approval'
      return route.fulfill({ json: { run_id: 'run-ui', status: state.phase, error_code: null } })
    }
    if (path.endsWith('/approval') && request.method() === 'POST') {
      state.phase = 'approved'
      return route.fulfill({ json: { interaction_id: 'interaction-ui', status: 'approved' } })
    }
    if (path.endsWith('/interrupt') && request.method() === 'POST') {
      state.phase = 'interrupted'
      return route.fulfill({ json: { run_id: 'run-ui', status: 'interrupted', error_code: null } })
    }
    return route.fulfill({ status: 404, json: { detail: 'Unexpected mocked endpoint' } })
  })

  await page.goto('/workspace')
  await expect(page.locator('.monaco-editor')).toContainText('select * from fixtures')
  return state
}

test('Agent sidebar stays usable beside editor and terminal through approval and stop', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const state = await mockedWorkspace(page)

  await page.getByRole('button', { name: 'Agent', exact: true }).click()
  await expect(page.getByRole('complementary', { name: 'Agent 助手' })).toBeVisible()
  await expect(page.locator('.terminal-pane')).toBeVisible()
  await expect(page.locator('.monaco-editor')).toBeVisible()

  const editor = await page.locator('.workspace-center').boundingBox()
  const agent = await page.locator('.agent-panel').boundingBox()
  expect(editor.width).toBeGreaterThan(300)
  expect(editor.x + editor.width).toBeLessThanOrEqual(agent.x + 1)
  expect((await page.locator('.ide-topbar').boundingBox()).y).toBe(0)

  await page.getByText('README.md', { exact: true }).first().click()
  await expect(page.locator('.editor-tabs [aria-pressed="true"]')).toContainText('README.md')
  await page.getByLabel('发送给 Agent').fill('Read query.sql, then run Datasette and show the result.')
  await page.getByRole('button', { name: '发送', exact: true }).click()

  await expect(page.locator('.agent-activity').getByText('query.sql', { exact: true })).toBeVisible()
  await expect(page.locator('.agent-activity').getByText('datasette fixtures.db --get /fixtures.json', { exact: true })).toBeVisible()
  await expect(page.getByText('请求批准操作', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '批准', exact: true }).click()
  await expect(page.getByText('The command completed and the result is ready.', { exact: true })).toBeVisible()
  await expect(page.locator('.agent-tool-row').filter({ hasText: 'datasette' })).toContainText('succeeded (0)')

  await page.getByLabel('发送给 Agent').fill('Run another check.')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(page.getByText('请求批准操作', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '停止', exact: true }).click()
  await expect(page.getByText(/运行状态：已停止/)).toBeVisible()

  await page.getByRole('button', { name: '折叠', exact: true }).click()
  await expect(page.locator('.terminal-pane')).toBeHidden()
  await page.getByRole('button', { name: '展开终端', exact: true }).click()
  await expect(page.locator('.terminal-pane')).toBeVisible()
  expect(state.terminalConnections).toBe(1)
  await expect(page.locator('.editor-tabs [aria-pressed="true"]')).toContainText('README.md')
  expect(await page.evaluate(() => document.documentElement.scrollHeight - innerHeight)).toBeLessThanOrEqual(1)

  await page.reload()
  await page.getByRole('button', { name: 'Agent', exact: true }).click()
  await expect(page.getByLabel('对话')).toHaveValue('conv-ui')
  await expect(page.getByText('Run another check.', { exact: true })).toBeVisible()
})

test('Agent file edits refresh clean files and preserve dirty drafts as conflicts', async ({ page }) => {
  const state = await mockedWorkspace(page)

  state.contents['query.sql'] = 'select count(*) from fixtures;\n-- agent clean update\n'
  state.versions['query.sql'] = 2
  state.fileChange = { operationId: 'edit-clean', path: 'query.sql', version: 2 }
  await page.getByRole('button', { name: 'Agent', exact: true }).click()
  await expect(page.locator('.monaco-editor')).toContainText('agent clean update')

  await page.locator('.monaco-editor .view-lines').click()
  await page.keyboard.press('Control+End')
  await page.keyboard.type('\n-- local unsaved draft')
  await expect(page.locator('.monaco-editor')).toContainText('local unsaved draft')

  state.contents['query.sql'] = 'select count(*) from fixtures;\n-- second agent update\n'
  state.versions['query.sql'] = 3
  state.fileChange = { operationId: 'edit-conflict', path: 'query.sql', version: 3 }
  await expect(page.locator('.workspace-alert[role="alert"]')).toContainText('未保存草稿仍保留', { timeout: 15000 })
  await expect(page.locator('.monaco-editor')).toContainText('local unsaved draft')
  await expect(page.locator('.workspace-recovery')).toContainText('second agent update')
})
