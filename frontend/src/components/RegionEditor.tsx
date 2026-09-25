import { useRef, useState } from 'react'
import type { Point, Rect } from '../api'

type Mode = 'focal' | 'highlight' | 'focus'

/** Edit a focal point, a highlight rectangle, and a focus (crop) region in normalised 0..1 coordinates. */
export function RegionEditor({
  src,
  focal,
  highlight,
  focus,
  onChange,
}: {
  src: string
  focal: Point
  highlight: Rect | null
  focus: Rect | null
  onChange: (patch: { focal_point?: Point; highlight?: Rect | null; focus_region?: Rect | null }) => void
}) {
  const [mode, setMode] = useState<Mode>('highlight')
  const [draft, setDraft] = useState<Rect | null>(null)
  const start = useRef<Point | null>(null)
  const boxRef = useRef<HTMLDivElement>(null)

  const pos = (e: React.PointerEvent): Point => {
    const r = boxRef.current!.getBoundingClientRect()
    return { x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)) }
  }
  const toRect = (a: Point, b: Point): Rect => {
    const x = Math.min(a.x, b.x)
    const y = Math.min(a.y, b.y)
    return { x: round(x), y: round(y), w: round(Math.max(0.02, Math.min(1 - x, Math.abs(a.x - b.x)))), h: round(Math.max(0.02, Math.min(1 - y, Math.abs(a.y - b.y)))) }
  }

  const onDown = (e: React.PointerEvent) => {
    e.preventDefault()
    const p = pos(e)
    if (mode === 'focal') {
      onChange({ focal_point: { x: round(p.x), y: round(p.y) } })
      return
    }
    start.current = p
    setDraft({ x: p.x, y: p.y, w: 0, h: 0 })
    ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
  }
  const onMove = (e: React.PointerEvent) => {
    if (!start.current) return
    setDraft(toRect(start.current, pos(e)))
  }
  const onUp = (e: React.PointerEvent) => {
    if (!start.current) return
    const r = toRect(start.current, pos(e))
    start.current = null
    setDraft(null)
    if (r.w < 0.03 || r.h < 0.03) return
    onChange(mode === 'highlight' ? { highlight: r } : { focus_region: r })
  }

  const style = (r: Rect) => ({ left: `${r.x * 100}%`, top: `${r.y * 100}%`, width: `${r.w * 100}%`, height: `${r.h * 100}%` })

  return (
    <div className="region-editor">
      <div className="seg seg-sm" role="radiogroup" aria-label="Edit mode">
        <button className={mode === 'highlight' ? 'on' : ''} onClick={() => setMode('highlight')} type="button">
          Highlight
        </button>
        <button className={mode === 'focus' ? 'on' : ''} onClick={() => setMode('focus')} type="button">
          Focus region (crop)
        </button>
        <button className={mode === 'focal' ? 'on' : ''} onClick={() => setMode('focal')} type="button">
          Focal point
        </button>
      </div>
      <div className="region-canvas" ref={boxRef} onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} data-testid="region-canvas">
        <img src={src} alt="" draggable={false} />
        {focus && <div className="rg rg-focus" style={style(focus)} title="Focus region" />}
        {highlight && <div className="rg rg-highlight" style={style(highlight)} title="Highlight" />}
        {draft && <div className={`rg ${mode === 'highlight' ? 'rg-highlight' : 'rg-focus'} rg-draft`} style={style(draft)} />}
        <div className="rg-focal" style={{ left: `${focal.x * 100}%`, top: `${focal.y * 100}%` }} title="Focal point" />
      </div>
      <div className="region-actions small">
        <span className="muted">
          {mode === 'focal' ? 'Click to set where motion is centred.' : mode === 'highlight' ? 'Drag to outline what the viewer should notice.' : 'Drag to crop the asset (useful for portrait).'}
        </span>
        <span className="spacer" />
        {highlight && (
          <button className="link" onClick={() => onChange({ highlight: null })}>
            Clear highlight
          </button>
        )}
        {focus && (
          <button className="link" onClick={() => onChange({ focus_region: null })}>
            Clear crop
          </button>
        )}
      </div>
    </div>
  )
}

function round(v: number) {
  return Math.round(v * 10000) / 10000
}
