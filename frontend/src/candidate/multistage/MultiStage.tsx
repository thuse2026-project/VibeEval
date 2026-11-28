import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { renderMarkdown } from '../../shared/markdown'
import { WorkspaceIcon } from '../../shared/components/WorkspaceIcon'
import { FileTree } from '../../shared/components/FileTree'
import type { FileContentResponse, FileEntry, SaveFileResponse } from '../../shared/types'
import { SaveQueue } from '../workspace/SaveQueue'
import { TinyDbStage } from './TinyDbStage'
import { WorkspaceScreen } from '../workspace/WorkspaceScreen'
import { DatasetteVerification, hasDatasetteVerification } from './DatasetteVerification'
import '../workspace/workspace.css'
import type { AgentFileChange } from '../workspace/AgentPanelV2'
import { command, current, examRequest, type Attempt, type Receipt, stageTitle, v2 } from './api'
import { QuestionStage } from './QuestionStage'
import './multistage.css'
import { CandidateFrame } from '../../shared/components/CandidateFrame'
import './candidateAppearance.css'

// The private preview can extend a manually selected attempt to the last supported date.
const PRACTICE_DEADLINE = 253402300799

function CodeStage({ state, modelReady, refresh, remaining }: {
  state: Attempt; modelReady: boolean; refresh: (updated?: Attempt) => Promise<void>; remaining: number
}) {
  const phase = state.current_phase.id
  const prefix = `${v2}/phases/${phase}`
  const stagePath = `/stages/${state.current_stage.id}`
  const continuesToQuestions = state.next_action === 'finish_phase'
  const [files, setFiles] = useState<FileEntry[]>([])
  const [selected, setSelected] = useState('')
  const [revision, setRevision] = useState(0)
  const [text, setText] = useState('')
  const [summaryRevision, setSummaryRevision] = useState(0)
  const [question, setQuestion] = useState('')
  const taskDocument = useMemo(() => question ? renderMarkdown(question, true) : null, [question])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [testRunning, setTestRunning] = useState(false)
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
  const locked = busy || agentBusy || testRunning || Boolean(loadingPath) || remaining <= 0
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
        continuesToQuestions ? null : examRequest<{ value: string | null; revision: number }>(v2 + stagePath + '/summary', 'GET', undefined, abort.signal),
      ])
      if (abort.signal.aborted) return
      setFiles(tree.files); setQuestion(questions.items[0]?.body ?? '')
      setText(summary?.value ?? ''); setSummaryRevision(summary?.revision ?? 0)
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
  }, [prefix, queue, stagePath, continuesToQuestions])

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
      if (latest.current_phase.id !== phase || latest.current_phase.status !== 'active') { await refresh(); return }
      if (latest.next_action === 'finish_phase') {
        if (!submissionKey.current) submissionKey.current = { key: crypto.randomUUID(), summary: '' }
        const updated = await command<Attempt>('/phases/' + phase + '/finish', latest, {}, submissionKey.current.key)
        await refresh(updated)
        return
      }
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
    <WorkspaceScreen title={stageTitle(state)} remaining={remaining}
      practice={state.current_phase.deadline_at === PRACTICE_DEADLINE} files={files} openFiles={openFiles}
      activePath={selected || null} content={draft?.content ?? ''} editorRevision={revision}
      loadingPath={loadingPath}
      activeReadonly={!draft || draft.readonly || locked || !state.capabilities.includes('write_repository')}
      saveState={queue.saving ? 'saving' : openFiles.some(item => queue.drafts[item.path].error) ? 'error' : openFiles.some(item => item.dirty) ? 'idle' : 'saved'}
      saveFile={() => handle(() => queue.save(selected))}
      discardFile={async path => {
        window.clearTimeout(autosave.current.get(path))
        await queue.settle()
        const item = queue.drafts[path]
        if (item) { delete queue.drafts[path]; setRevision(value => value + 1) }
        return true
      }}
      busy={locked} onSelectFile={choose} onEdit={edit} onSubmit={submit} continuesToQuestions={continuesToQuestions}
      submitLabel={continuesToQuestions ? '结束编程，固定代码并进入问答' : '提交本阶段'}
      tabSelectionDisabled={busy || agentBusy || remaining <= 0}
      sessionId={terminalAvailable ? phase : null}
      terminalPrefix="/api/v2/phases/" resizable
      filesPane={(selectFile, activePath) => <FileTree files={files} activePath={activePath} onSelect={selectFile} directories />}
      problem={selectFile => <div className="problem-pane r2-problem">
        {taskDocument && <article className="task-document" dangerouslySetInnerHTML={taskDocument} />}
        {files.some(file => !file.readonly) && <section className="r2-editable-files">
          <h2>可编辑文件</h2>
          {files.filter(file => !file.readonly).map(file => <button key={file.path} title={file.path}
            onClick={() => selectFile(file.path)}><WorkspaceIcon name="code" />{file.path}</button>)}
        </section>}
        {!taskDocument && <pre>{question || '正在读取题目…'}</pre>}
      </div>}
      verification={hasDatasetteVerification(state.current_stage.key) ? (openTerminal, selectFile, panel) =>
        <DatasetteVerification panel={panel} phaseId={phase} task={state.current_stage.key} disabled={locked || !terminalAvailable}
          flush={() => queue.flush()} openTerminal={openTerminal} openFile={selectFile} onRunningChange={setTestRunning} /> : undefined}
      agentScope={remaining > 0 && state.capabilities.includes('use_agent') ? phase : null}
      initialAgentOpen={state.capabilities.includes('use_agent')}
      agentPrefix="/api/v2/phases/" agentModelReady={modelReady}
      agentCanExecute={state.capabilities.includes('use_terminal') || state.capabilities.includes('write_repository')}
      beforeAgentRun={() => queue.flush()} onAgentBusy={setAgentBusy} onAgentWorkspaceChange={onAgentWorkspaceChange}
      actions={<div className="r2-file-actions">
        <span aria-label="阶段进度">{state.stages.findIndex(item => item.id === state.current_stage.id) + 1}/{state.stages.length}</span>
        <button disabled={locked || queue.saving} onClick={() => handle(() => queue.flush())}>保存全部</button>
        <button disabled={locked || !selected} onClick={() => handle(() => select(selected, true))}>重新读取文件</button>
      </div>}
      submissionFields={continuesToQuestions ? undefined : <div className="r2-summary">
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

  async function refresh(updated?: Attempt) { accept(updated ?? await current()) }

  useEffect(() => {
    document.title = '考生工作台 · VibeEval'
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
    if (state.current_stage.key.startsWith('tinydb-')) {
      return <TinyDbStage key={state.current_phase.id} state={state} remaining={remaining} refresh={refresh} modelReady={modelReady} />
    }
    const answering = state.current_phase.key === 'answer' || state.current_phase.key === 'interview' || state.capabilities.includes('read_frozen_code')
    return answering
      ? <QuestionStage key={state.current_phase.id} state={state} refresh={refresh} remaining={remaining} />
      : <CodeStage key={state.current_phase.id} state={state} modelReady={modelReady} refresh={refresh} remaining={remaining} />
  }
  if (state?.status === 'completed') return <CandidateFrame>
    <section className="exam-departure"><span className="departure-mark"><WorkspaceIcon name="check" /></span>
      <h1>答卷已提交</h1><p className="flow-intro">感谢作答，可以离场。</p>
    </section>
  </CandidateFrame>
  const firstStage = state && state.stages[0]?.id === state.current_stage.id
  const ready = state?.current_stage.status === 'ready'
  const waiting = state && (state.status === 'preparing' || ['preparing', 'sealing'].includes(state.current_stage.status) || state.current_phase.status === 'closing')
  return <CandidateFrame>
    <div className="exam-overview"><section className="exam-next">
      <h1>{state ? firstStage && state.status === 'admitted' ? '考前准备' : stageTitle(state) : error ? '无法进入工作台' : '正在进入工作台'}</h1>
      {state?.stages.length === 4 && firstStage && <section className="exam-notice" aria-label="考试须知">
        <h2>考试须知</h2><p>第三、四题各有主观题。每题编程结束后显示完整题干，另有 10 分钟独立作答。</p>
      </section>}
      {error && <p className="flow-error" role="alert">{error} <a href="/entry?mode=r2">返回入场</a></p>}
      {state?.error_code && <p className="flow-error" role="alert">环境处理中：{state.error_code}</p>}
      {waiting && <p role="status">正在准备环境或固定答卷…</p>}
      {deadline != null && <p className="transition-time">下一阶段将在 <b>{Math.floor(remaining / 60)}:{String(remaining % 60).padStart(2, '0')}</b> 后自动开始。</p>}
      {state?.status === 'admitted' && <button className="flow-primary" disabled={busy} onClick={() => void transition(`/attempts/${state.attempt_id}/prepare`)}>准备环境</button>}
      {state && ready && <button className="flow-primary" disabled={busy}
        onClick={() => void transition(state.status === 'ready' ? `/attempts/${state.attempt_id}/start` : `/stages/${state.current_stage.id}/start`)}>开始作答</button>}
    </section>
    {state && <aside className="exam-outline"><h2>考试进度</h2><ol>{state.stages.map((item, index) =>
      <li key={item.id} aria-current={item.id === state.current_stage.id && !item.receipt ? 'step' : undefined}>
        <span className="stage-number">{item.receipt ? <WorkspaceIcon name="check" /> : String(index+1).padStart(2,'0')}</span>
        <div><strong>{item.title}</strong>{item.receipt && <small>回执 <code>{item.receipt.id}</code>{item.receipt.close_reason === 'expired' && ' · 到时提交'}</small>}</div>
        <span>{item.receipt ? '已提交' : item.id === state.current_stage.id ? '当前阶段' : '待开始'}</span>
      </li>)}</ol></aside>}
    </div>
  </CandidateFrame>
}
