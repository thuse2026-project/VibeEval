import { useState } from 'react'
import { WorkspaceIcon } from '../../shared/components/WorkspaceIcon'
import { examRequest, RequestError } from './api'
import { isReviewTestSample, testUnavailable } from './testRun'

const fixture = `import asyncio
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from datasette.app import Datasette

async def inspect():
    with TemporaryDirectory() as root:
        path = Path(root) / "data.db"
        with sqlite3.connect(path) as conn:
            conn.executescript("""
                create table items (id integer primary key, category text, score integer);
                insert into items values
                    (1, 'A', 10), (2, 'B', 20), (3, 'A', 30), (4, 'C', 40);
            """)
        conn.close()
        app = Datasette([path])
        try:
`

const scenarios: Record<string, { description: string; expected: string; probe: string }> = {
  'column-statistics': {
    description: '用固定数据打开分类 A 的表格页，翻到下一页，查看应用传给列统计弹窗的数据。',
    expected: '应有列统计数据。筛选仍是 A，统计包含 A 的两条记录，分页位置不参与统计。',
    probe: String.raw`            import json
            import re
            from urllib.parse import urlencode

            response = await app.client.get("/data/items?category=A&_size=1&_next=1")
            print("表格页 HTTP", response.status_code)
            match = re.search(r"window\._datasetteTableData\s*=\s*({.*?});", response.text)
            if not match:
                print("页面缺少表格数据")
                return
            stats = json.loads(match.group(1)).get("executeSql")
            if not stats:
                print("页面缺少列统计数据 (executeSql)")
                return
            print(json.dumps(stats, ensure_ascii=False, indent=2))
            condition = stats.get("filter", {})
            where = condition.get("where")
            sql = "select count(*) as matched_rows from items"
            if where:
                sql += " where " + where
            params = {**condition.get("params", {}), "sql": sql, "_shape": "objects"}
            result = await app.client.get(stats["queryUrl"] + "?" + urlencode(params))
            print("统计请求 HTTP", result.status_code)
            print(result.text)
`,
  },
  'facet-pagination': {
    description: '打开按分类统计的表格，跟随下一页链接，比较前后两页的计数和筛选链接。',
    expected: '两页都应显示 A=2、B=1、C=1。选择 A 的链接不应带上一页的 _next。',
    probe: `            from urllib.parse import parse_qs, urlsplit

            response = await app.client.get("/data/items.json?_facet=category&_size=2")
            for page in (1, 2):
                print("第", page, "页 HTTP", response.status_code)
                if response.status_code != 200:
                    print(response.text)
                    return
                data = response.json()
                rows = data["facet_results"]["results"]["category"]["results"]
                print("分类计数:", {row["value"]: row["count"] for row in rows})
                link = next((row["toggle_url"] for row in rows if row["value"] == "A"), None)
                print("选择 A 的参数:", parse_qs(urlsplit(link).query) if link else "无 A")
                if page == 1:
                    next_url = data["next_url"]
                    if not next_url:
                        print("缺少下一页链接")
                        return
                    response = await app.client.get(next_url.removeprefix("http://localhost"))
`,
  },
}

export function hasDatasetteVerification(task: string) {
  return Object.hasOwn(scenarios, task)
}

const testCommand = 'cd /workspace && python -m unittest discover -s tests -p "test_*.py" -v'

