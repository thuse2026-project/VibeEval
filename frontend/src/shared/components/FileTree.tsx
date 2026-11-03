import type { FileEntry } from '../types'

type Props = {
  files: FileEntry[]
  activePath: string | null
  onSelect: (path: string) => void
}

function sortFiles(files: FileEntry[]): FileEntry[] {
  return [...files].sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1
    return a.path.localeCompare(b.path)
  })
}

export function FileTree({ files, activePath, onSelect }: Props) {
  return (
    <div className="file-tree">
      {sortFiles(files).map((f) => (
        <button
          key={f.path}
          className={`file ${f.path === activePath ? 'active' : ''} ${f.readonly ? 'locked' : ''}`}
          onClick={() => !f.is_dir && onSelect(f.path)}
          title={f.readonly ? `${f.path}（只读）` : f.path}
        >
          <span className="file-icon">{f.is_dir ? '📁' : '📄'}</span>
          <span className="file-name">{f.path}</span>
          {f.readonly && <span className="lock">只读</span>}
        </button>
      ))}
    </div>
  )
}
