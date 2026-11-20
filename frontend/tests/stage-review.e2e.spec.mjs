import { test, expect } from '@playwright/test'

test('test observations preserve rubric drafts and never award automatic points', async ({ page }) => {
  const id = 'submission_' + 'c'.repeat(32)
  let observation = null
  let review = null
  const criteria = [{ key: 'tests', name: '新增测试', max: 15, category: 'tests', full: '有效新增验证', partial: '部分覆盖', zero: '复制不计分' }, { key: 'quality', name: '实现质量', max: 15, category: 'quality', full: '实现清楚', partial: '局部不足', zero: '缺少依据' }]
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/admin/me')) return route.fulfill({ json: { username: 'teacher', name: '考官', expires_at: Math.floor(Date.now()/1000)+10800, server_time: Math.floor(Date.now()/1000) } })
    if (path.endsWith('/candidate-tests')) {
      observation = { status: 'completed', notice: '考生程序辅助证据，不按通过数量给分。', observed_at: 1794990500, versions: { original: { status: 'completed', output: '' }, submitted: { status: 'completed', output: '' } }, rows: [{ id: 'test_candidate.Check.test_new_boundary', original: { status: 'failed', message: 'expected 2, actual 1' }, submitted: { status: 'failed', message: 'expected 2, actual 1' }, possible_public_copy: false }] }
      return route.fulfill({ json: observation })
    }
    if (path.endsWith('/review')) {
      const body = route.request().postDataJSON()
      expect(body.scores).toEqual({ tests: 5, quality: 10 })
      expect(body.evidence.tests).toBe('tests/test_candidate.py：正确发现尚未修好的边界')
      expect(body.novel_tests_confirmed).toBe(true)
      review = { ...body, revision: 1, reviewer: 'teacher', reviewed_at: 1794990500 }
      return route.fulfill({ json: review })
    }
    return route.fulfill({ json: { submission_id: id, title: '细则验收', candidate_name: '验收', candidate_id: 'fixture', exam_title: '验收', files: [{ path: 'tests/test_candidate.py', content: '# test source' }], changes: [], summary: null, criteria: [], manual_criteria: criteria, review_evidence_required: true, policies: ['复制公开测试不计新增分'], objective: { status: 'completed', score: 70, cap: null, groups: [], output: '' }, review, total_score: review ? 85 : null, candidate_tests: observation, candidate_tests_last_attempt: observation } })
  })
  await page.goto('/admin/r2/submissions/' + id)
  await expect(page.getByText('满分：有效新增验证')).toBeVisible()
  await page.getByLabel('新增测试（满分 15）', { exact: true }).fill('5')
  await page.getByLabel('实现质量（满分 15）', { exact: true }).fill('10')
  await page.getByLabel('本项依据：新增测试', { exact: true }).fill('tests/test_candidate.py：正确发现尚未修好的边界')
  await page.getByLabel('本项依据：实现质量', { exact: true }).fill('业务代码及注释说明改动范围')
  await page.getByLabel('评分依据', { exact: true }).fill('功能和测试质量分别评价')
  await expect(page.getByRole('button', { name: '保存考官评分' })).toBeDisabled()
  await page.getByRole('checkbox').check()
  await page.getByRole('button', { name: '运行新增测试对照' }).click()
  await expect(page.getByRole('cell', { name: '断言失败', exact: true })).toHaveCount(2)
  await expect(page.getByLabel('新增测试（满分 15）', { exact: true })).toHaveValue('5')
  await expect(page.getByLabel('本项依据：新增测试', { exact: true })).toHaveValue('tests/test_candidate.py：正确发现尚未修好的边界')
  await expect(page.getByText('未评 / 30', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '保存考官评分' }).click()
  await expect(page.getByText('85 / 100', { exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByLabel('新增测试（满分 15）', { exact: true })).toHaveValue('5')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test('login preserves the multi-stage review entry', async ({ page }) => {
  let authenticated = false
  const identity = { username: 'teacher', name: '考官', expires_at: Math.floor(Date.now() / 1000) + 10800, server_time: Math.floor(Date.now() / 1000) }
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/admin/login')) { authenticated = true; return route.fulfill({ json: identity }) }
    if (path.endsWith('/admin/me')) return route.fulfill(authenticated ? { json: identity } : { status: 401, json: { detail: '请登录' } })
    if (path.endsWith('/submissions')) return route.fulfill({ json: { items: [], total: 0, page: 1, page_size: 20 } })
    return route.fulfill({ status: 204 })
  })
  await page.goto('/admin/r2')
  await page.getByLabel('账号', { exact: true }).fill('teacher')
  await page.getByLabel('密码', { exact: true }).fill('fixture-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '多阶段已交卷答卷' })).toBeVisible()
  await expect(page).toHaveURL(/\/admin\/r2$/)
})

test('examiner sees ungraded values, saves a justified score, and restores it', async ({ page }) => {
  const row = { submission_id: 'submission_' + 'a'.repeat(32), title: '业务拓展 · 补齐列统计', candidate_id: 'review-fixture', candidate_name: '验收考生', exam_title: '验收', submitted_at: 1794990000, close_reason: 'manual' }
  let objective = null
  let review = null
  const detail = () => ({ ...row, task_version: 'task-v1', rule_version: '70-30-v1', summary: { value: '补齐统计数据并验证' }, files: [{ path: 'datasette/views/table.py', content: 'def example():\n    return 1' }, { path: 'tests/test_candidate.py', content: '# candidate regression' }], changes: [{ path: 'datasette/views/table.py', diff: '+return 1', truncated: false }], criteria: [{ key: 'behavior', name: '功能行为', max: 70 }], manual_criteria: [{ key: 'tests', name: '新增测试', max: 15 }, { key: 'quality', name: '实现质量', max: 15 }], objective, last_attempt: objective, review, total_score: objective && review ? 70 + Object.values(review.scores).reduce((a, b) => a + b, 0) : null })
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/admin/me')) return route.fulfill({ json: { username: 'teacher', name: '考官', expires_at: Math.floor(Date.now() / 1000) + 10800, server_time: Math.floor(Date.now() / 1000) } })
    if (path.endsWith('/evaluate')) {
      objective = { status: 'completed', score: 70, cap: null, groups: [{ key: 'behavior', name: '功能行为', max: 70, passed: true, score: 70 }], output: 'trusted behavior passed' }
      return route.fulfill({ json: objective })
    }
    if (path.endsWith('/review')) {
      const body = route.request().postDataJSON()
      expect(body.expected_revision).toBe(0)
      expect(body.scores).toEqual({ tests: 15, quality: 10 })
      review = { revision: 1, scores: body.scores, note: body.note, reviewer: 'teacher', reviewed_at: 1794990500 }
      return route.fulfill({ json: review })
    }
    if (path.endsWith(row.submission_id)) return route.fulfill({ json: detail() })
    if (path.endsWith('/submissions')) return route.fulfill({ json: { items: [row], total: 1, page: 1, page_size: 20 } })
    return route.fulfill({ status: 204 })
  })
  await page.goto('/admin/r2')
  await page.getByRole('button', { name: '查看与评分' }).click()
  await expect(page.getByText('未评 / 70', { exact: true })).toBeVisible()
  await expect(page.getByText('未评 / 30', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '保存考官评分' })).toBeDisabled()
  await page.getByLabel('新增测试（满分 15）', { exact: true }).fill('15')
  await page.getByLabel('实现质量（满分 15）', { exact: true }).fill('10')
  await page.getByLabel('评分依据', { exact: true }).fill('测试覆盖关键边界；实现清楚，但说明尚可完善。')
  await page.getByRole('button', { name: '开始客观评测' }).click()
  await expect(page.getByText('70 / 70', { exact: true })).toBeVisible()
  await expect(page.getByText('尚未评完', { exact: true })).toBeVisible()
  await expect(page.getByLabel('新增测试（满分 15）', { exact: true })).toHaveValue('15')
  await expect(page.getByLabel('实现质量（满分 15）', { exact: true })).toHaveValue('10')
  await expect(page.getByLabel('评分依据', { exact: true })).toHaveValue('测试覆盖关键边界；实现清楚，但说明尚可完善。')
  await page.getByRole('button', { name: '保存考官评分' }).click()
  await expect(page.getByText('95 / 100', { exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByLabel('实现质量（满分 15）', { exact: true })).toHaveValue('10')
  await expect(page.getByText('95 / 100', { exact: true })).toBeVisible()
  await page.getByLabel('查看答卷文件').selectOption('tests/test_candidate.py')
  await expect(page.getByLabel('固定答卷文件内容')).toContainText('# candidate regression')
})
