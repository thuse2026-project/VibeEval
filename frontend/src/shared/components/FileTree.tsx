import { WorkspaceIcon } from './WorkspaceIcon'
import { useCandidateTheme } from '../candidateTheme'
import { useMemo, useState } from 'react'
import type { FileEntry } from '../types'

type Props = {
  files: FileEntry[]
  activePath: string | null
  onSelect: (path: string) => void
  directories?: boolean
}

type Node = { name: string; path: string; file?: FileEntry; children: Map<string, Node> }

function directoryTree(files: FileEntry[]) {
  const root = new Map<string, Node>()
  for (const file of files) {
    let children = root
    let path = ''
    const parts = file.path.split('/')
    for (const [index, name] of parts.entries()) {
      path = path ? path + '/' + name : name
      let node = children.get(name)
      if (!node) {
        node = { name, path, children: new Map() }
        children.set(name, node)
      }
      if (index === parts.length - 1 && !file.is_dir) node.file = file
      children = node.children
    }
  }
  return root
}

function sortFiles(files: FileEntry[]): FileEntry[] {
  return [...files].sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1
    return a.path.localeCompare(b.path)
  })
}

export function FileTree({ files, activePath, onSelect, directories = false }: Props) {
  const themed = useCandidateTheme() !== null
  const [query, setQuery] = useState('')
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const tree = useMemo(() => directories ? directoryTree(files) : null, [directories, files])

  const fileButton = (file: FileEntry, label: string) => (
    <button key={file.path} type="button"
      className={`file ${file.path === activePath ? 'active' : ''} ${file.readonly ? 'locked' : ''}`}
      onClick={() => !file.is_dir && onSelect(file.path)}
      aria-current={file.path === activePath ? 'true' : undefined}
      title={file.readonly ? `${file.path}（只读）` : file.path}>
      <span className="file-icon" aria-hidden="true">{themed ? <WorkspaceIcon name={file.is_dir ? 'files' : file.path.endsWith('.md') ? 'book' : 'code'} /> : file.is_dir ? '📁' : '📄'}</span>
      <span className="file-name">{label}</span>
      {file.readonly && <span className="lock">只读</span>}
    </button>
  )

  function branch(nodes: Map<string, Node>) {
    return [...nodes.values()].sort((a, b) => Number(!!a.file) - Number(!!b.file) || a.name.localeCompare(b.name)).map(node =>
      node.file ? fileButton(node.file, node.name) : (
        <details key={node.path} open={expanded[node.path] ?? node.path === 'repository'}>
          <summary title={node.path} onClick={event => {
            event.preventDefault()
            setExpanded(prior => ({ ...prior, [node.path]: !(prior[node.path] ?? node.path === 'repository') }))
          }}>{node.name}</summary>
          <div className="file-branch">{branch(node.children)}</div>
        </details>
      ))
  }

  if (tree) {
    const filter = query.trim().toLowerCase()
    const matching = filter ? sortFiles(files).filter(file => !file.is_dir && file.path.toLowerCase().includes(filter)) : []
    return <div className="file-tree directory-tree">
      <input type="search" aria-label="筛选文件路径" placeholder="筛选文件…" value={query} onChange={event => setQuery(event.target.value)} />
      {filter ? matching.length ? matching.map(file => fileButton(file, file.path)) : <p>没有匹配的文件。</p> : branch(tree)}
    </div>
  }

  return (
    <div className="file-tree">
      {sortFiles(files).map(file => fileButton(file, file.path))}
    </div>
  )
}
