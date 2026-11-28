import { WorkspaceIcon } from './WorkspaceIcon'

export function MarkdownViewSwitch({ preview, change, readOnly = false }: {
  preview: boolean
  change: (preview: boolean) => void
  readOnly?: boolean
}) {
  return <label className="markdown-view-switch">
    <WorkspaceIcon name={preview ? 'book' : 'code'} />
    <select aria-label="Markdown 视图" value={preview ? 'preview' : 'source'} onChange={event => change(event.target.value === 'preview')}>
      <option value="source">{readOnly ? '文本源码' : '文本编辑器'}</option>
      <option value="preview">Markdown 预览</option>
    </select>
  </label>
}
