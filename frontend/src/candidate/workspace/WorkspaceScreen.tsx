import { useState } from 'react'
import { CodeEditor } from '../../shared/components/Editor'
import { FileTree } from '../../shared/components/FileTree'
import { TerminalPane } from '../../shared/components/Terminal'
import { formatRemaining } from '../../shared/format'
import type { FileEntry, SaveState } from '../../shared/types'

type Props = {
  title: string
  remaining: number
  files: FileEntry[]
  activePath: string | null
  content: string
  activeReadonly: boolean
  saveState: SaveState
  busy: boolean
  sessionId: string
  command: {id:number;text:string}|null
  terminalEnabled: boolean
  onTerminalConnection: (connected:boolean)=>void
  terminalConnected: boolean
  onSelectFile: (path: string) => void
  onEdit: (content: string) => void
  onRunTests: () => void
  onSubmit: () => void
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
  activePath,
  content,
  activeReadonly,
  saveState,
  busy,
  sessionId, command, terminalEnabled, onTerminalConnection,
  terminalConnected,
  onSelectFile,
  onEdit,
  onRunTests,
  onSubmit,
}: Props) {
  const [leftTab, setLeftTab] = useState<'files' | 'problem'>('files')
  const [aiOpen, setAiOpen] = useState(false)
  const [termOpen, setTermOpen] = useState(true)

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
        <button disabled={busy || !terminalConnected || !terminalEnabled} onClick={onRunTests}>运行测试</button>
        <button className="submit" disabled={busy || !terminalEnabled} onClick={onSubmit}>
          {busy ? '正在处理…' : '提交答卷'}
        </button>
      </div>

      <div className={`ide-main ${aiOpen ? '' : 'without-ai'}`}>
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
              <p>修复 src/booking.py 的会议室预约缺陷：允许首尾相接预约，取消后释放时段。完整题面见 README.md。</p>
              <p className="muted">完成后运行公开测试验证，再提交答卷。</p>
            </div>
          )}
        </aside>

        <CodeEditor
          path={activePath ?? '选择文件'}
          value={content}
          readOnly={activeReadonly}
          onChange={onEdit}
        />

        {aiOpen && (
          <aside className="ai-panel">
            <div className="ai-head">
              <span>AI 结对助手</span>
              <div className="ai-actions">
                <span className="tag gray">Release 2</span>
                <button className="icon-btn" onClick={() => setAiOpen(false)} title="折叠">
                  ×
                </button>
              </div>
            </div>
            <div className="ai-msg">
              选择文件或代码片段后，输入问题开始与 AI 协作。本期为无 AI MVP，此处为后续版本预留。
            </div>
            <div className="ai-input">
              <textarea placeholder="描述你要完成的任务…" disabled />
              <button className="btn primary" disabled>
                发送
              </button>
            </div>
          </aside>
        )}
      </div>

      {termOpen ? (
        <TerminalPane sessionId={sessionId} enabled={terminalEnabled} command={command} onConnection={onTerminalConnection}/>
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
    </div>
  )
}
