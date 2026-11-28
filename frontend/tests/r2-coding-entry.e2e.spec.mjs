import { expect, test } from '@playwright/test'

async function openCoding(page, { terminal = false } = {}) {
  const stage = { id: 'stage3', key: 'column-statistics', title: '列统计', status: 'active' }
  const now = Math.floor(Date.now() / 1000)
  const attempt = {
    attempt_id: 'attempt', status: 'running', state_version: 5, server_now: now,
    current_stage: stage, stages: [stage], next_action: 'finish_phase',
    current_phase: { id: 'coding3', key: 'coding', title: '编程', status: 'active', deadline_at: now + 3000 },
    capabilities: ['read_repository', 'write_repository', ...(terminal ? ['use_terminal'] : [])],
  }
  const responses = new Map([
    ['/api/v1/sessions/current', { session_id: 'candidate' }],
    ['/api/v1/sessions/candidate/exam-access', attempt],
    ['/api/v2/attempts/current', attempt],
    ['/api/v2/phases/coding3/files', { files: [{ path: 'README.md', readonly: true }] }],
    ['/api/v2/phases/coding3/questions', { items: [{ body: '编程任务说明' }] }],
    ['/api/v2/phases/coding3/file', { path: 'README.md', content: '# 编程任务说明', version: 1 }],
    ['/r2-preview/model', { configured: false }],
  ])
  if (terminal) await page.routeWebSocket('**/terminal', () => {})
  const summaryReads = []
  await page.route(/\/(api\/v[12]\/|r2-preview\/model)/, route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/test-run')) return route.fallback()
    if (path.endsWith('/summary')) summaryReads.push(path)
    return responses.has(path)
      ? route.fulfill({ json: responses.get(path) })
      : route.fulfill({ status: 409, json: { detail: { code: 'STAGE_NOT_SUBMITTABLE' } } })
  })
  await page.goto('/r2-workspace')
  await expect(page.locator('.editor-file-status')).toContainText('README.md')
  return summaryReads
}

test('coding before an interview opens without access to the final submission summary', async ({ page }) => {
  const summaryReads = await openCoding(page)
  const view = page.getByRole('combobox', { name: 'Markdown 视图' })
  await expect(view).toHaveValue('source')
  await expect(page.locator('.view-lines')).toContainText('# 编程任务说明')
  await view.selectOption('preview')
  await expect(page.getByRole('heading', { name: '编程任务说明' })).toBeVisible()
  await expect(page.locator('.editor-host')).toBeHidden()
  await page.reload()
  await expect(view).toHaveValue('source')
  expect(summaryReads).toEqual([])
  await page.getByRole('button', { name: '结束编程，固定代码并进入问答' }).click()
  await expect(page.getByRole('dialog')).toContainText('开始 10 分钟独立问答')
  await expect(page.getByRole('dialog').getByRole('textbox')).toHaveCount(0)
})


test('recorded tests distinguish a review sample, unavailable runner, real failure and real pass', async ({ page }) => {
  const run = { run_id: 'run-3', status: 'failed', exit_code: 1, output: 'FAILED (failures=1)',
    code_version: 'a'.repeat(64), output_truncated: false, evidence_recorded: true, error_code: null }
  let response = { json: { ...run, run_id: undefined, status: 'completed', exit_code: 0, output: '[本地模拟测试输出]\nOK' } }
  await page.route('**/test-run', route => route.fulfill(response))
  await openCoding(page, { terminal: true })
  const button = page.getByRole('button', { name: '运行并记录测试' })
  const result = page.locator('.r2-test-run-result')

  await button.click()
  await expect(page.getByText('测试未执行。当前环境未连接测试沙箱。')).toBeVisible()
  await expect(result).toHaveCount(0)
  response = { json: run }
  await button.click()
  await expect(result).toContainText('测试未通过')
  await expect(result).toContainText('退出码 1')
  response = { json: { ...run, status: 'completed' } }
  await button.click()
  await expect(result.locator('header')).toHaveAttribute('data-passed', 'false')
  response = { json: { ...run, status: 'completed', exit_code: 0, output: 'OK' } }
  await button.click()
  await expect(result).toContainText('测试通过')
  response = { status: 503, json: { detail: { code: 'TEST_RUN_UNAVAILABLE' } } }
  await button.click()
  await expect(page.getByText('测试未执行。当前环境未连接测试沙箱。')).toBeVisible()
  await expect(result).toHaveCount(0)
})
