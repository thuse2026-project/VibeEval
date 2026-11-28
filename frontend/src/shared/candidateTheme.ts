import { createContext, useContext } from 'react'

export const palettes = {
  cool: { label: '冷白', surface: '#ffffff', ink: '#222b38', muted: '#556477', accent: '#265bab', selection: '#dfebfb' },
  graphite: { label: '石墨深色', surface: '#202329', ink: '#e2e5ec', muted: '#acb4c2', accent: '#9cc4ff', selection: '#33445e' },
  warm: { label: '暖白', surface: '#fcf9f3', ink: '#302c29', muted: '#696057', accent: '#514a92', selection: '#eae5f4' },
} as const
export type CandidateTheme = keyof typeof palettes
export const CandidateThemeContext = createContext<{ theme: CandidateTheme; select: (theme: CandidateTheme) => void } | null>(null)
export const useCandidateTheme = () => useContext(CandidateThemeContext)
