import { useEffect, useState, type ReactNode } from 'react'
import { FileTree } from '../../shared/components/FileTree'
import type { FileEntry, SaveState } from '../../shared/types'
import { WorkspaceScreen } from '../workspace/WorkspaceScreen'
import { examRequest, v2, type Attempt } from './api'

type Props = {
  state: Attempt; remaining: number; busy: boolean; modelReady?: boolean
  saveNote?: () => void; discardNote?: () => Promise<boolean>; processing?: boolean
  notes?: string; editNotes?: (value: string) => void; noteRevision?: number
  saveState: SaveState; alerts?: ReactNode
  submitLabel: string; submit: () => Promise<void>
}

export function ReadingWorkspace({ state, remaining, busy, modelReady, notes, editNotes,
  saveNote, discardNote, processing, noteRevision = 0, saveState, alerts, submitLabel, submit }: Props) {
  const phase = state.current_phase.id
  const prefix = v2 + '/phases/' + phase
  const [files, setFiles] = useState<FileEntry[]>([])
  const [path, setPath] = useState<string | null>(null)
  const [documents, setDocuments] = useState<Record<string, string>>({})
  const [opened, setOpened] = useState<string[]>([])
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const isNote = path === 'note.md'
  const hasContent = path !== null && Object.hasOwn(documents, path)
  useEffect(() => {
    const abort = new AbortController()
    examRequest<{ files: FileEntry[] }>(prefix + '/files', 'GET', undefined, abort.signal)
      .then(({ files }) => {
        if (abort.signal.aborted) return
        setFiles(files.map(file => ({ ...file, readonly: true })))
        const first = files.find(f => /^(repository\/)?readme\.md$/i.test(f.path)) ?? files.find(f => !f.is_dir)
        if (first) setPath(previous => previous ?? first.path)
      }).catch(e => { if (!abort.signal.aborted) setError(String(e)) })
    return () => abort.abort()
  }, [prefix, retry])
  useEffect(() => {
    if (!path) return
    setOpened(previous => previous.includes(path) ? previous : [...previous, path])
    if (isNote || hasContent) return
    const abort = new AbortController()
    examRequest<{ content: string }>(prefix + '/file?path=' + encodeURIComponent(path), 'GET', undefined, abort.signal)
      .then(file => { if (!abort.signal.aborted) setDocuments(previous => ({ ...previous, [path]: file.content })) })
      .catch(e => { if (!abort.signal.aborted) setError(String(e)) })
    return () => abort.abort()
  }, [prefix, path, isNote, hasContent, retry])
  const tree = notes === undefined ? files : [
    { path: 'note.md', readonly: !editNotes, is_dir: false, version: noteRevision }, ...files,
  ]
  const agentAllowed = state.capabilities.includes('use_agent') && remaining > 0
  return <div className="candidate-workspace workspace-active r2-workspace">
    {alerts}
    {error && <div role="alert" className="workspace-alert">{error}
      <button onClick={() => { setError(''); setRetry(i => i + 1) }}>重新读取</button></div>}
    <WorkspaceScreen title={state.current_stage.title + ' · ' + state.current_phase.title}
      initialProblem={state.current_phase.key === 'inspect'} remaining={remaining} resizable files={tree} activePath={path}
      filesPane={(select, selected) => <FileTree files={tree} directories activePath={selected} onSelect={select} />}
      problem={selectFile => <div className="problem-pane"><h2>{state.current_phase.title}</h2>
        {editNotes ? '阅读仓库源码，可在 note.md 记录探索笔记。结束探索后独立回答五问。'
          : <><p>同事修改了 TinyDB 的表操作。请按以下公开规则审阅实现，并记录你能用代码解释的行为差异。</p>
            <button onClick={() => selectFile('repository/tinydb/table.py')}>打开 table.py</button>
            <h3>预期行为</h3><ul>
              <li>各命名表相互独立。插入的文档可读，自动编号与当前表已有编号不冲突，重新打开数据库后仍可插入。</li>
              <li>查询返回当前匹配的文档；更新或删除后再次查询须反映新状态。字段缺失不应中断普通查询。</li>
              <li>更新只修改选定记录的指定字段，删除只移除选定记录。</li>
              <li>JSONStorage 重新打开后保留已写入的数据；MemoryStorage 只在同一实例内保留。</li>
            </ul><h3>阅读范围</h3><p>使用内置 Document、整数编号和可 JSON 序列化的字典。可沿 insert、search、update 或 remove 追踪输入和结果，按需查看 database.py、queries.py 和 storages.py。不要求处理事务、并发写入或外部扩展。</p>
            <p>阅读 20 分钟后，另有 10 分钟填写位置、触发条件、实际行为、期望行为和根因；可报告未发现问题。</p></>}</div>}
      openFiles={opened.map(name => ({ path: name, readonly: name !== 'note.md' || !editNotes,
        dirty: name === 'note.md' && !!editNotes && saveState !== 'saved' }))}
      content={isNote ? notes ?? '' : documents[path ?? ''] ?? ''}
      editorRevision={isNote ? noteRevision : Number(hasContent)}
      loadingPath={!isNote && !hasContent ? path : null}
      activeReadonly={!isNote || !editNotes || busy || remaining <= 0}
      onSelectFile={setPath} onEdit={(name, value) => { if (name === 'note.md') editNotes?.(value) }}
      sessionId={null} agentScope={agentAllowed ? phase : null} agentPrefix="/api/v2/phases/"
      agentCanExecute={false} agentModelReady={modelReady} initialAgentOpen={agentAllowed}
      saveState={saveState} saveHint="自动保存 · UTF-8"
      saveFile={editNotes ? saveNote : undefined} discardFile={discardNote} processing={processing}
      busy={busy} submitLabel={submitLabel} onSubmit={submit} directSubmit />
  </div>
}
