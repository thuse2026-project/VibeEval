import Editor from '@monaco-editor/react'
import { languageFromPath } from '../format'

type Props = {
  path: string
  value: string
  readOnly: boolean
  onChange: (value: string) => void
}

export function CodeEditor({ path, value, readOnly, onChange }: Props) {
  return (
    <div className="editor-area">
      <div className="editor-tabs">
        <span className="etab active">
          {path}
          {readOnly && <span className="tag gray">只读</span>}
        </span>
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
