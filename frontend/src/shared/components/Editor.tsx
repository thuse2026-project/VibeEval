import Editor from '@monaco-editor/react'
import { languageFromPath } from '../format'

type Props = {
  tabs: Array<{ path: string; readonly: boolean; dirty: boolean }>
  path: string
  value: string
  readOnly: boolean
  onSelect: (path: string) => void
  onChange: (value: string) => void
}

export function CodeEditor({ tabs, path, value, readOnly, onSelect, onChange }: Props) {
  return (
    <div className="editor-area">
      <div className="editor-tabs">
        {tabs.map((tab) => (
          <button
            key={tab.path}
            className={`etab ${tab.path === path ? 'active' : ''}`}
            onClick={() => onSelect(tab.path)}
          >
            {tab.path}
            {tab.dirty && <span aria-label="未保存">●</span>}
            {tab.readonly && <span className="tag gray">只读</span>}
          </button>
        ))}
      </div>
      <Editor
        height="100%"
        language={languageFromPath(path)}
        value={value}
        onChange={(v) => onChange(v ?? '')}
        theme="vs-dark"
        options={{
          readOnly,
          minimap: { enabled: false },
          fontSize: 13,
          lineNumbers: 'on',
          scrollBeyondLastLine: false,
          automaticLayout: true,
        }}
      />
    </div>
  )
}
