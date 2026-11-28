import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import * as monaco from 'monaco-editor/editor/editor.api.js'
import 'monaco-editor/languages/definitions/python/register.js'
import 'monaco-editor/languages/definitions/markdown/register.js'
import EditorWorker from 'monaco-editor/editor/editor.worker.js?worker'
import { palettes, useCandidateTheme } from '../candidateTheme'
import { MarkdownViewSwitch } from './MarkdownViewSwitch'
import { WorkspaceIcon } from './WorkspaceIcon'
import { renderMarkdown } from '../markdown'
import { languageFromPath } from '../format'

self.MonacoEnvironment = { getWorker: () => new EditorWorker() }

type Props = {
  tabs: {path:string;readonly:boolean;dirty:boolean}[]
  onSelect: (path:string)=>void
  onClose: (path: string) => Promise<boolean>
  selectDisabled: boolean
  closeDisabled: boolean
  loadRevision: number
  path: string | null
  value: string
  readOnly: boolean
  wrapLines?: boolean
  onChange: (path: string, value: string) => void
}

export function CodeEditor(props: Props) {
  const appearance = useCandidateTheme()
  const theme = appearance?.theme
  const [markdownModes, setMarkdownModes] = useState<Record<string, 'source' | 'preview'>>({})
  const path = props.path
  const markdown = Boolean(theme && path && /\.(md|markdown)$/i.test(path))
  const mode = (path && markdownModes[path]) || 'source'
  const preview = markdown && mode === 'preview'
  const rendered = useMemo(() => preview ? renderMarkdown(props.value, true) : null, [preview, props.value])
  const latest = useRef(props)
  latest.current = props
  const host = useRef<HTMLDivElement>(null)
  const editor = useRef<monaco.editor.IStandaloneCodeEditor | null>(null)
  const models = useRef(new Map<string, {
    model: monaco.editor.ITextModel
    view: monaco.editor.ICodeEditorViewState | null
  }>())
  const activePath = useRef<string | null>(null)
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
      if (name && models.current.get(name)?.model === instance.getModel()) {
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
      activePath.current = null
    }
  }, [])

  useLayoutEffect(() => {
    const instance = editor.current!
    switching.current = true
    try {
      const previous = activePath.current ? models.current.get(activePath.current) : undefined
      const changedPath = activePath.current !== props.path
      if (changedPath && previous) previous.view = instance.saveViewState()
      activePath.current = props.path
      if (!props.path) {
        if (changedPath) instance.setModel(null)
        return
      }
      let next = models.current.get(props.path)
      if (!next) {
        next = { model: monaco.editor.createModel(latest.current.value, languageFromPath(props.path)), view: null }
        models.current.set(props.path, next)
      } else if (next.model.getValue() !== latest.current.value) {
        next.model.setValue(latest.current.value)
      }
      if (changedPath) {
        instance.setModel(next.model)
        if (next.view) instance.restoreViewState(next.view)
      }
    } finally {
      switching.current = false
    }
  }, [props.path, props.loadRevision])

  useLayoutEffect(() => {
    editor.current?.updateOptions({ readOnly: props.readOnly, wordWrap: props.wrapLines ? 'on' : 'off' })
  }, [props.readOnly, props.wrapLines])

  useLayoutEffect(() => {
    if (!theme) return
    const palette = palettes[theme]
    const name = 'candidate-' + theme
    editor.current?.updateOptions({ fontSize: 14, lineHeight: 22, padding: { top: 9, bottom: 10 },
      fontFamily: '"Cascadia Code", Consolas, "Noto Sans SC", monospace' })
    monaco.editor.defineTheme(name, {
      base: theme === 'graphite' ? 'vs-dark' : 'vs', inherit: true,
      rules: [
        { token: 'keyword', foreground: palette.accent.slice(1) },
        { token: 'comment', foreground: palette.muted.slice(1) },
        { token: 'string', foreground: theme === 'graphite' ? 'c7d4a3' : '47683c' },
      ],
      colors: { 'editor.background': palette.surface, 'editor.foreground': palette.ink,
        'editorLineNumber.foreground': palette.muted, 'editor.selectionBackground': palette.selection,
        'editor.lineHighlightBorder': '#00000000' },
    })
    monaco.editor.setTheme(name)
  }, [theme])

  async function closeTab(tabPath: string) {
    if (await props.onClose(tabPath)) {
      setMarkdownModes(previous => {
        if (!Object.hasOwn(previous, tabPath)) return previous
        const next = { ...previous }
        delete next[tabPath]
        return next
      })
    }
  }

  return <div className="editor-area">
    <nav className="editor-tabs" aria-label="已打开文件">
      {props.tabs.map(tab => <div key={tab.path} className={`editor-tab${tab.path === props.path ? ' active' : ''}`}>
        <button type="button" className="etab" aria-pressed={tab.path === props.path} title={tab.path}
          disabled={props.selectDisabled} onClick={() => props.onSelect(tab.path)}>
          <span className="editor-tab-name">{theme ? tab.path.split('/').pop() : tab.path}</span>
          {tab.dirty && <span aria-label="未保存修改">●</span>}
          {tab.readonly && <span className="tag gray" title="只读" aria-label="只读">{theme ? <WorkspaceIcon name="lock" /> : '只读'}</span>}
        </button>
        <button type="button" className="editor-tab-close" aria-label={`关闭 ${tab.path}`} title={`关闭 ${tab.path}`}
          disabled={props.closeDisabled} onClick={() => void closeTab(tab.path)}>×</button>
      </div>)}
    </nav>
    {theme && path && <div className="editor-path">
      <span title={path}>{path}</span>
      {markdown && <MarkdownViewSwitch preview={preview} change={show =>
        setMarkdownModes(previous => ({ ...previous, [path]: show ? 'preview' : 'source' }))} />}
    </div>}
    <div className="editor-host" ref={host} hidden={!props.path || preview} />
    {rendered && <div key={path} className="markdown-reader" role="region" aria-label={path + ' 预览'} tabIndex={0}>
      <article dangerouslySetInnerHTML={rendered} />
    </div>}
    {!props.path && <div className="editor-empty">从文件列表打开文件</div>}
  </div>
}
