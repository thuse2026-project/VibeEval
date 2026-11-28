import { MarkdownViewSwitch } from '../../shared/components/MarkdownViewSwitch'
import { useMemo } from 'react'
import { renderMarkdown } from '../../shared/markdown'
import { useEffect, useState } from 'react'
import type { FileContentResponse, FileEntry } from '../../shared/types'
import { examRequest, v2 } from './api'

export function ReadonlyFiles({ phase, sha, initialPath }: { phase: string; sha?: string; initialPath?: string }) {
  const [files, setFiles] = useState<FileEntry[]>([])
  const [selected, setSelected] = useState('')
  const [content, setContent] = useState('')
  const [error, setError] = useState('')
  const [preview, setPreview] = useState(false)

  useEffect(() => {
    const abort = new AbortController()
    examRequest<{files: FileEntry[]}>(v2+'/phases/'+phase+'/files', 'GET', undefined, abort.signal).then(tree => {
      if (abort.signal.aborted) return
      const paths = tree.files.filter(f => !f.is_dir)
      setFiles(paths)
      setSelected(paths.find(f => f.path === initialPath)?.path ?? paths.find(f => /^(repository\/)?readme\.md$/i.test(f.path))?.path ?? paths[0]?.path ?? '')
    }).catch(e => { if (!abort.signal.aborted) setError(String(e)) })
    return () => abort.abort()
  }, [phase, initialPath])
  useEffect(() => {
    const abort = new AbortController()
    setContent('')
    if (selected) examRequest<FileContentResponse>(v2+'/phases/'+phase+'/file?path='+encodeURIComponent(selected), 'GET', undefined, abort.signal)
      .then(f => { if (!abort.signal.aborted) setContent(f.content) }).catch(e => { if (!abort.signal.aborted) setError(String(e)) })
    return () => abort.abort()
  }, [phase, selected])
  const isMarkdown = /\.(md|markdown)$/i.test(selected)
  const markdown = useMemo(() => preview && isMarkdown ? renderMarkdown(content, true) : null, [isMarkdown, preview, content])
  return <section className="r2-frozen-files" aria-label={sha ? '冻结代码' : '只读源码'}>
    <div className="r2-section-heading"><span>{sha ? '编程结束时封存 · 只读' : '只读源码'}</span>{sha && <code title={sha}>{sha.slice(0, 12)}</code>}</div>
    {error && <p role="alert">{error}</p>}
    <label className="reference-file">文件<select value={selected} onChange={e => { setPreview(false); setSelected(e.target.value) }}>
      {files.map(f => <option key={f.path}>{f.path}</option>)}</select></label>
    {isMarkdown && <div className="reference-view"><MarkdownViewSwitch readOnly preview={preview} change={setPreview} /></div>}
    {markdown ? <article className="reference-markdown" tabIndex={0} dangerouslySetInnerHTML={markdown} /> : <pre className="source-preview" tabIndex={0}>{content}</pre>}
  </section>
}
