import { useEffect, useRef } from 'react'
import { Terminal as XTerm } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'

type Props = {
  connected: boolean
  output: string
}

export function TerminalPane({ connected, output }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const termRef = useRef<XTerm | null>(null)

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

    const onResize = () => fit.fit()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      term.dispose()
    }
  }, [])

  useEffect(() => {
    if (termRef.current && output) {
      termRef.current.write(output)
    }
  }, [output])

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
