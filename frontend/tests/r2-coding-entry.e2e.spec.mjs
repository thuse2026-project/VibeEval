import { expect, test } from '@playwright/test'

test('coding before an interview opens without access to the final submission summary', async ({ page }) => {
  const stage = { id: 'stage3', key: 'column-statistics', title: '列统计', status: 'active' }
  const now = Math.floor(Date.now() / 1000)
  const attempt = {
    attempt_id: 'attempt', status: 'running', state_version: 5, server_now: now,
    current_stage: stage, stages: [stage], next_action: 'finish_phase',
    current_phase: { id: 'coding3', key: 'coding', title: '编程', status: 'active', deadline_at: now + 3000 },
    capabilities: ['read_repository', 'write_repository'],
  }
  const responses = new Map([
    ['/api/v1/sessions/current', { session_id: 'candidate' }],
    ['/api/v1/sessions/candidate/exam-access', attempt],
    ['/api/v2/attempts/current', attempt],
    ['/api/v2/phases/coding3/files', { files: [{ path: 'README.md', readonly: true }] }],
    ['/api/v2/phases/coding3/questions', { items: [{ body: '编程任务说明' }] }],
    ['/api/v2/phases/coding3/file', { path: 'README.md', content: '编程任务说明', version: 1 }],
    ['/r2-preview/model', { configured: false }],
  ])
  const summaryReads = []
  await page.route(/\/(api\/v[12]\/|r2-preview\/model)/, route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/summary')) summaryReads.push(path)
    return responses.has(path)
      ? route.fulfill({ json: responses.get(path) })
      : route.fulfill({ status: 409, json: { detail: { code: 'STAGE_NOT_SUBMITTABLE' } } })
  })
  await page.goto('/r2-workspace')
  await expect(page.locator('.editor-file-status')).toContainText('README.md')
  expect(summaryReads).toEqual([])
  await page.getByRole('button', { name: '结束编程，固定代码并进入问答' }).click()
  await expect(page.getByRole('dialog')).toContainText('开始 10 分钟独立问答')
  await expect(page.getByRole('dialog').getByRole('textbox')).toHaveCount(0)
})
