import { test, expect } from '@playwright/test';

async function workspace(page) {
  const contents = {
    'src/booking.py': 'class BookingService:\n    pass\n',
    'README.md': '# 中文题目说明\n这不是 Python 代码。\n',
    'tests/test_candidate.py': '# candidate regression tests\n',
    'tests/test_public.py': '# readonly public tests\n',
    'src/main.py': '# readonly command line example\n',
  };
  const writes = [];
  const state = { contents, writes, attempts:[], connections:0, readDelay: 0, versions: {}, failReads: false };
  await page.routeWebSocket('**/api/v1/sessions/**/terminal', socket => {
    state.connections++;
    socket.send(JSON.stringify({ type: 'output', data: Array.from({length: 300}, (_, i) => `terminal line ${i}\r\n`).join('') }));
  });
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url());
    let body;
    if (url.pathname.endsWith('/current')||url.pathname.endsWith('/status')) body = {
      session_id: 'editor-switch', status: 'in_progress',
      started_at: Date.now() / 1000, deadline_at: Date.now() / 1000 + 3600,
      candidate: { id: 'test', name: '测试' },
      exam_info: { title: '文件切换回归', duration_minutes: 60 },
    };
    else if (url.pathname.endsWith('/files')) body = { files: Object.keys(contents).map(path => ({
      path, is_dir: false, readonly: ['README.md', 'tests/test_public.py', 'src/main.py'].includes(path), version: 1,
    })) };
    else if (route.request().method() === 'PUT') {
      const value = route.request().postDataJSON();
      state.attempts.push(value);
      if(value.expected_version!==(state.versions[value.path]??1))return route.fulfill({status:409,json:{detail:{code:'FILE_VERSION_CONFLICT',message:'文件已修改'}}});
      writes.push(value); contents[value.path] = value.content;
      state.versions[value.path] = (state.versions[value.path] ?? 1) + 1;
      body = { new_version: state.versions[value.path] };
    } else {
      if (state.failReads) return route.fulfill({ status: 503, json: { detail: '文件暂时读取失败' } });
      const path = url.searchParams.get('path');
      body = { path, content: contents[path], version: state.versions[path] ?? 1 };
      if (state.readDelay) await new Promise(resolve => setTimeout(resolve, state.readDelay));
    }
    await route.fulfill({ json: body });
  });
  await page.goto('/workspace');
  await expect(page.locator('.monaco-editor')).toContainText('BookingService');
  return state;
}

test('reading and switching files never changes candidate code', async ({ page }) => {
  const { contents, writes } = await workspace(page);
  for (const path of ['README.md', 'src/booking.py', 'tests/test_public.py',
    'tests/test_candidate.py', 'src/main.py', 'README.md', 'src/booking.py']) {
    await page.getByText(path, { exact: true }).first().click();
    await expect(page.locator('.editor-tabs [aria-pressed="true"]')).toContainText(path);
    await expect(page.locator('.monaco-editor')).toContainText(contents[path].split('\n')[0]);
  }
  await page.waitForTimeout(5500); // Cover the actual automatic save interval.
  expect(writes).toEqual([]);
  await page.locator('.monaco-editor .view-lines').click();
  await page.keyboard.press('Control+End');
  await page.evaluate(() => navigator.clipboard.writeText('\n# actual candidate edit'));
  await page.keyboard.press('Control+v');
  await page.getByText('README.md', { exact: true }).first().click();
  await expect(page.locator('.editor-tabs [aria-pressed="true"]')).toContainText('README.md');
  await page.waitForTimeout(5500);
  expect(writes).toHaveLength(1);
  expect(writes[0].path).toBe('src/booking.py');
  expect(writes[0].content).toContain('BookingService');
  expect(writes[0].content).toContain('# actual candidate edit');
});

test('rapid typing and delayed file reads preserve file identity and undo', async ({ page }) => {
  const state = await workspace(page);
  await page.locator('.monaco-editor .view-lines').click();
  await page.keyboard.press('Control+End');
  await page.keyboard.type('\n# rapid candidate typing 0123456789 abcdefghijklmnopqrstuvwxyz');
  await expect(page.locator('.monaco-editor')).toContainText('rapid candidate typing 0123456789 abcdefghijklmnopqrstuvwxyz');
  await page.getByText('tests/test_candidate.py', { exact: true }).first().click();
  await expect(page.locator('.editor-tabs [aria-pressed="true"]')).toContainText('tests/test_candidate.py');
  await page.locator('.monaco-editor .view-lines').click();
  await page.keyboard.press('Control+End');
  await page.keyboard.type('\n# candidate test edit');
  await page.keyboard.press('Control+z');
  await expect(page.locator('.monaco-editor')).not.toContainText('candidate test edit');
  state.readDelay = 1500;
  const start = Date.now();
  await page.getByText('src/booking.py', { exact: true }).first().click();
  await expect(page.locator('.monaco-editor')).toContainText('rapid candidate typing 0123456789 abcdefghijklmnopqrstuvwxyz');
  expect(Date.now() - start).toBeLessThan(1000);
  // A late response for booking must not replace the currently displayed readonly file.
  await page.getByText('src/main.py', { exact: true }).first().click();
  await expect(page.locator('.editor-tabs [aria-pressed="true"]')).toContainText('src/main.py');
  await expect(page.locator('.monaco-editor')).toContainText('readonly command line example');
  await expect(page.locator('.editor-tabs [aria-pressed="true"]')).toContainText('只读');
  await expect.poll(()=>state.writes.some(value=>value.path==='src/booking.py')).toBe(true);
  expect(state.writes.find(value => value.path === 'src/booking.py').content)
    .toContain('rapid candidate typing 0123456789 abcdefghijklmnopqrstuvwxyz');
  expect(state.writes.every(value => !['src/main.py', 'README.md', 'tests/test_public.py'].includes(value.path))).toBe(true);
  // Revisit a cached file after a terminal writer changes it. The new version must appear.
  state.contents['src/booking.py'] += '\n# external terminal update\n';
  state.versions['src/booking.py']++;
  await page.getByText('src/booking.py', { exact: true }).first().click();
  await expect(page.locator('.monaco-editor')).toContainText('external terminal update');
});

