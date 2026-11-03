import Editor, { loader } from '@monaco-editor/react'
import * as monaco from 'monaco-editor'
import EditorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import { languageFromPath } from '../format'

self.MonacoEnvironment = { getWorker: () => new EditorWorker() }
loader.config({ monaco })

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
        key={path}
        path={path}
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
