import { useEffect, useRef, useState } from 'react'
import { Terminal as XTerm } from '@xterm/xterm'
import { WorkspaceIcon } from './WorkspaceIcon'
import { palettes, useCandidateTheme } from '../candidateTheme'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'

export function TerminalPane({ sessionId, inputEnabled, apiPrefix = '/api/v1/sessions/', visible = true, onCollapse }: {
  sessionId: string | null; inputEnabled: boolean; apiPrefix?: string; visible?: boolean; onCollapse?: () => void
}) {
  const appearance = useCandidateTheme()
  const theme = appearance?.theme
  const container = useRef<HTMLDivElement>(null)
  const [attempt, setAttempt] = useState(0)
  const [status, setStatus] = useState('终端未连接')
  const allowInput=useRef(inputEnabled)
  allowInput.current=inputEnabled
  const terminal = useRef<XTerm | null>(null)
  const [clipboardError, setClipboardError] = useState('')
  const focusTerminal = useRef<(() => void) | null>(null)

  useEffect(() => {
    if (!container.current) return
    const term = new XTerm({
      cursorBlink: true, fontSize: 13,
      theme: { background: '#111827', foreground: '#d1d5db' },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(container.current)
    terminal.current = term
    let socket: WebSocket | null = null
    let disposed = false
    setStatus('正在连接终端…')
    term.attachCustomKeyEventHandler(event => {
      if (!(event.ctrlKey || event.metaKey)) return true
      if (event.key.toLowerCase() === 'v') {
        if (!navigator.clipboard?.readText) return false
        event.preventDefault()
        if (event.type === 'keydown' && !event.repeat && allowInput.current && socket?.readyState === WebSocket.OPEN) {
          void navigator.clipboard.readText().then(text => {
            if (!disposed && allowInput.current && socket?.readyState === WebSocket.OPEN) term.paste(text)
          }).catch(() => setClipboardError('请在终端内使用浏览器右键菜单粘贴。'))
        }
        return false
      }
      if (event.key.toLowerCase() === 'c' && term.hasSelection()) return false
      return true
    })
    const resize = () => {
      if (!container.current?.offsetHeight) return
      fit.fit()
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({
        type: 'resize', cols: Math.min(300, Math.max(1, term.cols)), rows: Math.min(150, Math.max(1, term.rows)),
      }))
    }
    const input = term.onData(data => {
      if (!allowInput.current||socket?.readyState !== WebSocket.OPEN) return
      let chunk = ''
      let bytes = 0
      for (const char of data) {
        const size = new TextEncoder().encode(char).length
        if (bytes + size > 8000) {
          socket.send(JSON.stringify({ type: 'input', data: chunk }))
          chunk = ''; bytes = 0
        }
        chunk += char; bytes += size
      }
      if (chunk) socket.send(JSON.stringify({ type: 'input', data: chunk }))
    })
    const observer = new ResizeObserver(resize)
    observer.observe(container.current)
    focusTerminal.current = () => { resize(); term.focus() }
    resize()
    if (sessionId) {
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      socket = new WebSocket(protocol + '//' + window.location.host + apiPrefix + encodeURIComponent(sessionId) + '/terminal')
      socket.onopen = () => {
        if (disposed) return
        setStatus('终端已连接 · /workspace')
        resize()
      }
      socket.onmessage = event => {
        if (disposed) return
        try {
          const frame = JSON.parse(String(event.data)) as { type: string; data?: unknown }
          if (frame.type === 'output' && typeof frame.data === 'string') term.write(frame.data)
        } catch { setStatus('终端协议错误，请重新连接') }
      }
      socket.onerror = () => { if (!disposed) setStatus('终端连接失败，请检查会话与网络') }
      socket.onclose = event => {
        if (!disposed) setStatus(event.code === 1013
          ? '会话忙，稍后手动重连（不会重放输入）'
          : '终端断开（' + event.code + '），请检查考试状态后重连')
      }
    }
    return () => {
      terminal.current = null
      disposed = true
      focusTerminal.current = null
      socket?.close()
      input.dispose()
      observer.disconnect()
      term.dispose()
    }
  }, [sessionId, attempt, apiPrefix])

  useEffect(() => {
    if (!visible || !sessionId) return
    const frame = requestAnimationFrame(() => {
      focusTerminal.current?.()
    })
    return () => cancelAnimationFrame(frame)
  }, [visible, sessionId])

  useEffect(() => {
    if (terminal.current && theme) {
      const palette = palettes[theme]
      terminal.current.options.theme = { background: palette.surface, foreground: palette.ink,
        cursor: palette.accent, selectionBackground: palette.selection }
    }
  }, [theme, sessionId, attempt, apiPrefix])

  return <div className="terminal-pane">
    <div className="terminal-head">
      <span role="status">{sessionId ? status : '终端已禁用：当前不能写入'}</span>
      <div className="terminal-actions">
        {sessionId && <><button aria-label="粘贴" title="粘贴" disabled={!inputEnabled} onClick={async () => {
          const target = terminal.current
          try {
            const text = await navigator.clipboard.readText()
            if (target && target === terminal.current && allowInput.current) { target.focus(); target.paste(text) }
            setClipboardError('')
          } catch { focusTerminal.current?.(); setClipboardError('请在终端内按 Ctrl+V 粘贴。') }
        }}>{theme ? <WorkspaceIcon name="clipboard" /> : '粘贴'}</button>
        <button aria-label="复制" title="复制选区" onClick={async () => {
          const text = terminal.current?.getSelection()
          if (!text) { setClipboardError('请先选中终端输出。'); return }
          try { await navigator.clipboard.writeText(text); setClipboardError('') }
          catch { setClipboardError('请使用系统复制快捷键。') }
        }}>{theme ? <WorkspaceIcon name="copy" /> : '复制'}</button></>}
        <button aria-label="重新连接" title="重新连接" disabled={!sessionId} onClick={() => setAttempt(value => value + 1)}>{theme ? <WorkspaceIcon name="refresh" /> : '重新连接'}</button>
        {onCollapse && <button aria-label="折叠" onClick={onCollapse} title="折叠终端">{theme ? <WorkspaceIcon name="collapse" /> : '折叠'}</button>}
      </div>
    </div>
    {clipboardError && <small role="status">{clipboardError}</small>}
    <div ref={container} className="terminal-body" />
  </div>
}
