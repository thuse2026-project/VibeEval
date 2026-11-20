import { useCallback, useEffect, useRef, useState } from 'react'
import { FileTree } from '../../shared/components/FileTree'
import type { FileContentResponse, FileEntry, SaveFileResponse } from '../../shared/types'
import { SaveQueue } from '../workspace/SaveQueue'
import { WorkspaceScreen } from '../workspace/WorkspaceScreen'
import { DatasetteVerification, hasDatasetteVerification } from './DatasetteVerification'
import '../workspace/workspace.css'
import type { AgentFileChange } from '../workspace/AgentPanelV2'
import { command, current, examRequest, type Attempt, type Receipt, v2 } from './api'
import './multistage.css'

// The private preview can extend a manually selected attempt to the last supported date.
const PRACTICE_DEADLINE = 253402300799

function CodeStage({ state, modelReady, refresh, remaining }: {
  state: Attempt; modelReady: boolean; refresh: () => Promise<void>; remaining: number
}) {
  const phase = state.current_phase.id
  const prefix = `${v2}/phases/${phase}`
  const stagePath = `/stages/${state.current_stage.id}`
  const [files, setFiles] = useState<FileEntry[]>([])
  const [selected, setSelected] = useState('')
  const [revision, setRevision] = useState(0)
  const [text, setText] = useState('')
  const [summaryRevision, setSummaryRevision] = useState(0)
  const [question, setQuestion] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [agentConflict, setAgentConflict] = useState<FileContentResponse | null>(null)
  const [agentBusy, setAgentBusy] = useState(false)
  const [loadingPath, setLoadingPath] = useState<string | null>(null)
  const [, redraw] = useState(0)
  const request = useRef(0)
  const submissionKey = useRef<{ key: string; summary: string } | null>(null)
  const submitting = useRef(false)
  const autosave = useRef(new Map<string, number>())
  const [queue] = useState(() => new SaveQueue(
    (path, version, content) => examRequest<SaveFileResponse>(prefix + '/file', 'PUT', { path, expected_version: version, content }),
    () => redraw(value => value + 1),
  ))
  const locked = busy || agentBusy || Boolean(loadingPath) || remaining <= 0
  const draft = queue.drafts[selected]

  async function select(path: string, reload = false) {
    const cached = queue.drafts[path]
    if (cached?.dirty && reload && !window.confirm('重新读取会放弃此文件未保存的编辑，继续吗？')) return
    const id = ++request.current
    if (cached) setSelected(path)
    setLoadingPath(!cached || reload ? path : null)
    if (cached?.dirty && !reload) return
    if (cached && !reload) {
      // Let rapid tab switches settle before calling the sandbox.
      await new Promise<void>(resolve => window.setTimeout(resolve, 250))
      if (request.current !== id || queue.drafts[path]?.dirty) return
    }
    try {
      const value = await examRequest<FileContentResponse>(prefix + '/file?path=' + encodeURIComponent(path))
      if (request.current !== id) return
      const latest = queue.drafts[path]
      if (latest && (latest.version > value.version || (!reload && (
        latest.dirty || latest.error || latest.version === value.version
      )))) return
      queue.load(path, value.content, value.version, files.find(item => item.path === path)?.readonly ?? path === 'README.md')
      setAgentConflict(previous => previous?.path === path ? null : previous)
      setSelected(path); setRevision(value => value + 1)
    } catch (failure) {
      if (request.current === id) throw failure
    } finally { if (request.current === id) setLoadingPath(null) }
  }

  useEffect(() => {
    const abort = new AbortController()
    void (async () => {
      const [tree, questions, summary] = await Promise.all([
        examRequest<{ files: FileEntry[] }>(prefix + '/files', 'GET', undefined, abort.signal),
        examRequest<{ items: { body: string }[] }>(prefix + '/questions', 'GET', undefined, abort.signal),
        examRequest<{ value: string | null; revision: number }>(v2 + stagePath + '/summary', 'GET', undefined, abort.signal),
      ])
      if (abort.signal.aborted) return
      setFiles(tree.files); setQuestion(questions.items[0]?.body ?? '')
      setText(summary.value ?? ''); setSummaryRevision(summary.revision)
      const first = tree.files.find(item => item.path === 'README.md') ?? tree.files[0]
      if (first) {
        const id = request.current
        const value = await examRequest<FileContentResponse>(prefix + '/file?path=' + encodeURIComponent(first.path), 'GET', undefined, abort.signal)
        if (abort.signal.aborted || request.current !== id) return
        queue.load(first.path, value.content, value.version, first.readonly)
        setSelected(first.path); setRevision(value => value + 1)
      }
    })().catch(failure => { if (!abort.signal.aborted) setError((failure as Error).message) })
    return () => { request.current += 1; abort.abort() }
  }, [prefix, queue, stagePath])

  useEffect(() => {
    const timers = autosave.current
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (Object.values(queue.drafts).some(item => item.dirty) || submitting.current) event.preventDefault()
    }
    window.addEventListener('beforeunload', beforeUnload)
    return () => {
      for (const timer of timers.values()) window.clearTimeout(timer)
      window.removeEventListener('beforeunload', beforeUnload)
    }
  }, [queue])

  function edit(path: string, content: string) {
    if (locked || submitting.current) return
    queue.edit(path, content)
    window.clearTimeout(autosave.current.get(path))
    autosave.current.set(path, window.setTimeout(() => {
      autosave.current.delete(path)
      if (!submitting.current) void queue.save(path).catch(failure => setError((failure as Error).message))
    }, 5000))
  }

  const onAgentWorkspaceChange = useCallback(async ({ path, version }: AgentFileChange) => {
    try {
      await queue.settle()
      const opened = queue.drafts[path]
      if (!opened || (version !== null && opened.version >= version)) return
      const file = await examRequest<FileContentResponse>(prefix + '/file?path=' + encodeURIComponent(path))
      const latest = queue.drafts[path]
      if (!latest || latest.version >= file.version) return
      if (latest.dirty || latest.error) {
        window.clearTimeout(autosave.current.get(path))
        autosave.current.delete(path)
        latest.error = 'Agent 已修改该文件，你的草稿已保留。'
        setAgentConflict(file)
        return
      }
      queue.load(path, file.content, file.version, latest.readonly)
      setRevision(value => value + 1)
    } catch (failure) { setError((failure as Error).message) }
  }, [prefix, queue])

  async function resolveAgentConflict(useDraft: boolean) {
    if (!agentConflict) return
    const { path, content, version } = agentConflict
    const local = queue.drafts[path]
    if (!local) return
    queue.load(path, content, version, local.readonly)
    if (useDraft) queue.edit(path, local.content)
    setAgentConflict(null)
    setRevision(value => value + 1)
    if (useDraft) await queue.save(path)
  }

  async function saveSummary() {
    const saved = await examRequest<{ revision: number }>(v2 + stagePath + '/summary', 'PUT', { expected_revision: summaryRevision, text })
    setSummaryRevision(saved.revision)
  }

  async function submit() {
    if (locked || submitting.current) throw new Error('请等待当前操作结束后提交。')
    submitting.current = true
    setBusy(true); setError('')
    try {
      await queue.flush()
      const latest = await current()
      if (latest.current_stage.id !== state.current_stage.id) { await refresh(); return }
      if (submissionKey.current?.summary !== text) submissionKey.current = { key: crypto.randomUUID(), summary: text }
      await command<Receipt>(stagePath + '/submit', latest, { summary: text }, submissionKey.current.key)
      await refresh()
    } catch (failure) {
      await refresh().catch(() => undefined)
      throw failure
    } finally { submitting.current = false; setBusy(false) }
  }

  const handle = (action: () => Promise<unknown>) => { setError(''); void action().catch(failure => setError((failure as Error).message)) }
  const openFiles = Object.entries(queue.drafts).map(([path, item]) => ({ path, readonly: item.readonly, dirty: item.dirty }))
  const choose = (path: string | null) => {
    if (busy) return
    if (path) handle(() => select(path))
    else {
      request.current++
      setSelected('')
      setLoadingPath(null)
    }
  }
  const terminalAvailable = files.length > 0 && remaining > 0 && state.capabilities.includes('use_terminal')
  return <div className="candidate-workspace workspace-active r2-workspace">
    {error && <div className="workspace-alert" role="alert">{error}<button onClick={() => setError('')}>关闭</button></div>}
    {agentConflict && <div className="workspace-alert" role="alert">Agent 已修改 {agentConflict.path}，你的草稿已保留。
      <details><summary>查看服务器版本</summary><pre>{agentConflict.content}</pre></details>
      <button disabled={locked} onClick={() => handle(() => resolveAgentConflict(false))}>使用服务器版本</button>
      <button disabled={locked} onClick={() => handle(() => resolveAgentConflict(true))}>保存我的草稿</button>
    </div>}
    <WorkspaceScreen title={state.current_stage.title} remaining={remaining}
      practice={state.current_phase.deadline_at === PRACTICE_DEADLINE} files={files} openFiles={openFiles}
      activePath={selected || null} content={draft?.content ?? ''} editorRevision={revision}
      loadingPath={loadingPath}
      activeReadonly={!draft || draft.readonly || locked || !state.capabilities.includes('write_repository')}
      saveState={queue.saving ? 'saving' : openFiles.some(item => queue.drafts[item.path].error) ? 'error' : openFiles.some(item => item.dirty) ? 'idle' : 'saved'}
      busy={locked} onSelectFile={choose} onEdit={edit} onSubmit={submit} submitLabel="提交本阶段"
      tabSelectionDisabled={busy || agentBusy || remaining <= 0}
      sessionId={terminalAvailable ? phase : null}
      terminalPrefix="/api/v2/phases/" resizable
      filesPane={(selectFile, activePath) => <FileTree files={files} activePath={activePath} onSelect={selectFile} directories />}
      problem={selectFile => <div className="problem-pane r2-problem">
        {files.some(file => !file.readonly) && <section className="r2-editable-files">
          <h2>可编辑文件</h2>
          {files.filter(file => !file.readonly).map(file => <button key={file.path} title={file.path}
            onClick={() => selectFile(file.path)}>{file.path}</button>)}
        </section>}
        <pre>{question || '正在读取题目…'}</pre>
      </div>}
      verification={hasDatasetteVerification(state.current_stage.key) ? (openTerminal, selectFile) =>
        <DatasetteVerification task={state.current_stage.key} disabled={locked || !terminalAvailable}
          flush={() => queue.flush()} openTerminal={openTerminal} openFile={selectFile} /> : undefined}
      agentScope={remaining > 0 && state.capabilities.includes('use_agent') ? phase : null}
      agentPrefix="/api/v2/phases/" agentModelReady={modelReady}
      agentCanExecute={state.capabilities.includes('use_terminal') || state.capabilities.includes('write_repository')}
      beforeAgentRun={() => queue.flush()} onAgentBusy={setAgentBusy} onAgentWorkspaceChange={onAgentWorkspaceChange}
      actions={<div className="r2-file-actions">
        <span aria-label="阶段进度">{state.stages.findIndex(item => item.id === state.current_stage.id) + 1}/{state.stages.length}</span>
        <button disabled={locked || queue.saving} onClick={() => handle(() => queue.flush())}>保存代码</button>
        <button disabled={locked || !selected} onClick={() => handle(() => select(selected, true))}>重新读取文件</button>
      </div>}
      submissionFields={<div className="r2-summary">
        <label htmlFor="r2-summary">改动与验证说明</label>
        <textarea id="r2-summary" placeholder="写明主要改动和验证结果" value={text} maxLength={1500}
          disabled={locked} onChange={event => setText(event.target.value)} />
        <button disabled={locked} onClick={() => handle(saveSummary)}>保存说明</button>
      </div>}
    />
  </div>
}

