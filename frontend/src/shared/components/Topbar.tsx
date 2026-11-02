import type { ReactNode } from 'react'

export function Topbar({ left, right }: { left: ReactNode; right?: ReactNode }) {
  return (
    <header className="topbar">
      <span className="logo">VibeEval</span>
      <div className="topbar-main">{left}</div>
      <div className="topbar-right">{right}</div>
    </header>
  )
}
