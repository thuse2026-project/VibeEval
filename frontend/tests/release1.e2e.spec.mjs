import { test, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';

const headers = { 'X-VibeEval-Request': '1' };

async function enter(page, token) {
  await page.goto('/');
  await page.getByLabel('入场凭证').fill(token);
  await page.getByRole('button', { name: '校验并入场' }).click();
  await page.getByRole('link', { name: '进入工作台 / 恢复答卷' }).click();
  await expect(page.getByRole('checkbox')).toBeVisible({timeout:30000});
  await page.getByRole('checkbox').check();
  await page.getByRole('button', { name: '准备环境',exact:true }).click();
  await expect(page.getByRole('button', { name: '确认开始作答并计时' })).toBeEnabled();
  await page.getByRole('button', { name: '确认开始作答并计时' }).click();
  await expect(page.getByText('终端已连接 · /workspace', { exact: true })).toBeVisible();
  return (await page.request.get('/api/v1/sessions/current', { headers })).json();
}

async function selectFile(page, path) {
  await page.locator('.file-tree').getByText(path, { exact: true }).click();
  await expect(page.locator('.editor-tabs [aria-pressed="true"]')).toContainText(path);
  await expect(page.locator('.editor-tabs [aria-pressed="true"]')).not.toContainText('只读');
}

async function paste(page, value) {
  await page.locator('.monaco-editor .view-lines').click();
  await page.keyboard.press('Control+Home');
  await page.keyboard.press('Control+a');
  await page.evaluate(text => navigator.clipboard.writeText(text), value);
  await page.keyboard.press('Control+v');
  await expect(page.locator('.save-state')).toContainText('未保存');
}

async function runTests(page) {
  await expect(page.locator('.save-state')).toContainText('已保存',{timeout:30000});
  await page.locator('.terminal-body').click();
  await page.keyboard.type('python -m unittest discover -s tests -p "test_*.py" -v',{delay:1});
  await page.keyboard.press('Enter');
}

test('real workspace saves, conflicts, PTY tests, submission and examiner snapshot', async ({ page }) => {
  test.skip(!process.env.VIBEEVAL_E2E_TOKEN, 'Provide a fresh one-use test invitation');
  const sockets=[];
  page.on('websocket',ws=>{if(ws.url().includes('/terminal'))sockets.push(ws.url());});
  const session = await enter(page, process.env.VIBEEVAL_E2E_TOKEN);
  await page.locator('.terminal-body').click();
  await page.keyboard.type('export VIBEEVAL_FOLD_MARK=kept; cd /tmp',{delay:1});
  await page.keyboard.press('Enter');
  await page.getByRole('button',{name:'折叠',exact:true}).click();
  await page.getByRole('button',{name:'展开终端'}).click();
  await page.locator('.terminal-body').click();
  await page.keyboard.type('printf "\\nFOLD:%s:%s\\n" "$VIBEEVAL_FOLD_MARK" "$PWD"; cd /workspace',{delay:1});
  await page.keyboard.press('Enter');
  await expect(page.locator('.terminal').first()).toContainText('FOLD:kept:/tmp');
  expect(sockets).toHaveLength(1);
  const base = `/api/v1/sessions/${session.session_id}`;
  await runTests(page);
  await expect(page.locator('.terminal').first()).toContainText('FAILED (failures=2)');

  // This is a fake acceptance candidate, never the distributed problem template.
  const reference = await readFile(new URL('../../maintainer/python-debug-01/reference/booking.py', import.meta.url), 'utf8');
  await paste(page, reference.replace('维护者参考修复。禁止挂载到考生工作区。', '联合验收的模拟考生修复。'));
  await selectFile(page, 'tests/test_candidate.py');
  const regression = 'import sys\nimport unittest\nfrom pathlib import Path\nfrom datetime import datetime\n' +
    'sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))\nfrom booking import BookingService\n' +
    'class RegressionTests(unittest.TestCase):\n    def test_reverse_adjacent(self):\n' +
    '        service = BookingService()\n        service.reserve("A", datetime(2026,1,1,10), datetime(2026,1,1,11))\n' +
    '        self.assertEqual(service.reserve("A", datetime(2026,1,1,9), datetime(2026,1,1,10)).room_id, "A")\n';
  await paste(page, regression);
  await runTests(page);
  await expect(page.locator('.terminal').first()).toContainText('Ran 9 tests');
  await expect(page.locator('.terminal').first()).toContainText('OK');
  const protectedWrite = await page.request.put(`${base}/file`, { headers, data: {
    path: 'tests/test_public.py', expected_version: 1, content: 'cannot replace public tests',
  } });
  expect(protectedWrite.status()).toBe(403);

  const candidate = await (await page.request.get(`${base}/file?path=tests/test_candidate.py`, { headers })).json();
  const external = await page.request.put(`${base}/file`, { headers, data: {
    path: candidate.path, expected_version: candidate.version, content: candidate.content + '\n# external writer\n',
  } });
  expect(external.status()).toBe(200);
  await paste(page, regression + '\n# retain-local-conflict\n');
  await expect(page.locator('.workspace-alert[role="alert"]')).toContainText('文件已修改',{timeout:15000});
  await expect(page.locator('.monaco-editor')).toContainText('retain-local-conflict');
  await page.getByRole('button',{name:'重新读取并比较'}).click();
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: '使用服务器内容（放弃本地修改）' }).click();
  await expect(page.locator('.workspace-recovery')).toHaveCount(0);
  await expect(page.locator('.save-state')).toContainText('已保存');
  await selectFile(page,'src/booking.py');
  await expect(page.locator('.editor-tabs [aria-pressed="true"]')).toContainText('src/booking.py');
  await expect(page.locator('.editor-tabs [aria-pressed="true"]')).not.toContainText('只读');
  await paste(page, reference + '\n# final-edit-before-submit\n');
  const receiptResponse = page.waitForResponse(response => response.url().endsWith('/submit') && response.status() === 202, { timeout: 90000 });
  await page.getByRole('button', { name: '提交答卷', exact: true }).click();
  await page.getByRole('button',{name:'确认提交',exact:true}).click();
  const receipt = await (await receiptResponse).json();
  await expect(page.getByRole('heading', { name: '答卷已提交' })).toBeVisible();
  await page.reload();
  await expect(page.locator('.submission-id').filter({hasText:'提交编号'})).toContainText(receipt.submission_id);
  expect((await page.request.get(`${base}/files`, { headers })).status()).toBe(409);

  if (!process.env.VIBEEVAL_E2E_ADMIN_PASSWORD) {
    test.info().annotations.push({type:'coverage',description:'Candidate workflow verified; examiner credentials not supplied, examiner snapshot check not rerun.'});
    return;
  }
  await page.goto('/admin');
  await page.getByLabel('账号', { exact: true }).fill(process.env.VIBEEVAL_E2E_ADMIN_USER ?? 'examiner');
  await page.getByLabel('密码', { exact: true }).fill(process.env.VIBEEVAL_E2E_ADMIN_PASSWORD);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await page.getByLabel('考核场次').selectOption(session.exam_info.id);
  const row = page.getByRole('row').filter({ hasText: session.candidate.id });
  await row.getByRole('link', { name: '查看答卷' }).click();
  await page.getByRole('button', { name: 'src/booking.py', exact: true }).click();
  await expect(page.locator('pre').first()).toContainText('final-edit-before-submit');
  await expect(page.getByText(receipt.submission_id, { exact: true })).toBeVisible();
});

test('real deadline submission preserves a visible unsaved local draft', async ({ page }) => {
  test.skip(!process.env.VIBEEVAL_E2E_DEADLINE_TOKEN, 'Provide a fresh one-minute invitation');
  await enter(page, process.env.VIBEEVAL_E2E_DEADLINE_TOKEN);
  await expect.poll(async () => page.locator('.timer').textContent(), { timeout: 70000, intervals: [200] }).toMatch(/00:04$/);
  await page.locator('.monaco-editor .view-lines').click();
  await page.keyboard.press('Control+End');
  await page.keyboard.press('Enter');
  await page.evaluate(() => navigator.clipboard.writeText('# local-last-second-draft'));
  await page.keyboard.press('Control+v');
  await expect(page.getByRole('heading', { name: '答卷已提交' })).toBeVisible({ timeout: 30000 });
  await expect(page.getByLabel('未保存草稿 src/booking.py')).toHaveValue(/local-last-second-draft/);
  await expect(page.getByRole('heading', { name: '以下本地草稿尚未确认保存，不属于已提交答卷' })).toBeVisible();
});
