type Props = { label: string; horizontal?: boolean; value: number; min: number; max: number; change: (value: number) => void }

// The resized pane is after the separator (the bottom or right edge of its parent).
export function PaneResize({ label, horizontal = false, value, min, max, change }: Props) {
  const resize = (next: number) => change(Math.max(min, Math.min(max, Math.round(next))))
  return <div role="separator" tabIndex={0} className={horizontal ? 'resize-row' : 'resize-column'} title={label}
    aria-label={label} aria-orientation={horizontal ? 'horizontal' : 'vertical'} aria-valuenow={value}
    aria-valuemin={min} aria-valuemax={max} aria-valuetext={value + '%'}
    onPointerDown={e => { if (e.button === 0) { e.preventDefault(); e.currentTarget.setPointerCapture(e.pointerId) } }}
    onPointerMove={e => {
      if (!e.currentTarget.hasPointerCapture(e.pointerId)) return
      const box = e.currentTarget.parentElement!.getBoundingClientRect()
      resize(horizontal ? (box.bottom - e.clientY) / box.height * 100 : (box.right - e.clientX) / box.width * 100)
    }}
    onKeyDown={e => {
      if (e.key === (horizontal ? 'ArrowUp' : 'ArrowLeft')) { e.preventDefault(); resize(value + 2) }
      if (e.key === (horizontal ? 'ArrowDown' : 'ArrowRight')) { e.preventDefault(); resize(value - 2) }
    }} />
}
