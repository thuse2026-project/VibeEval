// Small, single-stroke controls share the same grid at desktop toolbar sizes.
const paths = {
  agent: 'm8 3 1.5 4.5L14 9l-4.5 1.5L8 15l-1.5-4.5L2 9l4.5-1.5L8 3Zm7.5-1 .7 2.3 2.3.7-2.3.7-.7 2.3-.7-2.3-2.3-.7 2.3-.7.7-2.3Z',
  chat: 'M16 12.5a2 2 0 0 1-2 2H8l-4.5 3v-3.4A2 2 0 0 1 2 12V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z',
  add: 'M10 4v12M4 10h12',
  edit: 'm3 13 9.8-9.8a1.7 1.7 0 0 1 2.4 0l1.6 1.6a1.7 1.7 0 0 1 0 2.4L7 17l-4.8.8L3 13Zm8.5-8.5 4 4',
  close: 'm5 5 10 10M15 5 5 15',
  send: 'M10 16V4m-5 5 5-5 5 5',
  stop: 'M5 5h10v10H5z',
  play: 'm6 3 10 7-10 7z',
  code: 'm6 7-3 3 3 3m8-6 3 3-3 3m-3-9-2 12',
  book: 'M10 5C7 3 4 3 2 4v11c3-1 5-1 8 1 3-2 5-2 8-1V4c-2-1-5-1-8 1Zm0 0v11',
  files: 'M2 6V4a1 1 0 0 1 1-1h5l2 3h7a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V6z',
  test: 'M7 2h6m-5 0v6l-5 7a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-7V2m-6 11h8',
  clipboard: 'M7 4H4v13h12V4h-3M7 2h6v4H7z',
  check: 'm3 10 4 4L17 4',
  terminal: 'm3 5 4 4-4 4m6 1h7',
  settings: 'M3 5h5m4 0h5M3 15h9m4 0h1M8 2v6m4 4v6',
  collapse: 'm5 8 5 5 5-5',
  refresh: 'M16.5 8a6.5 6.5 0 1 0 0 5m.5-10v5h-5',
  copy: 'M7 7h10v10H7zM4 13H3V3h10v1',
  lock: 'M4 9h12v8H4zM6 9V6a4 4 0 0 1 8 0v3',
} as const

export function WorkspaceIcon({ name }: { name: keyof typeof paths }) {
  return <svg className="workspace-icon" width="16" height="16" viewBox="0 0 20 20" aria-hidden="true"
    fill="none" stroke="currentColor" strokeWidth="1.55" strokeLinecap="round" strokeLinejoin="round">
    <path d={paths[name]} />
  </svg>
}