export function DatasetteVerification({ task, phaseId, disabled, flush, openTerminal, openFile, onRunningChange, panel }: {
  panel?: { open: boolean; show: () => void }
  phaseId: string
  task: string
  disabled: boolean
  flush: () => Promise<void>
  openTerminal: () => void
  openFile: (path: string) => void
  onRunningChange: (running: boolean) => void
}) {
  const [status, setStatus] = useState('')
  const [copying, setCopying] = useState(false)
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<{
    run_id?: string; status: string; exit_code: number | null; output: string; output_truncated: boolean;
    code_version: string; evidence_recorded: boolean; error_code: string | null
  } | null>(null)
  const scenario = scenarios[task]
  if (!scenario) return null

  async function copy(label: string, command: string) {
    setCopying(true)
    setStatus('')
    try {
      await flush()
    } catch {
      setStatus('代码保存失败，请处理保存问题后再验证。')
      setCopying(false)
      return
    }
    try {
      await navigator.clipboard.writeText(command)
      setStatus(`已复制${label}命令。请在终端提示符后粘贴并执行。`)
    } catch {
      setStatus('剪贴板不可用，请展开命令并手动复制到终端。')
    } finally {
      openTerminal()
      setCopying(false)
    }
  }

  async function runRecordedTest() {
    panel?.show()
    setRunning(true)
    setResult(null)
    setStatus('正在请求运行测试…')
    onRunningChange(true)
    try {
      await flush()
      const value = await examRequest<NonNullable<typeof result>>(`/api/v2/phases/${phaseId}/test-run`, 'POST', { command: testCommand })
      if (isReviewTestSample(value)) {
        setStatus(testUnavailable)
      } else if (!value.run_id) {
        setStatus('无法确认测试结果。服务端未返回运行编号。')
      } else {
        setResult(value)
        setStatus('')
      }
    } catch (failure) {
      setStatus(failure instanceof RequestError && failure.code === 'TEST_RUN_UNAVAILABLE'
        ? testUnavailable : (failure as Error).message)
    } finally {
      setRunning(false)
      onRunningChange(false)
    }
  }

  const reproduce = `cd /workspace && python - <<'PY'
${fixture}${scenario.probe}        finally:
            app.close()

asyncio.run(inspect())
PY`

  const reproduction = <>
    <h2>复现业务行为</h2>
    <p>{scenario.description}</p>
    <p>{scenario.expected}</p>
    <button type="button" disabled={disabled || copying || running} onClick={() => void copy('复现', reproduce)}>复制复现命令</button>
    <details><summary>查看复现命令</summary><pre>{reproduce}</pre></details>
  </>
  const runButton = <button type="button" className="record-test" disabled={disabled || copying || running} onClick={() => void runRecordedTest()}>
      {panel && <WorkspaceIcon name="play" />}
      {running ? '测试运行中…' : '运行并记录测试'}
    </button>
  const passed = result?.status === 'completed' && result.exit_code === 0
  const outcome = passed ? '测试通过' : result?.status === 'timed_out' ? '测试超时' : '测试未通过'
  if (panel) {
    const commandDetails = (label: string, script: string) => <details className="command-details">
      <summary>查看{label}命令</summary>
      <pre tabIndex={0}>{script}</pre>
      <button disabled={disabled || copying || running} onClick={() => void copy(label, script)}>
        <WorkspaceIcon name="clipboard" />复制{label}命令
      </button>
    </details>
    return <>
      <div className="test-shortcut">{runButton}</div>
      <section className="problem-pane r2-verification verification-guide" hidden={!panel.open}>
        <h2>测试与验证</h2>
        {result && <section className="r2-test-run-result" aria-live="polite">
          <header data-passed={passed}>
            <strong>{passed && <WorkspaceIcon name="check" />}{outcome}</strong>
            <span>退出码 {result.exit_code ?? '不可用'}</span>
          </header>
          <pre tabIndex={0}>{result.output || '无输出'}{result.output_truncated && '\n（输出已截断）'}</pre>
          <small>代码版本 <code title={result.code_version}>{result.code_version.slice(0, 12)}</code></small>
          {result.error_code && <p>{result.error_code}</p>}
          {!result.evidence_recorded && <p>阶段已关闭，结果未进入冻结证据。</p>}
        </section>}
        {status && <p role="status">{status}</p>}
        <section className="guide-section">
          <h3>公开测试与回归用例</h3>
          <p>公开测试覆盖部分核心行为。可在回归文件中补充边界用例，再一起运行。</p>
          <div className="test-files">
            <button onClick={() => openFile('tests/test_public.py')}><WorkspaceIcon name="code" />查看公开测试</button>
            <button onClick={() => openFile('tests/test_candidate.py')}><WorkspaceIcon name="code" />打开回归测试</button>
          </div>
          {commandDetails('测试', testCommand)}
        </section>
        <section className="guide-section">
          <h3>复现业务行为</h3>
          <p>{scenario.description}</p>
          <blockquote><strong>预期表现</strong>{scenario.expected}</blockquote>
          {commandDetails('复现', reproduce)}
        </section>
      </section>
    </>
  }
  return <>
    <section className="problem-pane r2-verification">
      {reproduction}
      <h2>运行测试</h2>
      <p>公开测试覆盖部分核心行为。可在回归测试文件中补充边界用例，然后运行同一命令。</p>
      <button type="button" disabled={disabled || copying || running} onClick={() => void copy('测试', testCommand)}>复制测试命令</button>
      {runButton}
      <details><summary>查看测试命令</summary><pre>{testCommand}</pre></details>
      {result && <section className="r2-test-run-result" aria-live="polite">
        <p>{outcome} · 退出码 {result.exit_code ?? '不可用'}{result.error_code ? ` · ${result.error_code}` : ''}</p>
        <p>运行代码版本 <code title={result.code_version}>{result.code_version.slice(0, 12)}</code>
          {!result.evidence_recorded && ' · 阶段已关闭，结果未进入冻结证据'}</p>
        <pre>{result.output || '无输出'}{result.output_truncated ? '\n（输出已截断）' : ''}</pre>
      </section>}
      <div className="r2-verification-files">
        <button type="button" onClick={() => openFile('tests/test_public.py')}>查看公开测试</button>
        <button type="button" onClick={() => openFile('tests/test_candidate.py')}>打开回归测试</button>
      </div>

      {status && <p role="status">{status}</p>}
    </section>
  </>
}
