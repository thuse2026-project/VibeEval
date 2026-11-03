import { useEffect, useRef, useState } from 'react'
import { Terminal as XTerm } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'

export function TerminalPane({ sessionId }: { sessionId: string | null }) {
  const container = useRef<HTMLDivElement>(null)
  const [attempt, setAttempt] = useState(0)
  const [status, setStatus] = useState('终端未连接')

  useEffect(() => {
    if (!container.current) return
    const term = new XTerm({
      cursorBlink: true, fontSize: 13,
      theme: { background: '#111827', foreground: '#d1d5db' },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(container.current)
    let socket: WebSocket | null = null
    let disposed = false
    const resize = () => {
      if (!container.current?.offsetHeight) return
      fit.fit()
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({
        type: 'resize', cols: Math.min(300, Math.max(1, term.cols)), rows: Math.min(150, Math.max(1, term.rows)),
      }))
    }
    const input = term.onData(data => {
      if (socket?.readyState !== WebSocket.OPEN) return
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
    resize()
    if (sessionId) {
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      socket = new WebSocket(protocol + '//' + window.location.host + '/api/v1/sessions/' + encodeURIComponent(sessionId) + '/terminal')
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
      disposed = true
      socket?.close()
      input.dispose()
      observer.disconnect()
      term.dispose()
    }
  }, [sessionId, attempt])

  return <div className="terminal">
    <div className="terminal-head">
      <span role="status">{sessionId ? status : '终端已禁用：当前不能写入'}</span>
      <button disabled={!sessionId} onClick={() => setAttempt(value => value + 1)}>重新连接</button>
    </div>
    <div ref={container} className="terminal-body" />
  </div>
}
