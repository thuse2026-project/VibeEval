import type { ReactNode } from 'react'
import { AppearanceSettings } from './CandidateAppearance'

export function CandidateFrame({ children, entryHref = '/entry?mode=r2' }: { children: ReactNode; entryHref?: string }) {
  return <div className="candidate-frame">
    <header className="candidate-frame-bar">
      <a href={entryHref} aria-label="VibeEval 考生入场"><i aria-hidden="true">&lt;/&gt;</i>VibeEval</a>
      <span>考生工作台</span>
      <AppearanceSettings />
    </header>
    <main className="candidate-frame-main">{children}</main>
  </div>
}
