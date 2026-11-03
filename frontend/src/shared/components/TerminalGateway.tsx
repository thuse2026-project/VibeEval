import { useEffect, useRef, useState } from 'react'
import { Terminal as XTerm } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { usingMock } from '../api'
import '@xterm/xterm/css/xterm.css'

type Props = {
  sessionId: string | null
  command: { id: number; data: string } | null
}

const apiBase = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000/api/v1'
const wsBase = import.meta.env.VITE_WS_BASE_URL ?? apiBase.replace(/^http/, 'ws')

export function TerminalPane({ sessionId, command }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const termRef = useRef<XTerm | null>(null)
  const socketRef = useRef<WebSocket | null>(null)
  const sendRef = useRef<(data: string) => void>(() => undefined)
  const resizeRef = useRef<() => void>(() => undefined)
  const mockLine = useRef('')
  const lastCommand = useRef<number | null>(null)
  const [socketConnected, setSocketConnected] = useState(false)
  const connected = usingMock ? !!sessionId : socketConnected

  useEffect(() => {
    if (!containerRef.current) return
    const term = new XTerm({
      cursorBlink: true,
      fontSize: 13,
      theme: { background: '#111827', foreground: '#d1d5db' },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(containerRef.current)
    fit.fit()
    termRef.current = term

    const mockInput = (data: string) => {
      if (data === '\r') {
        const line = mockLine.current.trim()
        term.write('\r\n')
        if (line === 'pytest') term.write('==== 2 passed in 0.12s ====\r\n')
        mockLine.current = ''
        term.write('$ ')
        return
      }
      if (data === '\u007f') {
        if (mockLine.current) {
          mockLine.current = mockLine.current.slice(0, -1)
          term.write('\b \b')
        }
        return
      }
      mockLine.current += data
      term.write(data)
    }

    sendRef.current = (data) => {
      if (usingMock) {
        mockInput(data)
      } else if (socketRef.current?.readyState === WebSocket.OPEN) {
        socketRef.current.send(JSON.stringify({ type: 'input', data }))
      }
    }
    resizeRef.current = () => {
      fit.fit()
      if (socketRef.current?.readyState === WebSocket.OPEN) {
        socketRef.current.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
      }
    }

    const input = term.onData((data) => sendRef.current(data))
    const observer = new ResizeObserver(() => resizeRef.current())
    observer.observe(containerRef.current)
    return () => {
      input.dispose()
      observer.disconnect()
      term.dispose()
      termRef.current = null
    }
  }, [])

  useEffect(() => {
    if (!sessionId || !termRef.current) return
    if (usingMock) {
      termRef.current.write('$ ')
      return
    }

    const socket = new WebSocket(`${wsBase}/sessions/${encodeURIComponent(sessionId)}/terminal`)
    socketRef.current = socket
    socket.onopen = () => {
      setSocketConnected(true)
      resizeRef.current()
    }
    socket.onmessage = (event) => {
      try {
        const frame = JSON.parse(String(event.data)) as { type: string; data?: string }
        if (frame.type === 'output' && frame.data) termRef.current?.write(frame.data)
      } catch {
        termRef.current?.write('\r\n[终端协议错误]\r\n')
      }
    }
    socket.onerror = () => setSocketConnected(false)
    socket.onclose = () => setSocketConnected(false)
    return () => {
      socket.close()
      socketRef.current = null
      setSocketConnected(false)
    }
  }, [sessionId])

  useEffect(() => {
    if (!command || command.id === lastCommand.current) return
    lastCommand.current = command.id
    sendRef.current(command.data)
  }, [command])

  return (
    <div className="terminal">
      <div className="terminal-head">
        <span className={`conn ${connected ? 'online' : 'offline'}`}>
          {connected ? '终端已连接 · /workspace' : '终端断开'}
        </span>
      </div>
      <div ref={containerRef} className="terminal-body" />
    </div>
  )
}
