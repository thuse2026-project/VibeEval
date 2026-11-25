import { expect, test } from '@playwright/test'

test('coding before an interview opens without access to the final submission summary', async ({ page }) => {
  const stages = [
    { id: 'stage1', key: 'tinydb-one', title: '基础题一', status: 'submitted', receipt: { id: 'receipt-1', close_reason: 'manual' } },
    { id: 'stage2', key: 'tinydb-two', title: '基础题二', status: 'submitted', receipt: { id: 'receipt-2', close_reason: 'manual' } },
    { id: 'stage3', key: 'column-statistics', title: '列统计', status: 'active', receipt: null },
    { id: 'stage4', key: 'facet-pagination', title: '筛选与分页', status: 'ready', receipt: null },
  ]
  const stage = stages[2]
  const now = Math.floor(Date.now() / 1000)
  const attempt = {
    attempt_id: 'attempt', status: 'running', state_version: 5, server_now: now,
    current_stage: stage, stages, next_action: 'finish_phase',
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
  await expect(page.getByRole('complementary', { name: '考试进度' })).toContainText('第 3 / 4 题')
  await expect(page.getByRole('complementary', { name: '考试进度' })).toContainText('主观题')
  expect(summaryReads).toEqual([])
  await page.getByRole('button', { name: '结束编程，固定代码并进入问答' }).click()
  await expect(page.getByRole('dialog')).toContainText('确认结束编程？')
  await expect(page.getByRole('dialog').getByRole('textbox')).toHaveCount(0)
})

test('four-stage admission explains subjective questions before preparation', async ({ page }) => {
  const now = Math.floor(Date.now() / 1000)
  const stages = [
    { id: 'stage1', key: 'tinydb-one', title: '基础题一', status: 'ready', receipt: null },
    { id: 'stage2', key: 'tinydb-two', title: '基础题二', status: 'pending', receipt: null },
    { id: 'stage3', key: 'column-statistics', title: '列统计', status: 'pending', receipt: null },
    { id: 'stage4', key: 'facet-pagination', title: '筛选与分页', status: 'pending', receipt: null },
  ]
  const attempt = {
    attempt_id: 'attempt-notice', status: 'admitted', state_version: 1, server_now: now,
    current_stage: stages[0], stages, next_action: 'submit_stage',
    current_phase: { id: 'phase-ready', key: 'transition', title: '准备', status: 'idle', deadline_at: null },
    capabilities: [],
  }
  await page.route('**/api/v1/sessions/current', route => route.fulfill({ json: { session_id: 'candidate-notice' } }))
  await page.route('**/api/v1/sessions/candidate-notice/exam-access', route => route.fulfill({ json: attempt }))
  await page.route('**/r2-preview/model', route => route.fulfill({ json: { configured: false } }))
  await page.goto('/r2-workspace')
  await expect(page.getByRole('dialog', { name: '本场考试包含主观题' })).toBeVisible()
  await expect(page.getByRole('dialog')).toContainText('第 3、4 题 · 主观题')
  await page.getByRole('button', { name: '知道了' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
})
