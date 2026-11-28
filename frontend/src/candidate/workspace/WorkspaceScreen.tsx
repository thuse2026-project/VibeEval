import { WorkspaceIcon } from '../../shared/components/WorkspaceIcon'
import { AppearanceSettings } from '../../shared/components/CandidateAppearance'
import { useCandidateTheme } from '../../shared/candidateTheme'
import { PaneResize } from '../../shared/components/PaneResize'
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { CodeEditor } from '../../shared/components/Editor'
import { FileTree } from '../../shared/components/FileTree'
import { TerminalPane } from '../../shared/components/TerminalGateway'
import { formatRemaining } from '../../shared/format'
import { AgentPanel, type AgentFileChange } from './AgentPanelV2'
import type { FileEntry, SaveState } from '../../shared/types'

type SelectFile = (path: string) => void

type Props = {
  title: string
  remaining: number
  practice?: boolean
  files: FileEntry[]
  openFiles: Array<{ path: string; readonly: boolean; dirty: boolean }>
  activePath: string | null
  loadingPath?: string | null
  content: string
  editorRevision: number
  activeReadonly: boolean
  saveState: SaveState
  sessionId: string | null
  showTerminal?: boolean
  terminalPrefix?: string
  resizable?: boolean
  problem?: (selectFile: SelectFile) => ReactNode
  verification?: (openTerminal: () => void, selectFile: SelectFile, panel?: { open: boolean; show: () => void }) => ReactNode
  filesPane?: (selectFile: SelectFile, activePath: string | null) => ReactNode
  agentScope?: string | null
  agentPrefix?: string
  agentModelReady?: boolean
  agentCanExecute?: boolean
  beforeAgentRun?: () => Promise<void>
  onAgentBusy?: (busy: boolean) => void
  onAgentWorkspaceChange?: (change: AgentFileChange) => void
  initialAgentOpen?: boolean
  saveFile?: () => void
  discardFile?: (path: string) => Promise<boolean>
  processing?: boolean
  initialProblem?: boolean
  directSubmit?: boolean
  saveHint?: string
  actions?: ReactNode
  submissionFields?: ReactNode
  submissionNotice?: ReactNode
  submitLabel?: string
  continuesToQuestions?: boolean
  busy: boolean
  tabSelectionDisabled?: boolean
  onSelectFile: (path: string | null) => void
  onEdit: (path: string, content: string) => void
  onSubmit: () => Promise<void>
}

const saveLabel: Record<SaveState, string> = {
  idle: '未保存',
  saving: '保存中…',
  saved: '已保存',
  conflict: '版本冲突',
  error: '保存失败',
}

