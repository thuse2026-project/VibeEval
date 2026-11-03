import Editor, { loader } from '@monaco-editor/react'
import * as monaco from 'monaco-editor/editor/editor.api.js'
import 'monaco-editor/languages/definitions/python/register.js'
import 'monaco-editor/languages/definitions/markdown/register.js'
import EditorWorker from 'monaco-editor/editor/editor.worker.js?worker'
import { languageFromPath } from '../format'

// Load editor assets locally: sandbox deployments must not depend on a public CDN.
self.MonacoEnvironment = { getWorker: () => new EditorWorker() }
loader.config({ monaco })

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
