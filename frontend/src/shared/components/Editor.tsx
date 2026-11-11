import { useLayoutEffect, useRef } from 'react'
import * as monaco from 'monaco-editor/editor/editor.api.js'
import 'monaco-editor/languages/definitions/python/register.js'
import 'monaco-editor/languages/definitions/markdown/register.js'
import EditorWorker from 'monaco-editor/editor/editor.worker.js?worker'
import { languageFromPath } from '../format'

self.MonacoEnvironment = { getWorker: () => new EditorWorker() }

type Props = {
  tabs: {path:string;readonly:boolean;dirty:boolean}[]
  onSelect: (path:string)=>void
  busy: boolean
  loadRevision: number
  path: string
  value: string
  readOnly: boolean
  onChange: (path: string, value: string) => void
}

export function CodeEditor(props: Props) {
  const latest = useRef(props)
  latest.current = props
  const host = useRef<HTMLDivElement>(null)
  const editor = useRef<monaco.editor.IStandaloneCodeEditor | null>(null)
  const models = useRef(new Map<string, {
    model: monaco.editor.ITextModel
    view: monaco.editor.ICodeEditorViewState | null
  }>())
  const activePath = useRef('')
  const switching = useRef(false)

  useLayoutEffect(() => {
    const instance = monaco.editor.create(host.current!, {
      model: null, theme: 'vs-dark', automaticLayout: true,
      minimap: { enabled: false }, fontSize: 13, lineNumbers: 'on',
      scrollBeyondLastLine: false, scrollBeyondLastColumn: 3,
      scrollbar: { alwaysConsumeMouseWheel: true },
    })
    editor.current = instance
    const changes = instance.onDidChangeModelContent(() => {
      if (switching.current || instance.getOption(monaco.editor.EditorOption.readOnly)) return
      const name = activePath.current
      if (models.current.get(name)?.model === instance.getModel()) {
        latest.current.onChange(name, instance.getValue())
      }
    })
    const ownedModels = models.current
    return () => {
      changes.dispose()
      instance.dispose()
      for (const { model } of ownedModels.values()) model.dispose()
      ownedModels.clear()
      editor.current = null
      activePath.current = ''
    }
  }, [])

  useLayoutEffect(() => {
    const instance = editor.current!
    switching.current = true
    try {
      const previous = models.current.get(activePath.current)
      const changedPath = activePath.current !== props.path
      if (changedPath && previous) previous.view = instance.saveViewState()
      let next = models.current.get(props.path)
      if (!next) {
        next = { model: monaco.editor.createModel(latest.current.value, languageFromPath(props.path)), view: null }
        models.current.set(props.path, next)
      } else if (next.model.getValue() !== latest.current.value) {
        next.model.setValue(latest.current.value)
      }
      activePath.current = props.path
      if (changedPath) {
        instance.setModel(next.model)
        if (next.view) instance.restoreViewState(next.view)
      }
    } finally {
      switching.current = false
    }
  }, [props.path, props.loadRevision])

  useLayoutEffect(() => {
    editor.current?.updateOptions({ readOnly: props.readOnly })
  }, [props.readOnly])

  return <div className="editor-area">
    <nav className="editor-tabs" aria-label="已打开文件">
      {props.tabs.map(tab=><button type="button" key={tab.path} aria-pressed={tab.path===props.path}
        disabled={props.busy} className={`etab ${tab.path===props.path?'active':''}`} onClick={()=>props.onSelect(tab.path)}>
        {tab.path}{tab.dirty&&<span aria-label="未保存修改">●</span>}{tab.readonly&&<span className="tag gray">只读</span>}
      </button>)}
    </nav>
    <div className="editor-host" ref={host} />
  </div>
}