export function WorkspaceScreen({
  title,
  remaining,
  practice = false,
  files,
  openFiles,
  activePath,
  loadingPath,
  content,
  editorRevision,
  activeReadonly,
  saveState,
  sessionId,
  terminalPrefix,
  showTerminal = true,
  resizable = false,
  problem,
  verification,
  filesPane,
  agentScope = sessionId,
  agentPrefix,
  agentModelReady = true,
  agentCanExecute = true,
  beforeAgentRun,
  onAgentBusy,
  onAgentWorkspaceChange,
  initialAgentOpen = false, directSubmit = false, saveHint = "5 秒自动保存 · Python 3.12 · UTF-8",
  saveFile, discardFile, processing, initialProblem, actions,
  submissionFields,
  submissionNotice = '提交前会保存全部修改并冻结快照；提交后不能继续编辑。',
  submitLabel = '提交答卷',
  continuesToQuestions = false,
  busy,
  tabSelectionDisabled = busy,
  onSelectFile,
  onEdit,
  onSubmit,
}: Props) {
  const themed = useCandidateTheme() !== null
  const [leftTab, setLeftTab] = useState<'files' | 'problem' | 'verification'>(initialProblem ? 'problem' : 'files')
  const [agentOpened, setAgentOpened] = useState(initialAgentOpen)
  const [agentVisible, setAgentVisible] = useState(initialAgentOpen)
  const agentOpen = Boolean(agentScope && agentVisible)
  const main = useRef<HTMLDivElement>(null)
  const [fileWidth, setFileWidth] = useState(themed ? 22 : 19)
  const [agentWidth, setAgentWidth] = useState(27)
  const [termOpen, setTermOpen] = useState(true)
  const [confirmSubmit, setConfirmSubmit] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [closedPaths, setClosedPaths] = useState<Set<string>>(() => new Set())
  const tabs = openFiles.filter(file => !closedPaths.has(file.path))
  const editorPath = activePath && !closedPaths.has(activePath) ? activePath : null

  function selectFile(path: string) {
    setClosedPaths(previous => {
      if (!previous.has(path)) return previous
      const next = new Set(previous)
      next.delete(path)
      return next
    })
    onSelectFile(path)
  }

  const [terminalHeight, setTerminalHeight] = useState(28)
  useEffect(() => {
    if (!saveFile) return
    const shortcut = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 's') {
        e.preventDefault()
        if (!busy && !activeReadonly && editorPath) saveFile()
      }
    }
    window.addEventListener('keydown', shortcut, true)
    return () => window.removeEventListener('keydown', shortcut, true)
  }, [saveFile, busy, activeReadonly, editorPath])

  async function closeFile(path: string) {
    if (discardFile && tabs.find(tab => tab.path === path)?.dirty) {
      if (!window.confirm('当前修改尚未保存。放弃未保存的修改并关闭文件？') || !await discardFile(path)) return false
    }
    setClosedPaths(previous => new Set(previous).add(path))
    if (path === editorPath) {
      const index = tabs.findIndex(tab => tab.path === path)
      onSelectFile(tabs[index + 1]?.path ?? tabs[index - 1]?.path ?? null)
    }
    return true
  }

  function divider(side: 'files' | 'agent') {
    const filesDivider = side === 'files'
    const min = filesDivider ? 18 : 22
    const max = filesDivider ? 36 : 38
    const value = filesDivider ? fileWidth : agentWidth
    const setWidth = filesDivider ? setFileWidth : setAgentWidth
    const direction = filesDivider ? 1 : -1
    const resize = (width: number) => setWidth(Math.max(min, Math.min(max, Math.round(width))))
    const label = filesDivider ? '调整文件区宽度' : '调整 Agent 区宽度'
    return <div className="pane-divider" role="separator" tabIndex={0} aria-orientation="vertical"
      aria-label={label} title={label} aria-valuemin={min} aria-valuemax={max}
      aria-valuenow={value} aria-valuetext={`${value}%`}
      onPointerDown={event => {
        if (event.button !== 0) return
        event.preventDefault()
        event.currentTarget.focus()
        event.currentTarget.setPointerCapture(event.pointerId)
      }}
      onPointerMove={event => {
        if (!event.currentTarget.hasPointerCapture(event.pointerId) || !main.current) return
        const bounds = main.current.getBoundingClientRect()
        const offset = filesDivider ? event.clientX - bounds.left : bounds.right - event.clientX
        if (bounds.width > 0) resize(offset / bounds.width * 100)
      }}
      onKeyDown={event => {
        const delta = event.key === 'ArrowLeft' ? -direction : event.key === 'ArrowRight' ? direction : 0
        if (!delta && event.key !== 'Home' && event.key !== 'End') return
        event.preventDefault()
        resize(event.key === 'Home' ? min : event.key === 'End' ? max : value + 2 * delta)
      }} />
  }

  const submit = async () => {
    setSubmitting(true)
    setSubmitError(null)
    try {
      await onSubmit()
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : '交卷失败，请重试')
      setSubmitting(false)
    }
  }

  return (
    <div className="ide">
      <div className="ide-topbar">
        <span className="brand-mark" aria-hidden="true">&lt;/&gt;</span>
        <span className="logo">VibeEval</span>
        <span className="task">{title}</span>
        {themed && <span className="spacer" />}
        <span className="timer mono">{submitting || processing ? '正在提交…' : practice ? <small>测试模式 · 本阶段不计时</small> : <><small>剩余 </small>{formatRemaining(remaining)}</>}</span>
        <span className={`save-state ${saveState}`}>
          <span className="save-dot" />
          {saveLabel[saveState]}
        </span>
        {!themed && <span className="spacer" />}
        <AppearanceSettings />
        {saveFile && <button className="agent-open" title="保存当前文件 (Ctrl+S)" disabled={busy || activeReadonly || !editorPath} onClick={saveFile}>保存</button>}
        {agentScope && <button className="agent-open" aria-expanded={agentOpen} onClick={() => {
          setAgentOpened(true)
          setAgentVisible(!agentOpen)
        }}>Agent</button>}
        <button className="submit" aria-label={submitLabel} title={submitLabel} disabled={busy || remaining<=0} onClick={() => {
          if (directSubmit) { void onSubmit(); return }
          setSubmitError(null); setConfirmSubmit(true)
        }}>
          {themed && continuesToQuestions ? '结束编程' : submitLabel}
        </button>
      </div>

      <div ref={main} className={`ide-main ${agentOpen ? 'has-ai' : 'has-no-ai'}`}
        style={resizable ? { '--file-width': `${fileWidth}%`, '--agent-width': `${agentWidth}%` } as CSSProperties : undefined}>
        <aside className="sidebar">
          <div className="tabs">
            <button aria-pressed={leftTab === 'files'} className={`tab ${leftTab === 'files' ? 'active' : ''}`} onClick={() => setLeftTab('files')}>
              {themed && <WorkspaceIcon name="files" />}文件
            </button>
            <button aria-pressed={leftTab === 'problem'} className={`tab ${leftTab === 'problem' ? 'active' : ''}`} onClick={() => setLeftTab('problem')}>
              {themed && <WorkspaceIcon name="book" />}题目
            </button>
            {verification && <button aria-pressed={leftTab === 'verification'} className={`tab ${leftTab === 'verification' ? 'active' : ''}`} onClick={() => setLeftTab('verification')}>
              {themed && <WorkspaceIcon name="test" />}验证
            </button>}
          </div>
          {verification && (themed || leftTab === 'verification') && verification(() => {
            setTermOpen(true)
            requestAnimationFrame(() => main.current?.querySelector<HTMLTextAreaElement>('.xterm-helper-textarea')?.focus())
          }, selectFile, themed ? { open: leftTab === 'verification', show: () => setLeftTab('verification') } : undefined)}
          <div hidden={leftTab !== 'files'} className="tree-container">{filesPane?.(selectFile, editorPath) ?? <FileTree files={files} activePath={editorPath} onSelect={selectFile} />}</div>
          {leftTab === 'problem' && (
            problem?.(selectFile) ?? <div className="problem-pane">
              <span className="pane-caption">任务说明</span>
              <h2>会议室预约缺陷修复</h2>
              <p>修复会议室预约的首尾相接冲突、取消后仍占用时段两个缺陷，并补充回归测试。</p>
              <button onClick={() => selectFile('README.md')}>打开完整题目说明</button>
              <h3>公开测试</h3><code className="test-command">python -m unittest discover -s tests -p 'test_*.py' -v</code>
              <p className="muted">原始题目有两项预期失败；实际结果以终端为准。</p>
            </div>
          )}
        </aside>

        {resizable && divider('files')}
        <div className="ide-center">
          <CodeEditor
            tabs={tabs}
            selectDisabled={tabSelectionDisabled}
            closeDisabled={busy}
            loadRevision={editorRevision}
            path={editorPath}
            value={content}
            readOnly={activeReadonly}
            wrapLines={resizable}
            onSelect={selectFile}
            onClose={closeFile}
            onChange={onEdit}
          />
          {showTerminal && <>
            {resizable && termOpen && <PaneResize horizontal label="调整终端高度" min={14} max={65} value={terminalHeight} change={setTerminalHeight} />}
            <div className="ide-terminal" hidden={!termOpen} style={resizable ? { height: terminalHeight + '%' } : undefined}>
              <TerminalPane sessionId={sessionId} inputEnabled={!busy} apiPrefix={terminalPrefix}
                visible={termOpen} onCollapse={() => setTermOpen(false)} />
            </div>
            {!termOpen && (
              <button className="terminal-toggle" onClick={() => setTermOpen(true)}>
                展开终端
              </button>
            )}
          </>}
        </div>
        {resizable && agentOpen && divider('agent')}
        {agentScope && agentOpened && <AgentPanel key={agentScope} sessionId={agentScope}
          prefix={agentPrefix} modelReady={agentModelReady} canExecute={agentCanExecute} hidden={!agentOpen}
          beforeRun={beforeAgentRun} onBusy={onAgentBusy}
          onClose={() => setAgentVisible(false)} onWorkspaceChange={onAgentWorkspaceChange} />}
      </div>

      <footer className="ide-statusbar">
        <span className="editor-file-status" role="status" title={loadingPath ?? editorPath ?? undefined}>
          {loadingPath ? `正在读取 ${loadingPath}…` : editorPath ? editorPath + (activeReadonly ? ' · 只读' : ' · 可编辑') : '请选择文件'}
        </span>
        {actions}<span>{saveHint}</span>
      </footer>
      {confirmSubmit && (
        <div className="modal-backdrop">
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="submit-title" onKeyDown={event => {
            if (event.key === 'Escape' && !submitting) setConfirmSubmit(false);
            if (event.key === 'Tab') {
              const controls = event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), textarea:not(:disabled)');
              const first = controls[0], last = controls[controls.length - 1];
              if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
              if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
            }
          }}>
            <span className="section-icon" aria-hidden="true">↑</span>
            <h2 id="submit-title">{continuesToQuestions ? '确认结束编程？' : '确认提交答卷？'}</h2>
            <p className="muted">提交前请核对以下信息</p>
            <dl className="submission-summary"><div><dt>作答时间</dt><dd className="mono">{submitting ? '正在固定答卷…' : practice ? '本阶段不计时' : formatRemaining(remaining)}</dd></div><div><dt>文件状态</dt><dd>{saveLabel[saveState]}</dd></div></dl>
            <p>{continuesToQuestions ? '将停止 Agent 和终端、保存并固定代码，此后不能修改或返回编程。代码固定后开始 10 分钟独立问答。' : submissionNotice}</p>
            {submissionFields}
            {submitError && <div className="banner banner-error">{submitError}</div>}
            <div className="modal-actions">
              <button className="btn" autoFocus disabled={submitting} onClick={() => setConfirmSubmit(false)}>返回工作台</button>
              <button className="btn primary" disabled={submitting || remaining<=0} onClick={() => void submit()}>
                {continuesToQuestions ? (submitting ? '正在固定代码…' : '确认结束编程') : (submitting ? '提交中…' : '确认提交')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
