import { useState } from 'react'
import { CodeEditor } from '../../shared/components/Editor'
import { FileTree } from '../../shared/components/FileTree'
import { TerminalPane } from '../../shared/components/TerminalGateway'
import { formatRemaining } from '../../shared/format'
import type { FileEntry, SaveState } from '../../shared/types'

type Props = {
  title: string
  remaining: number
  files: FileEntry[]
  openFiles: Array<{ path: string; readonly: boolean; dirty: boolean }>
  activePath: string | null
  content: string
  activeReadonly: boolean
  saveState: SaveState
  sessionId: string | null
  busy: boolean
  onSelectFile: (path: string) => void
  onEdit: (content: string) => void
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
  activeReadonly,
  saveState,
  sessionId,
  busy,
  onSelectFile,
  onEdit,
  onSubmit,
}: Props) {
  const [leftTab, setLeftTab] = useState<'files' | 'problem'>('files')
  const [termOpen, setTermOpen] = useState(true)
  const [confirmSubmit, setConfirmSubmit] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)

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
        <button className="submit" disabled={busy} onClick={() => { setSubmitError(null); setConfirmSubmit(true) }}>
          提交答卷
        </button>
      </div>

      <div className="ide-main has-no-ai">
        <aside className="sidebar">
          <div className="tabs">
            <button className={`tab ${leftTab === 'files' ? 'active' : ''}`} onClick={() => setLeftTab('files')}>
              文件
            </button>
            <button className={`tab ${leftTab === 'problem' ? 'active' : ''}`} onClick={() => setLeftTab('problem')}>
              题目
            </button>
          </div>
          {leftTab === 'files' ? (
            <FileTree files={files} activePath={activePath} onSelect={onSelectFile} />
          ) : (
            <div className="problem-pane">
              <span className="pane-caption">任务说明</span>
              <h2>会议室预约缺陷修复</h2>
              <p>修复会议室预约的首尾相接冲突、取消后仍占用时段两个缺陷，并补充回归测试。</p>
              <button onClick={() => onSelectFile('README.md')}>打开完整题目说明</button>
              <h3>公开测试</h3><code className="test-command">python -m unittest discover -s tests -p 'test_*.py' -v</code>
              <p className="muted">原始题目有两项预期失败；实际结果以终端为准。</p>
            </div>
          )}
        </aside>

        <CodeEditor
          tabs={openFiles}
          path={activePath ?? '选择文件'}
          value={content}
          readOnly={activeReadonly}
          onSelect={onSelectFile}
          onChange={onEdit}
        />
      </div>

      <div hidden={!termOpen}>
        <TerminalPane sessionId={sessionId} />
      </div>
      {!termOpen && (
        <button className="terminal-toggle" onClick={() => setTermOpen(true)}>
          展开终端
        </button>
      )}
      {termOpen && (
        <button className="terminal-close" onClick={() => setTermOpen(false)} title="折叠终端">
          折叠
        </button>
      )}
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
              <button className="btn primary" disabled={submitting} onClick={() => void submit()}>
                {submitting ? '提交中…' : '确认提交'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
