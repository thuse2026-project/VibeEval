import { useState } from 'react'
import { CodeEditor } from '../../shared/components/Editor'
import { FileTree } from '../../shared/components/FileTree'
import { TerminalPane } from '../../shared/components/TerminalGateway'
import { formatRemaining } from '../../shared/format'
import type { FileEntry, SaveState } from '../../shared/types'
import { AgentPanel, type AgentFileChange } from './AgentPanelV2'

type Props = {
  title: string
  remaining: number
  files: FileEntry[]
  openFiles: Array<{ path: string; readonly: boolean; dirty: boolean }>
  activePath: string | null
  content: string
  editorRevision: number
  activeReadonly: boolean
  saveState: SaveState
  sessionId: string | null
  busy: boolean
  onSelectFile: (path: string) => void
  onEdit: (path: string, content: string) => void
  onAgentWorkspaceChange: (change: AgentFileChange) => void
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
  files,
  openFiles,
  activePath,
  content,
  editorRevision,
  activeReadonly,
  saveState,
  sessionId,
  busy,
  onSelectFile,
  onEdit,
  onAgentWorkspaceChange,
  onSubmit,
}: Props) {
  const [leftTab, setLeftTab] = useState<'files' | 'problem'>('files')
  const [termOpen, setTermOpen] = useState(true)
  const [confirmSubmit, setConfirmSubmit] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [agentOpen, setAgentOpen] = useState(false)

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
        <span className="timer mono"><small>剩余 </small>{formatRemaining(remaining)}</span>
        <span className={`save-state ${saveState}`}>
          <span className="save-dot" />
          {saveLabel[saveState]}
        </span>
        <span className="spacer" />
        <button className="agent-open" disabled={!sessionId} onClick={() => setAgentOpen(true)}>Agent</button>
        <button className="submit" disabled={busy || remaining<=0} onClick={() => { setSubmitError(null); setConfirmSubmit(true) }}>
          提交答卷
        </button>
      </div>

      <div className={`ide-main ${agentOpen ? 'with-agent' : ''}`}>
        <aside className="sidebar">
          <div className="tabs">
            <button className={`tab ${leftTab === 'files' ? 'active' : ''}`} onClick={() => setLeftTab('files')}>
              文件
            </button>
            <button className={`tab ${leftTab === 'problem' ? 'active' : ''}`} onClick={() => setLeftTab('problem')}>
              题目
            </button>
          </div>
          <div className="sidebar-scroll">
            {leftTab === 'files' ? (
              <FileTree files={files} activePath={activePath} onSelect={onSelectFile} />
            ) : (
              <div className="problem-pane">
                <span className="pane-caption">任务说明</span>
                <h2>当前编程任务</h2>
                <p>完整题面、运行命令和验收要求以当前题目的 README.md 为准。</p>
                <button onClick={() => onSelectFile('README.md')}>打开完整题目说明</button>
                <p className="muted">运行测试和调试请使用下方真实终端。</p>
              </div>
            )}
          </div>
        </aside>

        <section className="workspace-center">
          <CodeEditor
            tabs={openFiles}
            busy={busy}
            loadRevision={editorRevision}
            path={activePath ?? '选择文件'}
            value={content}
            readOnly={activeReadonly}
            onSelect={onSelectFile}
            onChange={onEdit}
          />
          <div className="terminal-slot" hidden={!termOpen}>
            <TerminalPane sessionId={sessionId} inputEnabled={!busy} onCollapse={() => setTermOpen(false)} />
          </div>
          {!termOpen && (
            <button className="terminal-toggle" onClick={() => setTermOpen(true)}>
              展开终端
            </button>
          )}
        </section>
        {agentOpen && <AgentPanel sessionId={sessionId} onClose={() => setAgentOpen(false)} onWorkspaceChange={onAgentWorkspaceChange} />}
      </div>
      <footer className="ide-statusbar"><span>{activePath ?? '请选择文件'}{activeReadonly ? ' · 只读' : ' · 可编辑'}</span><span>5 秒自动保存 · Python 3.12 · UTF-8</span></footer>
      {confirmSubmit && (
        <div className="modal-backdrop">
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="submit-title" onKeyDown={event => {
            if (event.key === 'Escape' && !submitting) setConfirmSubmit(false);
            if (event.key === 'Tab') {
              const buttons = event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)');
              const first = buttons[0], last = buttons[buttons.length - 1];
              if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
              if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
            }
          }}>
            <span className="section-icon" aria-hidden="true">↑</span>
            <h2 id="submit-title">确认提交答卷？</h2>
            <p className="muted">提交前请核对以下信息</p>
            <dl className="submission-summary"><div><dt>剩余时间</dt><dd className="mono">{formatRemaining(remaining)}</dd></div><div><dt>文件状态</dt><dd>{saveLabel[saveState]}</dd></div></dl>
            <p>提交前会保存全部修改并冻结快照；提交后不能继续编辑。</p>
            {submitError && <div className="banner banner-error">{submitError}</div>}
            <div className="modal-actions">
              <button className="btn" autoFocus disabled={submitting} onClick={() => setConfirmSubmit(false)}>返回工作台</button>
              <button className="btn primary" disabled={submitting || remaining<=0} onClick={() => void submit()}>
                {submitting ? '提交中…' : '确认提交'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
