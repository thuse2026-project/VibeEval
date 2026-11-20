import { test, expect } from '@playwright/test';

test('coding completion freezes the phase instead of submitting the stage', async ({ page }) => {
  const now = Math.floor(Date.now() / 1000);
  const posts = [];
  const state = {
    attempt_id: 'attempt-1', status: 'running', state_version: 4, server_now: now,
    transition_deadline_at: null, error_code: null, next_action: 'finish_phase',
    current_stage: { id: 'stage-3', key: 'column-statistics', ordinal: 3, status: 'active', title: '业务拓展' },
    current_phase: { id: 'phase-coding', key: 'coding', title: '编程', status: 'active', deadline_at: now + 3000 },
    capabilities: ['read_repository', 'write_repository'],
    stages: [{ id: 'stage-3', title: '业务拓展', status: 'active', receipt: null }],
  };
  await page.route('**/api/v1/sessions/**', route => route.fulfill({ json: route.request().method() === 'POST' ? state : { session_id: 'candidate-1' } }));
  await page.route('**/r2-preview/model', route => route.fulfill({ json: { configured: false } }));
  await page.route('**/api/v2/**', route => {
    const url = new URL(route.request().url());
    if (route.request().method() === 'POST') { posts.push(url.pathname); return route.fulfill({ json: state }); }
    if (url.pathname.endsWith('/attempts/current')) return route.fulfill({ json: state });
    if (url.pathname.endsWith('/files')) return route.fulfill({ json: { files: [{ path: 'solution.py', is_dir: false, readonly: false, version: 1 }] } });
    if (url.pathname.endsWith('/questions')) return route.fulfill({ json: { items: [{ body: '完成实现并验证。' }] } });
    if (url.pathname.endsWith('/file')) return route.fulfill({ json: { path: 'solution.py', content: '# solution', version: 1 } });
    return route.fulfill({ status: 404, json: { detail: { code: 'NOT_FOUND' } } });
  });
  await page.goto('/r2-workspace');
  await expect(page.getByRole('button', { name: '结束编程，进入独立问答' })).toBeVisible();
  await expect(page.getByText('改动与验证说明')).toHaveCount(0);
  await page.getByRole('button', { name: '结束编程，进入独立问答' }).click();
  await expect(page.getByText('将停止 Agent 和终端、保存并固定代码，此后不能修改或返回编程。')).toBeVisible();
  await page.getByRole('button', { name: '确认提交' }).click();
  await expect.poll(() => posts).toContain('/api/v2/phases/phase-coding/finish');
  expect(posts).not.toContain('/api/v2/stages/stage-3/submit');
});

