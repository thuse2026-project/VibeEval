import { test, expect } from '@playwright/test'

const reply = [
  '# Filter plan',
  '',
  'The **count** and page link need attention in `datasette/views/table.py`.',
  '',
  '1. Count matching rows across pages.',
  '2. Reset the page when filters change.',
  '',
  '```python',
  'def count_rows(rows):',
  '    return len(rows)',
  '```',
  '',
  '<script>window.agentMarkdownXss = true</script>',
].join('\n')

test('R2 Agent history renders safe Markdown after restoring a conversation', async ({ page }) => {
  const now = Math.floor(Date.now() / 1000)
  const attempt = {
    attempt_id: 'attempt-markdown', status: 'in_progress', server_now: now,
    state_version: 1, transition_deadline_at: null, error_code: null,
    current_stage: { id: 'stage-facets', key: 'facet-pagination', status: 'active', title: '筛选与分页' },
    current_phase: { id: 'phase-facets', status: 'active', deadline_at: now + 3600 },
    capabilities: ['use_agent'],
    stages: [{ id: 'stage-facets', title: '筛选与分页', status: 'active', receipt: null }],
  }

  await page.route('**/r2-preview/model', route => route.fulfill({ json: { configured: true } }))
  await page.route('**/api/**', async route => {
    const { pathname } = new URL(route.request().url())
    if (pathname === '/api/v1/sessions/current') return route.fulfill({ json: { session_id: 'session-markdown' } })
    if (pathname.endsWith('/exam-access')) return route.fulfill({ json: attempt })
    if (pathname === '/api/v2/attempts/current') return route.fulfill({ json: attempt })
    if (pathname.endsWith('/files')) return route.fulfill({ json: { files: [{ path: 'README.md', is_dir: false, readonly: true, version: 1 }] } })
    if (pathname.endsWith('/questions')) return route.fulfill({ json: { items: [{ body: 'Fix filtering and pagination.' }] } })
    if (pathname.endsWith('/summary')) return route.fulfill({ json: { value: '', revision: 0 } })
    if (pathname.endsWith('/file')) return route.fulfill({ json: { path: 'README.md', content: '# Fixture', version: 1 } })
    if (pathname.endsWith('/agent/conversations')) return route.fulfill({ json: { items: [{ conversation_id: 'conversation-markdown', title: '计划' }] } })
    if (pathname.endsWith('/history')) return route.fulfill({ json: { turns: [{
      user_message: 'Read the pagination code and propose a fix.',
      steps: [{ response: { message: { content: reply } } }],
    }] } })
    if (pathname.endsWith('/runs') || pathname.endsWith('/reads') || pathname.endsWith('/operations')) {
      return route.fulfill({ json: { items: [] } })
    }
    if (pathname.endsWith('/events/stream')) {
      return route.fulfill({ contentType: 'text/event-stream', body: 'event: stream.end\ndata: {"code":"CONNECTION_WINDOW_ENDED","retryable":true}\n\n' })
    }
    return route.fulfill({ status: 204, body: '' })
  })

  await page.goto('/r2-workspace')
  await expect(page.getByRole('button', { name: 'Agent', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Agent', exact: true })).toHaveAttribute('aria-expanded', 'true')

  const rendered = page.locator('.agent-message.assistant .agent-markdown')
  await expect(rendered.getByRole('heading', { name: 'Filter plan' })).toBeVisible()
  await expect(rendered.locator('strong')).toHaveText('count')
  await expect(rendered.locator('code').first()).toHaveText('datasette/views/table.py')
  await expect(rendered.locator('ol li')).toHaveCount(2)
  await expect(rendered.locator('pre code')).toContainText('return len(rows)')
  await expect(rendered.locator('script')).toHaveCount(0)
  await expect(rendered).toContainText('<script>window.agentMarkdownXss = true</script>')

  await page.reload()
  await expect(page.getByRole('button', { name: 'Agent', exact: true })).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByLabel('对话', { exact: true })).toHaveValue('conversation-markdown')
  await expect(page.locator('.agent-message.assistant .agent-markdown').getByRole('heading', { name: 'Filter plan' })).toBeVisible()
})