test('terminal scrolling stays inside the viewport with the topbar visible', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await workspace(page);
  await expect(page.getByText('终端已连接 · /workspace', { exact: true })).toBeVisible();
  for (const size of [{width:1440,height:900}, {width:900,height:600}]) {
    await page.setViewportSize(size);
    const before = await page.locator('.ide-topbar').boundingBox();
    await page.locator('.terminal-body').hover();
    await page.mouse.wheel(0, -2000);
    await page.mouse.wheel(0, 5000);
    const after = await page.locator('.ide-topbar').boundingBox();
    expect(after.y).toBe(before.y);
    expect(after.y).toBeGreaterThanOrEqual(0);
    expect(await page.evaluate(() => document.documentElement.scrollHeight - innerHeight)).toBeLessThanOrEqual(1);
    const terminal = await page.locator('.terminal-pane').boundingBox();
    expect(terminal.y + terminal.height).toBeLessThanOrEqual(size.height + 1);
  }
  await page.getByRole('button', { name: '折叠', exact: true }).click();
  await expect(page.locator('.terminal-pane')).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollHeight - innerHeight)).toBeLessThanOrEqual(1);
  await page.getByRole('button', { name: '展开终端' }).click();
  await expect(page.locator('.terminal-pane')).toBeVisible();
  expect(state.connections).toBe(1);
  state.failReads = true;
  await page.getByText('README.md', { exact: true }).first().click();
  await expect(page.locator('.workspace-alert[role="alert"]')).toContainText('文件暂时读取失败');
  state.failReads=false;
  await page.getByRole('button',{name:'重试读取',exact:true}).click();
  await expect(page.locator('.editor-tabs [aria-pressed="true"]')).toContainText('README.md');
  await expect(page.locator('.monaco-editor')).toContainText('中文题目说明');
  expect(await page.evaluate(() => document.documentElement.scrollHeight - innerHeight)).toBeLessThanOrEqual(1);
  expect((await page.locator('.ide-topbar').boundingBox()).y).toBeGreaterThanOrEqual(0);
  await page.screenshot({ path: '../runtime/workspace-scroll-fixed.png' });
});

test('version comparison rejects a second conflict and preserves the other file', async ({page})=>{
  const state=await workspace(page);
  await page.locator('.monaco-editor .view-lines').click();
  await page.keyboard.press('Control+End');
  await page.keyboard.type('\n# local booking change');
  await expect(page.locator('.editor-tabs [aria-pressed="true"]')).toContainText('●');
  await page.getByText('tests/test_candidate.py',{exact:true}).first().click();
  await expect(page.locator('.editor-tabs [aria-pressed="true"]')).toContainText('tests/test_candidate.py');
  await page.locator('.monaco-editor .view-lines').click();
  await page.keyboard.press('Control+End');
  await page.keyboard.type('\n# independent test change');
  state.contents['src/booking.py']+='\n# server first change';state.versions['src/booking.py']=2;
  await page.getByText('src/booking.py',{exact:true}).first().click();
  await page.getByRole('button',{name:'重新读取并比较'}).waitFor({timeout:15000});
  await page.getByRole('button',{name:'重新读取并比较'}).click();
  await expect(page.locator('.workspace-recovery')).toContainText('server first change');
  state.contents['src/booking.py']+='\n# server second change';state.versions['src/booking.py']=3;
  page.once('dialog',d=>d.accept());
  await page.getByRole('button',{name:'确认用本地内容覆盖此版本'}).click();
  await expect(page.locator('.workspace-alert').first()).toContainText('文件已修改');
  expect(state.contents['src/booking.py']).toContain('server second change');
  await expect(page.locator('.monaco-editor')).toContainText('local booking change');
  await page.getByRole('button',{name:'重新读取并比较'}).click();
  await expect(page.locator('.workspace-recovery')).toContainText('server second change');
  page.once('dialog',d=>d.accept());
  await page.getByRole('button',{name:'使用服务器内容（放弃本地修改）'}).click();
  await expect(page.locator('.workspace-recovery')).toHaveCount(0);
  await expect(page.locator('.monaco-editor')).toContainText('server second change');
  await expect(page.locator('.monaco-editor')).not.toContainText('local booking change');
  await page.getByText('tests/test_candidate.py',{exact:true}).first().click();
  await expect(page.locator('.monaco-editor')).toContainText('independent test change');
  expect(state.connections).toBe(1);
});
