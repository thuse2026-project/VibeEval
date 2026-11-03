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
  terminalCommand: { id: number; data: string } | null
  onSelectFile: (path: string) => void
  onEdit: (content: string) => void
  onRunTests: () => void
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
  terminalCommand,
  onSelectFile,
  onEdit,
  onRunTests,
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
        <span className="logo">VibeEval</span>
        <span className="task">{title}</span>
        <span className="timer mono">{formatRemaining(remaining)}</span>
        <span className={`save-state ${saveState}`}>
          <span className="save-dot" />
          {saveLabel[saveState]}
        </span>
        <span className="spacer" />
        <button onClick={onRunTests}>运行测试</button>
        <button className="submit" onClick={() => setConfirmSubmit(true)}>
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
              <p>修复 src/main.py 中 fix_me 函数的缺陷，使其在 value 为真时返回固定结果。</p>
              <p className="muted">完成后运行公开测试验证，再提交答卷。</p>
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

      {termOpen ? (
        <TerminalPane sessionId={sessionId} command={terminalCommand} />
      ) : (
        <button className="terminal-toggle" onClick={() => setTermOpen(true)}>
          展开终端
        </button>
      )}
      {termOpen && (
        <button className="terminal-close" onClick={() => setTermOpen(false)} title="折叠终端">
          折叠
        </button>
      )}
      {confirmSubmit && (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => !submitting && setConfirmSubmit(false)}>
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="submit-title" onMouseDown={(event) => event.stopPropagation()}>
            <h2 id="submit-title">确认提交答卷？</h2>
            <p>提交前会保存全部修改并冻结快照；提交后不能继续编辑。</p>
            {submitError && <div className="banner banner-error">{submitError}</div>}
            <div className="modal-actions">
              <button className="btn" disabled={submitting} onClick={() => setConfirmSubmit(false)}>继续作答</button>
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
