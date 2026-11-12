import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

function moduleUrl(source) {
  const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } });
  return `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`;
}

const clockUrl = moduleUrl(readFileSync(new URL('../src/shared/clock.ts', import.meta.url), 'utf8'));
const apiSource = readFileSync(new URL('../src/candidate/workspace/api.ts', import.meta.url), 'utf8')
  .replace(/(['"])\.\.\/\.\.\/shared\/clock\1/g, JSON.stringify(clockUrl));
const apiUrl = moduleUrl(apiSource);
const agentSource = readFileSync(new URL('../src/candidate/workspace/agentApi.ts', import.meta.url), 'utf8')
  .replace(/(['"])\.\/api\1/g, JSON.stringify(apiUrl));
const { agentApi } = await import(moduleUrl(agentSource));

test('Agent requests stay same-origin and turns request read-only mode', async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  try {
    globalThis.fetch = async (url, init) => {
      requests.push({ url, init });
      return new Response(JSON.stringify({ run_id: 'run-1', status: 'queued' }), { status: 202 });
    };
    await agentApi.turn('session/a', 'conversation b', 'Explain this code');
    const [{ url, init }] = requests;
    assert.equal(url, '/api/v1/sessions/session%2Fa/agent/conversations/conversation%20b/turns');
    assert.equal(init.credentials, 'same-origin');
    assert.equal(init.headers['X-VibeEval-Request'], '1');
    assert.equal(JSON.parse(init.body).mode, 'read_only');
    assert.equal(JSON.parse(init.body).text, 'Explain this code');
    assert.ok(JSON.parse(init.body).request_key);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
