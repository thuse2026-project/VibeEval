import { useEffect, useState, type ReactNode } from 'react'
import { FileTree } from '../../shared/components/FileTree'
import type { FileEntry, SaveState } from '../../shared/types'
import { WorkspaceScreen } from '../workspace/WorkspaceScreen'
import { examRequest, v2, type Attempt } from './api'

type Props = {
  state: Attempt; remaining: number; busy: boolean; modelReady?: boolean
  notes?: string; editNotes?: (value: string) => void; noteRevision?: number
  saveState: SaveState; alerts?: ReactNode
  submitLabel: string; submit: () => Promise<void>
}

export function ReadingWorkspace({ state, remaining, busy, modelReady, notes, editNotes,
  noteRevision = 0, saveState, alerts, submitLabel, submit }: Props) {
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
      remaining={remaining} resizable files={tree} activePath={path}
      filesPane={(select, selected) => <FileTree files={tree} directories activePath={selected} onSelect={select} />}
      problem={() => <div className="problem-pane"><h2>{state.current_phase.title}</h2>
        {editNotes ? '阅读仓库源码，可在 note.md 记录探索笔记。结束探索后独立回答五问。'
          : '审阅 repository/tinydb/table.py 的表操作，并查阅相关依赖。结束阅读后，在缺陷报告中说明判断依据。'}</div>}
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
      busy={busy} submitLabel={submitLabel} onSubmit={submit} directSubmit />
  </div>
}
