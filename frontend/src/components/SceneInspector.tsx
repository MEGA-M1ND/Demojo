import { useRef, useState } from 'react'
import { fmtSeconds, urls, type Asset, type Claim, type Motion, type Project, type Scene, type TimelineScene } from '../api'
import { Field } from './common'
import { RegionEditor } from './RegionEditor'

const MOTIONS: { id: Motion; label: string }[] = [
  { id: 'static', label: 'Static' },
  { id: 'gentle_push_in', label: 'Gentle push-in' },
  { id: 'pan_left', label: 'Pan left' },
  { id: 'pan_right', label: 'Pan right' },
  { id: 'focus_zoom', label: 'Zoom to highlight' },
]

const ROLES = ['hook', 'intro', 'feature', 'step', 'showcase', 'cta'] as const

export function nearestFrame(asset: Asset, t: number): number {
  const fr = asset.frames ?? []
  if (!fr.length) return 0
  let best = fr[0]
  for (const f of fr) if (Math.abs(f.t - t) < Math.abs(best.t - t)) best = f
  return best.index
}

export function sceneThumb(project: Project, scene: Scene): string | null {
  if (!scene.asset_id) return null
  const a = project.assets.find((x) => x.id === scene.asset_id)
  if (!a) return null
  if (a.kind === 'video') return urls.frame(project.id, a.id, nearestFrame(a, scene.clip_in_s ?? 0))
  return urls.assetThumb(project.id, a.id)
}

