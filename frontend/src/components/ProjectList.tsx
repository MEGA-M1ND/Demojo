import { useEffect, useState } from 'react'
import { api, urls, type ApiError, type ProjectSummary } from '../api'
import { ErrorBox } from './common'

export function ProjectList({ onOpen }: { onOpen: (id: string) => void }) {
  const [items, setItems] = useState<ProjectSummary[] | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)

  const load = () =>
    api
      .listProjects()
      .then((p) => {
        setItems(p)
        setError(null)
      })
      .catch(setError)

  useEffect(() => {
    load()
  }, [])

  const create = async () => {
    setBusy(true)
    try {
      const p = await api.createProject(name.trim() || undefined)
      onOpen(p.id)
    } catch (e) {
      setError(e as ApiError)
    } finally {
      setBusy(false)
    }
  }

  const remove = async (p: ProjectSummary) => {
    if (!window.confirm(`Delete “${p.name}” and all of its media and exports? This cannot be undone.`)) return
    try {
      await api.deleteProject(p.id)
      load()
    } catch (e) {
      setError(e as ApiError)
    }
  }

  return (
    <div className="home">
      <section className="hero">
        <h1>Turn your real product screens into a narrated demo video.</h1>
        <p className="lead">
          Upload screenshots, product photos, or a rough screen recording. Describe the product. Review an editable story grounded in
          what you provided, then export an MP4 with narration and captions.
        </p>
        <form
          className="create-row"
          onSubmit={(e) => {
            e.preventDefault()
            create()
          }}
        >
          <input
            aria-label="Project name"
            placeholder="Project name, e.g. “Launch video – invoices”"
            value={name}
            maxLength={80}
            onChange={(e) => setName(e.target.value)}
          />
          <button className="btn btn-primary" disabled={busy} type="submit">
            {busy ? 'Creating…' : 'New project'}
          </button>
        </form>
      </section>
      <ErrorBox error={error?.body} onDismiss={() => setError(null)} />
      <section>
        <h2 className="section-title">Projects</h2>
        {items === null ? (
          <div className="muted">Loading…</div>
        ) : items.length === 0 ? (
          <div className="empty">
            <p>No projects yet.</p>
            <p className="muted">Create one above. Everything stays on this machine except the AI requests sent to OpenRouter when you generate a story or narration.</p>
          </div>
        ) : (
          <div className="project-grid">
            {items.map((p) => (
              <article key={p.id} className="project-card">
                <button className="project-open" onClick={() => onOpen(p.id)} aria-label={`Open ${p.name}`}>
                  <div className="project-thumb">
                    {p.thumb_asset_id ? <img src={urls.assetThumb(p.id, p.thumb_asset_id)} alt="" /> : <span className="muted">No media yet</span>}
                  </div>
                  <div className="project-meta">
                    <strong>{p.name}</strong>
                    <span className="muted small">
                      {p.asset_count} asset{p.asset_count === 1 ? '' : 's'} · {p.has_storyboard ? 'storyboard' : 'no story yet'} · {p.export_count} export
                      {p.export_count === 1 ? '' : 's'}
                    </span>
                    <span className="muted small">Updated {new Date(p.updated_at).toLocaleString()}</span>
                  </div>
                </button>
                <button className="btn btn-ghost btn-sm danger" onClick={() => remove(p)}>
                  Delete
                </button>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
