import { test, expect } from '@playwright/test';

test('reading and switching files never changes candidate code', async ({ page }) => {
  const contents = {
    'src/booking.py': 'class BookingService:\n    pass\n',
    'README.md': '# 中文题目说明\n这不是 Python 代码。\n',
    'tests/test_candidate.py': '# candidate regression tests\n',
    'tests/test_public.py': '# readonly public tests\n',
  };
  const writes = [];
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url());
    let body;
    if (url.pathname.endsWith('/current')) body = {
      session_id: 'editor-switch', status: 'in_progress',
      started_at: Date.now() / 1000, deadline_at: Date.now() / 1000 + 3600,
      candidate: { id: 'test', name: '测试' },
      exam_info: { title: '文件切换回归', duration_minutes: 60 },
    };
    else if (url.pathname.endsWith('/files')) body = { files: Object.keys(contents).map(path => ({
      path, is_dir: false, readonly: ['README.md', 'tests/test_public.py'].includes(path), version: 1,
    })) };
    else if (route.request().method() === 'PUT') {
      const value = route.request().postDataJSON();
      writes.push(value); contents[value.path] = value.content;
      body = { new_version: 2 };
    } else body = { content: contents[url.searchParams.get('path')], version: 1 };
    await route.fulfill({ json: body });
  });
  await page.goto('/');
  await expect(page.locator('.monaco-editor')).toContainText('BookingService');
  for (const path of ['README.md', 'src/booking.py', 'tests/test_public.py',
    'tests/test_candidate.py', 'README.md', 'src/booking.py']) {
    await page.getByText(path, { exact: true }).first().click();
    await expect(page.locator('.editor-tabs')).toContainText(path);
    await expect(page.locator('.monaco-editor')).toContainText(contents[path].split('\n')[0]);
  }
  await page.waitForTimeout(5500); // Cover the actual automatic save interval.
  expect(writes).toEqual([]);
  await page.locator('.monaco-editor .view-lines').click();
  await page.keyboard.press('Control+End');
  await page.evaluate(() => navigator.clipboard.writeText('\n# actual candidate edit'));
  await page.keyboard.press('Control+v');
  await page.getByText('README.md', { exact: true }).first().click();
  await expect(page.locator('.editor-tabs')).toContainText('README.md');
  await page.waitForTimeout(5500);
  expect(writes).toHaveLength(1);
  expect(writes[0].path).toBe('src/booking.py');
  expect(writes[0].content).toContain('BookingService');
  expect(writes[0].content).toContain('# actual candidate edit');
});
