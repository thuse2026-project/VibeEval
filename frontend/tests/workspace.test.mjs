import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

async function load(relative) {
  const source = readFileSync(new URL(relative, import.meta.url), 'utf8');
  const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } });
  const clockSource=readFileSync(new URL('../src/shared/clock.ts',import.meta.url),'utf8');
  const clockCode=ts.transpileModule(clockSource,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}}).outputText;
  const clockURL=`data:text/javascript;base64,${Buffer.from(clockCode).toString('base64')}`;
  const linked=outputText.replace(/(['"])\.\.\/\.\.\/shared\/clock\1/g,JSON.stringify(clockURL));
  return import(`data:text/javascript;base64,${Buffer.from(linked).toString('base64')}`);
}
const { SaveQueue } = await load('../src/candidate/workspace/SaveQueue.ts');

test('serial saves keep latest edits and use server version across files', async () => {
  const calls = [];
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const queue = new SaveQueue(async (path, version, content) => {
    calls.push({ path, version, content });
    if (calls.length === 1) await pending;
    return { new_version: version + 1 };
  }, () => {});
  queue.load('a', 'old', 1, false); queue.load('b', '', 3, false);
  queue.edit('a', 'one');
  const first = queue.save('a');
  await Promise.resolve();
  queue.edit('a', 'two'); queue.edit('b', 'other');
  const second = queue.save('a'); const third = queue.save('b');
  assert.equal(calls.length, 1);
  release();
  await Promise.all([first, second, third]);
  await queue.flush();
  assert.deepEqual(calls, [
    { path: 'a', version: 1, content: 'one' },
    { path: 'a', version: 2, content: 'two' },
    { path: 'b', version: 3, content: 'other' },
  ]);
  assert.equal(queue.drafts.a.dirty, false);
});

test('uncertain save blocks automatic overwrite and submission flush, preserving local edits', async () => {
  let calls = 0;
  const queue = new SaveQueue(async () => { calls++; throw new Error('FILE_SAVE_UNCERTAIN'); }, () => {});
  queue.load('a', 'old', 1, false); queue.edit('a', 'local');
  await assert.rejects(queue.save('a'), /FILE_SAVE_UNCERTAIN/);
  queue.edit('a', 'local again');
  await assert.rejects(queue.flush(), /FILE_SAVE_UNCERTAIN/);
  assert.equal(calls, 1);
  assert.equal(queue.drafts.a.content, 'local again');
  assert.equal(queue.drafts.a.version, 1);
});

test('readonly documents cannot be edited or saved', async () => {
  const queue = new SaveQueue(async () => { assert.fail('readonly save'); }, () => {});
  queue.load('README.md', 'rules', 1, true); queue.edit('README.md', 'changed');
  await queue.flush();
  assert.equal(queue.drafts['README.md'].content, 'rules');
});

test('HTTP requests use cookie/header authentication and preserve structured conflict errors', async () => {
  const { workspaceRequest, WorkspaceError } = await load('../src/candidate/workspace/api.ts');
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async (url, init) => {
      assert.equal(url, '/api/v1/sessions/ses_test/file');
      assert.equal(init.credentials, 'same-origin');
      assert.equal(init.headers['X-VibeEval-Request'], '1');
      return new Response(JSON.stringify({ detail: { code: 'FILE_VERSION_CONFLICT', message: '已修改' } }), { status: 409 });
    };
    await assert.rejects(workspaceRequest('ses_test/file', 'PUT', {}), error => {
      assert.ok(error instanceof WorkspaceError);
      assert.equal(error.code, 'FILE_VERSION_CONFLICT');
      assert.equal(error.message, '已修改');
      return true;
    });
  } finally { globalThis.fetch = original; }
});