export function SceneInspector({
  project,
  scene,
  index,
  timing,
  narrationOn,
  onChange,
  onRegenerate,
  regenBusy,
}: {
  project: Project
  scene: Scene
  index: number
  timing: TimelineScene | undefined
  narrationOn: boolean
  onChange: (s: Scene) => void
  onRegenerate: (instruction: string) => void
  regenBusy: boolean
}) {
  const [instruction, setInstruction] = useState('')
  const [claimMsg, setClaimMsg] = useState<string | null>(null)
  const videoRef = useRef<HTMLVideoElement>(null)
  const set = (patch: Partial<Scene>) => onChange({ ...scene, ...patch, origin: scene.origin })
  const asset = scene.asset_id ? project.assets.find((a) => a.id === scene.asset_id) : undefined
  const words = scene.narration.trim() ? scene.narration.trim().split(/\s+/).length : 0

  const replaceSource = (value: string) => {
    if (value === '__title') {
      set({ source_kind: 'title_card', asset_id: null, clip_in_s: null, clip_out_s: null, focus_region: null, highlight: null, motion: 'gentle_push_in' })
      return
    }
    const a = project.assets.find((x) => x.id === value)
    if (!a) return
    if (a.kind === 'video') {
      const dur = a.duration_s ?? 5
      set({ source_kind: 'clip', asset_id: a.id, clip_in_s: 0, clip_out_s: Math.min(dur, 5), focus_region: null, highlight: null, motion: 'static' })
    } else {
      set({ source_kind: 'image', asset_id: a.id, clip_in_s: null, clip_out_s: null, focus_region: null, highlight: null })
    }
  }

  const setClaim = (c: Claim, status: Claim['status']) => set({ claims: scene.claims.map((x) => (x.id === c.id ? { ...x, status } : x)) })

  const removeClaim = (c: Claim) => {
    const strip = (t: string) => t.split(c.text).join('').replace(/\s{2,}/g, ' ').replace(/\s+([.,!?])/g, '$1').trim()
    const narration = strip(scene.narration)
    const headline = strip(scene.headline)
    const subline = strip(scene.subline)
    const still = [narration, headline, subline].some((t) => t.toLowerCase().includes(c.text.toLowerCase().trim()))
    if (still) {
      setClaimMsg('That statement is not an exact match in the script. Edit the text to remove it, then click Remove again.')
      return
    }
    setClaimMsg(null)
    onChange({ ...scene, narration, headline, subline, claims: scene.claims.map((x) => (x.id === c.id ? { ...x, status: 'removed' } : x)) })
  }

  const clipDur = scene.clip_in_s !== null && scene.clip_out_s !== null ? scene.clip_out_s - scene.clip_in_s : null
  const regionSrc = asset ? (asset.kind === 'video' ? urls.frame(project.id, asset.id, nearestFrame(asset, scene.clip_in_s ?? 0)) : urls.assetMaster(project.id, asset.id)) : null

  return (
    <div className="inspector" data-testid="inspector">
      <div className="insp-head">
        <h3>Scene {index + 1}</h3>
        <select aria-label="Scene role" value={scene.role} onChange={(e) => set({ role: e.target.value as Scene['role'] })}>
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        <span className="spacer" />
        {timing && (
          <span className="muted small" title="Resolved from measured narration when available">
            {fmtSeconds(timing.n_frames / 30)} on screen
            {timing.speech ? ` · voice ${fmtSeconds(timing.speech.duration_s)}${timing.speech.measured ? '' : ' (est.)'}` : ''}
          </span>
        )}
      </div>

      {scene.selection_reason && <p className="reason">Why this media: {scene.selection_reason}</p>}

      <Field label="Media">
        <select aria-label="Scene media" value={scene.source_kind === 'title_card' ? '__title' : scene.asset_id ?? ''} onChange={(e) => replaceSource(e.target.value)}>
          <option value="__title">Title card (brand background)</option>
          {project.assets.map((a) => (
            <option key={a.id} value={a.id}>
              {a.kind === 'video' ? 'Recording' : a.classification === 'photo' ? 'Photo' : 'Screenshot'}: {a.label}
            </option>
          ))}
        </select>
      </Field>

      {scene.source_kind === 'clip' && asset && (
        <div className="clip-editor">
          <video ref={videoRef} src={urls.assetOriginal(project.id, asset.id)} controls muted preload="metadata" className="clip-video" />
          <div className="clip-row">
            <Field label="In (s)">
              <input
                type="number"
                step={0.1}
                min={0}
                max={asset.duration_s ?? undefined}
                value={scene.clip_in_s ?? 0}
                onChange={(e) => set({ clip_in_s: clampNum(+e.target.value, 0, (scene.clip_out_s ?? 1) - 0.5) })}
              />
            </Field>
            <Field label="Out (s)">
              <input
                type="number"
                step={0.1}
                min={0}
                max={asset.duration_s ?? undefined}
                value={scene.clip_out_s ?? 0}
                onChange={(e) => set({ clip_out_s: clampNum(+e.target.value, (scene.clip_in_s ?? 0) + 0.5, asset.duration_s ?? 9999) })}
              />
            </Field>
            <div className="clip-btns">
              <button className="btn btn-ghost btn-sm" type="button" onClick={() => videoRef.current && set({ clip_in_s: round1(Math.min(videoRef.current.currentTime, (scene.clip_out_s ?? 1) - 0.5)) })}>
                Set in = playhead
              </button>
              <button className="btn btn-ghost btn-sm" type="button" onClick={() => videoRef.current && set({ clip_out_s: round1(Math.max(videoRef.current.currentTime, (scene.clip_in_s ?? 0) + 0.5)) })}>
                Set out = playhead
              </button>
              <button className="btn btn-ghost btn-sm" type="button" onClick={() => videoRef.current && (videoRef.current.currentTime = scene.clip_in_s ?? 0)}>
                Preview from in
              </button>
            </div>
          </div>
          <div className="muted small">
            Clip length {fmtSeconds(clipDur)} of {fmtSeconds(asset.duration_s)}. {timing?.hold_frames ? `Holds the final frame for ${fmtSeconds(timing.hold_frames / 30)} so narration can finish (never loops).` : ''}
          </div>
          {asset.frames && (
            <div className="frame-strip" aria-label="Sampled frames">
              {asset.frames.map((f) => (
                <button
                  key={f.index}
                  className={`frame ${scene.clip_in_s !== null && scene.clip_out_s !== null && f.t >= scene.clip_in_s && f.t <= scene.clip_out_s ? 'in' : ''}`}
                  onClick={() => videoRef.current && (videoRef.current.currentTime = f.t)}
                  title={`Seek to ${f.t.toFixed(2)}s`}
                  type="button"
                >
                  <img src={urls.frame(project.id, asset.id, f.index)} alt="" loading="lazy" />
                  <span>{f.t.toFixed(1)}s</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      <Field label="Headline (on screen)" hint={`${scene.headline.length}/80`}>
        <input aria-label="Headline" value={scene.headline} maxLength={80} onChange={(e) => set({ headline: e.target.value })} />
      </Field>
      <Field label="Subline (optional)" hint={`${scene.subline.length}/120`}>
        <input aria-label="Subline" value={scene.subline} maxLength={120} onChange={(e) => set({ subline: e.target.value })} />
      </Field>
      <Field label={narrationOn ? 'Narration (spoken)' : 'Narration (captions only — narration is off)'} hint={`${words} words · ~${(words / 2.6 + (words ? 0.3 : 0)).toFixed(1)}s spoken`}>
        <textarea aria-label="Narration" rows={3} value={scene.narration} maxLength={420} onChange={(e) => set({ narration: e.target.value })} />
      </Field>

      {scene.source_kind !== 'clip' && (
        <Field label={`Planned duration: ${scene.planned_duration_s.toFixed(1)}s`} hint="Extended automatically if the narration needs more time; narration is never cut off.">
          <input type="range" min={1.5} max={12} step={0.5} value={scene.planned_duration_s} onChange={(e) => set({ planned_duration_s: +e.target.value })} aria-label="Planned duration" />
        </Field>
      )}

      {regionSrc && (
        <Field label="Framing">
          <RegionEditor
            src={regionSrc}
            focal={scene.focal_point}
            highlight={scene.highlight}
            focus={scene.focus_region}
            onChange={(p) => set(p as Partial<Scene>)}
          />
          <div className="two-col">
            <label className="inline">
              Fit
              <select value={scene.fit_mode} onChange={(e) => set({ fit_mode: e.target.value as Scene['fit_mode'] })}>
                <option value="contain">Contain (whole asset, no crop)</option>
                <option value="cover">Cover (fill frame, crops edges)</option>
              </select>
            </label>
          </div>
        </Field>
      )}

      <div className="two-col">
        <label className="inline">
          Motion
          <select value={scene.motion} onChange={(e) => set({ motion: e.target.value as Motion })}>
            {MOTIONS.filter((m) => scene.source_kind !== 'title_card' || m.id === 'static' || m.id === 'gentle_push_in').map((m) => (
              <option key={m.id} value={m.id} disabled={m.id === 'focus_zoom' && !scene.highlight}>
                {m.label}
                {m.id === 'focus_zoom' && !scene.highlight ? ' (draw a highlight first)' : ''}
              </option>
            ))}
          </select>
        </label>
        <label className="inline">
          Transition in
          <select value={scene.transition_in} onChange={(e) => set({ transition_in: e.target.value as Scene['transition_in'] })} disabled={index === 0}>
            <option value="cut">Cut</option>
            <option value="dissolve">Short dissolve</option>
          </select>
        </label>
      </div>
      {scene.source_kind === 'image' && <p className="muted small">Stills are shown as stills with camera motion — no pointer or clicks are simulated.</p>}

      {scene.claims.length > 0 && (
        <div className="claims">
          <h4>Claims in this scene</h4>
          {claimMsg && <div className="notice notice-warn small">{claimMsg}</div>}
          <ul>
            {scene.claims.map((c) => (
              <li key={c.id} className={`claim claim-${c.status}`}>
                <div className="claim-text">“{c.text}”</div>
                <div className="claim-meta small">
                  <span className={`pill basis-${c.basis}`}>{c.basis === 'user_detail' ? 'From your details' : c.basis === 'visible_asset' ? 'Visible in asset' : 'Not supported by your inputs'}</span>
                  {c.source_ref && <span className="muted">source: {c.source_ref}</span>}
                  <span className="spacer" />
                  {c.status === 'needs_confirmation' && (
                    <>
                      <button className="btn btn-sm btn-primary" onClick={() => setClaim(c, 'confirmed')}>
                        Confirm it’s true
                      </button>
                      <button className="btn btn-sm btn-ghost" onClick={() => removeClaim(c)}>
                        Remove from script
                      </button>
                    </>
                  )}
                  {c.status === 'confirmed' && <span className="ok">Confirmed by you</span>}
                  {c.status === 'removed' && <span className="muted">Removed</span>}
                  {c.status === 'ok' && <span className="muted">Traceable</span>}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="regen">
        <input aria-label="Instruction for regeneration" placeholder="Optional instruction, e.g. “shorter, focus on the export button”" value={instruction} maxLength={400} onChange={(e) => setInstruction(e.target.value)} />
        <button className="btn btn-ghost" onClick={() => onRegenerate(instruction)} disabled={regenBusy}>
          {regenBusy ? 'Regenerating…' : 'Regenerate this scene'}
        </button>
      </div>
    </div>
  )
}

function clampNum(v: number, lo: number, hi: number) {
  if (Number.isNaN(v)) return lo
  return Math.round(Math.min(hi, Math.max(lo, v)) * 1000) / 1000
}

function round1(v: number) {
  return Math.round(v * 10) / 10
}
