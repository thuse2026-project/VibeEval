import { useId, useState, type CSSProperties, type ReactNode } from 'react'
import { CandidateThemeContext, palettes, useCandidateTheme, type CandidateTheme } from '../candidateTheme'

const storageKey = 'vibeeval.candidate.appearance'
export function CandidateAppearance({children}: {children: ReactNode}) {
  const [theme, setTheme] = useState<CandidateTheme>(() => {
    try {
      const saved = localStorage.getItem(storageKey)
      if (saved && Object.hasOwn(palettes, saved)) return saved as CandidateTheme
    } catch { /* Storage can be unavailable in private sessions. */ }
    return 'cool'
  })
  function select(value: CandidateTheme) {
    setTheme(value)
    try { localStorage.setItem(storageKey, value) } catch { /* Keep the session preference. */ }
  }
  const palette = palettes[theme]
  const style = { '--surface': palette.surface, '--ink': palette.ink, '--muted': palette.muted,
    '--accent': palette.accent, '--selection': palette.selection } as CSSProperties
  return <CandidateThemeContext.Provider value={{ theme, select }}>
    <div data-candidate-theme={theme} style={style}>{children}</div>
  </CandidateThemeContext.Provider>
}

export function AppearanceSettings() {
  const appearance = useCandidateTheme()
  const id = useId()
  if (!appearance) return null
  return <div className="appearance-settings">
    <button type="button" popoverTarget={id}>设置</button>
    <div id={id} popover="auto" className="appearance-menu">
      <label>界面主题<select value={appearance.theme} onChange={event => appearance.select(event.target.value as CandidateTheme)}>
        {Object.entries(palettes).map(([key, palette]) => <option key={key} value={key}>{palette.label}</option>)}
      </select></label>
    </div>
  </div>
}
