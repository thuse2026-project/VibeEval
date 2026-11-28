import { expect, test } from '@playwright/test'

test('reading the repository and editing note.md keep the agent conversation list stable', async ({ page }) => {
  const now = Math.floor(Date.now() / 1000)
  const stage = { id: 'stage1', key: 'tinydb-understanding', status: 'active', title: '仓库理解' }
  const state = {
    attempt_id: 'exam', status: 'running', state_version: 1, server_now: now, next_action: 'finish_phase',
    current_stage: stage, stages: [stage],
    current_phase: { id: 'explore', key: 'explore', status: 'active', title: '仓库探索', deadline_at: now + 1200 },
    capabilities: ['read_repository', 'edit_notes', 'read_notes', 'use_agent'],
  }
  let lists = 0
  let note = ''
  await page.route(/\/(api\/v[12]\/|r2-preview\/model)/, route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/model')) return route.fulfill({ json: { configured: true } })
    if (path.endsWith('/sessions/current')) return route.fulfill({ json: { session_id: 'candidate' } })
    if (path.endsWith('/notes')) {
      if (route.request().method() === 'PUT') note = route.request().postDataJSON().text
      return route.fulfill({ json: { value: note, revision: note ? 1 : 0 } })
    }
    if (path.endsWith('/conversations')) { lists++; return route.fulfill({ json: { items: [] } }) }
    if (path.endsWith('/files')) return route.fulfill({ json: { files: [{ path: 'repository/LICENSE' }, { path: 'repository/README.md' }] } })
    if (path.endsWith('/file')) return route.fulfill({ json: { content: '# TinyDB' } })
    return route.fulfill({ json: state })
  })
  await page.goto('/r2-workspace')
  await expect(page.locator('.editor-file-status')).toContainText('repository/README.md')
  await expect(page.getByRole('button', { name: '新建对话' })).toBeEnabled()
  const view = page.getByRole('combobox', { name: 'Markdown 视图' })
  await expect(view).toHaveValue('source')
  await view.selectOption('preview')
  await expect(page.getByRole('heading', { name: 'TinyDB' })).toBeVisible()
  const folder = page.locator('.file-tree details').first()
  await folder.locator('summary').first().click()
  await page.locator('.file-tree').getByTitle('note.md').click()
  await expect(view).toHaveValue('source')
  await expect(folder).not.toHaveAttribute('open', '')
  await page.getByRole('button', { name: '题目', exact: true }).click()
  await page.getByRole('button', { name: '文件', exact: true }).click()
  await expect(folder).not.toHaveAttribute('open', '')
  const input = page.getByLabel('发送给 Agent')
  await input.fill('第一行')
  await input.press('Shift+Enter')
  await input.press('a')
  await expect(input).toHaveValue('第一行\na')
  await expect(page.locator('.ide-terminal')).toHaveCount(0)
  const divider = page.getByRole('separator', { name: '调整文件区宽度' })
  const width = Number(await divider.getAttribute('aria-valuenow'))
  await divider.press('ArrowRight')
  await expect(divider).toHaveAttribute('aria-valuenow', String(width + 2))
  await page.locator('.view-lines').click()
  await page.keyboard.type('read the table implementation')
  await page.keyboard.press('Control+s')
  await expect.poll(() => note).toBe('read the table implementation')
  const clock = page.locator('.timer')
  await expect(clock).not.toHaveText(await clock.innerText())
  expect(lists).toBe(1)
})