export default function MultiStage() {
  const [state, setState] = useState<Attempt | null>(null)
  const [modelReady, setModelReady] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [clock, setClock] = useState(() => performance.now())
  const sync = useRef(performance.now())
  const version = useRef(-1)
  function accept(value: Attempt) {
    if (value.state_version < version.current) return
    version.current = value.state_version
    sync.current = performance.now()
    setClock(sync.current)
    setState(value)
  }

  async function refresh() { accept(await current()) }

  useEffect(() => {
    const abort = new AbortController()
    async function start() {
      const entry = await examRequest<{ session_id: string }>('/api/v1/sessions/current', 'GET', undefined, abort.signal)
      const result = await examRequest<Attempt>(`/api/v1/sessions/${entry.session_id}/exam-access`, 'POST', {}, abort.signal)
      const model = await examRequest<{ configured: boolean }>('/r2-preview/model', 'GET', undefined, abort.signal)
      if (!abort.signal.aborted) { setModelReady(model.configured); accept(result) }
    }
    void start().catch(failure => { if (!abort.signal.aborted) setError((failure as Error).message) })
    const clockTimer = window.setInterval(() => setClock(performance.now()), 300)
    return () => { abort.abort(); window.clearInterval(clockTimer) }
  }, [])

  const polling = !!state && state.status !== 'completed'
  useEffect(() => {
    if (!polling) return
    let stopped = false
    let timer = 0
    async function poll() {
      try { const value = await current(); if (!stopped) accept(value) }
      catch { /* The next poll can recover a brief network interruption. */ }
      finally { if (!stopped) timer = window.setTimeout(() => void poll(), 2500) }
    }
    timer = window.setTimeout(() => void poll(), 2500)
    return () => { stopped = true; window.clearTimeout(timer) }
  }, [polling])

  async function transition(path: string) {
    if (!state || busy) return
    setBusy(true); setError('')
    try { accept(await command<Attempt>(path, state)) }
    catch (failure) { setError((failure as Error).message); await refresh().catch(() => undefined) }
    finally { setBusy(false) }
  }

  const active = state?.current_phase.status === 'active'
  const deadline = active ? state?.current_phase.deadline_at : state?.transition_deadline_at
  const remaining = deadline == null ? 0 : Math.max(0, Math.ceil(deadline - (state?.server_now ?? 0) - (clock - sync.current) / 1000))
  if (state?.current_phase.status === 'active' && state.current_stage.status === 'active') {
    return <CodeStage key={state.current_phase.id} state={state} modelReady={modelReady} refresh={refresh} remaining={remaining} />
  }
  return <div className="r2-page">
    <header className="topbar"><a className="brand" href="/entry?mode=r2">VibeEval</a><span>Datasette 两题联测</span></header>
    <main>
      <section className="r2-flow-card">
        <h1>{state?.status === 'completed' ? '两阶段已提交' : state?.current_stage.title ?? '正在进入工作台'}</h1>
        {error && <p role="alert">{error} <a href="/entry?mode=r2">返回入场</a></p>}
        {state?.error_code && <p role="alert">环境处理中：{state.error_code}</p>}
        {state && <ol className="r2-progress">{state.stages.map(item => <li key={item.id}
          aria-current={item.id === state.current_stage.id && !item.receipt ? 'step' : undefined}>
          <strong>{item.title}</strong>
          <span>{item.receipt ? '已提交' : item.id === state.current_stage.id ? '当前阶段' : '待开始'}</span>
          {item.receipt && <small>回执 <code>{item.receipt.id}</code>{item.receipt.close_reason === 'expired' && ' · 到时提交'}</small>}
        </li>)}</ol>}
        {deadline != null && <p>下一阶段将在 {Math.floor(remaining / 60)} 分 {String(remaining % 60).padStart(2, '0')} 秒后自动开始。</p>}
        {state?.status === 'admitted' && <button className="primary" disabled={busy} onClick={() => void transition(`/attempts/${state.attempt_id}/prepare`)}>准备环境</button>}
        {state?.status !== 'completed' && state?.current_stage.status === 'ready' && <button className="primary" disabled={busy}
          onClick={() => void transition(state.status === 'ready' ? `/attempts/${state.attempt_id}/start` : `/stages/${state.current_stage.id}/start`)}>开始作答</button>}
        {state && (state.status === 'preparing' || ['preparing', 'sealing'].includes(state.current_stage.status) || state.current_phase.status === 'closing') && <p role="status">正在准备环境或固定答卷…</p>}
      </section>
    </main>
  </div>
}