test('independent answers preserve conflicts and show frozen paged evidence', async ({ page }) => {
  const now = Math.floor(Date.now() / 1000);
  let completed = false;
  let conflict = true;
  let answerReads = 0;
  const writes = [];
  const state = () => ({
    attempt_id: 'attempt-1', status: completed ? 'completed' : 'running', state_version: completed ? 8 : 7,
    server_now: now, transition_deadline_at: null, error_code: null, next_action: completed ? null : 'submit_exam',
    current_stage: { id: 'stage-4', key: 'facet-pagination', ordinal: 4, status: completed ? 'submitted' : 'active', title: '历史 Bug 修复' },
    current_phase: { id: 'phase-interview', key: 'interview', title: '独立问答', status: completed ? 'closed' : 'active', deadline_at: now + 600 },
    capabilities: completed ? [] : ['edit_answers', 'read_frozen_code', 'read_repository'],
    stages: [{ id: 'stage-4', title: '历史 Bug 修复', status: completed ? 'submitted' : 'active', receipt: completed ? { id: 'receipt-1', close_reason: 'manual' } : null }],
  });

  await page.route('**/api/v1/sessions/**', route => route.fulfill({ json: route.request().method() === 'POST' ? state() : { session_id: 'candidate-1' } }));
  await page.route('**/r2-preview/model', route => route.fulfill({ json: { configured: false } }));
  await page.route('**/api/v2/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.endsWith('/attempts/current')) return route.fulfill({ json: state() });
    if (path.endsWith('/questions')) return route.fulfill({ json: { items: [
      { id: 'q1', title: '根因判断', body: '说明触发条件和核对依据。', answer_schema: { type: 'string', maxLength: 1500 } },
      { id: 'q2', title: '回归验证', body: '说明验证边界。', answer_schema: { type: 'string', maxLength: 1500 } },
    ] } });
    if (path.endsWith('/answers') && route.request().method() === 'GET') return route.fulfill({ json: answerReads++ === 0 ? {} : { q1: { revision: 2, value: '另一窗口版本' } } });
    if (path.includes('/answers/q1') && route.request().method() === 'PUT') {
      const body = route.request().postDataJSON(); writes.push(body);
      if (conflict) { conflict = false; return route.fulfill({ status: 409, json: { detail: { code: 'REVISION_CONFLICT' } } }); }
      return route.fulfill({ json: { revision: 3, value: body.value, saved_at: now } });
    }
    if (path.includes('/answers/q2') && route.request().method() === 'PUT') return route.fulfill({ json: { revision: 1, value: '', saved_at: now } });
    if (path.endsWith('/frozen-code')) return route.fulfill({ json: { workspace_id: 'workspace-1', sha256: 'a'.repeat(64), coding_phase_id: 'phase-coding', closed_at: now - 5, evidence: { coding_phase_id: 'phase-coding', sequence: 3, completeness: 'partial', reason: 'CAPTURE_GAP' } } });
    if (path.endsWith('/files')) return route.fulfill({ json: { files: [{ path: 'solution.py', is_dir: false, readonly: true, version: 2 }] } });
    if (path.endsWith('/file')) return route.fulfill({ json: { path: 'solution.py', content: 'def fixed():\n    return True\n', version: 2 } });
    if (path.endsWith('/evidence')) {
      const cursor = url.searchParams.get('cursor');
      return route.fulfill({ json: {
        coding_phase_id: 'phase-coding', sha256: 'a'.repeat(64), completeness: 'partial', reason: 'CAPTURE_GAP',
        items: cursor ? [{ id: 'message-1', kind: 'message', actor: 'agent', at: now - 7, data: { text: '已读取 solution.py' } }]
          : [{ id: 'run-start', kind: 'test_run', actor: 'candidate', at: now - 10, data: { run_id: 'run-1', status: 'started', command: 'pytest', code_version: 'v2' } }],
        next_cursor: cursor ? null : 'phase-coding:1',
      } });
    }
    if (path.endsWith('/submit')) { completed = true; return route.fulfill({ json: { id: 'receipt-1', close_reason: 'manual' } }); }
    return route.fulfill({ status: 404, json: { detail: { code: 'NOT_FOUND' } } });
  });

  await page.goto('/r2-workspace');
  await expect(page.getByRole('heading', { name: '根因判断' })).toBeVisible();
  await expect(page.getByText('记录不完整：平台检测到部分操作记录缺失，请考官复核。')).toBeVisible();
  await expect(page.getByText('截止时未完成')).toBeVisible();
  await expect(page.getByText('def fixed():')).toBeVisible();

  const answer = page.getByLabel('你的回答');
  await answer.fill('我核对了实际失败输出，确认触发条件。');
  await expect(page.getByText('版本冲突，草稿已保留')).toBeVisible();
  await expect(answer).toHaveValue('我核对了实际失败输出，确认触发条件。');
  await page.getByRole('button', { name: '重试保存' }).click();
  await expect(page.getByText('已保存', { exact: true }).last()).toBeVisible();
  expect(writes.map(item => item.expected_revision)).toEqual([0, 2]);

  await page.getByRole('button', { name: '加载更多记录' }).click();
  await expect(page.getByText('已读取 solution.py')).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await expect(page.getByRole('button', { name: '提交整场考试' })).toBeVisible();
  await page.getByRole('button', { name: '提交整场考试' }).click();
  await expect(page.getByRole('heading', { name: '答卷已提交' })).toBeVisible();
  await expect(page.getByText('感谢作答，可以离场。')).toBeVisible();
});
