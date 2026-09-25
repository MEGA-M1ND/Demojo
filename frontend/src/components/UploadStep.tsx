import { useRef, useState } from 'react'
import { api, fmtBytes, fmtSeconds, uploadAsset, urls, type ApiError, type AppConfig, type Asset, type Project } from '../api'
import { ErrorBox, Notice } from './common'

type UploadRow = { key: string; name: string; size: number; progress: number; state: 'uploading' | 'processing' | 'done' | 'error'; error?: string; abort?: () => void }

const ACCEPT = 'image/jpeg,image/png,image/webp,video/mp4,video/quicktime,video/webm,.mp4,.mov,.webm,.m4v'

export function UploadStep({ project, config, reload, onNext }: { project: Project; config: AppConfig | null; reload: () => Promise<Project | null>; onNext: () => void }) {
  const [rows, setRows] = useState<UploadRow[]>([])
  const [error, setError] = useState<ApiError | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const [dragId, setDragId] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const logoRef = useRef<HTMLInputElement>(null)
  const [accent, setAccent] = useState(project.details.accent_color)

  const update = (key: string, patch: Partial<UploadRow>) => setRows((r) => r.map((x) => (x.key === key ? { ...x, ...patch } : x)))

  const uploadFiles = async (files: File[], role: 'media' | 'logo' = 'media') => {
    for (const file of files) {
      const key = `${file.name}-${file.size}-${Math.random().toString(36).slice(2, 7)}`
      const { promise, abort } = uploadAsset(project.id, file, role, (f) => update(key, { progress: f, state: f >= 1 ? 'processing' : 'uploading' }))
      setRows((r) => [...r, { key, name: file.name, size: file.size, progress: 0, state: 'uploading', abort }])
      try {
        await promise
        update(key, { state: 'done', progress: 1 })
        await reload()
      } catch (e) {
        update(key, { state: 'error', error: (e as ApiError).body.message })
      }
    }
  }

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    const files = Array.from(e.dataTransfer.files)
    if (files.length) uploadFiles(files)
  }

  const media = project.assets
  const move = async (from: number, to: number) => {
    if (to < 0 || to >= media.length) return
    const ids = media.map((a) => a.id)
    const [x] = ids.splice(from, 1)
    ids.splice(to, 0, x)
    try {
      await api.reorderAssets(project.id, ids)
      await reload()
    } catch (e) {
      setError(e as ApiError)
    }
  }

  const saveAccent = async (value: string) => {
    if (!/^#[0-9a-fA-F]{6}$/.test(value)) return
    try {
      await api.patchProject(project.id, { details: { accent_color: value } })
      await reload()
    } catch (e) {
      setError(e as ApiError)
    }
  }

  const limits = config?.limits
  const hasVideo = media.some((a) => a.kind === 'video')

  return (
    <div className="step-body">
      <div className="step-intro">
        <h2>Upload your real product media</h2>
        <p className="muted">
          Screenshots or product photos (JPG, PNG, WebP{limits ? `, up to ${limits.max_images} images of ${limits.max_image_mb} MB` : ''}) and optionally one screen recording
          (MP4, MOV, WebM{limits ? `, up to ${Math.round(limits.max_video_seconds / 60)} min / ${limits.max_video_mb} MB` : ''}). One asset plus a description is enough to start.
        </p>
      </div>

      <div
        className={`dropzone ${dragOver ? 'drag' : ''}`}
        onDragOver={(e) => {
          e.preventDefault()
          setDragOver(true)
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
        onClick={() => inputRef.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && inputRef.current?.click()}
        aria-label="Upload files"
      >
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT}
          multiple
          hidden
          data-testid="file-input"
          onChange={(e) => {
            const files = Array.from(e.target.files ?? [])
            e.target.value = ''
            if (files.length) uploadFiles(files)
          }}
        />
        <div className="drop-icon" aria-hidden>
          ⤒
        </div>
        <strong>Drop files here or click to choose</strong>
        <span className="muted small">Files are validated on the server; unsupported or corrupt media is rejected with a reason.</span>
      </div>

      {rows.length > 0 && (
        <ul className="upload-list">
          {rows.map((r) => (
            <li key={r.key} className={`upload-row up-${r.state}`}>
              <span className="up-name">{r.name}</span>
              <span className="muted small">{fmtBytes(r.size)}</span>
              <div className="bar small-bar" aria-label={`Upload progress ${Math.round(r.progress * 100)}%`}>
                <div className="bar-fill" style={{ width: `${Math.round(r.progress * 100)}%` }} />
              </div>
              <span className="up-state">
                {r.state === 'uploading' && `${Math.round(r.progress * 100)}%`}
                {r.state === 'processing' && 'Checking & processing…'}
                {r.state === 'done' && 'Added'}
                {r.state === 'error' && <span className="danger">{r.error}</span>}
              </span>
              {r.state === 'uploading' && (
                <button className="link" onClick={() => r.abort?.()}>
                  Cancel
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      <ErrorBox error={error?.body} onDismiss={() => setError(null)} />

      {media.length === 0 ? (
        <div className="empty">
          <p>No media yet.</p>
          <p className="muted small">Tip: three to six clear screenshots of the key screens, or a 30–90 second recording of the workflow, work best.</p>
        </div>
      ) : (
        <div className="asset-grid">
          {media.map((a, i) => (
            <AssetCard
              key={a.id}
              projectId={project.id}
              asset={a}
              index={i}
              total={media.length}
              onMove={move}
              onChanged={reload}
              onError={setError}
              dragging={dragId === a.id}
              onDragStart={() => setDragId(a.id)}
              onDropOn={() => {
                if (dragId && dragId !== a.id) move(media.findIndex((m) => m.id === dragId), i)
                setDragId(null)
              }}
            />
          ))}
        </div>
      )}

      <div className="brand-panel card">
        <h3>Brand</h3>
        <div className="brand-row">
          <div className="logo-box">
            {project.logo ? <img src={urls.assetMaster(project.id, project.logo.id)} alt="Logo" /> : <span className="muted small">No logo</span>}
          </div>
          <div>
            <input
              ref={logoRef}
              type="file"
              accept="image/png,image/jpeg,image/webp"
              hidden
              onChange={(e) => {
                const f = e.target.files?.[0]
                e.target.value = ''
                if (f) uploadFiles([f], 'logo')
              }}
            />
            <button className="btn btn-ghost btn-sm" onClick={() => logoRef.current?.click()}>
              {project.logo ? 'Replace logo' : 'Add logo (optional)'}
            </button>
            {project.logo && (
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => api.deleteAsset(project.id, project.logo!.id).then(reload).catch(setError)}
              >
                Remove
              </button>
            )}
            <div className="muted small">A transparent PNG works best. It appears on title cards and in a corner of each scene.</div>
          </div>
          <div className="accent">
            <label htmlFor="accent">Accent colour</label>
            <div className="accent-row">
              <input id="accent" type="color" value={accent} onChange={(e) => setAccent(e.target.value.toUpperCase())} onBlur={() => saveAccent(accent)} />
              <input
                aria-label="Accent colour hex"
                className="hex"
                value={accent}
                maxLength={7}
                onChange={(e) => setAccent(e.target.value)}
                onBlur={() => saveAccent(accent)}
              />
            </div>
          </div>
        </div>
      </div>

      <Notice>
        <strong>How your media is used.</strong> When you generate a story, resized copies of your images{hasVideo ? ' and up to 24 timestamped frames from your recording' : ''} are sent
        to OpenRouter{config && !config.fixture_mode ? ` (${config.models.vision})` : ''} for analysis. Originals stay on this machine and are used for rendering.
        {hasVideo && ' The recording’s original audio is muted in the video; new narration is created from the approved storyboard.'}
      </Notice>

      <div className="step-actions">
        <span />
        <button className="btn btn-primary" onClick={onNext} disabled={media.length === 0}>
          Continue to Describe →
        </button>
      </div>
    </div>
  )
}

function AssetCard({
  projectId,
  asset,
  index,
  total,
  onMove,
  onChanged,
  onError,
  dragging,
  onDragStart,
  onDropOn,
}: {
  projectId: string
  asset: Asset
  index: number
  total: number
  onMove: (from: number, to: number) => void
  onChanged: () => void
  onError: (e: ApiError) => void
  dragging: boolean
  onDragStart: () => void
  onDropOn: () => void
}) {
  const [label, setLabel] = useState(asset.label)
  const saveLabel = () => {
    if (label.trim() && label !== asset.label) api.patchAsset(projectId, asset.id, { label }).then(onChanged).catch(onError)
  }
  return (
    <article
      className={`asset-card ${dragging ? 'dragging' : ''}`}
      draggable
      onDragStart={onDragStart}
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault()
        e.stopPropagation()
        onDropOn()
      }}
      data-testid="asset-card"
    >
      <div className="asset-thumb">
        <img src={urls.assetThumb(projectId, asset.id)} alt={asset.label} loading="lazy" />
        <span className="asset-pos">{index + 1}</span>
        {asset.kind === 'video' && <span className="asset-kind">Recording · {fmtSeconds(asset.duration_s)}</span>}
      </div>
      <div className="asset-body">
        <input aria-label="Display label" value={label} maxLength={60} onChange={(e) => setLabel(e.target.value)} onBlur={saveLabel} />
        <div className="asset-meta muted small">
          {asset.width}×{asset.height}
          {asset.kind === 'video' ? ` · ${asset.fps ? asset.fps.toFixed(0) : '?'} fps${asset.has_audio ? ' · audio muted' : ''}` : ''} · {fmtBytes(asset.size_bytes)}
        </div>
        <div className="asset-actions">
          {asset.kind === 'image' ? (
            <select
              aria-label="Classification"
              value={asset.classification}
              onChange={(e) => api.patchAsset(projectId, asset.id, { classification: e.target.value as 'screenshot' | 'photo' }).then(onChanged).catch(onError)}
            >
              <option value="screenshot">Screenshot</option>
              <option value="photo">Product photo</option>
            </select>
          ) : (
            <span className="pill">Screen recording</span>
          )}
          <span className="spacer" />
          <button className="icon-btn" title="Move earlier" aria-label="Move earlier" disabled={index === 0} onClick={() => onMove(index, index - 1)}>
            ↑
          </button>
          <button className="icon-btn" title="Move later" aria-label="Move later" disabled={index === total - 1} onClick={() => onMove(index, index + 1)}>
            ↓
          </button>
          <button
            className="icon-btn danger"
            title="Delete"
            aria-label="Delete asset"
            onClick={() => window.confirm(`Delete “${asset.label}”?`) && api.deleteAsset(projectId, asset.id).then(onChanged).catch(onError)}
          >
            ✕
          </button>
        </div>
      </div>
    </article>
  )
}
