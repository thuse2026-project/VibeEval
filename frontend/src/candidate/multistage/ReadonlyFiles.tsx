import { useMemo } from 'react'
import { renderMarkdown } from '../../shared/markdown'
import { useEffect, useState } from 'react'
import type { FileContentResponse, FileEntry } from '../../shared/types'
import { examRequest, v2 } from './api'

export function ReadonlyFiles({ phase, sha, notes }: { phase: string; sha?: string; notes?: string | null }) {
  const [files, setFiles] = useState<FileEntry[]>([])
  const [selected, setSelected] = useState('')
  const [content, setContent] = useState('')
  const [error, setError] = useState('')
  const isNote = notes != null && selected === 'note.md'

  useEffect(() => {
    const abort = new AbortController()
    examRequest<{files: FileEntry[]}>(v2+'/phases/'+phase+'/files', 'GET', undefined, abort.signal).then(tree => {
      if (abort.signal.aborted) return
      const paths = tree.files.filter(f => !f.is_dir)
      setFiles(paths)
      setSelected(paths.find(f => /^(repository\/)?readme\.md$/i.test(f.path))?.path ?? paths[0]?.path ?? '')
    }).catch(e => { if (!abort.signal.aborted) setError(String(e)) })
    return () => abort.abort()
  }, [phase])
  useEffect(() => {
    const abort = new AbortController()
    setContent('')
    if (selected && !isNote) examRequest<FileContentResponse>(v2+'/phases/'+phase+'/file?path='+encodeURIComponent(selected), 'GET', undefined, abort.signal)
      .then(f => { if (!abort.signal.aborted) setContent(f.content) }).catch(e => { if (!abort.signal.aborted) setError(String(e)) })
    return () => abort.abort()
  }, [phase, selected, isNote])
  const markdown = useMemo(() => /\.md$/i.test(selected) ? renderMarkdown(isNote ? notes || '笔记为空' : content, true) : null, [selected, isNote, notes, content])
  return <section className="r2-frozen-files" aria-label={sha ? '冻结代码' : '只读源码'}>
    <div className="r2-section-heading"><h2>{sha ? '冻结代码' : '只读源码'}</h2>{sha && <code title={sha}>{sha.slice(0, 12)}</code>}</div>
    {error && <p role="alert">{error}</p>}
    <label className="reference-file">文件<select value={selected} onChange={e => setSelected(e.target.value)}>
      {notes != null && <option>note.md</option>}
      {files.map(f => <option key={f.path}>{f.path}</option>)}</select></label>
    {markdown ? <article className="reference-markdown" tabIndex={0} dangerouslySetInnerHTML={markdown} /> : <pre className="source-preview" tabIndex={0}>{content}</pre>}
  </section>
}
